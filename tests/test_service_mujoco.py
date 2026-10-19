from __future__ import annotations

import pytest

pytest.importorskip("mujoco")

from fastapi.testclient import TestClient

from simulation_runtime.service.app import create_app
from simulation_runtime.service.config import Settings, TokenPolicy

TOKEN = "mujoco-test-token-0123456789"


def test_mujoco_session_over_http(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        tokens=(TokenPolicy("mujoco", TOKEN, frozenset({"playground", "test", "typed", "exec"})),),
        backends=("mujoco_so101",),
        max_sessions=2,
        default_exec_budget=2,
        execution_timeout_sec=30.0,
        request_wait_sec=20.0,
        worker_start_timeout_sec=60.0,
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/sessions",
            json={"backend": "mujoco_so101", "task": "pick_cube", "seed": 0},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        created = response.json()
        session_id = created["session"]["session_id"]
        observation = created["observation"]
        assert observation["cameras"]["top_cam"]["width"] == 256
        assert observation["instruction"].startswith("Pick up the red cube")

        step = client.post(
            f"/v1/sessions/{session_id}/steps",
            json={
                "action": {
                    "arm_joint_state": [0.0, -0.4, 0.4, 0.0, 0.0],
                    "ee_joint_state": [1.0],
                }
            },
            headers=headers,
        )
        assert step.status_code == 200, step.text
        result = step.json()
        assert result["status"] == "succeeded"
        assert result["result"]["applied"] is True
        assert result["result"]["native_steps_used"] == 1

        image = client.get(
            result["result"]["observation"]["cameras"]["wrist_cam"]["href"], headers=headers
        )
        assert image.status_code == 200
        assert image.content.startswith(b"\x89PNG\r\n\x1a\n")

        closed = client.post(f"/v1/sessions/{session_id}/close", headers=headers)
        assert closed.json()["status"] == "succeeded"
