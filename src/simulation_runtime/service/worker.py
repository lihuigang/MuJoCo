"""One simulator session per worker process.

The worker owns the only live simulation world and processes requests strictly
serially. It never retries an action: if it dies or times out, the service marks
the operation outcome uncertain and interrupts the session.
"""

from __future__ import annotations

from typing import Any

from ..backends import create_backend
from ..core.errors import ExecutionTimeout, SimulationRuntimeError
from ..core.session import SessionLimits, SimulationSession


def _error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, SimulationRuntimeError):
        return {"code": exc.code, "message": exc.message}
    return {"code": "simulator_failure", "message": f"{type(exc).__name__}: {exc}"}


def _step_payload(outcome) -> dict[str, Any]:
    return {
        "observation": outcome.observation,
        "applied": outcome.applied,
        "already_terminal": outcome.already_terminal,
        "is_reset": outcome.is_reset,
        "success": outcome.success,
        "terminated": outcome.terminated,
        "truncated": outcome.truncated,
        "native_steps_used": outcome.native_steps_used,
        "native_steps_remaining": outcome.native_steps_remaining,
        "control_hz": outcome.control_hz,
        "sequence": outcome.sequence,
    }


def _exec_payload(outcome) -> dict[str, Any]:
    return {
        "ok": outcome.ok,
        "error": outcome.error,
        "stdout": outcome.stdout,
        "stderr": outcome.stderr,
        "observation": outcome.observation,
        "success": outcome.success,
        "terminated": outcome.terminated,
        "truncated": outcome.truncated,
        "native_steps_used": outcome.native_steps_used,
        "native_steps_remaining": outcome.native_steps_remaining,
        "executions_used": outcome.executions_used,
        "executions_remaining": outcome.executions_remaining,
        "sequence": outcome.sequence,
    }


def _handle(session: SimulationSession, request: dict[str, Any]) -> dict[str, Any]:
    op = request.get("op")
    if op == "status":
        return {"ok": True, "kind": "status", "status": session.status()}
    if op == "step":
        outcome = session.apply_action(request.get("action"))
        return {"ok": True, "kind": "step", "result": _step_payload(outcome)}
    if op == "reset":
        outcome = session.reset()
        return {"ok": True, "kind": "reset", "result": _step_payload(outcome)}
    if op == "execute":
        outcome = session.execute(request.get("code"))
        return {"ok": True, "kind": "execute", "result": _exec_payload(outcome)}
    if op == "close":
        status = session.close(str(request.get("reason", "closed_by_client")))
        return {"ok": True, "kind": "close", "status": status}
    raise ValueError(f"unknown worker operation {op!r}")


def worker_main(connection, spec: dict[str, Any]) -> None:
    session = None
    try:
        backend = create_backend(spec["backend"])
        backend_session = backend.create_session(spec["task"], seed=spec["seed"])
        session = SimulationSession(
            backend_session,
            phase=spec["phase"],
            mode=spec["mode"],
            seed=spec["seed"],
            limits=SessionLimits(
                native_action_limit=backend_session.native_action_limit,
                exec_budget=spec["exec_budget"],
                execution_timeout_sec=spec["execution_timeout_sec"],
            ),
        )
        connection.send(
            {
                "ok": True,
                "ready": True,
                "status": session.status(),
                "observation": session.public_observation(),
            }
        )
    except BaseException as exc:
        connection.send(
            {
                "ok": False,
                "fatal": True,
                "error": _error(exc),
                "status": None if session is None else session.status(),
            }
        )
        if session is not None:
            session.interrupt("initialization_failed")
        connection.close()
        return

    try:
        while True:
            try:
                request = connection.recv()
            except (EOFError, OSError):
                break
            if request is None:
                break
            try:
                response = _handle(session, request)
            except ExecutionTimeout as exc:
                response = {
                    "ok": False,
                    "error": {"code": "execution_timeout", "message": str(exc)},
                }
            except SimulationRuntimeError as exc:
                response = {"ok": False, "error": _error(exc)}
            except BaseException as exc:
                session.interrupt("internal_error")
                response = {"ok": False, "error": _error(exc)}
            response["status"] = session.status()
            connection.send(response)
            if request.get("op") == "close":
                break
    finally:
        try:
            if session is not None and session.state == "active":
                session.close("worker_shutdown")
        finally:
            connection.close()
