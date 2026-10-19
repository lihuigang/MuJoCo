"""SQLite metadata, operation log, observations and artifact index."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SESSION_FIELDS = frozenset(
    {
        "state", "reason", "success", "terminated", "truncated",
        "native_action_limit", "native_steps_used", "native_steps_remaining",
        "execution_budget", "executions_used", "executions_remaining",
        "observation_sequence", "latest_observation_id", "updated_at", "closed_at",
    }
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task TEXT NOT NULL,
    phase TEXT NOT NULL,
    mode TEXT NOT NULL,
    seed INTEGER NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 0,
    terminated INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    native_action_limit INTEGER NOT NULL,
    native_steps_used INTEGER NOT NULL DEFAULT 0,
    native_steps_remaining INTEGER NOT NULL,
    execution_budget INTEGER NOT NULL DEFAULT 0,
    executions_used INTEGER NOT NULL DEFAULT 0,
    executions_remaining INTEGER NOT NULL DEFAULT 0,
    observation_sequence INTEGER NOT NULL DEFAULT 0,
    latest_observation_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    closed_at REAL
);
CREATE INDEX IF NOT EXISTS ix_sessions_project ON sessions(project_id, state);

CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    idempotency_key TEXT,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    result_json TEXT,
    error_json TEXT,
    outcome_uncertain INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_operations_session ON operations(session_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_operations_idempotency
    ON operations(session_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    created_at REAL NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_observations_session ON observations(session_id, sequence);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_artifacts_project ON artifacts(project_id, session_id);
"""


