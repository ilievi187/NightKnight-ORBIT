"""Worker fleet registry, secure pairing and heartbeat lifecycle.

Security model (see docs/WORKER-API.md):
- This service never opens its own socket; it is served by the loopback-only
  FastAPI app (127.0.0.1 bind + TrustedHost middleware encode the boundary).
- Pairing codes are single-use, short-lived and stored ONLY as SHA-256 hashes.
- Worker tokens are 256-bit secrets returned once at registration; only their
  SHA-256 hashes are persisted. Tokens authorize ONLY heartbeat/claim/report
  for tasks explicitly assigned to that worker by the supervisor.
- A registry with zero workers literally renders zero workers — statuses are
  never fabricated. Agents "profiles" (ALEX/NOVA/HERMES, harness.PROFILES)
  remain separate from connected live workers.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

WORKER_STATUSES = ("LIVE", "IDLE", "OFFLINE")
DEFAULT_HEARTBEAT_TTL = 20.0
PAIRING_TTL = 600.0  # 10 minutes (issue #12: short-lived pairing credential)
PAIRING_ATTEMPT_LOCK = 5  # evidence counter; also burns the pairing after abuse

ALLOWED_KINDS = ("mock", "hermes", "opencode", "custom")


class FleetError(RuntimeError):
    """Raised for invalid fleet operations."""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def format_pairing_code() -> str:
    """Three groups of six hex chars = 96 bits of entropy, e.g. `A9F1C2-77B03E-9D1C40`."""
    return "-".join(secrets.token_hex(3).upper() for _ in range(3))


class FleetStore:
    """SQLite persistence. Shares the ORBIT database file, additive schema."""

    def __init__(self, filename: Path):
        self.filename = Path(filename)
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    version TEXT,
                    status TEXT NOT NULL DEFAULT 'OFFLINE',
                    token_hash TEXT NOT NULL UNIQUE,
                    registered_at REAL NOT NULL,
                    last_heartbeat REAL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS workers_status ON workers(status);
                CREATE TABLE IF NOT EXISTS pairings (
                    id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    burned INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS pairings_open ON pairings(expires_at, used_at, burned);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.filename, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout = 30000")
            with db:
                yield db
        finally:
            db.close()

    def insert_pairing(self, pairing_id: str, code_hash: str, now: float, ttl: float) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO pairings(id,code_hash,created_at,expires_at) VALUES(?,?,?,?)",
                       (pairing_id, code_hash, now, now + ttl))

    def pairing_by_code_hash(self, code_hash: str):
        with self.connect() as db:
            return db.execute("SELECT * FROM pairings WHERE code_hash=?", (code_hash,)).fetchone()

    def pairing(self, pairing_id: str):
        with self.connect() as db:
            return db.execute("SELECT * FROM pairings WHERE id=?", (pairing_id,)).fetchone()

    def mark_pairing_used(self, pairing_id: str, now: float) -> None:
        with self.connect() as db:
            db.execute("UPDATE pairings SET used_at=?, burned=1 WHERE id=?", (now, pairing_id))

    def pairing_fail(self, pairing_id: str) -> int:
        with self.connect() as db:
            db.execute("UPDATE pairings SET failed_attempts = failed_attempts + 1 WHERE id=?", (pairing_id,))
            row = db.execute("SELECT failed_attempts FROM pairings WHERE id=?", (pairing_id,)).fetchone()
        return int(row["failed_attempts"]) if row else 0

    def burn_pairing(self, pairing_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE pairings SET burned=1 WHERE id=?", (pairing_id,))

    def pairings_recent(self, limit: int = 20) -> list[dict]:
        """Dashboard listing. NEVER includes code plaintext or hashes."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, created_at, expires_at, used_at, failed_attempts, burned "
                "FROM pairings ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        now = time.time()
        out = []
        for r in rows:
            out.append({
                "pairing_id": r["id"],
                "created_at": r["created_at"],
                "expires_at": r["expires_at"],
                "expired": r["expires_at"] < now,
                "used": r["used_at"] is not None,
                "burned": bool(r["burned"]),
                "failed_attempts": int(r["failed_attempts"]),
            })
        return out

    def insert_worker(self, worker: dict) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO workers(id,name,host,kind,capabilities_json,version,status,token_hash,registered_at,last_heartbeat,revoked,revoked_at) "
                "VALUES(:id,:name,:host,:kind,:capabilities_json,:version,:status,:token_hash,:registered_at,:last_heartbeat,:revoked,:revoked_at)",
                worker)

    def worker(self, worker_id: str):
        if not worker_id:
            return None
        with self.connect() as db:
            return db.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()

    def worker_by_token_hash(self, token_hash: str):
        with self.connect() as db:
            return db.execute("SELECT * FROM workers WHERE token_hash=? AND revoked=0", (token_hash,)).fetchone()

    def workers(self, include_revoked: bool = False) -> list[dict]:
        query = "SELECT * FROM workers" + ("" if include_revoked else " WHERE revoked=0") + " ORDER BY registered_at"
        with self.connect() as db:
            return [dict(r) for r in db.execute(query).fetchall()]

    def expire_stale(self, cutoff: float) -> list[str]:
        """Mark workers offline if their last heartbeat is older than the TTL."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT id FROM workers WHERE revoked=0 AND status IN ('LIVE','IDLE') AND "
                "((last_heartbeat IS NULL AND registered_at < ?) OR (last_heartbeat IS NOT NULL AND last_heartbeat < ?))",
                (cutoff, cutoff)).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                db.executemany("UPDATE workers SET status='OFFLINE' WHERE id=?", [(i,) for i in ids])
        return ids

    def set_heartbeat(self, worker_id: str, now: float, status: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE workers SET last_heartbeat=?, status=? WHERE id=? AND revoked=0", (now, status, worker_id))

    def set_status_for(self, worker_id: str, status: str) -> None:
        if status not in WORKER_STATUSES:
            raise FleetError(f"invalid status {status!r}")
        with self.connect() as db:
            db.execute("UPDATE workers SET status=? WHERE id=? AND revoked=0", (status, worker_id))

    def revoke(self, worker_id: str, now: float) -> bool:
        with self.connect() as db:
            cur = db.execute("UPDATE workers SET revoked=1, revoked_at=?, status='OFFLINE' WHERE id=? AND revoked=0",
                             (now, worker_id))
        return cur.rowcount == 1


def _validate_capabilities(raw) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise FleetError("capabilities must be a non-empty list of strings")
    out: list[str] = []
    for item in raw[:24]:
        text = str(item).strip()
        if not text or len(text) > 64 or any(ch in text for ch in " \t;|"):
            raise FleetError(f"invalid capability: {text[:40]!r}")
        out.append(text)
    return out


class Fleet:
    """Registry + pairing service. Served only by the loopback FastAPI app."""

    def __init__(self, store: FleetStore, heartbeat_ttl: float = DEFAULT_HEARTBEAT_TTL,
                 pairing_ttl: float = PAIRING_TTL,
                 running_task_ids: Callable[[str], bool] | None = None):
        """`running_task_ids(worker_id)` -> bool: does the worker hold a RUNNING
        task right now? Injected by the mission API so LIVE/IDLE is factual."""
        self.store = store
        self.heartbeat_ttl = heartbeat_ttl
        self.pairing_ttl = pairing_ttl
        self._running_task_ids = running_task_ids or (lambda worker_id: False)

    # --- pairing ---------------------------------------------------------------
    def create_pairing(self, now: float | None = None) -> dict:
        """Display-once pairing credential for onboarding a local worker."""
        now = time.time() if now is None else now
        code = format_pairing_code()
        pairing_id = uuid.uuid4().hex
        self.store.insert_pairing(pairing_id, _sha256(code), now, self.pairing_ttl)
        return {
            "pairing_id": pairing_id,
            "pairing_code": code,  # shown once in the desktop UI; hashed at rest
            "expires_in": int(self.pairing_ttl),
            "expires_at": now + self.pairing_ttl,
        }

    def evaluate_pairing(self, pairing_code: str, now: float | None = None) -> dict:
        """Read-only pairing validity check (pre-registration validation)."""
        now = time.time() if now is None else now
        row = self.store.pairing_by_code_hash(_sha256(pairing_code or ""))
        if row is None:
            raise FleetError("unknown pairing code")
        if bool(row["burned"]) or row["expires_at"] < now:
            raise FleetError("pairing code expired or already used")
        return {"pairing_id": row["id"]}

    def consume_pairing(self, pairing_code: str, *, success: bool, now: float | None = None) -> None:
        """Burn after first successful use; count and lock after repeated failures."""
        now = time.time() if now is None else now
        row = self.store.pairing_by_code_hash(_sha256(pairing_code or ""))
        if row is None:
            raise FleetError("unknown pairing code")
        if bool(row["burned"]) or row["expires_at"] < now:
            raise FleetError("pairing code expired or already used")
        if success:
            self.store.mark_pairing_used(row["id"], now)
            return
        attempts = self.store.pairing_fail(row["id"])
        if attempts >= PAIRING_ATTEMPT_LOCK:
            self.store.burn_pairing(row["id"])

    # --- registration ------------------------------------------------------------
    def register(self, pairing_code: str, name: str, host: str, kind: str,
                 capabilities, version: str | None = None, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
            raise FleetError("name must be 1..64 chars")
        if not isinstance(host, str) or not host.strip() or len(host.strip()) > 120:
            raise FleetError("host is required (worker machine identity)")
        if kind not in ALLOWED_KINDS:
            raise FleetError(f"kind must be one of {ALLOWED_KINDS}")
        caps = _validate_capabilities(capabilities)
        self.evaluate_pairing(pairing_code, now)  # fail fast on unknown/expired/burned codes
        token = secrets.token_urlsafe(32)  # 256-bit; returned ONCE, stored hashed
        worker_id = uuid.uuid4().hex
        worker = {
            "id": worker_id,
            "name": name.strip(),
            "host": host.strip(),
            "kind": kind,
            "capabilities_json": json.dumps(caps),
            "version": ((version or "").strip()[:40]) or None,
            "status": "OFFLINE",  # honest until the first heartbeat arrives
            "token_hash": _sha256(token),
            "registered_at": now,
            "last_heartbeat": None,
            "revoked": 0,
            "revoked_at": None,
        }
        try:
            self.store.insert_worker(worker)
        except sqlite3.IntegrityError:
            raise FleetError("worker token collision; retry") from None
        try:
            self.consume_pairing(pairing_code, success=True, now=now)
        except FleetError:
            self.store.revoke(worker_id, now)  # rollback; registration must be atomic-feeling
            raise
        return {
            "worker_id": worker_id,
            "worker_token": token,
            "heartbeat_ttl": self.heartbeat_ttl,
            "capabilities": caps,
            "status": worker["status"],
        }

    # --- auth + lifecycle --------------------------------------------------------
    def authorize(self, worker_token) -> dict | None:
        if not isinstance(worker_token, str) or not 20 <= len(worker_token) <= 200:
            return None
        row = self.store.worker_by_token_hash(_sha256(worker_token))
        return dict(row) if row else None

    def worker(self, worker_id: str) -> dict | None:
        row = self.store.worker(worker_id)
        if row is None:
            return None
        return dict(row)

    def capable_workers(self, capability: str, now: float) -> list[dict]:
        out = []
        for w in self.store.workers():
            if w["status"] not in ("LIVE", "IDLE"):
                continue
            if not self.is_heartbeat_fresh(w, now):
                continue
            caps = json.loads(w.get("capabilities_json") or "[]")
            if capability in caps:
                out.append(w)
        return out

    def is_heartbeat_fresh(self, worker: dict, now: float) -> bool:
        value = worker.get("last_heartbeat")
        return bool(value) and (now - value) <= self.heartbeat_ttl

    def heartbeat(self, worker_id: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        worker = self.store.worker(worker_id)
        if worker is None or worker["revoked"]:
            raise FleetError("unknown or revoked worker")
        running = bool(self._running_task_ids(worker_id))
        status = "LIVE" if running else "IDLE"
        self.store.set_heartbeat(worker_id, now, status)
        return {"worker_id": worker_id, "status": status, "heartbeat_ttl": self.heartbeat_ttl}

    def refresh_status(self, busy_worker_ids: set[str], now: float) -> None:
        """Called by the supervisor after worker/task mutations."""
        for w in self.store.workers():
            if not w["last_heartbeat"] or not self.is_heartbeat_fresh(w, now):
                continue
            if w["status"] in ("LIVE", "IDLE"):
                preferred = "LIVE" if (w["id"] in busy_worker_ids or self._running_task_ids(w["id"])) else "IDLE"
                if w["status"] != preferred:
                    self.store.set_status_for(w["id"], preferred)

    def age(self, now: float | None = None) -> list[str]:
        """Expire stale heartbeats -> OFFLINE. Never invents live presence."""
        now = time.time() if now is None else now
        return self.store.expire_stale(now - self.heartbeat_ttl)

    def note_offline(self, worker: dict, now: float | None = None) -> None:
        """Immediate OFFLINE transition (supervisor lost-worker detection)."""
        now = time.time() if now is None else now
        self.store.set_status_for(worker["id"], "OFFLINE")

    def revoke(self, worker_id: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.store.revoke(worker_id, now)

    # --- dashboard view ------------------------------------------------------------
    def public_workers(self, now: float, task_of_worker: Callable[[str], dict | None] | None = None) -> list[dict]:
        """Dashboard payload. Token hashes and secrets are NEVER included."""
        task_of_worker = task_of_worker or (lambda worker_id: None)
        out = []
        for w in self.store.workers():
            task = task_of_worker(w["id"])
            fresh = self.is_heartbeat_fresh(w, now)
            if w["revoked"] or not fresh:
                status = "OFFLINE"
            elif task and task["status"] == "RUNNING":
                status = "LIVE"
            else:
                status = "IDLE"
            out.append({
                "id": w["id"],
                "name": w["name"],
                "host": w["host"],
                "kind": w["kind"],
                "version": w["version"],
                "capabilities": json.loads(w.get("capabilities_json") or "[]"),
                "status": status,
                "registered_at": w["registered_at"],
                "last_heartbeat": w["last_heartbeat"],
                "heartbeat_age": None if not w["last_heartbeat"] else round(now - w["last_heartbeat"], 1),
                "assigned_task": {"task_id": task["task_id"], "title": task["title"], "status": task["status"]} if task else None,
            })
        return out
