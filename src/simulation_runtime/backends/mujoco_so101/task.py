"""MuJoCo SO-101 pick-cube task extracted from the EmbodiedEvo experiment."""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

from ...core.backend import BackendTransition
from ...core.errors import InvalidAction
from ...core.types import Observation

MODEL_NAME = "robotstudio_so101"
TASK_NAME = "pick_cube"
ARM_JOINTS = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
)
GRIPPER_JOINT = "gripper"
GRIPPER_CLOSED = -0.17453
GRIPPER_OPEN = 1.74533
HOME = np.array([0.0, -0.565, 0.284, 1.631, 0.0], dtype=np.float64)
CONTROL_HZ = 20.0
PHYSICS_STEPS = 10
NATIVE_ACTION_LIMIT = 300
IMAGE_SIZE = (320, 320)
ASSET_DIR = Path(__file__).resolve().parent / "assets" / MODEL_NAME


def _add_scene(xml: str) -> str:
    """Add the benchmark scene to Menagerie's robot-only MJCF."""

    root = ET.fromstring(xml)
    visual = root.find("visual")
    if visual is None:
        visual = ET.SubElement(root, "visual")
    ET.SubElement(
        visual,
        "headlight",
        diffuse="0.7 0.7 0.7",
        ambient="0.35 0.35 0.35",
        specular="0.1 0.1 0.1",
    )
    asset = root.find("asset")
    if asset is None:
        asset = ET.SubElement(root, "asset")
    ET.SubElement(asset, "material", name="floor_mat", rgba="0.65 0.62 0.56 1")
    ET.SubElement(asset, "material", name="target_mat", rgba="0.15 0.75 0.25 1")
    world = root.find("worldbody")
    if world is None:
        raise RuntimeError("SO-101 model has no worldbody")
    ET.SubElement(world, "light", pos="0 -0.4 1.2", dir="0 0 -1", directional="true")
    ET.SubElement(
        world,
        "geom",
        name="table",
        type="plane",
        pos="0 0 0",
        size="1 1 0.02",
        material="floor_mat",
        friction="1 0.01 0.001",
    )
    target = ET.SubElement(world, "body", name="target", pos="0.20 0.13 0.004")
    ET.SubElement(
        target,
        "geom",
        name="target_pad",
        type="box",
        size="0.05 0.05 0.004",
        material="target_mat",
        friction="1 0.01 0.001",
    )
    cube = ET.SubElement(world, "body", name="cube", pos="0.20 -0.10 0.02")
    ET.SubElement(cube, "freejoint", name="cube_joint")
    ET.SubElement(
        cube,
        "geom",
        name="cube_geom",
        type="box",
        size="0.016 0.016 0.016",
        mass="0.020",
        rgba="0.85 0.12 0.10 1",
        friction="1.2 0.01 0.001",
        condim="6",
        solref="0.01 1",
    )
    ET.SubElement(world, "body", name="camera_target", pos="0.18 0 0.08")
    ET.SubElement(
        world,
        "camera",
        name="top_cam",
        mode="targetbody",
        target="camera_target",
        pos="0.32 -0.52 0.48",
        fovy="52",
    )
    return ET.tostring(root, encoding="unicode")


def _asset_map() -> dict[str, bytes]:
    if not ASSET_DIR.is_dir():
        raise RuntimeError(f"SO-101 model assets are missing from {ASSET_DIR}")
    assets = {
        path.relative_to(ASSET_DIR).as_posix(): path.read_bytes()
        for path in sorted(ASSET_DIR.rglob("*"))
        if path.is_file() and path.name not in {"LICENSE", "README.md", "CHANGELOG.md"}
    }
    if "so101.xml" not in assets:
        raise RuntimeError("SO-101 model asset map is missing so101.xml")
    return assets


def _load_model():
    try:
        import mujoco
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "MuJoCo is not installed; install the runtime with "
            "`python -m pip install 'simulation-runtime[mujoco]'`"
        ) from exc
    assets = _asset_map()
    xml = assets["so101.xml"].decode("utf-8")
    return mujoco, mujoco.MjModel.from_xml_string(_add_scene(xml), assets=assets)


