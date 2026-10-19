"""Observation and action types shared by every backend."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .errors import InvalidAction

ARM_JOINTS = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
)

# Action forms accepted by the SO-101 and fake backends. Capabilities advertise
# the same schema so clients do not need to hard-code simulator internals.
JOINT_ACTION_KEYS = frozenset({"arm_joint_state", "ee_joint_state"})
IK_ACTION_KEYS = frozenset({"ee_position", "ee_joint_state"})


def _finite_vector(value: Any, name: str, length: int) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.shape != (length,) or not np.isfinite(array).all():
        raise InvalidAction(f"{name} must be a finite vector with shape ({length},)")
    return array


def validate_action(action: Any) -> dict[str, np.ndarray]:
    """Validate one typed action without mutating simulator state."""

    if not isinstance(action, dict):
        raise InvalidAction("action must be a JSON object")
    keys = frozenset(action)
    if keys == JOINT_ACTION_KEYS:
        arm = _finite_vector(action["arm_joint_state"], "arm_joint_state", len(ARM_JOINTS))
        gripper = _finite_vector(action["ee_joint_state"], "ee_joint_state", 1)
        if not 0.0 <= gripper[0] <= 1.0:
            raise InvalidAction("ee_joint_state must be normalized to [0, 1]")
        return {"arm_joint_state": arm, "ee_joint_state": gripper}
    if keys == IK_ACTION_KEYS:
        position = _finite_vector(action["ee_position"], "ee_position", 3)
        gripper = _finite_vector(action["ee_joint_state"], "ee_joint_state", 1)
        if not 0.0 <= gripper[0] <= 1.0:
            raise InvalidAction("ee_joint_state must be normalized to [0, 1]")
        return {"ee_position": position, "ee_joint_state": gripper}
    raise InvalidAction(
        "Expected joint keys ['arm_joint_state', 'ee_joint_state'] or "
        "IK keys ['ee_position', 'ee_joint_state']"
    )


@dataclass
class Observation:
    """Detached native observation; camera arrays stay native-resolution."""

    instruction: str
    vision: dict[str, np.ndarray] = field(default_factory=dict)
    state: dict[str, np.ndarray] = field(default_factory=dict)
    action: dict[str, np.ndarray] = field(default_factory=dict)
    frequency: float = 20.0

    def copy(self) -> Observation:
        return Observation(
            instruction=self.instruction,
            vision={name: frame.copy() for name, frame in self.vision.items()},
            state={name: np.array(value, copy=True) for name, value in self.state.items()},
            action={name: np.array(value, copy=True) for name, value in self.action.items()},
            frequency=self.frequency,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "instruction": self.instruction,
            "vision": {name: frame.copy() for name, frame in self.vision.items()},
            "state": {name: np.array(value, copy=True) for name, value in self.state.items()},
            "action": {name: np.array(value, copy=True) for name, value in self.action.items()},
            "frequency": float(self.frequency),
        }

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> Observation:
        payload = copy.deepcopy(payload)
        return cls(
            instruction=str(payload["instruction"]),
            vision={name: np.asarray(value) for name, value in payload["vision"].items()},
            state={name: np.asarray(value) for name, value in payload["state"].items()},
            action={name: np.asarray(value) for name, value in payload["action"].items()},
            frequency=float(payload.get("frequency", 20.0)),
        )

    def public(self) -> dict[str, Any]:
        """Return a detached copy safe to hand to a submitted program."""

        return copy.deepcopy(self.to_wire())


def jsonable(value: Any) -> Any:
    """Convert numpy values into JSON-native values for persisted metadata."""

    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"Unsupported observation value: {type(value).__name__}")
