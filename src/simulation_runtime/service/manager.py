"""Session lifecycle, worker processes, serialized operation queue and reaping."""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import queue
import threading
import time
from typing import Any

from .. import PROTOCOL, __version__
from ..backends import create_backend
from ..core.errors import (
    ActionModeMismatch,
    CapacityExhausted,
    IdempotencyConflict,
    InvalidRequest,
    NotFound,
    SessionBusy,
    SessionClosed,
    SessionInterrupted,
    SimulatorFailure,
)
from .artifacts import ArtifactStore, new_id
from .config import Settings, TokenPolicy
from .store import Store
from .worker import worker_main

LOGGER = logging.getLogger("simulation_runtime.service")

WRITE_KINDS = frozenset({"step", "reset", "execute", "close"})
UNCERTAIN_CODES = frozenset(
    {"execution_timeout", "simulator_failure", "internal_error", "worker_crashed", "operation_timeout"}
)


def request_hash(kind: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"kind": kind, "payload": payload}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkerTimeout(Exception):
    pass


class WorkerDied(Exception):
    pass


class WorkerHandle:
    def __init__(self, spec: dict[str, Any], *, start_timeout: float):
        context = multiprocessing.get_context("spawn")
        self._conn, child_conn = context.Pipe(duplex=True)
        self._process = context.Process(
            target=worker_main, args=(child_conn, spec), daemon=True,
            name=f"sim-{spec['task']}-{spec['seed']}",
        )
        self._process.start()
        child_conn.close()
        try:
            ready = self._receive(start_timeout)
        except WorkerTimeout as exc:
            self.terminate()
            raise SimulatorFailure("worker did not initialize before the start timeout") from exc
        except WorkerDied as exc:
            self.terminate()
            raise SimulatorFailure(
                f"worker exited during initialization (exit code {self._process.exitcode})"
            ) from exc
        if not ready.get("ready"):
            error = ready.get("error", {})
            self.terminate()
            raise SimulatorFailure(
                f"worker initialization failed: {error.get('message', 'unknown error')}"
            )
        self.ready = ready

    def _receive(self, timeout: float) -> dict[str, Any]:
        if not self._conn.poll(timeout):
            if not self._process.is_alive():
                raise WorkerDied("worker process is not alive")
            raise WorkerTimeout("worker did not answer in time")
        try:
            return self._conn.recv()
        except (EOFError, OSError) as exc:
            raise WorkerDied("worker connection closed") from exc

    def call(self, request: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        if not self._process.is_alive():
            raise WorkerDied("worker process is not alive")
        try:
            self._conn.send(request)
        except (BrokenPipeError, OSError) as exc:
            raise WorkerDied("worker connection closed") from exc
        return self._receive(timeout)

    def terminate(self) -> None:
        try:
            self._conn.close()
        except OSError:
            pass
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=5)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=2)


