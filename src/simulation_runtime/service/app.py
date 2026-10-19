"""HTTP API for the standalone simulation runtime.

Every project gets its own bearer token. Authentication maps a token to a
project id and a set of scopes; all session and artifact lookups are scoped by
that project id, so IDs from another project resolve as ``404``.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..backends import create_backend
from ..core.errors import (
    InvalidRequest,
    OperationFailed,
    SimulationRuntimeError,
    Unauthorized,
)
from .artifacts import ArtifactStore
from .auth import authenticate, require_scope
from .config import Settings, TokenPolicy, load_settings
from .manager import SessionManager
from .store import Store

LOGGER = logging.getLogger("simulation_runtime.service")

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_IDEMPOTENCY_KEY = 200


class CreateSessionRequest(BaseModel):
    backend: str = "mujoco_so101"
    task: str = "pick_cube"
    phase: Literal["playground", "test"] = "playground"
    mode: Literal["typed", "exec"] = "typed"
    seed: int = Field(default=0, ge=0)
    exec_budget: int | None = None


class StepRequest(BaseModel):
    action: Any
    idempotency_key: str | None = None


class ExecutionRequest(BaseModel):
    code: str
    idempotency_key: str | None = None


class LifecycleRequest(BaseModel):
    idempotency_key: str | None = None
    reason: str = "closed_by_client"


def session_wire(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": row["id"],
        "project_id": row["project_id"],
        "task": row["task"],
        "phase": row["phase"],
        "mode": row["mode"],
        "seed": row["seed"],
        "state": row["state"],
        "reason": row["reason"],
        "success": bool(row["success"]),
        "terminated": bool(row["terminated"]),
        "truncated": bool(row["truncated"]),
        "native_action_limit": row["native_action_limit"],
        "native_steps_used": row["native_steps_used"],
        "native_steps_remaining": row["native_steps_remaining"],
        "execution_budget": row["execution_budget"],
        "executions_used": row["executions_used"],
        "executions_remaining": row["executions_remaining"],
        "observation_sequence": row["observation_sequence"],
        "latest_observation_id": row["latest_observation_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_at": row["closed_at"],
    }


def _decoded(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def operation_wire(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": row["id"],
        "session_id": row["session_id"],
        "kind": row["kind"],
        "status": row["status"],
        "result": _decoded(row["result_json"]),
        "error": _decoded(row["error_json"]),
        "outcome_uncertain": bool(row["outcome_uncertain"]),
        "idempotency_key": row["idempotency_key"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


def _key(body_value: str | None, header_value: str | None) -> str | None:
    key = header_value if header_value is not None else body_value
    if key is None:
        return None
    if not isinstance(key, str) or not key.strip() or len(key) > MAX_IDEMPOTENCY_KEY:
        raise InvalidRequest(
            f"idempotency key must be a non-empty string of at most {MAX_IDEMPOTENCY_KEY} characters"
        )
    return key


def _operation_response(operation: dict[str, Any]) -> JSONResponse:
    body = operation_wire(operation)
    status_code = 200 if body["status"] in {"succeeded", "failed"} else 202
    headers = {
        "Location": (
            f"/v1/sessions/{operation['session_id']}/operations/{operation['id']}"
        )
    }
    if status_code == 202:
        headers["Retry-After"] = "0.2"
    return JSONResponse(status_code=status_code, content=_json(body), headers=headers)


def _json(value: Any) -> Any:
    """Convert numpy values that may still be attached to worker results."""

    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        active = settings or load_settings()
        store = Store(active.data_dir / "runtime.sqlite3")
        artifacts = ArtifactStore(active.data_dir, store)
        manager = SessionManager(active, store, artifacts)
        app.state.settings = active
        app.state.store = store
        app.state.artifacts = artifacts
        app.state.manager = manager
        LOGGER.info(
            "simulation-runtime %s serving %d project token(s)", __version__, len(active.tokens)
        )
        try:
            yield
        finally:
            manager.shutdown()
            store.close()

    app = FastAPI(
        title="Simulation Runtime",
        version=__version__,
        description="Standalone, project-scoped robot simulation service.",
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        length = request.headers.get("content-length")
        if length is not None:
            try:
                too_large = int(length) > MAX_BODY_BYTES
            except ValueError:
                too_large = True
            if too_large:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "request_too_large",
                            "message": f"request body exceeds {MAX_BODY_BYTES} bytes",
                        }
                    },
                )
        return await call_next(request)

    @app.exception_handler(SimulationRuntimeError)
    async def runtime_error(_request: Request, exc: SimulationRuntimeError):
        body: dict[str, Any] = {"error": {"code": exc.code, "message": exc.message}}
        headers = None
        if isinstance(exc, Unauthorized):
            headers = {"WWW-Authenticate": "Bearer"}
        if isinstance(exc, OperationFailed):
            body["operation"] = operation_wire(exc.operation)
        return JSONResponse(
            status_code=exc.http_status, content=_json(body), headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        details = [
            {"loc": list(item.get("loc", ())), "msg": str(item.get("msg", "")), "type": item.get("type")}
            for item in exc.errors()
        ]
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "request body or query parameters are invalid",
                    "details": details,
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code.get(exc.status_code, "http_error"),
                    "message": str(exc.detail),
                }
            },
        )

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, exc: Exception):
        LOGGER.exception("unhandled service error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "internal server error",
                }
            },
        )

    def get_policy(request: Request, authorization: str | None = Header(default=None)) -> TokenPolicy:
        return authenticate(request.app.state.settings, authorization)

    def get_manager(request: Request) -> SessionManager:
        return request.app.state.manager

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/readyz")
    def readyz(request: Request, response: Response) -> dict[str, Any]:
        active: Settings = request.app.state.settings
        errors: dict[str, str] = {}
        for name in active.backends:
            try:
                create_backend(name).capabilities()
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"
        try:
            request.app.state.store.count_sessions()
        except Exception as exc:
            errors["store"] = f"{type(exc).__name__}: {exc}"
        if errors:
            response.status_code = 503
            return {"ok": False, "errors": errors}
        return {"ok": True}

    @app.get("/v1/capabilities")
    def capabilities(
        policy: TokenPolicy = Depends(get_policy),
        manager: SessionManager = Depends(get_manager),
    ) -> dict[str, Any]:
        return manager.capabilities(policy)

    @app.post("/v1/sessions", status_code=201)
    def create_session(
        payload: CreateSessionRequest,
        policy: TokenPolicy = Depends(get_policy),
        manager: SessionManager = Depends(get_manager),
    ) -> dict[str, Any]:
        row = manager.create_session(
            policy,
            backend_name=payload.backend,
            task=payload.task,
            phase=payload.phase,
            mode=payload.mode,
            seed=payload.seed,
            exec_budget=payload.exec_budget,
        )
        observation = manager.latest_observation(policy, row["id"])
        return {"session": session_wire(row), "observation": observation}

    @app.get("/v1/sessions")
    def list_sessions(
        limit: int = Query(default=100, ge=1, le=500),
        policy: TokenPolicy = Depends(get_policy),
        manager: SessionManager = Depends(get_manager),
    ) -> dict[str, Any]:
        return {"sessions": [session_wire(row) for row in manager.list_sessions(policy, limit)]}

    @app.get("/v1/sessions/{session_id}")
    def get_session(
        session_id: str,
        policy: TokenPolicy = Depends(get_policy),
        manager: SessionManager = Depends(get_manager),
    ) -> dict[str, Any]:
        return session_wire(manager.get_session(policy, session_id))

    @app.get("/v1/sessions/{session_id}/observation")
    def latest_observation(
        session_id: str,
        policy: TokenPolicy = Depends(get_policy),
        manager: SessionManager = Depends(get_manager),
    ) -> dict[str, Any]:
        return manager.latest_observation(policy, session_id)

    @app.post("/v1/sessions/{session_id}/steps")
    def step(
        session_id: str,
        payload: StepRequest,
        wait: bool = Query(default=True),
        idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
        policy: TokenPolicy = Depends(get_policy),
        manager: SessionManager = Depends(get_manager),
    ):
        require_scope(policy, "typed")
        operation = manager.submit(
            policy,
            session_id,
            "step",
            {"action": _json(payload.action)},
            idempotency_key=_key(payload.idempotency_key, idempotency_header),
            wait=wait,
        )
        return _operation_response(operation)

    @app.post("/v1/sessions/{session_id}/executions")
    def execute(
        session_id: str,
        payload: ExecutionRequest,
        wait: bool = Query(default=True),
        idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
        policy: TokenPolicy = Depends(get_policy),
        manager: SessionManager = Depends(get_manager),
    ):
        require_scope(policy, "exec")
        operation = manager.submit(
            policy,
            session_id,
            "execute",
            {"code": payload.code},
            idempotency_key=_key(payload.idempotency_key, idempotency_header),
            wait=wait,
        )
        return _operation_response(operation)

    @app.post("/v1/sessions/{session_id}/reset")
    def reset(
        session_id: str,
        payload: LifecycleRequest | None = None,
        wait: bool = Query(default=True),
        idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
        policy: TokenPolicy = Depends(get_policy),
        manager: SessionManager = Depends(get_manager),
    ):
        body = payload or LifecycleRequest()
        operation = manager.submit(
            policy,
            session_id,
            "reset",
            {},
            idempotency_key=_key(body.idempotency_key, idempotency_header),
            wait=wait,
        )
        return _operation_response(operation)

    @app.post("/v1/sessions/{session_id}/close")
    def close(
        session_id: str,
        payload: LifecycleRequest | None = None,
        wait: bool = Query(default=True),
        idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
        policy: TokenPolicy = Depends(get_policy),
        manager: SessionManager = Depends(get_manager),
    ):
        body = payload or LifecycleRequest()
        operation = manager.submit(
            policy,
            session_id,
            "close",
            {"reason": body.reason},
            idempotency_key=_key(body.idempotency_key, idempotency_header),
            wait=wait,
        )
        return _operation_response(operation)

    @app.get("/v1/sessions/{session_id}/operations")
    def list_operations(
        session_id: str,
        limit: int = Query(default=50, ge=1, le=500),
        policy: TokenPolicy = Depends(get_policy),
        manager: SessionManager = Depends(get_manager),
    ) -> dict[str, Any]:
        return {
            "operations": [
                operation_wire(row)
                for row in manager.list_operations(policy, session_id, limit)
            ]
        }

    @app.get("/v1/sessions/{session_id}/operations/{operation_id}")
    def get_operation(
        session_id: str,
        operation_id: str,
        policy: TokenPolicy = Depends(get_policy),
        manager: SessionManager = Depends(get_manager),
    ) -> dict[str, Any]:
        return operation_wire(manager.get_operation(policy, session_id, operation_id))

    @app.get("/v1/artifacts/{artifact_id}")
    def download_artifact(
        artifact_id: str,
        policy: TokenPolicy = Depends(get_policy),
        manager: SessionManager = Depends(get_manager),
    ) -> Response:
        payload, media_type = manager.artifacts.read(policy.project_id, artifact_id)
        return Response(
            content=payload,
            media_type=media_type,
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app


app = create_app()
