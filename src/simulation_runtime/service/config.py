"""Service settings and project-token policy loading."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SCOPES = frozenset({"playground", "test", "typed", "exec"})
DEFAULT_SCOPES = frozenset({"playground", "typed"})
MIN_TOKEN_LENGTH = 16


@dataclass(frozen=True)
class TokenPolicy:
    project_id: str
    token: str = field(repr=False)
    scopes: frozenset[str] = DEFAULT_SCOPES
    max_sessions: int | None = None

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    tokens: tuple[TokenPolicy, ...]
    backends: tuple[str, ...] = ("mujoco_so101",)
    max_sessions: int = 4
    max_sessions_per_project: int = 2
    default_exec_budget: int = 100
    max_exec_budget: int = 1000
    execution_timeout_sec: float = 60.0
    operation_timeout_sec: float = 120.0
    request_wait_sec: float = 10.0
    worker_start_timeout_sec: float = 60.0
    session_idle_ttl_sec: float = 900.0
    session_max_lifetime_sec: float = 3600.0
    reap_interval_sec: float = 30.0

    def __post_init__(self):
        if not self.tokens:
            raise ValueError("at least one project token is required")
        if self.max_sessions <= 0 or self.max_sessions_per_project <= 0:
            raise ValueError("session capacity must be positive")
        if self.default_exec_budget < 0 or self.max_exec_budget < self.default_exec_budget:
            raise ValueError("invalid execution budget limits")
        if self.request_wait_sec < 0 or self.operation_timeout_sec <= 0:
            raise ValueError("invalid operation timeouts")

    def policies_for(self, scope: str | None = None) -> tuple[TokenPolicy, ...]:
        if scope is None:
            return self.tokens
        return tuple(policy for policy in self.tokens if policy.allows(scope))


def _policy_from_dict(payload: dict, *, require_strong: bool) -> TokenPolicy:
    if not isinstance(payload, dict):
        raise ValueError("each token entry must be an object")
    token = payload.get("token")
    project_id = payload.get("project_id")
    if not isinstance(token, str) or not token:
        raise ValueError("token entry requires a non-empty 'token'")
    if require_strong and len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"tokens must be at least {MIN_TOKEN_LENGTH} characters")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("token entry requires a non-empty 'project_id'")
    scopes = payload.get("scopes", sorted(DEFAULT_SCOPES))
    if not isinstance(scopes, list) or not scopes or not all(isinstance(s, str) for s in scopes):
        raise ValueError("'scopes' must be a non-empty list of strings")
    unknown = set(scopes) - SCOPES
    if unknown:
        raise ValueError(f"unknown scopes: {sorted(unknown)}")
    max_sessions = payload.get("max_sessions")
    if max_sessions is not None and (type(max_sessions) is not int or max_sessions <= 0):
        raise ValueError("'max_sessions' must be a positive integer when present")
    return TokenPolicy(
        project_id=project_id.strip(),
        token=token,
        scopes=frozenset(scopes),
        max_sessions=max_sessions,
    )


def parse_tokens(payload, *, require_strong: bool = True) -> tuple[TokenPolicy, ...]:
    if isinstance(payload, dict):
        payload = payload.get("tokens")
    if not isinstance(payload, list):
        raise ValueError("token configuration must contain a 'tokens' list")
    policies = tuple(_policy_from_dict(item, require_strong=require_strong) for item in payload)
    seen_tokens, seen_projects = set(), set()
    for policy in policies:
        if policy.token in seen_tokens:
            raise ValueError("duplicate token value in configuration")
        if policy.project_id in seen_projects:
            raise ValueError(f"duplicate project_id {policy.project_id!r}")
        seen_tokens.add(policy.token)
        seen_projects.add(policy.project_id)
    return policies


def _load_token_payload(env: dict[str, str]):
    inline = env.get("SIM_RUNTIME_TOKENS_JSON")
    if inline:
        return json.loads(inline)
    path = env.get("SIM_RUNTIME_TOKENS_FILE")
    if path:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    raise ValueError(
        "Set SIM_RUNTIME_TOKENS_FILE or SIM_RUNTIME_TOKENS_JSON to a project token list"
    )


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    data_dir = Path(
        env.get("SIM_RUNTIME_DATA_DIR", "./data")
    ).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    backends = tuple(
        name.strip()
        for name in env.get("SIM_RUNTIME_BACKENDS", "mujoco_so101").split(",")
        if name.strip()
    )
    if not backends:
        raise ValueError("SIM_RUNTIME_BACKENDS must list at least one backend")

    def number(name: str, default, cast=float):
        raw = env.get(name)
        return default if raw is None or raw == "" else cast(raw)

    return Settings(
        data_dir=data_dir,
        tokens=parse_tokens(_load_token_payload(env)),
        backends=backends,
        max_sessions=number("SIM_RUNTIME_MAX_SESSIONS", 4, int),
        max_sessions_per_project=number("SIM_RUNTIME_MAX_SESSIONS_PER_PROJECT", 2, int),
        default_exec_budget=number("SIM_RUNTIME_DEFAULT_EXEC_BUDGET", 100, int),
        max_exec_budget=number("SIM_RUNTIME_MAX_EXEC_BUDGET", 1000, int),
        execution_timeout_sec=number("SIM_RUNTIME_EXEC_TIMEOUT_SEC", 60.0),
        operation_timeout_sec=number("SIM_RUNTIME_OPERATION_TIMEOUT_SEC", 120.0),
        request_wait_sec=number("SIM_RUNTIME_REQUEST_WAIT_SEC", 10.0),
        worker_start_timeout_sec=number("SIM_RUNTIME_WORKER_START_TIMEOUT_SEC", 60.0),
        session_idle_ttl_sec=number("SIM_RUNTIME_SESSION_IDLE_TTL_SEC", 900.0),
        session_max_lifetime_sec=number("SIM_RUNTIME_SESSION_MAX_LIFETIME_SEC", 3600.0),
        reap_interval_sec=number("SIM_RUNTIME_REAP_INTERVAL_SEC", 30.0),
    )
