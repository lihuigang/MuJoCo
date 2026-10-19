"""Backend registry with lazy imports so MuJoCo is optional for SDK users."""

from __future__ import annotations

from ..core.backend import SimulationBackend


def create_backend(name: str) -> SimulationBackend:
    if name == "mujoco_so101":
        from .mujoco_so101 import MuJoCoSO101Backend

        return MuJoCoSO101Backend()
    if name == "fake":
        from .fake import FakeBackend

        return FakeBackend()
    raise ValueError(f"unknown simulation backend {name!r}")


def backend_names() -> tuple[str, ...]:
    return ("mujoco_so101", "fake")
