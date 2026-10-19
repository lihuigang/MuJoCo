"""MuJoCo SO-101 backend adapter."""

from __future__ import annotations

from ...core.backend import BackendSession
from .task import (
    CONTROL_HZ,
    IMAGE_SIZE,
    NATIVE_ACTION_LIMIT,
    TASK_NAME,
    MuJoCoSO101Session,
    _add_scene,
)

__all__ = ["MuJoCoSO101Backend", "MuJoCoSO101Session", "_add_scene"]


class MuJoCoSO101Backend:
    name = "mujoco_so101"

    def capabilities(self) -> dict:
        return {
            "name": self.name,
            "tasks": [
                {
                    "name": TASK_NAME,
                    "instruction": (
                        "Pick up the red cube and place it stably on the green target pad."
                    ),
                    "native_action_limit": NATIVE_ACTION_LIMIT,
                    "control_hz": CONTROL_HZ,
                    "cameras": [
                        {
                            "name": "top_cam",
                            "width": IMAGE_SIZE[1],
                            "height": IMAGE_SIZE[0],
                            "media_type": "image/png",
                        },
                        {
                            "name": "wrist_cam",
                            "width": IMAGE_SIZE[1],
                            "height": IMAGE_SIZE[0],
                            "media_type": "image/png",
                        },
                    ],
                }
            ],
            "action_schemas": [
                {
                    "name": "joint",
                    "fields": {
                        "arm_joint_state": {"shape": [5], "unit": "rad"},
                        "ee_joint_state": {"shape": [1], "range": [0, 1], "unit": "normalized"},
                    },
                },
                {
                    "name": "ik",
                    "fields": {
                        "ee_position": {"shape": [3], "unit": "m", "frame": "world"},
                        "ee_orientation": {
                            "shape": [4],
                            "unit": "quaternion (w, x, y, z)",
                            "frame": "world",
                            "optional": True,
                        },
                        "ee_joint_state": {"shape": [1], "range": [0, 1], "unit": "normalized"},
                    },
                },
            ],
            "coordinate_frame": "world (MuJoCo, metres)",
            "exec_compatible": True,
        }

    def create_session(self, task: str, *, seed: int = 0) -> BackendSession:
        if task != TASK_NAME:
            raise ValueError(f"unknown SO-101 task {task!r}; available: {TASK_NAME!r}")
        return MuJoCoSO101Session(seed=seed)
