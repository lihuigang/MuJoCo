"""Deterministic backend used by tests and SDK integration examples."""

from __future__ import annotations

import numpy as np

from ..core.backend import BackendTransition
from ..core.types import Observation

NATIVE_ACTION_LIMIT = 50
CONTROL_HZ = 10.0
IMAGE_SIZE = (48, 64)


class FakeSession:
    task = "reach"
    native_action_limit = NATIVE_ACTION_LIMIT
    control_hz = CONTROL_HZ

    def __init__(self, *, seed: int = 0):
        self.seed = seed
        self.position = np.array([0.0, 0.0, 0.2], dtype=np.float64)
        self.gripper = 1.0
        self.steps = 0
        self.success = False
        self._instruction = "Move the end effector to the goal below z=0.05."

    def _observation(self) -> Observation:
        phase = self.steps / 20.0
        frame = np.zeros((*IMAGE_SIZE, 3), dtype=np.uint8)
        frame[:, :, 0] = (np.arange(IMAGE_SIZE[1]) * 3 + int(40 * phase)) % 256
        frame[:, :, 1] = (np.arange(IMAGE_SIZE[0])[:, None] * 4) % 256
        frame[:, :, 2] = 30
        return Observation(
            instruction=self._instruction,
            vision={"top_cam": frame, "wrist_cam": frame[:, ::-1].copy()},
            state={
                "ee_position": self.position.copy(),
                "ee_joint_state": np.array([self.gripper], dtype=np.float64),
            },
            action={
                "ee_position": self.position.copy(),
                "ee_joint_state": np.array([self.gripper], dtype=np.float64),
            },
            frequency=CONTROL_HZ,
        )

    def reset(self, seed: int) -> Observation:
        if seed != self.seed:
            raise ValueError("fake sessions own one fixed seed")
        self.position = np.array([0.0, 0.0, 0.2], dtype=np.float64)
        self.gripper = 1.0
        self.steps = 0
        self.success = False
        return self._observation()

    def step(self, action) -> BackendTransition:
        self.steps += 1
        if "ee_position" in action:
            self.position = np.asarray(action["ee_position"], dtype=np.float64).copy()
        else:
            arm = np.asarray(action["arm_joint_state"], dtype=np.float64)
            self.position[0] += float(arm[0]) * 0.01
            self.position[1] += float(arm[1]) * 0.01
            self.position[2] = 0.2 - 0.001 * self.steps
        self.gripper = float(np.asarray(action["ee_joint_state"])[0])
        self.success = bool(self.position[2] < 0.05 and self.gripper < 0.5)
        return BackendTransition(observation=self._observation(), success=self.success)

    def close(self) -> None:
        pass


class FakeBackend:
    name = "fake"

    def capabilities(self) -> dict:
        return {
            "name": self.name,
            "tasks": [
                {
                    "name": "reach",
                    "instruction": "Move the end effector to the goal below z=0.05.",
                    "native_action_limit": NATIVE_ACTION_LIMIT,
                    "control_hz": CONTROL_HZ,
                    "cameras": [
                        {"name": name, "width": IMAGE_SIZE[1], "height": IMAGE_SIZE[0],
                         "media_type": "image/png"}
                        for name in ("top_cam", "wrist_cam")
                    ],
                }
            ],
            "action_schemas": [
                {"name": "joint", "fields": {"arm_joint_state": {"shape": [5]},
                                             "ee_joint_state": {"shape": [1], "range": [0, 1]}}},
                {"name": "ik", "fields": {"ee_position": {"shape": [3], "unit": "m"},
                                          "ee_joint_state": {"shape": [1], "range": [0, 1]}}},
            ],
            "coordinate_frame": "fake",
            "exec_compatible": True,
        }

    def create_session(self, task: str, *, seed: int = 0) -> FakeSession:
        if task != "reach":
            raise ValueError(f"unknown fake task {task!r}")
        return FakeSession(seed=seed)
