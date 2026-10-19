from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from simulation_runtime.service.app import create_app
from simulation_runtime.service.config import Settings, TokenPolicy

ALPHA = "alpha-token-0123456789abcdef"
BETA = "beta-token-0123456789abcdef"
HOLD = {"ee_position": [0.0, 0.0, 0.2], "ee_joint_state": [1.0]}
GRASP = {"ee_position": [0.0, 0.0, 0.01], "ee_joint_state": [0.0]}


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        data_dir=tmp_path / "data",
        tokens=(
            TokenPolicy(
                project_id="alpha",
                token=ALPHA,
                scopes=frozenset({"playground", "typed", "exec"}),
                max_sessions=2,
            ),
            TokenPolicy(
                project_id="beta",
                token=BETA,
                scopes=frozenset({"playground", "test", "typed", "exec"}),
            ),
        ),
        backends=("fake",),
        max_sessions=4,
        max_sessions_per_project=2,
        default_exec_budget=3,
        execution_timeout_sec=5.0,
        operation_timeout_sec=30.0,
        request_wait_sec=10.0,
        worker_start_timeout_sec=30.0,
        session_idle_ttl_sec=3600.0,
        session_max_lifetime_sec=7200.0,
        reap_interval_sec=3600.0,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture()
def client(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def headers(token=ALPHA):
    return {"Authorization": f"Bearer {token}"}


def create(client, token=ALPHA, **payload):
    body = {
        "backend": "fake",
        "task": "reach",
        "phase": "playground",
        "mode": "typed",
        "seed": 5,
    }
    body.update(payload)
    response = client.post("/v1/sessions", json=body, headers=headers(token))
    assert response.status_code == 201, response.text
    return response.json()


def operation(response):
    assert response.status_code in (200, 202), response.text
    return response.json()


def test_auth_and_capabilities(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/readyz").json() == {"ok": True}

    response = client.get("/v1/capabilities")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"

    assert client.get("/v1/capabilities", headers=headers("wrong")).status_code == 401
    payload = client.get("/v1/capabilities", headers=headers()).json()
    assert payload["protocol"] == "simulation-runtime/v1"
    assert payload["service_version"]
    assert payload["session_modes"] == ["typed", "exec"]
    assert payload["features"]["exec"] is True
    assert [item["name"] for item in payload["backends"]] == ["fake"]
    assert payload["limits"]["max_sessions_per_project"] == 2


def test_typed_session_lifecycle_and_artifacts(client):
    created = create(client)
    session = created["session"]
    session_id = session["session_id"]
    assert session["state"] == "active"
    assert session["native_action_limit"] == 50
    observation = created["observation"]
    assert set(observation["cameras"]) == {"top_cam", "wrist_cam"}
    href = observation["cameras"]["top_cam"]["href"]
    assert href.startswith("/v1/artifacts/")

    response = client.get(f"/v1/sessions/{session_id}", headers=headers())
    assert response.status_code == 200
    assert response.json()["session_id"] == session_id

    response = client.get(f"/v1/sessions/{session_id}/observation", headers=headers())
    assert response.status_code == 200
    assert response.json()["observation_id"] == observation["observation_id"]

    response = client.post(
        f"/v1/sessions/{session_id}/steps",
        json={"action": HOLD},
        headers={**headers(), "Idempotency-Key": "first-step"},
    )
    first = operation(response)
    assert first["status"] == "succeeded"
    assert first["result"]["applied"] is True
    assert first["result"]["native_steps_used"] == 1
    assert first["result"]["observation"]["sequence"] == 1

    replay = operation(
        client.post(
            f"/v1/sessions/{session_id}/steps",
            json={"action": HOLD},
            headers={**headers(), "Idempotency-Key": "first-step"},
        )
    )
    assert replay["operation_id"] == first["operation_id"]
    assert replay["status"] == "succeeded"

    conflict = client.post(
        f"/v1/sessions/{session_id}/steps",
        json={"action": GRASP},
        headers={**headers(), "Idempotency-Key": "first-step"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    outcome = operation(
        client.post(f"/v1/sessions/{session_id}/steps", json={"action": GRASP}, headers=headers())
    )
    assert outcome["result"]["success"] is True
    assert outcome["result"]["terminated"] is True
    status = client.get(f"/v1/sessions/{session_id}", headers=headers()).json()
    assert status["success"] is True
    assert status["terminated"] is True

    image = client.get(href, headers=headers())
    assert image.status_code == 200
    assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert image.headers["content-type"] == "image/png"
    assert client.get(href, headers=headers(BETA)).status_code == 404

    closed = operation(
        client.post(f"/v1/sessions/{session_id}/close", json={}, headers=headers())
    )
    assert closed["status"] == "succeeded"
    status = client.get(f"/v1/sessions/{session_id}", headers=headers()).json()
    assert status["state"] == "closed"

    rejected = client.post(
        f"/v1/sessions/{session_id}/steps", json={"action": HOLD}, headers=headers()
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "session_closed"


def test_project_isolation(client):
    session_id = create(client)["session"]["session_id"]
    observation = client.get(
        f"/v1/sessions/{session_id}/observation", headers=headers()
    ).json()
    artifact_id = observation["cameras"]["top_cam"]["artifact_id"]

    assert client.get(f"/v1/sessions/{session_id}", headers=headers(BETA)).status_code == 404
    assert (
        client.get(f"/v1/sessions/{session_id}/observation", headers=headers(BETA)).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/sessions/{session_id}/steps",
            json={"action": HOLD},
            headers=headers(BETA),
        ).status_code
        == 404
    )
    assert (
        client.get(f"/v1/artifacts/{artifact_id}", headers=headers(BETA)).status_code == 404
    )
    listed = client.get("/v1/sessions", headers=headers(BETA)).json()["sessions"]
    assert listed == []
    assert len(client.get("/v1/sessions", headers=headers()).json()["sessions"]) == 1


def test_exec_session_persists_globals_and_enforces_budget(client):
    session_id = create(client, mode="exec", exec_budget=2)["session"]["session_id"]

    mismatch = client.post(
        f"/v1/sessions/{session_id}/steps", json={"action": HOLD}, headers=headers()
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "action_mode_mismatch"

    first = operation(
        client.post(
            f"/v1/sessions/{session_id}/executions",
            json={
                "code": (
                    "x = 41\n"
                    "step({'ee_position': [0, 0, 0.2], 'ee_joint_state': [1.0]})\n"
                    "print('moved')"
                )
            },
            headers=headers(),
        )
    )
    assert first["status"] == "succeeded"
    assert first["result"]["ok"] is True
    assert "moved" in first["result"]["stdout"]
    assert first["result"]["native_steps_used"] == 1
    assert first["result"]["executions_used"] == 1

    second = operation(
        client.post(
            f"/v1/sessions/{session_id}/executions",
            json={"code": "print(x + 1)"},
            headers=headers(),
        )
    )
    assert second["result"]["ok"] is True
    assert "42" in second["result"]["stdout"]

    exhausted = operation(
        client.post(
            f"/v1/sessions/{session_id}/executions",
            json={"code": "print('nope')"},
            headers=headers(),
        )
    )
    assert exhausted["status"] == "failed"
    assert exhausted["error"]["code"] == "budget_exhausted"
    status = client.get(f"/v1/sessions/{session_id}", headers=headers()).json()
    assert status["state"] == "active"
    assert status["executions_used"] == 2


def test_reset_rules(client):
    test_session = create(client, token=BETA, phase="test")["session"]["session_id"]
    forbidden = operation(
        client.post(f"/v1/sessions/{test_session}/reset", json={}, headers=headers(BETA))
    )
    assert forbidden["status"] == "failed"
    assert forbidden["error"]["code"] == "forbidden"
    assert client.get(f"/v1/sessions/{test_session}", headers=headers(BETA)).json()[
        "state"
    ] == "active"

    playground = create(client)["session"]["session_id"]
    operation(
        client.post(
            f"/v1/sessions/{playground}/steps", json={"action": HOLD}, headers=headers()
        )
    )
    reset = operation(
        client.post(f"/v1/sessions/{playground}/reset", json={}, headers=headers())
    )
    assert reset["status"] == "succeeded"
    assert reset["result"]["is_reset"] is True
    assert reset["result"]["native_steps_remaining"] == 50
    assert reset["result"]["native_steps_used"] == 1


def test_execution_timeout_interrupts_without_replay(tmp_path):
    app = create_app(make_settings(tmp_path, execution_timeout_sec=0.4))
    with TestClient(app) as client:
        session_id = create(client, mode="exec", exec_budget=5)["session"]["session_id"]
        response = client.post(
            f"/v1/sessions/{session_id}/executions",
            json={"code": "while True:\n    pass"},
            headers=headers(),
        )
        timed_out = operation(response)
        assert timed_out["status"] == "failed"
        assert timed_out["error"]["code"] == "execution_timeout"
        assert timed_out["outcome_uncertain"] is True
        status = client.get(f"/v1/sessions/{session_id}", headers=headers()).json()
        assert status["state"] == "interrupted"
        assert status["reason"] == "execution_timeout"
        again = client.post(
            f"/v1/sessions/{session_id}/executions",
            json={"code": "print('replayed?')"},
            headers=headers(),
        )
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "session_interrupted"


def test_viewer_page_is_served_without_auth(client):
    redirect = client.get("/", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/viewer"
    page = client.get("/viewer")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert "Simulation Runtime" in page.text
    assert "wrist_cam" in page.text
    assert "Authorization" in page.text
    assert client.get("/v1/sessions").status_code == 401
