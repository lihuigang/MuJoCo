from __future__ import annotations

import pytest

from simulation_runtime.backends.fake import FakeSession
from simulation_runtime.core.errors import (
    ActionModeMismatch,
    BudgetExhausted,
    InvalidAction,
    PermissionDenied,
)
from simulation_runtime.core.sandbox import validate_snippet
from simulation_runtime.core.session import SessionLimits, SimulationSession


def make_session(*, phase="playground", mode="typed", exec_budget=0, seed=3):
    backend = FakeSession(seed=seed)
    return SimulationSession(
        backend,
        phase=phase,
        mode=mode,
        seed=seed,
        limits=SessionLimits(
            native_action_limit=backend.native_action_limit,
            exec_budget=exec_budget,
            execution_timeout_sec=2.0,
        ),
    )


HOLD = {"ee_position": [0.0, 0.0, 0.2], "ee_joint_state": [1.0]}
GRASP = {"ee_position": [0.0, 0.0, 0.01], "ee_joint_state": [0.0]}


def test_typed_step_and_success():
    session = make_session()
    outcome = session.apply_action(HOLD)
    assert outcome.applied and not outcome.success
    assert outcome.native_steps_used == 1
    assert outcome.native_steps_remaining == 49
    outcome = session.apply_action(GRASP)
    assert outcome.success and outcome.terminated
    # Once terminal, further steps are accepted but do not advance physics.
    outcome = session.apply_action(HOLD)
    assert outcome.already_terminal and not outcome.applied
    assert outcome.native_steps_used == 2


def test_attempt_budget_truncates_and_reset_renews_it():
    session = make_session()
    for _ in range(50):
        outcome = session.apply_action(HOLD)
    assert outcome.truncated and not outcome.success
    assert session.status()["native_steps_remaining"] == 0
    assert session.apply_action(HOLD).already_terminal

    renewal = session.reset()
    assert renewal.is_reset and renewal.applied
    assert renewal.native_steps_remaining == 50
    assert renewal.native_steps_used == 50
    assert session.control_steps == 50
    assert not session.truncated


def test_reset_is_forbidden_in_test_phase():
    session = make_session(phase="test")
    with pytest.raises(PermissionDenied):
        session.reset()
    assert session.state == "active"


def test_action_modes_are_separated():
    typed = make_session()
    with pytest.raises(ActionModeMismatch):
        typed.execute("print('nope')")

    executable = make_session(mode="exec", exec_budget=5)
    with pytest.raises(ActionModeMismatch):
        executable.apply_action(HOLD)
    # The explicit reset endpoint works in both modes and remains
    # playground-only, while programs use the reset primitive.
    assert executable.reset().is_reset


def test_exec_program_keeps_globals_and_uses_primitives():
    session = make_session(mode="exec", exec_budget=5)
    first = session.execute("counter = 1\nstep({'ee_position': [0, 0, 0.2], 'ee_joint_state': [1.0]})")
    assert first.ok and first.native_steps_used == 1
    second = session.execute("counter += 1\nprint(counter)\nprint(get_instruction())")
    assert second.ok
    assert "2" in second.stdout
    assert "Move the end effector" in second.stdout
    assert second.executions_used == 2
    assert second.executions_remaining == 3


def test_exec_budget_is_enforced_before_running():
    session = make_session(mode="exec", exec_budget=1)
    assert session.execute("x = 1").ok
    with pytest.raises(BudgetExhausted):
        session.execute("x = 2")
    assert session.state == "active"


def test_program_errors_are_reported_not_raised():
    session = make_session(mode="exec", exec_budget=2)
    result = session.execute("raise ValueError('boom')")
    assert not result.ok
    assert "boom" in result.error


def test_invalid_actions_are_rejected():
    session = make_session()
    with pytest.raises(InvalidAction):
        session.apply_action({"ee_position": [0, 0, 0], "gripper": 1})
    with pytest.raises(InvalidAction):
        session.apply_action({"ee_position": [0, 0], "ee_joint_state": [1]})
    with pytest.raises(InvalidAction):
        session.apply_action({"ee_position": [0, 0, 0], "ee_joint_state": [2]})
    with pytest.raises(InvalidAction):
        session.apply_action(
            {
                "ee_position": [0, 0, 0],
                "ee_orientation": [0, 0, 0, 0],
                "ee_joint_state": [1],
            }
        )
    assert session.control_steps == 0


def test_ik_orientation_target_is_accepted():
    session = make_session()
    transition = session.apply_action(
        {
            "ee_position": [0, 0, 0.2],
            "ee_orientation": [2.0, 0, 0, 0],
            "ee_joint_state": [1],
        }
    )
    assert transition is not None
    assert session.control_steps == 1


def test_sandbox_rejects_unsafe_programs():
    for code in (
        "import os",
        "open('x')",
        "getattr(get_instruction, '__globals__')",
        "env.reset()",
        "print(APIS)",
        "import numpy\nnumpy.ndarray.tofile",
    ):
        with pytest.raises(ValueError):
            validate_snippet(code)
    validate_snippet("import numpy as np\nimport math\nx = np.zeros(3)\nprint(math.pi)")
