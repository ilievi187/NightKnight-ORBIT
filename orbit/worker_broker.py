"""Loopback ORBIT worker broker.

This is the reference server for orbit.workers.v0.1. It leases tasks and
records results. It never executes task payloads.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .worker_dashboard import render_dashboard
from .worker_protocol import (
    PROTOCOL,
    HeartbeatRequest,
    Manifest,
    PairRequest,
    ProgressRequest,
    ProtocolError,
    ResultRequest,
    RevokeRequest,
    TaskCreate,
    assert_loopback_host,
    bounded_json,
    digest,
    isoformat,
    parse_time,
    scrub,
    utcnow,
    valid_nonce,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    runtime_name TEXT NOT NULL,
    host_id TEXT NOT NULL,
    model_id TEXT,
    model_id_source TEXT,
    capabilities_json TEXT NOT NULL,
    inventory_json TEXT NOT NULL,
    status TEXT NOT NULL,
    version TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    token_expires TEXT NOT NULL,
    last_heartbeat TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS pairing (
    code_hash TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS nonces (
    token_hash TEXT NOT NULL,
    nonce TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (token_hash, nonce)
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    required_json TEXT NOT NULL,
    grant_name TEXT NOT NULL,
    status TEXT NOT NULL,
    worker_id TEXT,
    lease_id TEXT,
    lease_expires TEXT,
    result_json TEXT,
    log_excerpt TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    worker_id TEXT,
    task_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capability_denials (
    task_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    PRIMARY KEY (task_id, worker_id)
);
CREATE TABLE IF NOT EXISTS pair_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL
);
"""

CSS_PATH = Path(__file__).with_name("static") / "worker.css"


def default_database() -> Path:
    return Path.home() / ".local" / "share" / "nightknight-orbit" / "worker-broker.sqlite3"