class SessionSlot:
    """Single background driver per session; the queue is the write serializer."""

    def __init__(self, manager: "SessionManager", session_id: str, worker: WorkerHandle):
        self.manager = manager
        self.session_id = session_id
        self.worker = worker
        self.write_lock = threading.Lock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._events: dict[str, threading.Event] = {}
        self._events_lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = threading.Thread(
            target=self._loop, daemon=True, name=f"driver-{session_id}"
        )
        self.thread.start()

    def enqueue(self, operation_id: str) -> threading.Event:
        event = self.event_for(operation_id)
        self._queue.put(operation_id)
        return event

    def event_for(self, operation_id: str) -> threading.Event:
        with self._events_lock:
            return self._events.setdefault(operation_id, threading.Event())

    def stop(self) -> None:
        self.stopped.set()
        self._queue.put(None)

    def _loop(self) -> None:
        while not self.stopped.is_set():
            operation_id = self._queue.get()
            if operation_id is None:
                break
            try:
                self._run_operation(operation_id)
            except BaseException:
                LOGGER.exception("session driver failed while handling %s", operation_id)
                self.manager.operation_driver_failed(self.session_id, operation_id)
            finally:
                with self._events_lock:
                    event = self._events.pop(operation_id, None)
                if event is not None:
                    event.set()

    def _run_operation(self, operation_id: str) -> None:
        store = self.manager.store
        operation = store.get_operation_any(operation_id)
        if operation is None or operation["status"] != "pending":
            return
        store.mark_operation_running(operation_id)
        try:
            payload = json.loads(operation["request_json"])
        except ValueError:
            store.finish_operation(
                operation_id,
                status="failed",
                error={"code": "invalid_request", "message": "stored request is not valid JSON"},
            )
            return
        request = {"op": operation["kind"]}
        if operation["kind"] == "step":
            request["action"] = payload.get("action")
        elif operation["kind"] == "execute":
            request["code"] = payload.get("code")
        elif operation["kind"] == "close":
            request["reason"] = payload.get("reason", "closed_by_client")
        timeout = (
            self.manager.settings.execution_timeout_sec + 30.0
            if operation["kind"] == "execute"
            else self.manager.settings.operation_timeout_sec
        )
        try:
            response = self.worker.call(request, timeout=timeout)
        except WorkerTimeout:
            self.manager.interrupt_session(
                self.session_id, "operation_timeout", operation_id=operation_id
            )
            return
        except WorkerDied:
            self.manager.interrupt_session(
                self.session_id, "worker_crashed", operation_id=operation_id
            )
            return
        self.manager.complete_operation(operation, response)


