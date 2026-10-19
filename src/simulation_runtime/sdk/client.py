"""Small stdlib-only client. No third-party dependencies are required.

The client deliberately exposes the operation envelope for low-level calls so
callers can implement their own polling. :class:`Session` wraps that envelope
with convenient synchronous methods that wait for a terminal operation.
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

TERMINAL_STATUSES = frozenset({"succeeded", "failed"})


class SimulationError(Exception):
    """Base class for every SDK error."""


class TransportError(SimulationError):
    """The service could not be reached or returned a non-JSON response."""


class ServiceError(SimulationError):
    """The service rejected a request with an HTTP error."""

    def __init__(self, status_code: int, code: str, message: str, payload: Any = None):
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.payload = payload


class OperationFailedError(SimulationError):
    """A queued operation finished with a failure."""

    def __init__(self, operation: dict[str, Any]):
        error = operation.get("error") or {}
        super().__init__(
            f"{error.get('code', 'operation_failed')}: "
            f"{error.get('message', 'operation failed')}"
        )
        self.operation = operation
        self.code = error.get("code", "operation_failed")
        self.outcome_uncertain = bool(operation.get("outcome_uncertain"))


class OperationTimeout(SimulationError):
    """Polling did not observe a terminal operation in time."""

    def __init__(self, operation: dict[str, Any]):
        super().__init__(f"operation {operation.get('operation_id')} did not finish in time")
        self.operation = operation


def _encode(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


class Client:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        user_agent: str = "simulation-runtime-sdk/0.1",
    ):
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            raise ValueError("token is required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.user_agent = user_agent

    # -- transport ------------------------------------------------------

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}{path}"
        if query:
            encoded = {key: str(value).lower() if isinstance(value, bool) else str(value)
                       for key, value in query.items() if value is not None}
            if encoded:
                url += "?" + urllib.parse.urlencode(encoded)
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        query: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        raw: bool = False,
        timeout: float | None = None,
    ) -> Any:
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json" if not raw else "*/*",
            "User-Agent": self.user_agent,
        }
        if body is not None:
            data = json.dumps(_encode(body), separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            self._url(path, query), data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                payload = response.read()
                media_type = response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            error: Any = None
            with contextlib.suppress(ValueError):
                error = json.loads(payload)
            if isinstance(error, dict) and isinstance(error.get("error"), dict):
                detail = error["error"]
                raise ServiceError(
                    exc.code,
                    str(detail.get("code", "http_error")),
                    str(detail.get("message", exc.reason)),
                    error,
                ) from None
            raise ServiceError(
                exc.code, "http_error", exc.reason or "HTTP error", payload.decode(errors="replace")
            ) from None
        except urllib.error.URLError as exc:
            raise TransportError(f"cannot reach {self.base_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TransportError(f"request to {self.base_url} timed out") from exc
        if raw or not media_type.startswith("application/json"):
            return payload
        if not payload:
            return None
        try:
            return json.loads(payload)
        except ValueError as exc:
            raise TransportError("service returned invalid JSON") from exc

    # -- capabilities and session reads ---------------------------------

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/v1/capabilities")

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        payload = self._request("GET", "/v1/sessions", query={"limit": limit})
        return payload["sessions"]

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{urllib.parse.quote(session_id)}")

    def observation(self, session_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/sessions/{urllib.parse.quote(session_id)}/observation"
        )

    def operation(self, session_id: str, operation_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/sessions/{urllib.parse.quote(session_id)}/operations/"
            f"{urllib.parse.quote(operation_id)}",
        )

    def operations(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/v1/sessions/{urllib.parse.quote(session_id)}/operations",
            query={"limit": limit},
        )
        return payload["operations"]

    def wait_operation(
        self,
        session_id: str,
        operation_id: str,
        *,
        timeout: float = 60.0,
        interval: float = 0.2,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            operation = self.operation(session_id, operation_id)
            if operation["status"] in TERMINAL_STATUSES:
                return operation
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OperationTimeout(operation)
            time.sleep(min(interval, remaining))

    # -- session lifecycle ----------------------------------------------

    def create_session(
        self,
        *,
        backend: str = "mujoco_so101",
        task: str = "pick_cube",
        phase: str = "playground",
        mode: str = "typed",
        seed: int = 0,
        exec_budget: int | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/sessions",
            body={
                "backend": backend,
                "task": task,
                "phase": phase,
                "mode": mode,
                "seed": seed,
                "exec_budget": exec_budget,
            },
        )

    def step(
        self,
        session_id: str,
        action: Any,
        *,
        idempotency_key: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sessions/{urllib.parse.quote(session_id)}/steps",
            body={"action": action},
            query={"wait": wait},
            idempotency_key=idempotency_key,
        )

    def execute(
        self,
        session_id: str,
        code: str,
        *,
        idempotency_key: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sessions/{urllib.parse.quote(session_id)}/executions",
            body={"code": code},
            query={"wait": wait},
            idempotency_key=idempotency_key,
        )

    def reset(
        self,
        session_id: str,
        *,
        idempotency_key: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sessions/{urllib.parse.quote(session_id)}/reset",
            body={},
            query={"wait": wait},
            idempotency_key=idempotency_key,
        )

    def close_session(
        self,
        session_id: str,
        *,
        reason: str = "closed_by_client",
        idempotency_key: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sessions/{urllib.parse.quote(session_id)}/close",
            body={"reason": reason},
            query={"wait": wait},
            idempotency_key=idempotency_key,
        )

    # -- artifacts -------------------------------------------------------

    def artifact_url(self, artifact_id: str) -> str:
        return self._url(f"/v1/artifacts/{urllib.parse.quote(artifact_id)}")

    def download_artifact(self, artifact_id: str) -> bytes:
        return self._request(
            "GET", f"/v1/artifacts/{urllib.parse.quote(artifact_id)}", raw=True
        )

    # -- convenience -----------------------------------------------------

    @contextlib.contextmanager
    def session(self, **kwargs: Any) -> Iterator["Session"]:
        payload = self.create_session(**kwargs)
        session = Session(self, payload["session"], payload["observation"])
        try:
            yield session
        finally:
            with contextlib.suppress(ServiceError):
                self.close_session(session.session_id)


class Session:
    """Synchronous convenience wrapper around one server session."""

    def __init__(self, client: Client, state: dict[str, Any], initial_observation: dict[str, Any]):
        self.client = client
        self.state = state
        self.initial_observation = initial_observation

    @property
    def session_id(self) -> str:
        return self.state["session_id"]

    @property
    def task(self) -> str:
        return self.state["task"]

    @property
    def phase(self) -> str:
        return self.state["phase"]

    @property
    def mode(self) -> str:
        return self.state["mode"]

    def status(self) -> dict[str, Any]:
        self.state = self.client.get_session(self.session_id)
        return self.state

    def observation(self) -> dict[str, Any]:
        return self.client.observation(self.session_id)

    def instruction(self) -> str:
        return str(self.observation()["instruction"])

    def download_camera(
        self, observation: dict[str, Any], camera: str, *, path: str | None = None
    ) -> bytes:
        artifact_id = observation["cameras"][camera]["artifact_id"]
        payload = self.client.download_artifact(artifact_id)
        if path is not None:
            with open(path, "wb") as stream:
                stream.write(payload)
        return payload

    def _run(
        self,
        method: Any,
        *args: Any,
        idempotency_key: str | None = None,
        wait_timeout: float = 120.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        operation = method(
            self.session_id,
            *args,
            idempotency_key=idempotency_key,
            wait=True,
            **kwargs,
        )
        if operation["status"] not in TERMINAL_STATUSES:
            operation = self.client.wait_operation(
                self.session_id, operation["operation_id"], timeout=wait_timeout
            )
        if operation["status"] == "failed":
            raise OperationFailedError(operation)
        return operation["result"]

    def step(
        self,
        action: Any,
        *,
        idempotency_key: str | None = None,
        wait_timeout: float = 120.0,
    ) -> dict[str, Any]:
        return self._run(
            self.client.step,
            action,
            idempotency_key=idempotency_key,
            wait_timeout=wait_timeout,
        )

    def execute(
        self,
        code: str,
        *,
        idempotency_key: str | None = None,
        wait_timeout: float = 300.0,
    ) -> dict[str, Any]:
        return self._run(
            self.client.execute,
            code,
            idempotency_key=idempotency_key,
            wait_timeout=wait_timeout,
        )

    def reset(
        self,
        *,
        idempotency_key: str | None = None,
        wait_timeout: float = 120.0,
    ) -> dict[str, Any]:
        return self._run(
            self.client.reset,
            idempotency_key=idempotency_key,
            wait_timeout=wait_timeout,
        )

    def close(self, *, reason: str = "closed_by_client") -> dict[str, Any]:
        operation = self.client.close_session(self.session_id, reason=reason)
        if operation["status"] not in TERMINAL_STATUSES:
            operation = self.client.wait_operation(
                self.session_id, operation["operation_id"], timeout=30.0
            )
        if operation["status"] == "failed":
            raise OperationFailedError(operation)
        return operation["result"]
