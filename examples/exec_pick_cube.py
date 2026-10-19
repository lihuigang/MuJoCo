"""Submit one restricted Python program to an exec session.

The program keeps state across executions and uses only the four primitives
(get_instruction, get_observation, step, reset) plus numpy/math. Set:

    SIM_RUNTIME_URL=https://sim.example.internal
    SIM_RUNTIME_TOKEN=<project token with playground + exec scopes>
"""

from __future__ import annotations

import os
from pathlib import Path

from simulation_runtime.sdk import Client

OUTPUT = Path(__file__).with_name("output")

PROGRAM = """
import numpy as np

def hold(position, gripper, steps=4):
    action = {"ee_position": list(position), "ee_joint_state": [gripper]}
    last = None
    for repeat in range(steps):
        last = step(action)
    return last

print(get_instruction())
hold((0.20, -0.10, 0.12), 1.0)
hold((0.20, -0.10, 0.035), 1.0)
hold((0.20, -0.10, 0.035), 0.0, steps=6)
hold((0.20, -0.10, 0.15), 0.0, steps=6)
hold((0.20, 0.13, 0.12), 0.0, steps=8)
last = hold((0.20, 0.13, 0.06), 1.0, steps=6)
print("success", last[4]["success"])
print("cube height", float(np.round(get_observation()["state"]["ee_pose"][2], 4)))
"""


def main() -> None:
    client = Client(os.environ["SIM_RUNTIME_URL"], os.environ["SIM_RUNTIME_TOKEN"])
    OUTPUT.mkdir(exist_ok=True)

    with client.session(
        backend="mujoco_so101",
        task="pick_cube",
        phase="playground",
        mode="exec",
        seed=int(os.environ.get("SIM_RUNTIME_SEED", "0")),
        exec_budget=10,
    ) as session:
        result = session.execute(PROGRAM)
        print("ok:", result["ok"])
        print("stdout:\n" + result["stdout"])
        if result["stderr"]:
            print("stderr:\n" + result["stderr"])
        print(
            {
                "success": result["success"],
                "native_steps_used": result["native_steps_used"],
                "native_steps_remaining": result["native_steps_remaining"],
                "executions_used": result["executions_used"],
            }
        )
        final = session.observation()
        session.download_camera(final, "top_cam", path=str(OUTPUT / "exec_final.png"))
        print(f"image written to {OUTPUT / 'exec_final.png'}")


if __name__ == "__main__":
    main()