class MuJoCoSO101Session:
    task = TASK_NAME
    native_action_limit = NATIVE_ACTION_LIMIT
    control_hz = CONTROL_HZ

    def __init__(self, *, seed: int = 0):
        if type(seed) is not int or seed < 0:
            raise ValueError("SO-101 scene seed must be a nonnegative integer")
        self.seed = seed
        self.mujoco, self.model = _load_model()
        self.data = self.mujoco.MjData(self.model)
        self.success = False
        self._success_frames = 0
        self._renderers: dict[str, object] = {}
        self._task_prompt = "Pick up the red cube and place it stably on the green target pad."
        self._joint_ids = np.array(
            [self._name_id(self.mujoco.mjtObj.mjOBJ_JOINT, name) for name in ARM_JOINTS]
        )
        self._joint_qpos = self.model.jnt_qposadr[self._joint_ids]
        self._joint_dofs = self.model.jnt_dofadr[self._joint_ids]
        self._gripper_id = self._name_id(self.mujoco.mjtObj.mjOBJ_JOINT, GRIPPER_JOINT)
        self._gripper_qpos = int(self.model.jnt_qposadr[self._gripper_id])
        self._cube_id = self._name_id(self.mujoco.mjtObj.mjOBJ_BODY, "cube")
        cube_joint = self._name_id(self.mujoco.mjtObj.mjOBJ_JOINT, "cube_joint")
        self._cube_qpos = int(self.model.jnt_qposadr[cube_joint])
        self._cube_dof = int(self.model.jnt_dofadr[cube_joint])
        self._target_id = self._name_id(self.mujoco.mjtObj.mjOBJ_BODY, "target")
        self._ee_site = self._name_id(self.mujoco.mjtObj.mjOBJ_SITE, "gripperframe")
        self._actuators = {
            name: self._name_id(self.mujoco.mjtObj.mjOBJ_ACTUATOR, name)
            for name in (*ARM_JOINTS, GRIPPER_JOINT)
        }
        self.reset(seed)

    def _name_id(self, kind, name):
        value = self.mujoco.mj_name2id(self.model, kind, name)
        if value < 0:
            raise RuntimeError(f"SO-101 model is missing {name!r}")
        return value

    def _randomized_cube_position(self, seed):
        rng = np.random.default_rng(seed)
        return np.array(
            [0.20 + rng.uniform(-0.035, 0.035), -0.10 + rng.uniform(-0.035, 0.035), 0.022],
            dtype=np.float64,
        )

    def reset(self, seed: int) -> Observation:
        if seed != self.seed:
            raise ValueError("each session owns one fixed seeded scene; create a new session")
        self.mujoco.mj_resetData(self.model, self.data)
        self.data.qpos[self._joint_qpos] = HOME
        self.data.qpos[self._gripper_qpos] = GRIPPER_OPEN
        self.data.qpos[self._cube_qpos : self._cube_qpos + 3] = self._randomized_cube_position(
            seed
        )
        self.data.qpos[self._cube_qpos + 3 : self._cube_qpos + 7] = (1.0, 0.0, 0.0, 0.0)
        for index, name in enumerate(ARM_JOINTS):
            self.data.ctrl[self._actuators[name]] = HOME[index]
        self.data.ctrl[self._actuators[GRIPPER_JOINT]] = GRIPPER_OPEN
        self.mujoco.mj_forward(self.model, self.data)
        for _ in range(40):
            self.mujoco.mj_step(self.model, self.data)
        self._success_frames = 0
        self.success = False
        return self._native_observation()

    @staticmethod
    def _normalize_gripper(radians):
        return float(
            np.clip((radians - GRIPPER_CLOSED) / (GRIPPER_OPEN - GRIPPER_CLOSED), 0, 1)
        )

    @staticmethod
    def _gripper_radians(normalized):
        return GRIPPER_CLOSED + float(normalized) * (GRIPPER_OPEN - GRIPPER_CLOSED)

    def _render(self, camera):
        renderer = self._renderers.get(camera)
        if renderer is None:
            renderer = self.mujoco.Renderer(
                self.model, height=IMAGE_SIZE[0], width=IMAGE_SIZE[1]
            )
            self._renderers[camera] = renderer
        renderer.update_scene(self.data, camera=camera)
        return np.asarray(renderer.render(), dtype=np.uint8).copy()

    def _native_observation(self) -> Observation:
        position = self.data.site_xpos[self._ee_site].copy()
        rotation = self.data.site_xmat[self._ee_site].reshape(3, 3)
        quat = np.empty(4, dtype=np.float64)
        self.mujoco.mju_mat2Quat(quat, rotation.ravel())
        arm = self.data.qpos[self._joint_qpos].astype(np.float64, copy=True)
        gripper = np.array(
            [self._normalize_gripper(self.data.qpos[self._gripper_qpos])], dtype=np.float64
        )
        command_arm = np.array(
            [self.data.ctrl[self._actuators[name]] for name in ARM_JOINTS], dtype=np.float64
        )
        command_gripper = np.array(
            [self._normalize_gripper(self.data.ctrl[self._actuators[GRIPPER_JOINT]])],
            dtype=np.float64,
        )
        return Observation(
            instruction=self._task_prompt,
            vision={
                "top_cam": self._render("top_cam"),
                "wrist_cam": self._render("wrist_cam"),
            },
            state={
                "arm_joint_state": arm,
                "ee_joint_state": gripper,
                "ee_pose": np.concatenate((position, quat)),
            },
            action={
                "arm_joint_state": command_arm,
                "ee_joint_state": command_gripper,
            },
            frequency=CONTROL_HZ,
        )

    def _joint_command(self, action):
        arm = np.asarray(action["arm_joint_state"], dtype=np.float64)
        gripper = np.asarray(action["ee_joint_state"], dtype=np.float64)
        for value, name in zip(arm, ARM_JOINTS, strict=True):
            actuator = self._actuators[name]
            low, high = self.model.actuator_ctrlrange[actuator]
            self.data.ctrl[actuator] = np.clip(value, low, high)
        self.data.ctrl[self._actuators[GRIPPER_JOINT]] = self._gripper_radians(gripper[0])

    def _ik_command(self, action):
        target = np.asarray(action["ee_position"], dtype=np.float64)
        gripper = np.asarray(action["ee_joint_state"], dtype=np.float64)
        desired_axis = np.array([0.0, 0.0, -1.0])
        q = self.data.qpos[self._joint_qpos].copy()
        trial = self.mujoco.MjData(self.model)
        trial.qpos[:] = self.data.qpos
        trial.qvel[:] = self.data.qvel
        trial.ctrl[:] = self.data.ctrl
        jac_pos = np.zeros((3, self.model.nv), dtype=np.float64)
        jac_rot = np.zeros((3, self.model.nv), dtype=np.float64)
        for _ in range(50):
            trial.qpos[self._joint_qpos] = q
            self.mujoco.mj_forward(self.model, trial)
            position_error = target - trial.site_xpos[self._ee_site]
            rotation = trial.site_xmat[self._ee_site].reshape(3, 3)
            tool_axis = rotation[:, 0]
            axis_error = desired_axis - tool_axis
            if np.linalg.norm(position_error) < 1e-3 and np.linalg.norm(axis_error) < 1e-2:
                break
            self.mujoco.mj_jacSite(self.model, trial, jac_pos, jac_rot, self._ee_site)
            position_jacobian = jac_pos[:, self._joint_dofs]
            skew_axis = np.array(
                [
                    [0.0, -tool_axis[2], tool_axis[1]],
                    [tool_axis[2], 0.0, -tool_axis[0]],
                    [-tool_axis[1], tool_axis[0], 0.0],
                ]
            )
            axis_jacobian = -skew_axis @ jac_rot[:, self._joint_dofs]
            jacobian = np.vstack((position_jacobian, 0.2 * axis_jacobian))
            error = np.concatenate((position_error, 0.2 * axis_error))
            normal = jacobian.T @ jacobian + 1e-4 * np.eye(len(ARM_JOINTS))
            dq = np.linalg.solve(normal, jacobian.T @ error)
            q += np.clip(dq, -0.08, 0.08)
            for index, joint_id in enumerate(self._joint_ids):
                low, high = self.model.jnt_range[joint_id]
                q[index] = np.clip(q[index], low, high)
        self._joint_command({"arm_joint_state": q, "ee_joint_state": gripper})

    def _apply_action(self, action):
        keys = set(action)
        if keys == {"arm_joint_state", "ee_joint_state"}:
            self._joint_command(action)
        elif keys == {"ee_position", "ee_joint_state"}:
            self._ik_command(action)
        else:
            raise InvalidAction("unsupported SO-101 action")

    def _update_success(self):
        cube = self.data.xpos[self._cube_id]
        target = self.data.xpos[self._target_id]
        speed = np.linalg.norm(self.data.qvel[self._cube_dof : self._cube_dof + 3])
        gripper = self.data.ctrl[self._actuators[GRIPPER_JOINT]]
        released = self._normalize_gripper(gripper) > 0.8
        inside = np.all(np.abs(cube[:2] - target[:2]) <= 0.045) and cube[2] < 0.05
        stable = bool(inside and released and speed < 0.08)
        self._success_frames = self._success_frames + 1 if stable else 0
        self.success = self._success_frames >= 5

    def step(self, action) -> BackendTransition:
        self._apply_action(action)
        for _ in range(PHYSICS_STEPS):
            self.mujoco.mj_step(self.model, self.data)
        self._update_success()
        return BackendTransition(observation=self._native_observation(), success=self.success)

    def close(self):
        for renderer in self._renderers.values():
            renderer.close()
        self._renderers.clear()
