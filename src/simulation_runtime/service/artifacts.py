"""Artifact persistence with server-generated identifiers and stdlib PNG output."""

from __future__ import annotations

import struct
import uuid
import zlib
from pathlib import Path
import re

import numpy as np

from ..core.errors import NotFound
from ..core.session import jsonable
from .store import Store

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _png(array: np.ndarray) -> bytes:
    if array.dtype != np.uint8 or array.ndim != 3 or array.shape[2] != 3:
        raise ValueError("camera frames must be HxWx3 uint8 RGB arrays")
    height, width, _ = array.shape
    if not (1 <= height <= 4096 and 1 <= width <= 4096):
        raise ValueError("camera frame dimensions are outside the supported range")
    raw = b"".join(b"\x00" + array[row].tobytes() for row in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


class ArtifactStore:
    def __init__(self, data_dir: Path, store: Store):
        self.root = Path(data_dir) / "artifacts"
        self.store = store

    def _write(self, project_id: str, session_id: str, payload: bytes, suffix: str) -> tuple[str, str]:
        for value in (project_id, session_id):
            if not SAFE_ID.match(value):
                raise ValueError("unsafe artifact owner identifier")
        artifact_id = new_id("art")
        folder = self.root / project_id / session_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{artifact_id}{suffix}"
        path.write_bytes(payload)
        return artifact_id, path.relative_to(self.root).as_posix()

    def save_observation(
        self,
        *,
        project_id: str,
        session_id: str,
        sequence: int,
        observation: dict,
    ) -> dict:
        obs_id = new_id("obs")
        cameras: dict[str, dict] = {}
        for name, frame in observation.get("vision", {}).items():
            if not SAFE_ID.match(str(name)):
                raise ValueError("unsafe camera name")
            array = np.asarray(frame)
            payload = _png(array)
            artifact_id, relative = self._write(project_id, session_id, payload, ".png")
            self.store.insert_artifact(
                artifact_id=artifact_id,
                session_id=session_id,
                project_id=project_id,
                media_type="image/png",
                relative_path=relative,
                byte_size=len(payload),
            )
            cameras[str(name)] = {
                "artifact_id": artifact_id,
                "href": f"/v1/artifacts/{artifact_id}",
                "media_type": "image/png",
                "width": int(array.shape[1]),
                "height": int(array.shape[0]),
            }
        data = {
            "observation_id": obs_id,
            "sequence": sequence,
            "instruction": str(observation["instruction"]),
            "state": jsonable(observation["state"]),
            "action": jsonable(observation["action"]),
            "control_hz": float(observation.get("frequency", 20.0)),
            "cameras": cameras,
        }
        self.store.insert_observation(
            observation_id=obs_id,
            session_id=session_id,
            project_id=project_id,
            sequence=sequence,
            data=data,
        )
        return data

    def read(self, project_id: str, artifact_id: str) -> tuple[bytes, str]:
        record = self.store.get_artifact(project_id, artifact_id)
        if record is None:
            raise NotFound("artifact not found")
        path = self.root / record["relative_path"]
        try:
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise NotFound("artifact content is no longer available") from exc
        return payload, record["media_type"]
