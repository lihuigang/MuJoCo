"""Backend-neutral session state: budgets, phases, actions and exec programs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .backend import BackendSession, BackendTransition
from .errors import (
    ActionModeMismatch,
    BudgetExhausted,
    ExecutionTimeout,
    InvalidAction,
    PermissionDenied,
    SessionClosed,
    SessionInterrupted,
    SimulatorFailure,
)
from .sandbox import run_program, validate_snippet
from .types import Observation, jsonable, validate_action

PHASES = ("playground", "test")
MODES = ("typed", "exec")


@dataclass(frozen=True)
class SessionLimits:
    native_action_limit: int
    exec_budget: int = 0
    execution_timeout_sec: float = 60.0

    def __post_init__(self):
        if type(self.native_action_limit) is not int or self.native_action_limit <= 0:
            raise ValueError("native_action_limit must be a positive integer")
        if type(self.exec_budget) is not int or self.exec_budget < 0:
            raise ValueError("exec_budget must be a nonnegative integer")
        if self.execution_timeout_sec <= 0:
            raise ValueError("execution_timeout_sec must be positive")


@dataclass
class StepOutcome:
    observation: dict[str, Any]
    applied: bool
    already_terminal: bool
    success: bool
    terminated: bool
    truncated: bool
    native_steps_used: int
    native_steps_remaining: int
    control_hz: float
    sequence: int
    is_reset: bool = False


@dataclass
class ExecOutcome:
    ok: bool
    error: str | None
    stdout: str
    stderr: str
    observation: dict[str, Any]
    success: bool
    terminated: bool
    truncated: bool
    native_steps_used: int
    native_steps_remaining: int
    executions_used: int
    executions_remaining: int
    sequence: int


class SimulationSession:
    """One seeded simulator world with a single serial action writer."""

    def __init__(
        self,
        backend: BackendSession,
        *,
        phase: str,
        mode: str,
        seed: int,
        limits: SessionLimits,
    ):
        if phase not in PHASES:
            raise ValueError(f"unknown phase {phase!r}")
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        if type(seed) is not int or seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        self.backend = backend
        self.task = backend.task
        self.phase = phase
        self.mode = mode
        self.seed = seed
        self.limits = limits
        self.native_action_limit = limits.native_action_limit
        self.control_hz = float(getattr(backend, "control_hz", 20.0))
        self.control_steps = 0
        self.attempt_start = 0
        self.executions_used = 0
        self.success = False
        self.terminated = False
        self.truncated = False
        self.state = "active"
        self.reason = "active"
        self.sequence = 0
        self._observation: Observation | None = None
        self._namespace: dict[str, Any] = {}
        try:
            self._observation = backend.reset(seed)
        except Exception as exc:
            self.state, self.reason = "interrupted", "initialization_failed"
            raise SimulatorFailure(f"backend failed to initialize: {exc}") from exc

    # -- status ---------------------------------------------------------

    @property
    def done(self) -> bool:
        return self.terminated or self.truncated

    @property
    def attempt_steps(self) -> int:
        return self.control_steps - self.attempt_start

    def native_remaining(self) -> int:
        return max(0, self.native_action_limit - self.attempt_steps)

    def exec_remaining(self) -> int:
        return max(0, self.limits.exec_budget - self.executions_used)

    def status(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "phase": self.phase,
            "mode": self.mode,
            "seed": self.seed,
            "state": self.state,
            "reason": self.reason,
            "success": bool(self.success),
            "terminated": bool(self.terminated),
            "truncated": bool(self.truncated),
            "native_action_limit": self.native_action_limit,
            "native_steps_used": self.control_steps,
            "native_steps_remaining": self.native_remaining(),
            "execution_budget": self.limits.exec_budget,
            "executions_used": self.executions_used,
            "executions_remaining": self.exec_remaining(),
            "observation_sequence": self.sequence,
            "control_hz": self.control_hz,
        }

    # -- observations ---------------------------------------------------

    def _require_active(self) -> None:
        if self.state == "closed":
            raise SessionClosed("session is closed")
        if self.state == "interrupted":
            raise SessionInterrupted(
                f"session was interrupted ({self.reason}); its outcome is not replayable"
            )

    def _observe(self, observation: Observation) -> None:
        self._observation = observation.copy()
        self.sequence += 1

    def public_observation(self) -> dict[str, Any]:
        if self._observation is None:
            raise SimulatorFailure("session has no observation")
        return self._observation.public()

    def instruction(self) -> str:
        return self._observation.instruction if self._observation else ""

    # -- typed actions --------------------------------------------------

    def _result(
        self, *, applied: bool, already_terminal: bool = False, is_reset: bool = False
    ) -> StepOutcome:
        return StepOutcome(
            observation=self.public_observation(),
            applied=applied,
            already_terminal=already_terminal,
            success=bool(self.success),
            terminated=bool(self.terminated),
            truncated=bool(self.truncated),
            native_steps_used=self.control_steps,
            native_steps_remaining=self.native_remaining(),
            control_hz=self.control_hz,
            sequence=self.sequence,
            is_reset=is_reset,
        )

    def _step_native(self, action: Any) -> StepOutcome:
        self._require_active()
        action = validate_action(action)
        if self.done:
            return self._result(applied=False, already_terminal=True)
        try:
            transition: BackendTransition = self.backend.step(action)
        except InvalidAction:
            raise
        except Exception as exc:
            self.interrupt("simulator_failure")
            raise SimulatorFailure(f"simulator step failed: {exc}") from exc
        self.control_steps += 1
        self.success = bool(transition.success)
        self.terminated = self.success
        self.truncated = not self.success and self.attempt_steps >= self.native_action_limit
        self._observe(transition.observation)
        return self._result(applied=True)

    def apply_action(self, action: Any) -> StepOutcome:
        if self.mode != "typed":
            raise ActionModeMismatch(
                "this session runs in exec mode; use /executions instead of /steps"
            )
        return self._step_native(action)

    # -- scene reset ----------------------------------------------------

    def _reset_scene(self) -> StepOutcome:
        self._require_active()
        if self.phase != "playground":
            raise PermissionDenied("reset is only allowed in playground; forbidden in test")
        try:
            observation = self.backend.reset(self.seed)
        except Exception as exc:
            self.interrupt("reset_failed")
            raise SimulatorFailure(f"simulator reset failed: {exc}") from exc
        # Cumulative telemetry and execution slots are retained; only the
        # native attempt allowance is renewed for the same seeded scene.
        self.attempt_start = self.control_steps
        self.success = self.terminated = self.truncated = False
        self._observe(observation)
        return self._result(applied=True, is_reset=True)

    def reset(self) -> StepOutcome:
        return self._reset_scene()

    # -- restricted code execution --------------------------------------

    def _program(self) -> dict[str, Any]:
        return {
            "get_instruction": self.instruction,
            "get_observation": self.public_observation,
            "step": self.program_step,
            "reset": self.program_reset,
        }

    def program_step(self, action: Any):
        outcome = self._step_native(action)
        return (
            outcome.observation,
            float(outcome.success),
            outcome.terminated,
            outcome.truncated,
            {"success": outcome.success},
        )

    def program_reset(self):
        outcome = self._reset_scene()
        return outcome.observation, {"task_prompt": self.instruction()}

    def execute(self, code: str) -> ExecOutcome:
        self._require_active()
        if self.mode != "exec":
            raise ActionModeMismatch(
                "this session runs in typed mode; use /steps instead of /executions"
            )
        try:
            validate_snippet(code)
        except (SyntaxError, TypeError, ValueError) as exc:
            raise InvalidAction(f"invalid program: {exc}") from exc
        if self.executions_used >= self.limits.exec_budget:
            raise BudgetExhausted(
                "execution budget exhausted; status, observations and close remain available"
            )
        self.executions_used += 1
        try:
            result = run_program(
                code,
                self._namespace,
                self._program(),
                timeout=self.limits.execution_timeout_sec,
            )
        except ExecutionTimeout:
            self.interrupt("execution_timeout")
            raise
        return ExecOutcome(
            ok=result.ok,
            error=result.error,
            stdout=result.stdout,
            stderr=result.stderr,
            observation=self.public_observation(),
            success=bool(self.success),
            terminated=bool(self.terminated),
            truncated=bool(self.truncated),
            native_steps_used=self.control_steps,
            native_steps_remaining=self.native_remaining(),
            executions_used=self.executions_used,
            executions_remaining=self.exec_remaining(),
            sequence=self.sequence,
        )

    # -- lifecycle ------------------------------------------------------

    def interrupt(self, reason: str) -> None:
        if self.state == "closed":
            return
        self.state, self.reason = "interrupted", reason
        try:
            self.backend.close()
        except Exception:
            pass

    def close(self, reason: str = "closed_by_client") -> dict[str, Any]:
        if self.state == "closed":
            return self.status()
        if self.state == "active":
            self.state, self.reason = "closed", reason
        try:
            self.backend.close()
        finally:
            pass
        return self.status()


def observation_metadata(observation: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe metadata for a wire observation, excluding camera pixels."""

    return {
        "instruction": observation["instruction"],
        "state": jsonable(observation["state"]),
        "action": jsonable(observation["action"]),
        "control_hz": float(observation.get("frequency", 20.0)),
        "cameras": sorted(observation.get("vision", {})),
    }


def camera_frames(observation: dict[str, Any]) -> dict[str, np.ndarray]:
    return {
        str(name): np.asarray(frame)
        for name, frame in observation.get("vision", {}).items()
    }
