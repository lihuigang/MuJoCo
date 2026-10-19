"""Backend contract implemented by simulator adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from .types import Observation


@dataclass
class BackendTransition:
    observation: Observation
    success: bool


class BackendSession(Protocol):
    task: str
    native_action_limit: int
    control_hz: float

    def reset(self, seed: int) -> Observation: ...

    def step(self, action: dict[str, np.ndarray]) -> BackendTransition: ...

    def close(self) -> None: ...


class SimulationBackend(Protocol):
    name: str

    def capabilities(self) -> dict[str, Any]: ...

    def create_session(self, task: str, *, seed: int) -> BackendSession: ...
