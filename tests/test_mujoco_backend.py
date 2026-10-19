from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("mujoco")

from simulation_runtime.backends.mujoco_so101 import MuJoCoSO101Backend
from simulation_runtime.backends.mujoco_so101.task import _quat_multiply
from simulation_runtime.core.session import SessionLimits, SimulationSession


def test_capabilities_describe_the_task():
    capabilities = MuJoCoSO101Backend().capabilities()
    task = capabilities["tasks"][0]
    assert task["name"] == "pick_cube"
    assert task["native_action_limit"] == 300
    assert task["control_hz"] == 20.0
    assert {camera["name"] for camera in task["cameras"]} == {"top_cam", "wrist_cam"}
    ik = next(
        schema for schema in capabilities["action_schemas"] if schema["name"] == "ik"
    )
    assert ik["fields"]["ee_orientation"]["optional"] is True


def test_native_reset_step_and_render():
    backend = MuJoCoSO101Backend()
    native = backend.create_session("pick_cube", seed=0)
    try:
        observation = native.reset(0)
        assert observation.instruction
        for name in ("top_cam", "wrist_cam"):
            frame = observation.vision[name]
            assert frame.shape == (320, 320, 3)
            assert frame.dtype.name == "uint8"
        assert observation.state["arm_joint_state"].shape == (5,)
        assert observation.state["ee_joint_state"].shape == (1,)

        transition = native.step(
            {
                "arm_joint_state": [0.0, -0.4, 0.4, 0.0, 0.0],
                "ee_joint_state": [1.0],
            }
        )
        assert transition.observation.vision["top_cam"].shape == (320, 320, 3)
        assert isinstance(transition.success, bool)
    finally:
        native.close()


def test_ik_action_tracks_orientation_target():
    backend = MuJoCoSO101Backend()
    native = backend.create_session("pick_cube", seed=0)
    try:
        observation = native.reset(0)
        position = observation.state["ee_pose"][:3].tolist()
        initial = observation.state["ee_pose"][3:]
        angle = np.radians(30.0)
        delta = np.array([np.cos(angle / 2), 0.0, 0.0, np.sin(angle / 2)])
        target = _quat_multiply(delta, initial)
        transition = None
        for _ in range(15):
            transition = native.step(
                {
                    "ee_position": position,
                    "ee_orientation": target.tolist(),
                    "ee_joint_state": [1.0],
                }
            )
        reached = transition.observation.state["ee_pose"][3:]
        error = 2.0 * np.degrees(
            np.arccos(np.clip(abs(float(np.dot(reached, target))), -1.0, 1.0))
        )
        assert error < 15.0
    finally:
        native.close()


def test_exec_program_can_read_observation_and_step():
    backend = MuJoCoSO101Backend()
    native = backend.create_session("pick_cube", seed=1)
    session = SimulationSession(
        native,
        phase="playground",
        mode="exec",
        seed=1,
        limits=SessionLimits(
            native_action_limit=native.native_action_limit,
            exec_budget=2,
            execution_timeout_sec=30.0,
        ),
    )
    try:
        result = session.execute(
            "obs = get_observation()\n"
            "print(sorted(obs['state']))\n"
            "step({'ee_position': [0.2, 0.0, 0.15], 'ee_joint_state': [0.5]})"
        )
        assert result.ok, result.error
        assert result.native_steps_used == 1
        assert "ee_pose" in result.stdout
        assert result.observation["vision"]["top_cam"].shape == (320, 320, 3)
    finally:
        session.close()
