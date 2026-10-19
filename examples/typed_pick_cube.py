"""Drive the SO-101 pick_cube task with typed IK actions.

This is an integration example, not a trained policy: it uses a fixed script
and may not solve the task for every seed. Set:

    SIM_RUNTIME_URL=https://sim.example.internal
    SIM_RUNTIME_TOKEN=<project token with playground + typed scopes>
"""

from __future__ import annotations

import os
from pathlib import Path

from simulation_runtime.sdk import Client

OUTPUT = Path(__file__).with_name("output")


def move(session, position, gripper, repeat=4):
    action = {"ee_position": list(position), "ee_joint_state": [gripper]}
    result = None
    for _ in range(repeat):
        result = session.step(action)
    return result


def main() -> None:
    client = Client(os.environ["SIM_RUNTIME_URL"], os.environ["SIM_RUNTIME_TOKEN"])
    OUTPUT.mkdir(exist_ok=True)

    with client.session(
        backend="mujoco_so101",
        task="pick_cube",
        phase="playground",
        mode="typed",
        seed=int(os.environ.get("SIM_RUNTIME_SEED", "0")),
    ) as session:
        print(session.instruction())
        observation = session.observation()
        session.download_camera(observation, "top_cam", path=str(OUTPUT / "000_initial.png"))

        move(session, (0.20, -0.10, 0.12), 1.0)
        move(session, (0.20, -0.10, 0.035), 1.0)
        move(session, (0.20, -0.10, 0.035), 0.0, repeat=6)
        move(session, (0.20, -0.10, 0.15), 0.0, repeat=6)
        move(session, (0.20, 0.13, 0.12), 0.0, repeat=8)
        result = move(session, (0.20, 0.13, 0.06), 1.0, repeat=6)

        final = session.observation()
        session.download_camera(final, "top_cam", path=str(OUTPUT / "001_final.png"))
        print(
            {
                "success": result["success"],
                "terminated": result["terminated"],
                "native_steps_used": result["native_steps_used"],
                "native_steps_remaining": result["native_steps_remaining"],
            }
        )
        print(f"images written to {OUTPUT}")


if __name__ == "__main__":
    main()