class WorkerBroker:
    def __init__(
        self,
        database: Path,
        clock: Any | None = None,
        *,
        pairing_ttl: int = 600,
        token_ttl: int = 12 * 3600,
        offline_after: int = 20,
        lease_ttl: int = 60,
        timestamp_skew: int = 60,
        failure_limit: int = 8,
        failure_window: int = 60,
    ) -> None:
        self.database = Path(database)
        self.clock = clock
        self.pairing_ttl = pairing_ttl
        self.token_ttl = token_ttl
        self.offline_after = offline_after
        self.lease_ttl = lease_ttl
        self.timestamp_skew = timestamp_skew
        self.failure_limit = failure_limit
        self.failure_window = failure_window
        self._lock = threading.RLock()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.database, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        try:
            os.chmod(self.database, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            self._conn.close()
            self._conn = None

    def now(self):
        if self.clock is not None:
            return self.clock.now()
        return utcnow()

    def create_pairing(self) -> dict[str, str]:
        import secrets

        with self._lock:
            code = secrets.token_hex(8)
            expires = self.now() + timedelta(seconds=self.pairing_ttl)
            self._conn.execute(
                "INSERT INTO pairing(code_hash, expires_at, used_at) VALUES (?, ?, NULL)",
                (digest(code), isoformat(expires)),
            )
            self._conn.commit()
            return {"protocol": PROTOCOL, "pairing_code": code, "expires_at": isoformat(expires)}

    def pair(self, request: PairRequest) -> dict[str, str]:
        import secrets

        with self._lock:
            try:
                if request.protocol != PROTOCOL:
                    raise ProtocolError(409, "protocol mismatch")
                moment = self.now()
                if self._failure_count(moment) >= self.failure_limit:
                    raise ProtocolError(429, "too many pairing attempts")
                row = self._conn.execute(
                    "SELECT code_hash, expires_at, used_at FROM pairing WHERE code_hash = ?",
                    (digest(request.pairing_code),),
                ).fetchone()
                if row is None or row["used_at"] is not None or parse_time(row["expires_at"]) <= moment:
                    self._record_failure(moment)
                    self._conn.commit()
                    raise ProtocolError(401, "pairing code rejected")
                token = secrets.token_urlsafe(32)
                worker_id = request.manifest.worker_id
                previous = self._conn.execute(
                    "SELECT worker_id FROM workers WHERE worker_id = ?", (worker_id,)
                ).fetchone()
                self._upsert_worker(request.manifest, digest(token), moment)
                self._conn.execute(
                    "UPDATE pairing SET used_at = ? WHERE code_hash = ?",
                    (isoformat(moment), digest(request.pairing_code)),
                )
                self._event(moment, worker_id, None, "paired" if previous is None else "token_rotated", "token stored as hash")
                self._conn.commit()
                return {
                    "protocol": PROTOCOL,
                    "worker_id": worker_id,
                    "token_type": "Bearer",
                    "token": token,
                    "expires_at": isoformat(moment + timedelta(seconds=self.token_ttl)),
                }
            except ProtocolError:
                raise
            except Exception:
                self._conn.rollback()
                raise

    def authenticate(self, authorization: str | None, nonce: str | None, timestamp: str | None) -> sqlite3.Row:
        with self._lock:
            token = _bearer(authorization)
            token_hash = digest(token)
            moment = self.now()
            row = self._conn.execute("SELECT * FROM workers WHERE token_hash = ?", (token_hash,)).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise ProtocolError(401, "credentials rejected")
            if parse_time(row["token_expires"]) <= moment:
                raise ProtocolError(401, "credentials expired")
            try:
                stamp = int(timestamp or "")
            except ValueError as exc:
                raise ProtocolError(401, "timestamp rejected") from exc
            skew = abs(stamp - int(moment.timestamp()))
            if skew > self.timestamp_skew:
                raise ProtocolError(401, "timestamp rejected")
            nonce = valid_nonce(nonce)
            seen = self._conn.execute(
                "SELECT nonce FROM nonces WHERE token_hash = ? AND nonce = ?",
                (token_hash, nonce),
            ).fetchone()
            if seen is not None:
                raise ProtocolError(409, "nonce replay")
            self._conn.execute(
                "INSERT INTO nonces(token_hash, nonce, seen_at) VALUES (?, ?, ?)",
                (token_hash, nonce, isoformat(moment)),
            )
            cutoff = isoformat(moment - timedelta(seconds=self.timestamp_skew * 2))
            self._conn.execute("DELETE FROM nonces WHERE seen_at < ?", (cutoff,))
            self._conn.commit()
            return row

    def heartbeat(self, worker: sqlite3.Row, manifest: Manifest) -> dict[str, Any]:
        with self._lock:
            self._require_same_worker(worker, manifest.worker_id)
            moment = self.now()
            self._sweep(moment)
            self._write_manifest(manifest, moment, keep_token=True)
            self._extend_leases(manifest.worker_id, moment)
            self._conn.commit()
            fresh = self._conn.execute("SELECT * FROM workers WHERE worker_id = ?", (manifest.worker_id,)).fetchone()
            return {
                "ok": True,
                "protocol": PROTOCOL,
                "presence": self._presence(fresh, moment),
                "server_time": isoformat(moment),
                "cancels": self._cancel_ids(manifest.worker_id),
            }

    def poll(self, worker: sqlite3.Row) -> dict[str, Any]:
        with self._lock:
            moment = self.now()
            self._sweep(moment)
            existing = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE worker_id = ? AND status IN ('leased', 'cancel_requested')
                ORDER BY created_at LIMIT 1
                """,
                (worker["worker_id"],),
            ).fetchone()
            if existing is not None:
                self._conn.commit()
                return self._poll_body(worker["worker_id"], existing)
            capabilities = set(json.loads(worker["capabilities_json"]))
            queued = self._conn.execute(
                "SELECT * FROM tasks WHERE status = 'queued' ORDER BY created_at, task_id"
            ).fetchall()
            chosen = None
            for task in queued:
                required = set(json.loads(task["required_json"]))
                if required <= capabilities:
                    if chosen is None:
                        chosen = task
                else:
                    self._deny_once(moment, task, worker["worker_id"], required - capabilities)
            if chosen is None:
                self._conn.commit()
                return self._poll_body(worker["worker_id"], None)
            lease_id = uuid.uuid4().hex
            expires = isoformat(moment + timedelta(seconds=self.lease_ttl))
            updated = self._conn.execute(
                """
                UPDATE tasks
                SET status = 'leased', worker_id = ?, lease_id = ?, lease_expires = ?, updated_at = ?
                WHERE task_id = ? AND status = 'queued'
                """,
                (worker["worker_id"], lease_id, expires, isoformat(moment), chosen["task_id"]),
            )
            if updated.rowcount != 1:
                self._conn.commit()
                return self._poll_body(worker["worker_id"], None)
            self._event(moment, worker["worker_id"], chosen["task_id"], "leased", "single lease")
            self._conn.commit()
            leased = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (chosen["task_id"],)).fetchone()
            return self._poll_body(worker["worker_id"], leased)

    def progress(self, worker: sqlite3.Row, task_id: str, body: ProgressRequest) -> dict[str, str]:
        with self._lock:
            task = self._owned_lease(worker, task_id, body.lease_id)
            moment = self.now()
            self._event(moment, worker["worker_id"], task_id, "progress", scrub(body.message)[:400] or body.status)
            self._conn.execute(
                "UPDATE tasks SET updated_at = ?, lease_expires = ? WHERE task_id = ?",
                (isoformat(moment), isoformat(moment + timedelta(seconds=self.lease_ttl)), task["task_id"]),
            )
            self._conn.commit()
            return {"ok": "true", "task_id": task_id}

    def result(self, worker: sqlite3.Row, task_id: str, body: ResultRequest) -> dict[str, str]:
        with self._lock:
            task = self._owned_lease(worker, task_id, body.lease_id)
            if task["status"] == "cancel_requested" and body.status != "cancelled":
                raise ProtocolError(409, "task is cancel requested")
            moment = self.now()
            result_json = bounded_json(body.result)
            excerpt = scrub(body.log_excerpt)[:2000]
            self._conn.execute(
                """
                UPDATE tasks
                SET status = ?, result_json = ?, log_excerpt = ?, updated_at = ?,
                    lease_id = NULL, lease_expires = NULL
                WHERE task_id = ?
                """,
                (body.status, result_json, excerpt, isoformat(moment), task_id),
            )
            self._event(moment, worker["worker_id"], task_id, "result", body.status)
            self._conn.commit()
            return {"ok": "true", "task_id": task_id, "status": body.status}

    def enqueue(self, body: TaskCreate) -> dict[str, Any]:
        with self._lock:
            moment = self.now()
            task_id = body.task_id or uuid.uuid4().hex
            if self._conn.execute("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone():
                raise ProtocolError(409, "task id already exists")
            required = body.required_capabilities or [body.task_type]
            payload = bounded_json(body.payload)
            self._conn.execute(
                """
                INSERT INTO tasks(
                    task_id, task_type, payload_json, required_json, grant_name, status,
                    worker_id, lease_id, lease_expires, result_json, log_excerpt, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', NULL, NULL, NULL, NULL, '', ?, ?)
                """,
                (task_id, body.task_type, payload, json.dumps(required), body.grant, isoformat(moment), isoformat(moment)),
            )
            self._event(moment, None, task_id, "queued", body.task_type)
            self._conn.commit()
            return {"protocol": PROTOCOL, "task_id": task_id, "status": "queued", "required_capabilities": required}

    def cancel(self, task_id: str) -> dict[str, str]:
        with self._lock:
            task = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None:
                raise ProtocolError(404, "task not found")
            moment = self.now()
            if task["status"] in {"completed", "rejected", "cancelled", "error"}:
                raise ProtocolError(409, "task already finished")
            if task["status"] == "queued":
                self._conn.execute(
                    "UPDATE tasks SET status = 'cancelled', updated_at = ? WHERE task_id = ?",
                    (isoformat(moment), task_id),
                )
                self._event(moment, None, task_id, "cancelled", "cancelled before lease")
                status = "cancelled"
            else:
                self._conn.execute(
                    "UPDATE tasks SET status = 'cancel_requested', updated_at = ? WHERE task_id = ?",
                    (isoformat(moment), task_id),
                )
                self._event(moment, task["worker_id"], task_id, "cancel_requested", "waiting for worker")
                status = "cancel_requested"
            self._conn.commit()
            return {"ok": "true", "task_id": task_id, "status": status}

    def revoke(self, worker_id: str) -> dict[str, str]:
        with self._lock:
            moment = self.now()
            row = self._conn.execute("SELECT worker_id FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
            if row is None:
                raise ProtocolError(404, "worker not found")
            self._conn.execute(
                "UPDATE workers SET revoked_at = ?, status = 'OFFLINE' WHERE worker_id = ?",
                (isoformat(moment), worker_id),
            )
            self._release_worker_tasks(worker_id, moment, because_revoked=True)
            self._event(moment, worker_id, None, "revoked", "token rejected from now")
            self._conn.commit()
            return {"ok": "true", "worker_id": worker_id, "presence": "OFFLINE"}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            moment = self.now()
            self._sweep(moment)
            self._conn.commit()
            workers = []
            for row in self._conn.execute("SELECT * FROM workers ORDER BY worker_id"):
                workers.append(self._public_worker(row, moment))
            tasks = []
            for row in self._conn.execute("SELECT * FROM tasks ORDER BY created_at, task_id"):
                tasks.append(self._public_task(row))
            events = []
            for row in self._conn.execute("SELECT ts, worker_id, task_id, kind FROM events ORDER BY id DESC LIMIT 30"):
                events.append({"ts": row["ts"], "worker_id": row["worker_id"], "task_id": row["task_id"], "kind": row["kind"]})
            return {"protocol": PROTOCOL, "server_time": isoformat(moment), "workers": workers, "tasks": tasks, "events": events}

    def worker_detail(self, worker_id: str) -> dict[str, Any]:
        with self._lock:
            moment = self.now()
            row = self._conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
            if row is None:
                raise ProtocolError(404, "worker not found")
            public = self._public_worker(row, moment)
            public["inventory"] = json.loads(row["inventory_json"])
            return public

    def task_detail(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise ProtocolError(404, "task not found")
            public = self._public_task(row)
            public["payload"] = json.loads(row["payload_json"])
            public["log_excerpt"] = row["log_excerpt"]
            return public

    def _upsert_worker(self, manifest: Manifest, token_hash: str, moment) -> None:
        inventory = bounded_json(manifest.inventory)
        expires = isoformat(moment + timedelta(seconds=self.token_ttl))
        self._conn.execute(
            """
            INSERT INTO workers(
                worker_id, runtime_name, host_id, model_id, model_id_source, capabilities_json,
                inventory_json, status, version, token_hash, token_expires, last_heartbeat,
                created_at, revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)
            ON CONFLICT(worker_id) DO UPDATE SET
                runtime_name = excluded.runtime_name,
                host_id = excluded.host_id,
                model_id = excluded.model_id,
                model_id_source = excluded.model_id_source,
                capabilities_json = excluded.capabilities_json,
                inventory_json = excluded.inventory_json,
                status = excluded.status,
                version = excluded.version,
                token_hash = excluded.token_hash,
                token_expires = excluded.token_expires,
                last_heartbeat = NULL,
                revoked_at = NULL
            """,
            (
                manifest.worker_id,
                manifest.runtime_name,
                manifest.host_id,
                manifest.model_id,
                manifest.model_id_source,
                json.dumps(manifest.capabilities),
                inventory,
                manifest.status,
                manifest.version,
                token_hash,
                expires,
                isoformat(moment),
            ),
        )

    def _write_manifest(self, manifest: Manifest, moment, keep_token: bool) -> None:
        del keep_token
        self._conn.execute(
            """
            UPDATE workers
            SET runtime_name = ?, host_id = ?, model_id = ?, model_id_source = ?,
                capabilities_json = ?, inventory_json = ?, status = ?, version = ?,
                last_heartbeat = ?
            WHERE worker_id = ?
            """,
            (
                manifest.runtime_name,
                manifest.host_id,
                manifest.model_id,
                manifest.model_id_source,
                json.dumps(manifest.capabilities),
                bounded_json(manifest.inventory),
                manifest.status,
                manifest.version,
                isoformat(moment),
                manifest.worker_id,
            ),
        )

    def _extend_leases(self, worker_id: str, moment) -> None:
        expires = isoformat(moment + timedelta(seconds=self.lease_ttl))
        self._conn.execute(
            """
            UPDATE tasks SET lease_expires = ?, updated_at = ?
            WHERE worker_id = ? AND status IN ('leased', 'cancel_requested')
            """,
            (expires, isoformat(moment), worker_id),
        )

    def _owned_lease(self, worker: sqlite3.Row, task_id: str, lease_id: str) -> sqlite3.Row:
        task = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if task is None:
            raise ProtocolError(404, "task not found")
        if task["status"] in {"completed", "rejected", "cancelled", "error"}:
            raise ProtocolError(409, "task already finished")
        if task["worker_id"] != worker["worker_id"] or task["lease_id"] != lease_id:
            raise ProtocolError(403, "lease rejected")
        if task["status"] not in {"leased", "cancel_requested"}:
            raise ProtocolError(409, "task is not leased")
        return task

    def _poll_body(self, worker_id: str, task: sqlite3.Row | None) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "lease": None if task is None else _public_lease(task),
            "cancels": self._cancel_ids(worker_id),
        }

    def _cancel_ids(self, worker_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT task_id FROM tasks WHERE worker_id = ? AND status = 'cancel_requested' ORDER BY task_id",
            (worker_id,),
        ).fetchall()
        return [row["task_id"] for row in rows]

    def _deny_once(self, moment, task: sqlite3.Row, worker_id: str, missing: set[str]) -> None:
        inserted = self._conn.execute(
            "INSERT OR IGNORE INTO capability_denials(task_id, worker_id) VALUES (?, ?)",
            (task["task_id"], worker_id),
        )
        if inserted.rowcount == 1:
            detail = "missing " + ",".join(sorted(missing))
            self._event(moment, worker_id, task["task_id"], "capability_denied", detail[:400])

    def _sweep(self, moment) -> None:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status IN ('leased', 'cancel_requested')"
        ).fetchall()
        for task in rows:
            worker = self._conn.execute("SELECT * FROM workers WHERE worker_id = ?", (task["worker_id"],)).fetchone()
            stale = worker is None or worker["revoked_at"] is not None or self._heartbeat_stale(worker, moment)
            if task["lease_expires"] and parse_time(task["lease_expires"]) <= moment:
                stale = True
            if not stale:
                continue
            if task["status"] == "cancel_requested":
                self._conn.execute(
                    "UPDATE tasks SET status = 'cancelled', lease_id = NULL, lease_expires = NULL, updated_at = ? WHERE task_id = ?",
                    (isoformat(moment), task["task_id"]),
                )
                self._event(moment, task["worker_id"], task["task_id"], "cancelled", "worker lost during cancel")
            else:
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'queued', worker_id = NULL, lease_id = NULL, lease_expires = NULL, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (isoformat(moment), task["task_id"]),
                )
                self._event(moment, task["worker_id"], task["task_id"], "lease_released", "heartbeat lost or lease expired")

    def _release_worker_tasks(self, worker_id: str, moment, because_revoked: bool) -> None:
        del because_revoked
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE worker_id = ? AND status IN ('leased', 'cancel_requested')",
            (worker_id,),
        ).fetchall()
        for task in rows:
            if task["status"] == "cancel_requested":
                status = "cancelled"
                worker_value = task["worker_id"]
            else:
                status = "queued"
                worker_value = None
            self._conn.execute(
                """
                UPDATE tasks
                SET status = ?, worker_id = ?, lease_id = NULL, lease_expires = NULL, updated_at = ?
                WHERE task_id = ?
                """,
                (status, worker_value, isoformat(moment), task["task_id"]),
            )

    def _heartbeat_stale(self, worker: sqlite3.Row, moment) -> bool:
        if worker["last_heartbeat"] is None:
            return True
        age = (moment - parse_time(worker["last_heartbeat"])).total_seconds()
        return age > self.offline_after

    def _presence(self, worker: sqlite3.Row, moment) -> str:
        if worker["revoked_at"] is not None or worker["status"] == "OFFLINE" or self._heartbeat_stale(worker, moment):
            return "OFFLINE"
        return "LIVE"

    def _public_worker(self, row: sqlite3.Row, moment) -> dict[str, Any]:
        inventory = json.loads(row["inventory_json"])
        return {
            "worker_id": row["worker_id"],
            "runtime_name": row["runtime_name"],
            "host_id": row["host_id"],
            "model_id": row["model_id"],
            "model_id_source": row["model_id_source"],
            "capabilities": json.loads(row["capabilities_json"]),
            "status": "OFFLINE" if row["revoked_at"] else row["status"],
            "presence": self._presence(row, moment),
            "version": row["version"],
            "last_heartbeat": row["last_heartbeat"],
            "revoked": row["revoked_at"] is not None,
            "inventory_summary": {
                "arbitrary_shell_from_dashboard": inventory.get("arbitrary_shell_from_dashboard"),
                "model_ready": inventory.get("model_ready"),
                "model_endpoint_probed": inventory.get("model_endpoint_probed"),
                "skill_count": inventory.get("skill_count"),
                "mcp_invoked": inventory.get("mcp_tools_invoked", inventory.get("mcp_invoked")),
            },
        }

    def _public_task(self, row: sqlite3.Row) -> dict[str, Any]:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        shell_executed = result.get("executed_shell") if isinstance(result, dict) else None
        return {
            "task_id": row["task_id"],
            "type": row["task_type"],
            "status": row["status"],
            "worker_id": row["worker_id"],
            "required_capabilities": json.loads(row["required_json"]),
            "grant": row["grant_name"],
            "result": result,
            "executed_shell": shell_executed,
            "updated_at": row["updated_at"],
        }

    def _event(self, moment, worker_id: str | None, task_id: str | None, kind: str, detail: str) -> None:
        self._conn.execute(
            "INSERT INTO events(ts, worker_id, task_id, kind, detail) VALUES (?, ?, ?, ?, ?)",
            (isoformat(moment), worker_id, task_id, kind, scrub(detail)[:400]),
        )

    def _record_failure(self, moment) -> None:
        self._conn.execute("INSERT INTO pair_failures(ts) VALUES (?)", (isoformat(moment),))
        cutoff = isoformat(moment - timedelta(seconds=self.failure_window))
        self._conn.execute("DELETE FROM pair_failures WHERE ts < ?", (cutoff,))

    def _failure_count(self, moment) -> int:
        cutoff = isoformat(moment - timedelta(seconds=self.failure_window))
        row = self._conn.execute("SELECT COUNT(*) AS n FROM pair_failures WHERE ts >= ?", (cutoff,)).fetchone()
        return int(row["n"])

    @staticmethod
    def _require_same_worker(worker: sqlite3.Row, worker_id: str) -> None:
        if worker["worker_id"] != worker_id:
            raise ProtocolError(403, "worker id mismatch")


def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise ProtocolError(401, "credentials rejected")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or " " in token:
        raise ProtocolError(401, "credentials rejected")
    return token


def _public_lease(task: sqlite3.Row) -> dict[str, Any]:
    return {
        "lease_id": task["lease_id"],
        "task_id": task["task_id"],
        "expires_at": task["lease_expires"],
        "cancel_requested": task["status"] == "cancel_requested",
        "offer": {
            "type": task["task_type"],
            "payload": json.loads(task["payload_json"]),
            "required_capabilities": json.loads(task["required_json"]),
            "grant": task["grant_name"],
        },
    }


def protocol_error_handler(_request: Request, exc: ProtocolError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


def build_router(broker: WorkerBroker):
    from fastapi import APIRouter

    router = APIRouter(prefix="/api/workers")

    def auth(request: Request) -> sqlite3.Row:
        return broker.authenticate(
            request.headers.get("authorization"),
            request.headers.get("x-orbit-nonce"),
            request.headers.get("x-orbit-timestamp"),
        )

    @router.get("/protocol")
    def protocol() -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "authentication": "bearer-token-plus-nonce",
            "bind": "loopback",
            "executes_payloads": False,
        }

    @router.post("/pairing", status_code=201)
    def pairing() -> dict[str, str]:
        return broker.create_pairing()

    @router.post("/pair", status_code=201)
    def pair(body: PairRequest) -> dict[str, str]:
        return broker.pair(body)

    @router.post("/heartbeat")
    def heartbeat(body: HeartbeatRequest, request: Request) -> dict[str, Any]:
        return broker.heartbeat(auth(request), body.manifest)

    @router.post("/poll")
    def poll(request: Request) -> dict[str, Any]:
        return broker.poll(auth(request))

    @router.post("/revoke")
    def revoke(body: RevokeRequest) -> dict[str, str]:
        return broker.revoke(body.worker_id)

    @router.post("/tasks", status_code=201)
    def create_task(body: TaskCreate) -> dict[str, Any]:
        return broker.enqueue(body)

    @router.get("/tasks")
    def list_tasks() -> dict[str, Any]:
        return {"protocol": PROTOCOL, "tasks": broker.snapshot()["tasks"]}

    @router.get("/tasks/{task_id}")
    def task_detail(task_id: str) -> dict[str, Any]:
        return broker.task_detail(task_id)

    @router.post("/tasks/{task_id}/progress")
    def progress(task_id: str, body: ProgressRequest, request: Request) -> dict[str, str]:
        return broker.progress(auth(request), task_id, body)

    @router.post("/tasks/{task_id}/result")
    def result(task_id: str, body: ResultRequest, request: Request) -> dict[str, str]:
        return broker.result(auth(request), task_id, body)

    @router.post("/tasks/{task_id}/cancel")
    def cancel(task_id: str) -> dict[str, str]:
        return broker.cancel(task_id)

    @router.get("")
    def list_workers() -> dict[str, Any]:
        snap = broker.snapshot()
        return {"protocol": PROTOCOL, "workers": snap["workers"], "server_time": snap["server_time"]}

    @router.get("/{worker_id}")
    def worker_detail(worker_id: str) -> dict[str, Any]:
        return broker.worker_detail(worker_id)

    return router


def mount_worker_api(app: FastAPI, broker: WorkerBroker) -> None:
    """Attach worker routes without replacing the host application's UI."""
    app.state.worker_broker = broker
    app.add_exception_handler(ProtocolError, protocol_error_handler)
    app.include_router(build_router(broker))


def _security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; img-src 'self'; script-src 'none'"
    return response


def create_worker_app(database: Path, clock: Any | None = None, **broker_kwargs: Any) -> FastAPI:
    broker = WorkerBroker(database, clock=clock, **broker_kwargs)
    app = FastAPI(title="ORBIT Worker Broker", version=PROTOCOL, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
    mount_worker_api(app, broker)
    app.state.broker = broker

    @app.middleware("http")
    async def browser_safety(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                from urllib.parse import urlsplit

                parsed = urlsplit(origin)
                if parsed.scheme not in {"http", "https"} or parsed.netloc != request.headers.get("host", ""):
                    response = JSONResponse(status_code=403, content={"detail": "Cross-origin mutation denied"})
                    return _security_headers(response)
        response = await call_next(request)
        return _security_headers(response)

    @app.get("/", include_in_schema=False)
    def dashboard():
        return HTMLResponse(render_dashboard(broker.snapshot()))

    @app.get("/worker.css", include_in_schema=False)
    def css():
        return FileResponse(CSS_PATH, media_type="text/css")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "orbit-worker-broker",
            "protocol": PROTOCOL,
            "bind": "127.0.0.1",
            "auth": "worker_bearer",
            "operator_auth": "loopback_only",
            "executes_payloads": False,
        }

    return app


def create_standalone() -> FastAPI:
    host = os.environ.get("ORBIT_WORKER_BIND", "127.0.0.1")
    assert_loopback_host(host)
    database = Path(os.environ.get("ORBIT_WORKER_DB", str(default_database()))).expanduser()
    return create_worker_app(database)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="orbit.worker_broker")
    parser.add_argument("--host", default=os.environ.get("ORBIT_WORKER_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ORBIT_WORKER_PORT", "8766")))
    parser.add_argument("--database", default=os.environ.get("ORBIT_WORKER_DB", str(default_database())))
    args = parser.parse_args(argv)
    assert_loopback_host(args.host)
    os.environ["ORBIT_WORKER_BIND"] = "127.0.0.1"
    os.environ["ORBIT_WORKER_PORT"] = str(args.port)
    os.environ["ORBIT_WORKER_DB"] = str(Path(args.database).expanduser())
    uvicorn.run(
        "orbit.worker_broker:create_standalone",
        factory=True,
        host="127.0.0.1",
        port=args.port,
        reload=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