def _decode(value: str | None) -> Any:
    return None if value is None else json.loads(value)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)

    # -- sessions -------------------------------------------------------

    def create_session(self, record: dict[str, Any]) -> dict[str, Any]:
        columns = ", ".join(record)
        placeholders = ", ".join("?" for _ in record)
        with self._lock, self._db:
            self._db.execute(
                f"INSERT INTO sessions ({columns}) VALUES ({placeholders})",
                tuple(record.values()),
            )
        return self.get_session_any(record["id"])

    def get_session(self, project_id: str, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE id = ? AND project_id = ?",
                (session_id, project_id),
            ).fetchone()
        return dict(row) if row else None

    def get_session_any(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sessions WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_sessions_by_state(self, states: tuple[str, ...]) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in states)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM sessions WHERE state IN ({placeholders})", states
            ).fetchall()
        return [dict(row) for row in rows]

    def count_sessions(self, project_id: str | None = None) -> int:
        with self._lock:
            if project_id is None:
                row = self._db.execute(
                    "SELECT COUNT(*) AS n FROM sessions WHERE state IN ('starting','active')"
                ).fetchone()
            else:
                row = self._db.execute(
                    "SELECT COUNT(*) AS n FROM sessions "
                    "WHERE state IN ('starting','active') AND project_id = ?",
                    (project_id,),
                ).fetchone()
        return int(row["n"])

    def update_session(self, session_id: str, **fields: Any) -> dict[str, Any]:
        unknown = set(fields) - SESSION_FIELDS
        if unknown:
            raise ValueError(f"unknown session fields: {sorted(unknown)}")
        if not fields:
            return self.get_session_any(session_id)
        fields.setdefault("updated_at", time.time())
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._lock, self._db:
            self._db.execute(
                f"UPDATE sessions SET {assignments} WHERE id = ?",
                (*fields.values(), session_id),
            )
        return self.get_session_any(session_id)

    def mark_stale_interrupted(self) -> int:
        now = time.time()
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE sessions SET state = 'interrupted', reason = 'service_restart', "
                "updated_at = ? WHERE state IN ('starting','active')",
                (now,),
            )
        return cursor.rowcount

    def list_stale_sessions(
        self, *, now: float, idle_ttl: float, max_lifetime: float
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sessions WHERE state = 'active' AND "
                "(? - updated_at > ? OR ? - created_at > ?)",
                (now, idle_ttl, now, max_lifetime),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- operations -----------------------------------------------------

    def create_operation(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Insert an operation; return (record, created)."""

        columns = ", ".join(record)
        placeholders = ", ".join("?" for _ in record)
        with self._lock:
            try:
                with self._db:
                    self._db.execute(
                        f"INSERT INTO operations ({columns}) VALUES ({placeholders})",
                        tuple(record.values()),
                    )
                return self.get_operation_any(record["id"]), True
            except sqlite3.IntegrityError:
                existing = self.find_operation_by_key(
                    record["session_id"], record.get("idempotency_key")
                )
                if existing is None:
                    raise
                return existing, False

    def find_operation_by_key(
        self, session_id: str, idempotency_key: str | None
    ) -> dict[str, Any] | None:
        if idempotency_key is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM operations WHERE session_id = ? AND idempotency_key = ?",
                (session_id, idempotency_key),
            ).fetchone()
        return dict(row) if row else None

    def get_active_operation(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM operations WHERE session_id = ? "
                "AND status IN ('pending','running') ORDER BY created_at LIMIT 1",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_operation_any(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_operation(
        self, project_id: str, session_id: str, operation_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM operations WHERE id = ? AND session_id = ? AND project_id = ?",
                (operation_id, session_id, project_id),
            ).fetchone()
        return dict(row) if row else None

    def list_operations(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM operations WHERE session_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_operation_running(self, operation_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE operations SET status = 'running', started_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (time.time(), operation_id),
            )

    def finish_operation(
        self,
        operation_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        outcome_uncertain: bool = False,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE operations SET status = ?, completed_at = ?, result_json = ?, "
                "error_json = ?, outcome_uncertain = ? WHERE id = ?",
                (
                    status,
                    time.time(),
                    None if result is None else json.dumps(result, separators=(",", ":")),
                    None if error is None else json.dumps(error, separators=(",", ":")),
                    int(outcome_uncertain),
                    operation_id,
                ),
            )

    def reset_pending_operations(self) -> None:
        """Server restart makes in-flight outcomes uncertain; never replay them."""

        with self._lock, self._db:
            self._db.execute(
                "UPDATE operations SET status = 'failed', completed_at = ?, "
                "error_json = ?, outcome_uncertain = 1 "
                "WHERE status IN ('pending','running')",
                (
                    time.time(),
                    json.dumps(
                        {
                            "code": "service_restart",
                            "message": "server restarted while the operation was in flight; "
                            "outcome uncertain and not replayed",
                            "outcome_uncertain": True,
                        }
                    ),
                ),
            )

    # -- observations and artifacts -------------------------------------

    def insert_observation(
        self,
        *,
        observation_id: str,
        session_id: str,
        project_id: str,
        sequence: int,
        data: dict[str, Any],
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO observations "
                "(id, session_id, project_id, sequence, created_at, data_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    observation_id,
                    session_id,
                    project_id,
                    sequence,
                    time.time(),
                    json.dumps(data, separators=(",", ":")),
                ),
            )

    def latest_observation(
        self, project_id: str, session_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM observations WHERE project_id = ? AND session_id = ? "
                "ORDER BY sequence DESC, created_at DESC LIMIT 1",
                (project_id, session_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "sequence": row["sequence"],
            "created_at": row["created_at"],
            **_decode(row["data_json"]),
        }

    def insert_artifact(
        self,
        *,
        artifact_id: str,
        session_id: str,
        project_id: str,
        media_type: str,
        relative_path: str,
        byte_size: int,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO artifacts "
                "(id, session_id, project_id, media_type, relative_path, byte_size, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    session_id,
                    project_id,
                    media_type,
                    relative_path,
                    byte_size,
                    time.time(),
                ),
            )

    def get_artifact(self, project_id: str, artifact_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM artifacts WHERE id = ? AND project_id = ?",
                (artifact_id, project_id),
            ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()