class SessionManager:
    def __init__(self, settings: Settings, store: Store, artifacts: ArtifactStore):
        self.settings = settings
        self.store = store
        self.artifacts = artifacts
        self._slots: dict[str, SessionSlot] = {}
        self._lock = threading.RLock()
        self._create_lock = threading.Lock()
        self._closed = threading.Event()
        clean_sessions = store.mark_stale_interrupted()
        store.reset_pending_operations()
        if clean_sessions:
            LOGGER.warning("marked %d session(s) interrupted after service restart", clean_sessions)
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True, name="reaper")
        self._reaper.start()

    # -- capabilities and reads -----------------------------------------

    def capabilities(self, policy: TokenPolicy) -> dict[str, Any]:
        backends = []
        for name in self.settings.backends:
            try:
                backends.append(create_backend(name).capabilities())
            except Exception as exc:
                LOGGER.error("backend %s unavailable for capabilities: %s", name, exc)
                backends.append({"name": name, "unavailable": True, "error": str(exc)})
        return {
            "protocol": PROTOCOL,
            "service_version": __version__,
            "backends": backends,
            "session_modes": ["typed", "exec"],
            "features": {
                "exec": policy.allows("exec"),
                "artifact_download": True,
                "async_operations": True,
                "live_viewer": False,
            },
            "limits": {
                "max_sessions_per_project": policy.max_sessions
                or self.settings.max_sessions_per_project,
                "execution_timeout_sec": self.settings.execution_timeout_sec,
                "request_wait_sec": self.settings.request_wait_sec,
            },
        }

    def get_session(self, policy: TokenPolicy, session_id: str) -> dict[str, Any]:
        row = self.store.get_session(policy.project_id, session_id)
        if row is None:
            raise NotFound("session not found")
        return row

    def list_sessions(self, policy: TokenPolicy, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list_sessions(policy.project_id, limit=limit)

    def latest_observation(self, policy: TokenPolicy, session_id: str) -> dict[str, Any]:
        self.get_session(policy, session_id)
        data = self.store.latest_observation(policy.project_id, session_id)
        if data is None:
            raise NotFound("session has no observation")
        return data

    def get_operation(
        self, policy: TokenPolicy, session_id: str, operation_id: str
    ) -> dict[str, Any]:
        self.get_session(policy, session_id)
        operation = self.store.get_operation(policy.project_id, session_id, operation_id)
        if operation is None:
            raise NotFound("operation not found")
        return operation

    def list_operations(
        self, policy: TokenPolicy, session_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.get_session(policy, session_id)
        return self.store.list_operations(session_id, limit=limit)

    # -- session creation -----------------------------------------------

    def create_session(
        self,
        policy: TokenPolicy,
        *,
        backend_name: str,
        task: str,
        phase: str,
        mode: str,
        seed: int,
        exec_budget: int | None,
    ) -> dict[str, Any]:
        if backend_name not in self.settings.backends:
            raise InvalidRequest(
                f"backend {backend_name!r} is not enabled; enabled: {list(self.settings.backends)}"
            )
        if phase not in {"playground", "test"}:
            raise InvalidRequest("phase must be 'playground' or 'test'")
        if mode not in {"typed", "exec"}:
            raise InvalidRequest("mode must be 'typed' or 'exec'")
        if type(seed) is not int or seed < 0:
            raise InvalidRequest("seed must be a nonnegative integer")
        if not policy.allows(phase):
            raise InvalidRequest(f"project is not allowed to create {phase} sessions")
        if not policy.allows(mode):
            raise InvalidRequest(f"project is not allowed to use {mode} sessions")
        try:
            backend = create_backend(backend_name)
            capabilities = backend.capabilities()
        except Exception as exc:
            raise SimulatorFailure(
                f"backend {backend_name!r} failed to load: {type(exc).__name__}: {exc}"
            ) from exc
        tasks = {item["name"]: item for item in capabilities.get("tasks", [])}
        if task not in tasks:
            raise InvalidRequest(
                f"unknown task {task!r}; available: {sorted(tasks)}"
            )
        project_limit = policy.max_sessions or self.settings.max_sessions_per_project
        if mode == "exec":
            budget = self.settings.default_exec_budget if exec_budget is None else exec_budget
            if type(budget) is not int or budget <= 0:
                raise InvalidRequest("exec_budget must be a positive integer")
            if budget > self.settings.max_exec_budget:
                raise InvalidRequest(
                    f"exec_budget exceeds the service maximum of {self.settings.max_exec_budget}"
                )
        else:
            budget = 0
        native_limit = int(tasks[task]["native_action_limit"])
        session_id = new_id("ses")
        now = time.time()
        # Capacity check and row insert must be atomic, otherwise concurrent
        # requests can both observe a free slot.
        with self._create_lock:
            if self.store.count_sessions(policy.project_id) >= project_limit:
                raise CapacityExhausted(
                    f"project {policy.project_id!r} already has {project_limit} active session(s)"
                )
            if self.store.count_sessions() >= self.settings.max_sessions:
                raise CapacityExhausted(
                    f"service already has {self.settings.max_sessions} active session(s)"
                )
            self.store.create_session(
                {
                    "id": session_id,
                    "project_id": policy.project_id,
                    "task": task,
                    "phase": phase,
                    "mode": mode,
                    "seed": seed,
                    "state": "starting",
                    "reason": "starting",
                    "native_action_limit": native_limit,
                    "native_steps_remaining": native_limit,
                    "execution_budget": budget,
                    "executions_remaining": budget,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        spec = {
            "backend": backend_name,
            "task": task,
            "phase": phase,
            "mode": mode,
            "seed": seed,
            "exec_budget": budget,
            "execution_timeout_sec": self.settings.execution_timeout_sec,
        }
        try:
            worker = WorkerHandle(spec, start_timeout=self.settings.worker_start_timeout_sec)
        except SimulatorFailure as exc:
            self.store.update_session(
                session_id, state="failed", reason="initialization_failed"
            )
            raise SimulatorFailure(str(exc)) from exc
        except Exception as exc:
            self.store.update_session(
                session_id, state="failed", reason="worker_spawn_failed"
            )
            raise SimulatorFailure(
                f"failed to start simulator worker: {type(exc).__name__}: {exc}"
            ) from exc
        ready = worker.ready
        try:
            observation = self.artifacts.save_observation(
                project_id=policy.project_id,
                session_id=session_id,
                sequence=int(ready["status"]["observation_sequence"]),
                observation=ready["observation"],
            )
            self._sync_status(
                session_id, ready["status"], latest_observation_id=observation["observation_id"]
            )
            self.store.update_session(session_id, state="active", reason="active")
        except Exception as exc:
            worker.terminate()
            self.store.update_session(
                session_id, state="failed", reason="observation_capture_failed"
            )
            raise SimulatorFailure(f"failed to capture initial observation: {exc}") from exc
        with self._lock:
            self._slots[session_id] = SessionSlot(self, session_id, worker)
        return self.store.get_session_any(session_id)

    # -- operations -----------------------------------------------------

    def submit(
        self,
        policy: TokenPolicy,
        session_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        if kind not in WRITE_KINDS:
            raise InvalidRequest(f"unknown operation kind {kind!r}")
        row = self.get_session(policy, session_id)
        if row["state"] == "closed":
            raise SessionClosed("session is closed")
        if row["state"] == "interrupted":
            raise SessionInterrupted(f"session is interrupted ({row['reason']})")
        if row["state"] != "active":
            raise SessionInterrupted(f"session is not active ({row['state']})")
        if kind == "step" and row["mode"] != "typed":
            raise ActionModeMismatch("exec sessions do not accept typed steps")
        if kind == "execute" and row["mode"] != "exec":
            raise ActionModeMismatch("typed sessions do not accept code executions")
        slot = self._slot(session_id)
        digest = request_hash(kind, payload)
        with slot.write_lock:
            existing = self.store.find_operation_by_key(session_id, idempotency_key)
            if existing is not None:
                if existing["request_hash"] != digest:
                    raise IdempotencyConflict(
                        "idempotency key was already used with a different request"
                    )
                operation = existing
            else:
                if self.store.get_active_operation(session_id) is not None:
                    raise SessionBusy(
                        "another operation is in flight; wait for it before writing again"
                    )
                operation, _ = self.store.create_operation(
                    {
                        "id": new_id("op"),
                        "session_id": session_id,
                        "project_id": policy.project_id,
                        "kind": kind,
                        "idempotency_key": idempotency_key,
                        "request_hash": digest,
                        "request_json": json.dumps(payload, separators=(",", ":")),
                        "status": "pending",
                        "created_at": time.time(),
                    }
                )
                slot.enqueue(operation["id"])
        if wait and operation["status"] in {"pending", "running"}:
            deadline = time.monotonic() + self.settings.request_wait_sec
            while operation["status"] in {"pending", "running"}:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                slot.event_for(operation["id"]).wait(min(remaining, 0.25))
                refreshed = self.store.get_operation_any(operation["id"])
                if refreshed is not None:
                    operation = refreshed
        return operation

    def _slot(self, session_id: str) -> SessionSlot:
        with self._lock:
            slot = self._slots.get(session_id)
        if slot is None or slot.stopped.is_set():
            raise SessionInterrupted("session worker is no longer running")
        return slot

    def complete_operation(self, operation: dict[str, Any], response: dict[str, Any]) -> None:
        operation_id = operation["id"]
        session_id = operation["session_id"]
        if not response.get("ok"):
            error = response.get("error") or {
                "code": "simulator_failure",
                "message": "worker returned no error detail",
            }
            status = response.get("status")
            if status:
                self._sync_status(session_id, status)
            uncertain = error.get("code") in UNCERTAIN_CODES
            if uncertain:
                self.interrupt_session(
                    session_id, str(error.get("code")), operation_id=operation_id
                )
                return
            self.store.finish_operation(operation_id, status="failed", error=error)
            return
        status = response.get("status") or {}
        if operation["kind"] == "close":
            self._sync_status(session_id, status)
            self.store.update_session(
                session_id,
                state=status.get("state", "closed"),
                reason=status.get("reason", "closed_by_client"),
                closed_at=time.time(),
            )
            self.store.finish_operation(
                operation_id, status="succeeded", result={"status": status}
            )
            self._stop_slot(session_id)
            return
        result = response.get("result") or {}
        observation = result.pop("observation", None)
        observation_data = None
        if observation is not None:
            observation_data = self.artifacts.save_observation(
                project_id=operation["project_id"],
                session_id=session_id,
                sequence=int(status.get("observation_sequence", 0)),
                observation=observation,
            )
        self._sync_status(
            session_id,
            status,
            latest_observation_id=(
                None if observation_data is None else observation_data["observation_id"]
            ),
        )
        if observation_data is not None:
            result["observation"] = observation_data
        self.store.finish_operation(operation_id, status="succeeded", result=result)

    def _sync_status(
        self, session_id: str, status: dict[str, Any], *, latest_observation_id: str | None = None
    ) -> None:
        fields = {
            key: status[key]
            for key in (
                "state", "reason", "success", "terminated", "truncated",
                "native_action_limit", "native_steps_used", "native_steps_remaining",
                "execution_budget", "executions_used", "executions_remaining",
                "observation_sequence",
            )
            if key in status
        }
        if latest_observation_id is not None:
            fields["latest_observation_id"] = latest_observation_id
        if fields:
            self.store.update_session(session_id, **fields)

    def interrupt_session(
        self, session_id: str, reason: str, *, operation_id: str | None = None
    ) -> None:
        with self._lock:
            slot = self._slots.pop(session_id, None)
        if slot is not None:
            slot.stop()
            slot.worker.terminate()
        row = self.store.get_session_any(session_id)
        if row is not None and row["state"] in {"starting", "active"}:
            self.store.update_session(session_id, state="interrupted", reason=reason)
        if operation_id is not None:
            self.store.finish_operation(
                operation_id,
                status="failed",
                error={
                    "code": reason,
                    "message": "the simulator worker stopped before confirming the result; "
                    "the outcome is uncertain and the action was not replayed",
                    "outcome_uncertain": True,
                },
                outcome_uncertain=True,
            )

    def operation_driver_failed(self, session_id: str, operation_id: str) -> None:
        self.interrupt_session(session_id, "worker_crashed", operation_id=operation_id)

    def _stop_slot(self, session_id: str) -> None:
        with self._lock:
            slot = self._slots.pop(session_id, None)
        if slot is not None:
            slot.stop()
            slot.worker.terminate()

    # -- reaping and shutdown -------------------------------------------

    def _reap_loop(self) -> None:
        interval = max(1.0, self.settings.reap_interval_sec)
        while not self._closed.wait(interval):
            now = time.time()
            try:
                stale = self.store.list_stale_sessions(
                    now=now,
                    idle_ttl=self.settings.session_idle_ttl_sec,
                    max_lifetime=self.settings.session_max_lifetime_sec,
                )
            except Exception:
                LOGGER.exception("session reaper query failed")
                continue
            for row in stale:
                with self._lock:
                    slot = self._slots.get(row["id"])
                if slot is None:
                    continue
                with slot.write_lock:
                    if self.store.get_active_operation(row["id"]) is not None:
                        continue
                    LOGGER.info("reaping session %s after TTL", row["id"])
                    with self._lock:
                        self._slots.pop(row["id"], None)
                    slot.stop()
                    slot.worker.terminate()
                    self.store.update_session(
                        row["id"], state="closed", reason="session_ttl", closed_at=now
                    )

    def shutdown(self) -> None:
        self._closed.set()
        with self._lock:
            slots = list(self._slots.values())
            self._slots.clear()
        for slot in slots:
            slot.stop()
            slot.worker.terminate()
        for row in self.store.list_sessions_by_state(("starting", "active")):
            self.store.update_session(
                row["id"], state="interrupted", reason="service_shutdown"
            )
        self.store.reset_pending_operations()
