"""Stable error taxonomy shared by the runtime, service and SDK."""

from __future__ import annotations


class SimulationRuntimeError(Exception):
    code = "runtime_error"
    http_status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidAction(SimulationRuntimeError):
    code = "invalid_action"
    http_status = 422


class InvalidRequest(SimulationRuntimeError):
    code = "invalid_request"
    http_status = 400


class BudgetExhausted(SimulationRuntimeError):
    code = "budget_exhausted"
    http_status = 409


class SessionClosed(SimulationRuntimeError):
    code = "session_closed"
    http_status = 409


class SessionInterrupted(SimulationRuntimeError):
    code = "session_interrupted"
    http_status = 409


class ActionModeMismatch(SimulationRuntimeError):
    code = "action_mode_mismatch"
    http_status = 409


class PermissionDenied(SimulationRuntimeError):
    code = "forbidden"
    http_status = 403


class SimulatorFailure(SimulationRuntimeError):
    code = "simulator_failure"
    http_status = 500


class CapacityExhausted(SimulationRuntimeError):
    code = "capacity_exhausted"
    http_status = 429


class SessionBusy(SimulationRuntimeError):
    code = "session_busy"
    http_status = 409


class IdempotencyConflict(SimulationRuntimeError):
    code = "idempotency_conflict"
    http_status = 409


class NotFound(SimulationRuntimeError):
    code = "not_found"
    http_status = 404


class Unauthorized(SimulationRuntimeError):
    code = "unauthorized"
    http_status = 401


class OperationFailed(SimulationRuntimeError):
    code = "operation_failed"
    http_status = 500

    def __init__(self, operation: dict, message: str, http_status: int):
        super().__init__(message)
        self.operation = operation
        self.http_status = http_status


class ExecutionTimeout(BaseException):
    """Not an Exception so submitted programs cannot swallow it."""

    code = "execution_timeout"
