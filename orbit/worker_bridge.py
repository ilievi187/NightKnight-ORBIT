"""ORBIT Worker Protocol v0.1, Runtime Discovery & Authenticated Worker Adapter (Issue #12 / #13).

Provides:
1. Real local runtime & host capability discovery for Google Antigravity (AGY),
   Hermes Agent, OpenCode (ALEX), and LUNA without fabricating live statuses or
   conflating worker harnesses with LLM models.
2. Versioned ORBIT Worker Protocol v0.1 data contracts:
   WorkerManifest, TaskOffer, Lease, TaskProgress, TaskResult, Cancel.
3. Authenticated loopback pairing (short-lived single-use pairing code + client
   nonce anti-replay -> scoped worker bearer token hashed at rest, secret masking).
4. Safe, lease-based task polling, claiming, heartbeat liveness, progress events,
   deterministic cancellation, and grant-confined non-destructive task execution
   with server-side file artifact verification (SHA-256 / content checks).
5. Authenticated SSH / Tailscale loopback forwarding specifications for remote
   hosts (ubuntu-hermes, nightknight-server, detti) without public binds.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import secrets
import shutil
import socket
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .tools import DENIED_NAMES, DENIED_SUFFIXES, MAX_READ_BYTES, MAX_WRITE_CHARS, checked_path

PROTOCOL_VERSION = "v0.1"
WORKER_STATES = ("IDLE", "LIVE", "BUSY", "DEGRADED", "ERROR", "OFFLINE", "BLOCKED")
LEASE_STATES = ("ACTIVE", "COMPLETED", "EXPIRED", "CANCELLED", "FAILED")
TASK_STATES = ("PENDING", "ASSIGNED", "RUNNING", "SUBMITTED", "VERIFIED", "FAILED", "BLOCKED", "CANCELLED")
ALLOWED_TASK_OPS = frozenset({"deliver", "retry-deliver", "workspace.inspect", "workspace.read", "workspace.write_scoped", "inspect", "read"})

DEFAULT_PAIRING_TTL = 300.0      # 5 minutes for one-time pairing credential
DEFAULT_TOKEN_TTL = 86_400.0     # 24 hours for scoped worker token
DEFAULT_HEARTBEAT_TTL = 20.0     # 20 seconds before worker is aged OFFLINE
DEFAULT_LEASE_TTL = 60.0         # 60 seconds per task lease

ANTIGRAVITY_TOOLS: tuple[str, ...] = (
    "run_command",
    "view_file",
    "write_to_file",
    "replace_file_content",
    "search_web",
    "read_url_content",
    "invoke_subagent",
    "define_subagent",
    "manage_subagents",
    "send_message",
    "schedule",
    "manage_task",
    "ask_question",
)


class WorkerProtocolError(RuntimeError):
    """Raised on worker protocol or state violations."""


class WorkerAuthError(WorkerProtocolError):
    """Raised on invalid, expired, replayed, or revoked worker credentials."""


class WorkerCancelledError(WorkerProtocolError):
    """Raised when a leased task has been cancelled by the coordinator."""


def mask_secret(value: str | None) -> str:
    """Mask a pairing code or bearer token so raw secrets never appear in logs or UI."""
    if not value or not isinstance(value, str):
        return "<empty>"
    clean = value.strip()
    if len(clean) <= 10:
        return "***REDACTED***"
    return f"{clean[:6]}...{clean[-4:]}"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Protocol v0.1 Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class WorkerManifest:
    """Truthful declaration of a worker runtime (harness != LLM model)."""
    worker_id: str
    runtime_name: str               # e.g. 'antigravity', 'hermes', 'opencode', 'luna'
    host_id: str                    # e.g. 'NightKnightPC'
    model_id: str | None = None     # e.g. 'Gemini 4 Argon (Medium)'; None if unknown
    capabilities: tuple[str, ...] = ("orbit.plan.deliver", "workspace.inspect", "workspace.read", "workspace.write_scoped")
    tool_allowlist: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    status: str = "IDLE"            # IDLE | LIVE | BUSY | DEGRADED | ERROR | OFFLINE
    version: str = "0.1.0"
    heartbeat_at: float = field(default_factory=time.time)
    transport: str = "loopback-http"
    resources: dict[str, Any] = field(default_factory=dict)
    endpoint: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "id": self.worker_id,
            "name": self.worker_id,
            "runtime_name": self.runtime_name,
            "kind": self.runtime_name,
            "host_id": self.host_id,
            "host": self.host_id,
            "model_id": self.model_id,
            "capabilities": list(self.capabilities),
            "tool_allowlist": list(self.tool_allowlist),
            "skills": list(self.skills),
            "skill_count": len(self.skills),
            "status": self.status,
            "version": self.version,
            "heartbeat_at": self.heartbeat_at,
            "transport": self.transport,
            "resources": dict(self.resources),
            "endpoint": self.endpoint,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkerManifest":
        wid = str(data.get("worker_id") or data.get("id") or data.get("name") or "").strip()
        runtime = str(data.get("runtime_name") or data.get("kind") or "").strip()
        host = str(data.get("host_id") or data.get("host") or socket.gethostname()).strip()
        if not wid or not runtime:
            raise ValueError("worker_id and runtime_name are required in WorkerManifest")
        status = str(data.get("status") or "IDLE").upper().strip()
        if status not in WORKER_STATES:
            raise ValueError(f"Invalid worker status: {status}")
        model_id = data.get("model_id")
        if model_id is not None:
            model_id = str(model_id).strip() or None
        return cls(
            worker_id=wid[:80],
            runtime_name=runtime[:40],
            host_id=host[:80],
            model_id=model_id,
            capabilities=tuple(str(x) for x in (data.get("capabilities") or ())),
            tool_allowlist=tuple(str(x) for x in (data.get("tool_allowlist") or ())),
            skills=tuple(str(x) for x in (data.get("skills") or ())),
            status=status,
            version=str(data.get("version") or "0.1.0")[:40],
            heartbeat_at=float(data.get("heartbeat_at") or time.time()),
            transport=str(data.get("transport") or "loopback-http")[:40],
            resources=dict(data.get("resources") or {}),
            endpoint=str(data["endpoint"])[:200] if data.get("endpoint") else None,
            notes=str(data.get("notes") or "")[:300],
        )


@dataclass
class TaskOffer:
    """Scoped task offered to an eligible worker."""
    task_id: str
    mission_id: str
    title: str
    capability_required: str
    spec: dict[str, Any]
    grant: dict[str, Any]
    attempt: int = 1
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Lease:
    """Bounded-time exclusive task lease held by one worker."""
    lease_id: str
    task_id: str
    mission_id: str
    worker_id: str
    acquired_at: float
    expires_at: float
    status: str = "ACTIVE"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskProgress:
    """Incremental progress event reported under an active lease."""
    task_id: str
    lease_id: str
    worker_id: str
    stage: str
    percent: int
    message: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskResult:
    """Structured task completion or failure report with artifact digests."""
    task_id: str
    lease_id: str
    worker_id: str
    status: str  # SUBMITTED | SUCCEEDED | FAILED | BLOCKED | CANCELLED | DEGRADED
    summary: str
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    completed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "lease_id": self.lease_id,
            "worker_id": self.worker_id,
            "status": self.status,
            "summary": self.summary[:2000],
            "artifacts": self.artifacts[:20],
            "logs": [str(line)[:300] for line in self.logs[:50]],
            "completed_at": self.completed_at,
        }


@dataclass
class Cancel:
    """Deterministic task/lease cancellation directive."""
    task_id: str
    lease_id: str | None
    reason: str
    cancelled_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Real Host & Runtime Discovery (No Invented Capabilities)
# ---------------------------------------------------------------------------

def inspect_host_resources() -> dict[str, Any]:
    """Read real CPU, RAM, and AMD/Linux DRM VRAM metrics from /proc and /sys without spawning GPU jobs."""
    uname = platform.uname()
    cpu_model = uname.processor or "unknown"
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        try:
            for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.lower().startswith("model name"):
                    cpu_model = line.partition(":")[2].strip()
                    break
        except OSError:
            pass

    mem_total_kib = 0
    mem_avail_kib = 0
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        try:
            for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("MemTotal:"):
                    mem_total_kib = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_avail_kib = int(line.split()[1])
        except (OSError, ValueError, IndexError):
            pass

    gpus: list[dict[str, Any]] = []
    drm_root = Path("/sys/class/drm")
    if drm_root.is_dir():
        for card_dir in sorted(drm_root.glob("card[0-9]*")):
            if "-" in card_dir.name:
                continue
            vram_total_path = card_dir / "device" / "mem_info_vram_total"
            vram_used_path = card_dir / "device" / "mem_info_vram_used"
            if vram_total_path.is_file():
                try:
                    total_b = int(vram_total_path.read_text(encoding="utf-8").strip())
                    used_b = int(vram_used_path.read_text(encoding="utf-8").strip()) if vram_used_path.is_file() else 0
                    gpus.append({
                        "card": card_dir.name,
                        "vram_total_mib": round(total_b / (1024 * 1024)),
                        "vram_used_mib": round(used_b / (1024 * 1024)),
                    })
                except (OSError, ValueError):
                    continue

    return {
        "hostname": uname.node or socket.gethostname(),
        "os": f"{uname.system} {uname.release} {uname.machine}",
        "cpu_model": cpu_model,
        "cpu_threads": os.cpu_count() or 1,
        "ram_total_mib": round(mem_total_kib / 1024) if mem_total_kib else None,
        "ram_available_mib": round(mem_avail_kib / 1024) if mem_avail_kib else None,
        "gpus": gpus,
    }


def _discover_skill_names(*roots: Path) -> tuple[str, ...]:
    found: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for item in sorted(root.iterdir()):
                if (item.is_dir() or item.is_symlink()) and not item.name.startswith("."):
                    if (item / "SKILL.md").is_file():
                        found.add(item.name)
        except OSError:
            continue
    return tuple(sorted(found))


def inspect_antigravity_runtime(
    *,
    worker_id: str | None = None,
    model_id: str = "Gemini 4 Argon (Medium)",
    binary_path: Path | None = None,
    skill_roots: tuple[Path, ...] | None = None,
) -> WorkerManifest:
    """Inspect the local Google Antigravity (AGY) agent runtime truthfully."""
    home = Path.home()
    resources = inspect_host_resources()
    host_id = resources["hostname"]
    wid = worker_id or f"antigravity-{host_id.lower()}"

    default_bin = binary_path or Path("/home/nightknight/.local/opt/Antigravity-x64/antigravity")
    agy_dir = home / ".gemini" / "antigravity"
    roots = skill_roots if skill_roots is not None else (
        home / ".gemini" / "config" / "skills",
        agy_dir / "builtin" / "skills",
    )
    skills = _discover_skill_names(*roots)

    bin_exists = default_bin.is_file() or shutil.which("antigravity") is not None or agy_dir.is_dir()
    status = "IDLE" if bin_exists else "OFFLINE"

    capabilities: list[str] = [
        "orbit.plan.deliver",
        "workspace.inspect",
        "workspace.read",
        "workspace.write_scoped",
        "unittest.run",
    ]
    if any(s.startswith("reverse-") for s in skills):
        capabilities.append("reverse_engineering.arsenal")

    return WorkerManifest(
        worker_id=wid,
        runtime_name="antigravity",
        host_id=host_id,
        model_id=model_id if status != "OFFLINE" else None,
        capabilities=tuple(capabilities) if status != "OFFLINE" else (),
        tool_allowlist=ANTIGRAVITY_TOOLS if status != "OFFLINE" else (),
        skills=skills,
        status=status,
        version="2.0-agy",
        heartbeat_at=time.time(),
        transport="loopback-http",
        resources=resources,
        endpoint=str(default_bin) if default_bin.is_file() else None,
        notes=f"Google Antigravity worker ({len(skills)} verified local skills; human write approval preserved)",
    )


def inspect_hermes_runtime(
    *,
    worker_id: str | None = None,
    health_url: str = "http://127.0.0.1:8642/health",
    binary_path: Path | None = None,
    client: httpx.Client | None = None,
    timeout: float = 1.5,
) -> WorkerManifest:
    """Probe the local Hermes Agent runtime (HTTP gateway on 127.0.0.1:8642 + CLI)."""
    resources = inspect_host_resources()
    host_id = resources["hostname"]
    wid = worker_id or f"hermes-{host_id.lower()}"
    hermes_bin = binary_path or Path.home() / ".local" / "bin" / "hermes"
    skills = _discover_skill_names(Path.home() / ".agents" / "skills")

    version = "unknown"
    gateway_ok = False
    http_client = client or httpx.Client(timeout=timeout)
    close_after = client is None
    try:
        resp = http_client.get(health_url, timeout=timeout)
        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("status") == "ok":
                gateway_ok = True
                version = str(payload.get("version") or "0.22.5")
    except Exception:
        gateway_ok = False
    finally:
        if close_after:
            http_client.close()

    cli_ok = hermes_bin.is_file() or shutil.which("hermes") is not None
    status = "IDLE" if (gateway_ok or cli_ok) else "OFFLINE"

    return WorkerManifest(
        worker_id=wid,
        runtime_name="hermes",
        host_id=host_id,
        model_id=None,  # Hermes is a worker runtime; model depends on active profile/provider
        capabilities=("orbit.plan.deliver", "workspace.inspect", "workspace.read", "workspace.write_scoped") if status != "OFFLINE" else (),
        tool_allowlist=("list_files", "read_file", "write_file") if status != "OFFLINE" else (),
        skills=skills[:60],
        status=status,
        version=version,
        heartbeat_at=time.time(),
        transport="loopback-http",
        resources=resources,
        endpoint=health_url if gateway_ok else (str(hermes_bin) if cli_ok else None),
        notes="Hermes local gateway live" if gateway_ok else ("Hermes CLI present (gateway offline)" if cli_ok else "Hermes runtime offline"),
    )


def inspect_opencode_runtime(
    *,
    worker_id: str | None = None,
    service_url: str = "http://127.0.0.1:49374",
    binary_path: Path | None = None,
    client: httpx.Client | None = None,
    timeout: float = 1.5,
) -> WorkerManifest:
    """Probe the local OpenCode (ALEX) worker runtime. Never mislabels OpenCode as an LLM model."""
    resources = inspect_host_resources()
    host_id = resources["hostname"]
    wid = worker_id or f"opencode-{host_id.lower()}"
    oc_bin = binary_path or Path.home() / ".local" / "bin" / "opencode"

    service_ok = False
    http_client = client or httpx.Client(timeout=timeout)
    close_after = client is None
    try:
        resp = http_client.get(f"{service_url.rstrip('/')}/doc", timeout=timeout)
        if resp.status_code == 200:
            service_ok = True
    except Exception:
        service_ok = False
    finally:
        if close_after:
            http_client.close()

    cli_ok = oc_bin.is_file() or shutil.which("opencode") is not None
    status = "IDLE" if (service_ok or cli_ok) else "OFFLINE"

    return WorkerManifest(
        worker_id=wid,
        runtime_name="opencode",
        host_id=host_id,
        model_id=None,  # OpenCode is a worker harness, not an LLM model
        capabilities=("orbit.plan.deliver", "workspace.inspect", "workspace.read", "workspace.write_scoped") if status != "OFFLINE" else (),
        tool_allowlist=("list_files", "read_file", "write_file") if status != "OFFLINE" else (),
        skills=(),
        status=status,
        version="2.2.1" if status != "OFFLINE" else "unknown",
        heartbeat_at=time.time(),
        transport="loopback-http",
        resources=resources,
        endpoint=service_url if service_ok else (str(oc_bin) if cli_ok else None),
        notes="OpenCode service live on loopback" if service_ok else ("OpenCode CLI present" if cli_ok else "OpenCode runtime offline"),
    )


def inspect_luna_runtime(
    *,
    worker_id: str = "luna-ubuntu-remote",
    host_id: str = "ubuntu-hermes",
    tunnel_health_url: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 1.5,
) -> WorkerManifest:
    """Probe LUNA runtime via authenticated SSH/Tailscale tunnel; reports OFFLINE unless verified."""
    tunnel_ok = False
    version = "unknown"
    if tunnel_health_url:
        http_client = client or httpx.Client(timeout=timeout)
        close_after = client is None
        try:
            resp = http_client.get(tunnel_health_url, timeout=timeout)
            if resp.status_code == 200:
                tunnel_ok = True
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                version = str(data.get("version") or "0.22.5")
        except Exception:
            tunnel_ok = False
        finally:
            if close_after:
                http_client.close()

    status = "IDLE" if tunnel_ok else "OFFLINE"
    return WorkerManifest(
        worker_id=worker_id,
        runtime_name="luna",
        host_id=host_id,
        model_id=None,
        capabilities=("orbit.plan.deliver", "workspace.inspect", "workspace.read") if tunnel_ok else (),
        tool_allowlist=("list_files", "read_file") if tunnel_ok else (),
        skills=(),
        status=status,
        version=version,
        heartbeat_at=time.time(),
        transport="tailscale-ssh-tunnel",
        resources={},
        endpoint=tunnel_health_url if tunnel_ok else None,
        notes="Connected via authenticated SSH/Tailscale tunnel" if tunnel_ok else "Remote tunnel not active (OFFLINE — never fake READY)",
    )


def inspect_all_runtimes(client: httpx.Client | None = None) -> list[WorkerManifest]:
    """Return truthful manifests for Antigravity, Hermes, OpenCode, and remote LUNA."""
    return [
        inspect_antigravity_runtime(),
        inspect_hermes_runtime(client=client),
        inspect_opencode_runtime(client=client),
        inspect_luna_runtime(client=client),
    ]


# ---------------------------------------------------------------------------
# Authenticated SSH / Tailscale Forwarding Specification (No Public Bind)
# ---------------------------------------------------------------------------

def build_remote_tunnel_spec(
    remote_host: str = "ubuntu-hermes",
    *,
    orbit_loopback_port: int = 8765,
    remote_loopback_port: int = 18765,
    tailscale_ip: str | None = None,
) -> dict[str, Any]:
    """Propose authenticated SSH / Tailscale loopback forwarding without exposing 0.0.0.0."""
    if not (1024 <= orbit_loopback_port <= 65535 and 1024 <= remote_loopback_port <= 65535):
        raise ValueError("Port numbers must be unprivileged (1024..65535)")
    target = tailscale_ip or remote_host
    ssh_cmd = [
        "ssh",
        "-NT",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-R", f"127.0.0.1:{remote_loopback_port}:127.0.0.1:{orbit_loopback_port}",
        target,
    ]
    return {
        "remote_host": remote_host,
        "tailscale_ip": tailscale_ip,
        "orbit_bind": f"127.0.0.1:{orbit_loopback_port}",
        "remote_worker_orbit_url": f"http://127.0.0.1:{remote_loopback_port}",
        "public_exposure": False,
        "root_required": False,
        "ssh_command": " ".join(ssh_cmd),
        "ssh_argv": ssh_cmd,
        "security_invariants": [
            "ORBIT remains bound strictly to 127.0.0.1 on NightKnightPC.",
            "Remote worker connects to its own 127.0.0.1 tunnel port forwarded over SSH/Tailscale.",
            "Short-lived single-use pairing code is still required over the tunnel.",
            "No 0.0.0.0 bind, LAN broadcast, or unauthenticated HTTP port is opened.",
        ],
    }


# ---------------------------------------------------------------------------
# Coordinator SQLite Store & Engine (Worker API v0.1 + Issue #12 Compatible)
# ---------------------------------------------------------------------------

class LocalWorkerCoordinator:
    """SQLite-backed coordinator implementing ORBIT Worker Enrollment API v0.1.

    Supports both standalone / embedded operation in ORBIT and full compatibility
    with Issue #12's `/api/workers/v0.1/{register,poll,claim,heartbeat,report}` contract.
    """

    def __init__(
        self,
        database: Path,
        default_workspace: Path,
        *,
        heartbeat_ttl: float = DEFAULT_HEARTBEAT_TTL,
        lease_ttl: float = DEFAULT_LEASE_TTL,
        pairing_ttl: float = DEFAULT_PAIRING_TTL,
        token_ttl: float = DEFAULT_TOKEN_TTL,
    ):
        self.database = Path(database)
        self.default_workspace = Path(default_workspace).resolve()
        self.heartbeat_ttl = heartbeat_ttl
        self.lease_ttl = lease_ttl
        self.pairing_ttl = pairing_ttl
        self.token_ttl = token_ttl
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA busy_timeout = 30000")
            with db:
                yield db
        finally:
            db.close()

    def _init_db(self) -> None:
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS worker_pairings (
                    pairing_id TEXT PRIMARY KEY,
                    code_sha256 TEXT NOT NULL UNIQUE,
                    code_hint TEXT NOT NULL,
                    allowed_runtime TEXT,
                    scopes_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL,
                    revoked_at REAL,
                    used_by_worker_id TEXT
                );
                CREATE TABLE IF NOT EXISTS worker_tokens (
                    token_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    token_sha256 TEXT NOT NULL UNIQUE,
                    token_hint TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE TABLE IF NOT EXISTS worker_nonces (
                    nonce_sha256 TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orbit_workers (
                    worker_id TEXT PRIMARY KEY,
                    runtime_name TEXT NOT NULL,
                    host_id TEXT NOT NULL,
                    model_id TEXT,
                    capabilities_json TEXT NOT NULL,
                    tool_allowlist_json TEXT NOT NULL,
                    skills_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    resources_json TEXT NOT NULL,
                    endpoint TEXT,
                    notes TEXT,
                    registered_at REAL NOT NULL,
                    last_heartbeat REAL NOT NULL,
                    assigned_task_id TEXT
                );
                CREATE TABLE IF NOT EXISTS worker_tasks (
                    task_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    capability_required TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    grant_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_id TEXT,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    result_json TEXT,
                    cancel_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS worker_leases (
                    lease_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    mission_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    worker_id TEXT,
                    task_id TEXT,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
            """)

    def log_event(self, kind: str, message: str, *, worker_id: str | None = None, task_id: str | None = None, now: float | None = None) -> None:
        ts = time.time() if now is None else now
        with self.connect() as db:
            db.execute(
                "INSERT INTO worker_events(worker_id, task_id, kind, message, created_at) VALUES(?,?,?,?,?)",
                (worker_id, task_id, kind, message[:500], ts),
            )

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM worker_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # --- Pairing & Token Lifecycle -----------------------------------------

    def create_pairing_code(
        self,
        *,
        allowed_runtime: str | None = None,
        scopes: list[str] | None = None,
        ttl: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Issue a short-lived single-use pairing code. Only its SHA-256 hash is persisted."""
        ts = time.time() if now is None else now
        expires = ts + (self.pairing_ttl if ttl is None else float(ttl))
        raw_code = f"orb_pair_{secrets.token_urlsafe(18)}"
        pairing_id = uuid.uuid4().hex
        code_hash = _sha256_hex(raw_code)
        hint = mask_secret(raw_code)
        scope_list = scopes or ["worker:register", "worker:heartbeat", "worker:poll", "worker:claim", "worker:report"]
        with self.connect() as db:
            db.execute(
                "INSERT INTO worker_pairings(pairing_id, code_sha256, code_hint, allowed_runtime, scopes_json, created_at, expires_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (pairing_id, code_hash, hint, allowed_runtime, json.dumps(scope_list), ts, expires),
            )
        self.log_event("pairing_issued", f"Issued pairing {pairing_id[:8]} ({hint}) runtime={allowed_runtime or '*'}", now=ts)
        return {
            "pairing_id": pairing_id,
            "pairing_code": raw_code,
            "code_hint": hint,
            "allowed_runtime": allowed_runtime,
            "scopes": scope_list,
            "created_at": ts,
            "expires_at": expires,
        }

    def revoke_pairing(self, pairing_id: str, *, now: float | None = None) -> bool:
        ts = time.time() if now is None else now
        with self.connect() as db:
            cur = db.execute(
                "UPDATE worker_pairings SET revoked_at=? WHERE pairing_id=? AND revoked_at IS NULL",
                (ts, pairing_id),
            )
        if cur.rowcount:
            self.log_event("pairing_revoked", f"Revoked pairing {pairing_id[:8]}", now=ts)
            return True
        return False

    def revoke_worker_tokens(self, worker_id: str, *, now: float | None = None) -> int:
        ts = time.time() if now is None else now
        with self.connect() as db:
            cur = db.execute(
                "UPDATE worker_tokens SET revoked_at=? WHERE worker_id=? AND revoked_at IS NULL",
                (ts, worker_id),
            )
            db.execute("UPDATE orbit_workers SET status='OFFLINE' WHERE worker_id=?", (worker_id,))
        if cur.rowcount:
            self.log_event("token_revoked", f"Revoked {cur.rowcount} token(s) for {worker_id}", worker_id=worker_id, now=ts)
        return cur.rowcount

    def _record_nonce(self, worker_id: str, nonce: str, ts: float) -> None:
        if not nonce or len(nonce.strip()) < 6:
            raise WorkerAuthError("Nonce must be at least 6 characters")
        digest = _sha256_hex(f"{worker_id}:{nonce.strip()}")
        with self.connect() as db:
            existing = db.execute("SELECT nonce_sha256 FROM worker_nonces WHERE nonce_sha256=?", (digest,)).fetchone()
            if existing:
                self.log_event("replay_blocked", f"Blocked replayed nonce from {worker_id}", worker_id=worker_id, now=ts)
                raise WorkerAuthError("Replay detected: nonce already used")
            db.execute("INSERT INTO worker_nonces(nonce_sha256, worker_id, seen_at) VALUES(?,?,?)", (digest, worker_id, ts))

    def register_worker(
        self,
        pairing_code: str,
        manifest: WorkerManifest | dict[str, Any],
        *,
        nonce: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Authenticate a one-time pairing credential + nonce, register worker, and return a scoped bearer token."""
        ts = time.time() if now is None else now
        if isinstance(manifest, dict):
            manifest = WorkerManifest.from_dict(manifest)

        if manifest.status == "OFFLINE":
            raise WorkerProtocolError(f"Runtime '{manifest.runtime_name}' is OFFLINE and cannot register as an active worker")

        if not pairing_code or not isinstance(pairing_code, str):
            raise WorkerAuthError("Missing pairing_code")

        code_hash = _sha256_hex(pairing_code.strip())
        with self.connect() as db:
            row = db.execute("SELECT * FROM worker_pairings WHERE code_sha256=?", (code_hash,)).fetchone()
            if not row:
                self.log_event("auth_failed", f"Invalid pairing code for {manifest.worker_id}", worker_id=manifest.worker_id, now=ts)
                raise WorkerAuthError("Invalid pairing credential")
            if row["revoked_at"] is not None:
                self.log_event("auth_failed", f"Revoked pairing code used by {manifest.worker_id}", worker_id=manifest.worker_id, now=ts)
                raise WorkerAuthError("Pairing credential has been revoked")
            if row["used_at"] is not None:
                self.log_event("replay_blocked", f"Replayed pairing code used by {manifest.worker_id}", worker_id=manifest.worker_id, now=ts)
                raise WorkerAuthError("Pairing credential was already consumed (replay denied)")
            if ts > float(row["expires_at"]):
                self.log_event("auth_failed", f"Expired pairing code used by {manifest.worker_id}", worker_id=manifest.worker_id, now=ts)
                raise WorkerAuthError("Pairing credential has expired")
            if row["allowed_runtime"] and row["allowed_runtime"] != manifest.runtime_name:
                raise WorkerAuthError(f"Pairing credential is scoped to runtime '{row['allowed_runtime']}', not '{manifest.runtime_name}'")

        if nonce is not None:
            self._record_nonce(manifest.worker_id, nonce, ts)

        raw_token = f"orb_wt_{secrets.token_urlsafe(24)}"
        token_hash = _sha256_hex(raw_token)
        token_hint = mask_secret(raw_token)
        token_id = uuid.uuid4().hex
        token_expires = ts + self.token_ttl
        scopes = json.loads(row["scopes_json"])

        with self.connect() as db:
            cur = db.execute(
                "UPDATE worker_pairings SET used_at=?, used_by_worker_id=? WHERE pairing_id=? AND used_at IS NULL",
                (ts, manifest.worker_id, row["pairing_id"]),
            )
            if cur.rowcount != 1:
                raise WorkerAuthError("Pairing credential race detected")
            db.execute(
                "INSERT INTO worker_tokens(token_id, worker_id, token_sha256, token_hint, scopes_json, created_at, expires_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (token_id, manifest.worker_id, token_hash, token_hint, json.dumps(scopes), ts, token_expires),
            )
            db.execute(
                """
                INSERT INTO orbit_workers(
                    worker_id, runtime_name, host_id, model_id, capabilities_json,
                    tool_allowlist_json, skills_json, status, version, transport,
                    resources_json, endpoint, notes, registered_at, last_heartbeat, assigned_task_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                ON CONFLICT(worker_id) DO UPDATE SET
                    runtime_name=excluded.runtime_name,
                    host_id=excluded.host_id,
                    model_id=excluded.model_id,
                    capabilities_json=excluded.capabilities_json,
                    tool_allowlist_json=excluded.tool_allowlist_json,
                    skills_json=excluded.skills_json,
                    status=excluded.status,
                    version=excluded.version,
                    transport=excluded.transport,
                    resources_json=excluded.resources_json,
                    endpoint=excluded.endpoint,
                    notes=excluded.notes,
                    last_heartbeat=excluded.last_heartbeat
                """,
                (
                    manifest.worker_id,
                    manifest.runtime_name,
                    manifest.host_id,
                    manifest.model_id,
                    json.dumps(list(manifest.capabilities)),
                    json.dumps(list(manifest.tool_allowlist)),
                    json.dumps(list(manifest.skills)),
                    manifest.status,
                    manifest.version,
                    manifest.transport,
                    json.dumps(manifest.resources),
                    manifest.endpoint,
                    manifest.notes,
                    ts,
                    ts,
                ),
            )

        self.log_event(
            "worker_registered",
            f"Registered worker {manifest.worker_id} (runtime={manifest.runtime_name}, host={manifest.host_id}, model={manifest.model_id or 'none'}, token={token_hint})",
            worker_id=manifest.worker_id,
            now=ts,
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "worker_id": manifest.worker_id,
            "id": manifest.worker_id,
            "worker_token": raw_token,
            "token": raw_token,
            "token_hint": token_hint,
            "scopes": scopes,
            "expires_at": token_expires,
            "heartbeat_ttl": self.heartbeat_ttl,
            "worker": self.get_worker(manifest.worker_id, now=ts),
        }

    def authenticate_worker(
        self,
        token: str | None,
        *,
        expected_worker_id: str | None = None,
        required_scope: str | None = None,
        nonce: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Validate a worker bearer token, check expiry/revocation/scope, and enforce anti-replay nonce if provided."""
        ts = time.time() if now is None else now
        if not token or not isinstance(token, str):
            raise WorkerAuthError("Missing worker authentication token")
        clean = token.strip()
        if clean.lower().startswith("bearer "):
            clean = clean[7:].strip()
        token_hash = _sha256_hex(clean)
        with self.connect() as db:
            trow = db.execute("SELECT * FROM worker_tokens WHERE token_sha256=?", (token_hash,)).fetchone()
        if not trow:
            raise WorkerAuthError("Invalid worker token")
        if trow["revoked_at"] is not None:
            raise WorkerAuthError("Worker token has been revoked")
        if ts > float(trow["expires_at"]):
            raise WorkerAuthError("Worker token has expired")
        worker_id = trow["worker_id"]
        if expected_worker_id and expected_worker_id != worker_id:
            raise WorkerAuthError(f"Token belongs to '{worker_id}', not '{expected_worker_id}'")
        scopes = json.loads(trow["scopes_json"] or "[]")
        if required_scope and required_scope not in scopes:
            raise WorkerAuthError(f"Worker token lacks required scope '{required_scope}'")
        if nonce is not None:
            self._record_nonce(worker_id, nonce, ts)
        worker = self.get_worker(worker_id, now=ts)
        if not worker:
            raise WorkerAuthError("Worker record not found")
        return worker

    # --- Liveness & Heartbeat ----------------------------------------------

    def age_workers_and_leases(self, *, now: float | None = None) -> None:
        """Deterministically transition stale workers to OFFLINE and expire stale leases."""
        ts = time.time() if now is None else now
        with self.connect() as db:
            rows = db.execute("SELECT * FROM orbit_workers WHERE status != 'OFFLINE'").fetchall()
            for r in rows:
                if ts - float(r["last_heartbeat"]) > self.heartbeat_ttl:
                    wid = r["worker_id"]
                    db.execute("UPDATE orbit_workers SET status='OFFLINE', assigned_task_id=NULL WHERE worker_id=?", (wid,))
                    # Fail or requeue active leases held by lost worker
                    active_leases = db.execute(
                        "SELECT * FROM worker_leases WHERE worker_id=? AND status='ACTIVE'", (wid,)
                    ).fetchall()
                    for lease in active_leases:
                        db.execute("UPDATE worker_leases SET status='EXPIRED' WHERE lease_id=?", (lease["lease_id"],))
                        db.execute(
                            "UPDATE worker_tasks SET status='PENDING', worker_id=NULL, updated_at=? "
                            "WHERE task_id=? AND status IN ('ASSIGNED', 'RUNNING')",
                            (ts, lease["task_id"]),
                        )
                    db.execute(
                        "INSERT INTO worker_events(worker_id, task_id, kind, message, created_at) VALUES(?,?,?,?,?)",
                        (wid, None, "heartbeat_lost", f"Worker {wid} exceeded heartbeat TTL ({self.heartbeat_ttl:.0f}s) -> OFFLINE", ts),
                    )

            expired = db.execute(
                "SELECT * FROM worker_leases WHERE status='ACTIVE' AND expires_at < ?", (ts,)
            ).fetchall()
            for lease in expired:
                db.execute("UPDATE worker_leases SET status='EXPIRED' WHERE lease_id=?", (lease["lease_id"],))
                db.execute(
                    "UPDATE worker_tasks SET status='PENDING', worker_id=NULL, updated_at=? "
                    "WHERE task_id=? AND status IN ('ASSIGNED', 'RUNNING')",
                    (ts, lease["task_id"]),
                )

    def heartbeat(
        self,
        worker_id: str,
        *,
        status: str | None = None,
        model_id: str | None = None,
        resources: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = time.time() if now is None else now
        self.age_workers_and_leases(now=ts)
        with self.connect() as db:
            row = db.execute("SELECT * FROM orbit_workers WHERE worker_id=?", (worker_id,)).fetchone()
            if not row:
                raise WorkerProtocolError(f"Unknown worker: {worker_id}")
            new_status = (status or row["status"] or "IDLE").upper()
            if new_status not in WORKER_STATES:
                raise WorkerProtocolError(f"Invalid status: {new_status}")
            if new_status == "OFFLINE":
                new_status = "IDLE"
            new_model = model_id if model_id is not None else row["model_id"]
            new_res = json.dumps(resources) if resources is not None else row["resources_json"]
            db.execute(
                "UPDATE orbit_workers SET status=?, model_id=?, resources_json=?, last_heartbeat=? WHERE worker_id=?",
                (new_status, new_model, new_res, ts, worker_id),
            )
            # Extend any active lease held by this worker
            db.execute(
                "UPDATE worker_leases SET expires_at=? WHERE worker_id=? AND status='ACTIVE'",
                (ts + self.lease_ttl, worker_id),
            )
            cancelled_rows = db.execute(
                "SELECT task_id, cancel_reason, updated_at FROM worker_tasks WHERE worker_id=? AND status='CANCELLED'",
                (worker_id,),
            ).fetchall()

        cancellations = [
            Cancel(task_id=r["task_id"], lease_id=None, reason=r["cancel_reason"] or "Cancelled", cancelled_at=r["updated_at"]).to_dict()
            for r in cancelled_rows
        ]
        return {
            "status": "ok",
            "worker": self.get_worker(worker_id, now=ts),
            "cancellations": cancellations,
            "server_time": ts,
        }

    def get_worker(self, worker_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        self.age_workers_and_leases(now=now)
        with self.connect() as db:
            row = db.execute("SELECT * FROM orbit_workers WHERE worker_id=?", (worker_id,)).fetchone()
        return self._format_worker_row(row) if row else None

    def list_workers(self, *, now: float | None = None) -> list[dict[str, Any]]:
        self.age_workers_and_leases(now=now)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM orbit_workers ORDER BY registered_at ASC").fetchall()
        return [self._format_worker_row(r) for r in rows]

    @staticmethod
    def _format_worker_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        caps = json.loads(d.pop("capabilities_json") or "[]")
        tools = json.loads(d.pop("tool_allowlist_json") or "[]")
        skills = json.loads(d.pop("skills_json") or "[]")
        resources = json.loads(d.pop("resources_json") or "{}")
        return {
            "worker_id": d["worker_id"],
            "id": d["worker_id"],
            "name": d["worker_id"],
            "runtime_name": d["runtime_name"],
            "kind": d["runtime_name"],
            "host_id": d["host_id"],
            "host": d["host_id"],
            "model_id": d["model_id"],
            "capabilities": caps,
            "tool_allowlist": tools,
            "skills": skills,
            "skill_count": len(skills),
            "status": d["status"],
            "version": d["version"],
            "transport": d["transport"],
            "resources": resources,
            "endpoint": d["endpoint"],
            "notes": d["notes"],
            "registered_at": d["registered_at"],
            "last_heartbeat": d["last_heartbeat"],
            "assigned_task_id": d["assigned_task_id"],
        }

    # --- Task Creation, Polling, Leasing, Progress, Verification, Cancellation ---

    def enqueue_task(
        self,
        title: str,
        spec: dict[str, Any],
        *,
        mission_id: str = "mission-local",
        capability_required: str = "orbit.plan.deliver",
        grant: dict[str, Any] | None = None,
        max_attempts: int = 3,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = time.time() if now is None else now
        task_id = uuid.uuid4().hex
        grant_obj = grant or {"root": str(self.default_workspace), "denied_paths": []}
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO worker_tasks(
                    task_id, mission_id, title, capability_required, spec_json,
                    grant_json, status, worker_id, attempt, max_attempts, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,1,?,?,?)
                """,
                (
                    task_id,
                    mission_id,
                    title[:160],
                    capability_required,
                    json.dumps(spec),
                    json.dumps(grant_obj),
                    "PENDING",
                    None,
                    max_attempts,
                    ts,
                    ts,
                ),
            )
        self.log_event("task_enqueued", f"Enqueued task '{title}' ({task_id[:8]}) cap={capability_required}", task_id=task_id, now=ts)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM worker_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["id"] = d["task_id"]
        d["spec"] = json.loads(d.pop("spec_json") or "{}")
        d["grant"] = json.loads(d.pop("grant_json") or "{}")
        d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
        return d

    def list_tasks(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            if mission_id:
                rows = db.execute("SELECT task_id FROM worker_tasks WHERE mission_id=? ORDER BY created_at ASC", (mission_id,)).fetchall()
            else:
                rows = db.execute("SELECT task_id FROM worker_tasks ORDER BY created_at ASC").fetchall()
        return [t for r in rows if (t := self.get_task(r["task_id"])) is not None]

    def poll_tasks(self, worker_id: str, *, now: float | None = None) -> list[dict[str, Any]]:
        """Return tasks eligible for `worker_id` based on its declared capabilities."""
        ts = time.time() if now is None else now
        self.age_workers_and_leases(now=ts)
        worker = self.get_worker(worker_id, now=ts)
        if not worker or worker["status"] == "OFFLINE":
            raise WorkerProtocolError(f"Worker {worker_id} is not online")
        caps = set(worker["capabilities"])
        with self.connect() as db:
            rows = db.execute(
                "SELECT task_id FROM worker_tasks WHERE status IN ('PENDING', 'ASSIGNED') "
                "AND (worker_id IS NULL OR worker_id=?) ORDER BY created_at ASC",
                (worker_id,),
            ).fetchall()
        offers: list[dict[str, Any]] = []
        for r in rows:
            t = self.get_task(r["task_id"])
            if not t:
                continue
            if t["capability_required"] not in caps:
                continue
            offers.append(
                TaskOffer(
                    task_id=t["task_id"],
                    mission_id=t["mission_id"],
                    title=t["title"],
                    capability_required=t["capability_required"],
                    spec=t["spec"],
                    grant=t["grant"],
                    attempt=t["attempt"],
                    created_at=t["created_at"],
                ).to_dict()
            )
        return offers

    def claim_task(self, task_id: str, worker_id: str, *, now: float | None = None) -> dict[str, Any]:
        """Atomically grant an exclusive Lease on `task_id` to `worker_id`. Prevents duplicate leases."""
        ts = time.time() if now is None else now
        self.age_workers_and_leases(now=ts)
        worker = self.get_worker(worker_id, now=ts)
        if not worker or worker["status"] == "OFFLINE":
            raise WorkerProtocolError(f"Worker {worker_id} is not online")

        with self.connect() as db:
            trow = db.execute("SELECT * FROM worker_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not trow:
                raise WorkerProtocolError(f"Task not found: {task_id}")
            if trow["capability_required"] not in set(worker["capabilities"]):
                raise WorkerProtocolError(
                    f"Worker {worker_id} lacks required capability '{trow['capability_required']}'"
                )
            # Check if an active lease already exists
            active_lease = db.execute(
                "SELECT * FROM worker_leases WHERE task_id=? AND status='ACTIVE'", (task_id,)
            ).fetchone()
            if active_lease:
                if active_lease["worker_id"] == worker_id:
                    # Idempotent re-claim by the same worker
                    return {
                        "lease": Lease(
                            lease_id=active_lease["lease_id"],
                            task_id=task_id,
                            mission_id=trow["mission_id"],
                            worker_id=worker_id,
                            acquired_at=active_lease["acquired_at"],
                            expires_at=active_lease["expires_at"],
                            status="ACTIVE",
                        ).to_dict(),
                        "task": self.get_task(task_id),
                    }
                raise WorkerProtocolError(f"Task {task_id} is already leased to worker '{active_lease['worker_id']}'")

            if trow["status"] not in ("PENDING", "ASSIGNED"):
                raise WorkerProtocolError(f"Task {task_id} is in state '{trow['status']}' and cannot be claimed")
            if trow["worker_id"] and trow["worker_id"] != worker_id:
                raise WorkerProtocolError(f"Task {task_id} is assigned to another worker '{trow['worker_id']}'")

            lease_id = uuid.uuid4().hex
            expires_at = ts + self.lease_ttl
            cur = db.execute(
                "UPDATE worker_tasks SET status='RUNNING', worker_id=?, updated_at=? "
                "WHERE task_id=? AND status IN ('PENDING', 'ASSIGNED')",
                (worker_id, ts, task_id),
            )
            if cur.rowcount != 1:
                raise WorkerProtocolError("Concurrent claim conflict")
            db.execute(
                "INSERT INTO worker_leases(lease_id, task_id, mission_id, worker_id, acquired_at, expires_at, status) "
                "VALUES(?,?,?,?,?,?,'ACTIVE')",
                (lease_id, task_id, trow["mission_id"], worker_id, ts, expires_at),
            )
            db.execute(
                "UPDATE orbit_workers SET status='BUSY', assigned_task_id=?, last_heartbeat=? WHERE worker_id=?",
                (task_id, ts, worker_id),
            )

        self.log_event("task_claimed", f"Worker {worker_id} claimed task {task_id[:8]} (lease {lease_id[:8]})", worker_id=worker_id, task_id=task_id, now=ts)
        return {
            "lease": Lease(
                lease_id=lease_id,
                task_id=task_id,
                mission_id=trow["mission_id"],
                worker_id=worker_id,
                acquired_at=ts,
                expires_at=expires_at,
                status="ACTIVE",
            ).to_dict(),
            "task": self.get_task(task_id),
        }

    def record_progress(self, progress: TaskProgress | dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        ts = time.time() if now is None else now
        if isinstance(progress, dict):
            progress = TaskProgress(
                task_id=str(progress["task_id"]),
                lease_id=str(progress.get("lease_id") or ""),
                worker_id=str(progress["worker_id"]),
                stage=str(progress.get("stage") or "RUN"),
                percent=max(0, min(100, int(progress.get("percent") or 0))),
                message=str(progress.get("message") or "")[:300],
                timestamp=ts,
            )
        with self.connect() as db:
            trow = db.execute("SELECT * FROM worker_tasks WHERE task_id=?", (progress.task_id,)).fetchone()
            if not trow:
                raise WorkerProtocolError(f"Task not found: {progress.task_id}")
            if trow["status"] == "CANCELLED":
                return {
                    "status": "cancelled",
                    "cancel": Cancel(
                        task_id=progress.task_id,
                        lease_id=progress.lease_id or None,
                        reason=trow["cancel_reason"] or "Task cancelled",
                        cancelled_at=trow["updated_at"],
                    ).to_dict(),
                }
            if trow["worker_id"] != progress.worker_id:
                raise WorkerProtocolError("Progress worker_id does not match task owner")
            db.execute("UPDATE orbit_workers SET last_heartbeat=? WHERE worker_id=?", (ts, progress.worker_id))
            if progress.lease_id:
                db.execute(
                    "UPDATE worker_leases SET expires_at=? WHERE lease_id=? AND status='ACTIVE'",
                    (ts + self.lease_ttl, progress.lease_id),
                )

        self.log_event(
            "task_progress",
            f"[{progress.percent}%] {progress.stage}: {progress.message}",
            worker_id=progress.worker_id,
            task_id=progress.task_id,
            now=ts,
        )
        return {"status": "ok", "cancel": None}

    def cancel_task(self, task_id: str, *, reason: str = "Cancelled by coordinator", now: float | None = None) -> dict[str, Any]:
        ts = time.time() if now is None else now
        with self.connect() as db:
            trow = db.execute("SELECT * FROM worker_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not trow:
                raise WorkerProtocolError(f"Task not found: {task_id}")
            wid = trow["worker_id"]
            db.execute(
                "UPDATE worker_tasks SET status='CANCELLED', cancel_reason=?, updated_at=? WHERE task_id=?",
                (reason[:300], ts, task_id),
            )
            db.execute(
                "UPDATE worker_leases SET status='CANCELLED' WHERE task_id=? AND status='ACTIVE'",
                (task_id,),
            )
            if wid:
                db.execute(
                    "UPDATE orbit_workers SET status='IDLE', assigned_task_id=NULL WHERE worker_id=? AND assigned_task_id=?",
                    (wid, task_id),
                )
        self.log_event("task_cancelled", f"Cancelled task {task_id[:8]}: {reason}", worker_id=wid, task_id=task_id, now=ts)
        return Cancel(task_id=task_id, lease_id=None, reason=reason, cancelled_at=ts).to_dict()

    def _verify_task_artifacts(self, task: dict[str, Any]) -> tuple[bool, str]:
        """Server-side verification of task output against workspace files (never trusts worker text alone)."""
        spec = task["spec"]
        grant = task["grant"]
        root = Path(grant.get("root") or self.default_workspace).resolve()
        denied = [str(p).rstrip("/") for p in (grant.get("denied_paths") or [])]
        op = str(spec.get("op") or "deliver")
        rel_path = str(spec.get("path") or "").strip()

        if rel_path:
            for d in denied:
                if rel_path == d or rel_path.startswith(d + "/"):
                    return False, f"GRANT DENIED: '{rel_path}' is in denied_paths"
            try:
                target = checked_path(root, rel_path)
            except (PermissionError, ValueError) as exc:
                return False, f"PATH REJECTED: {exc}"
        else:
            target = root

        if op in ("deliver", "retry-deliver", "workspace.write_scoped"):
            if not target.is_file():
                return False, f"MISSING ARTIFACT: {rel_path}"
            data = target.read_bytes()
            text = data.decode("utf-8", errors="replace")
            for needle in spec.get("must_contain") or []:
                if str(needle) not in text:
                    return False, f"CONTENT MISMATCH: {rel_path} missing required string {needle!r}"
            expected_sha = spec.get("expected_sha256")
            actual_sha = hashlib.sha256(data).hexdigest()
            if expected_sha and actual_sha != str(expected_sha).lower():
                return False, f"SHA256 MISMATCH: expected {expected_sha[:12]}..., got {actual_sha[:12]}..."
            return True, f"VERIFIED FILE: {rel_path} (sha256={actual_sha[:16]}..., {len(data)} bytes)"

        if op in ("inspect", "workspace.inspect"):
            if not target.exists():
                return False, f"MISSING TARGET: {rel_path}"
            return True, f"VERIFIED INSPECT: {rel_path or '.'} exists under {root.name}"

        if op in ("read", "workspace.read"):
            if not target.is_file():
                return False, f"MISSING FILE: {rel_path}"
            return True, f"VERIFIED READ: {rel_path} ({target.stat().st_size} bytes)"

        return False, f"UNSUPPORTED TASK OP: {op}"

    def report_result(
        self,
        result: TaskResult | dict[str, Any],
        *,
        worker_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Record worker submission and run independent server-side artifact verification."""
        ts = time.time() if now is None else now
        if isinstance(result, dict):
            wid = str(worker_id or result.get("worker_id") or "")
            raw_artifacts = result.get("artifacts") or []
            norm_artifacts = [
                {"path": a} if isinstance(a, str) else dict(a)
                for a in raw_artifacts
                if isinstance(a, (str, dict))
            ]
            result = TaskResult(
                task_id=str(result.get("task_id") or ""),
                lease_id=str(result.get("lease_id") or ""),
                worker_id=wid,
                status=str(result.get("status") or "SUBMITTED").upper(),
                summary=str(result.get("summary") or result.get("notes") or "submitted"),
                artifacts=norm_artifacts,
                logs=list(result.get("logs") or []),
                completed_at=ts,
            )

        with self.connect() as db:
            trow = db.execute("SELECT * FROM worker_tasks WHERE task_id=?", (result.task_id,)).fetchone()
            if not trow:
                raise WorkerProtocolError(f"Task not found: {result.task_id}")
            if trow["status"] == "CANCELLED":
                raise WorkerCancelledError(f"Task {result.task_id} was cancelled and cannot accept results")
            if trow["worker_id"] != result.worker_id:
                raise WorkerProtocolError(f"Task {result.task_id} is assigned to '{trow['worker_id']}', not '{result.worker_id}'")
            if trow["status"] not in ("RUNNING", "SUBMITTED"):
                raise WorkerProtocolError(f"Task {result.task_id} is in state '{trow['status']}', not RUNNING")

            if result.lease_id:
                lrow = db.execute("SELECT * FROM worker_leases WHERE lease_id=?", (result.lease_id,)).fetchone()
                if not lrow or lrow["status"] != "ACTIVE":
                    raise WorkerProtocolError("Lease is no longer active")

        task_dict = self.get_task(result.task_id)
        assert task_dict is not None

        if result.status in ("BLOCKED",):
            final_status = "BLOCKED"
            verify_msg = result.summary
        elif result.status in ("FAILED", "DEGRADED"):
            final_status = "FAILED"
            verify_msg = result.summary
        else:
            ok, verify_msg = self._verify_task_artifacts(task_dict)
            final_status = "VERIFIED" if ok else "FAILED"

        payload_json = json.dumps({
            **result.to_dict(),
            "verified": final_status == "VERIFIED",
            "verification_evidence": verify_msg,
        })

        with self.connect() as db:
            db.execute(
                "UPDATE worker_tasks SET status=?, result_json=?, updated_at=? WHERE task_id=?",
                (final_status, payload_json, ts, result.task_id),
            )
            db.execute(
                "UPDATE worker_leases SET status=? WHERE task_id=? AND status='ACTIVE'",
                ("COMPLETED" if final_status == "VERIFIED" else "FAILED", result.task_id),
            )
            next_worker_status = "DEGRADED" if result.status == "DEGRADED" else "IDLE"
            db.execute(
                "UPDATE orbit_workers SET status=?, assigned_task_id=NULL, last_heartbeat=? WHERE worker_id=?",
                (next_worker_status, ts, result.worker_id),
            )

        self.log_event(
            "task_reported",
            f"Task {result.task_id[:8]} -> {final_status}: {verify_msg}",
            worker_id=result.worker_id,
            task_id=result.task_id,
            now=ts,
        )
        return self.get_task(result.task_id)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# FastAPI Router for ORBIT Worker API v0.1
# ---------------------------------------------------------------------------

class PairingCreateRequest(BaseModel):
    allowed_runtime: str | None = None
    scopes: list[str] | None = None
    ttl: float = Field(default=DEFAULT_PAIRING_TTL, ge=5.0, le=3600.0)


class RegisterRequest(BaseModel):
    pairing_code: str = Field(min_length=6, max_length=200)
    nonce: str | None = Field(default=None, max_length=120)
    manifest: dict[str, Any] | None = None
    # Compatibility fields for flat registration payloads
    worker_id: str | None = None
    name: str | None = None
    runtime_name: str | None = None
    kind: str | None = None
    host_id: str | None = None
    host: str | None = None
    model_id: str | None = None
    capabilities: list[str] | None = None
    tool_allowlist: list[str] | None = None
    skills: list[str] | None = None
    status: str = "IDLE"
    version: str = "0.1.0"


class HeartbeatRequest(BaseModel):
    worker_id: str | None = None
    worker_token: str | None = None
    status: str | None = None
    model_id: str | None = None
    resources: dict[str, Any] | None = None


class PollRequest(BaseModel):
    worker_id: str | None = None
    worker_token: str | None = None


class ClaimRequest(BaseModel):
    task_id: str | None = None
    worker_id: str | None = None
    worker_token: str | None = None


class ProgressRequest(BaseModel):
    task_id: str
    lease_id: str = ""
    worker_id: str | None = None
    worker_token: str | None = None
    stage: str = "RUN"
    percent: int = Field(default=50, ge=0, le=100)
    message: str = Field(default="working", max_length=300)


class ReportRequest(BaseModel):
    task_id: str | None = None
    lease_id: str = ""
    worker_id: str | None = None
    worker_token: str | None = None
    status: str = "SUBMITTED"
    summary: str = ""
    notes: str = ""
    artifacts: list[Any] = Field(default_factory=list)
    artifact_details: list[dict[str, Any]] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)


class RevokeRequest(BaseModel):
    worker_id: str | None = None
    pairing_id: str | None = None
    worker_token: str | None = None


class EnqueueTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    mission_id: str = "mission-local"
    capability_required: str = "orbit.plan.deliver"
    spec: dict[str, Any]
    grant: dict[str, Any] | None = None


class CancelTaskRequest(BaseModel):
    task_id: str
    reason: str = "Cancelled by operator"


def _extract_token(authorization: str | None, x_worker_token: str | None, body_token: str | None = None) -> str | None:
    if x_worker_token:
        return x_worker_token
    if authorization:
        return authorization
    return body_token


def create_worker_router(coordinator: LocalWorkerCoordinator) -> APIRouter:
    """Create the versioned `/api/workers/v0.1` FastAPI router."""
    router = APIRouter()

    @router.get("/fleet")
    @router.get("")
    def get_fleet():
        return {
            "protocol_version": PROTOCOL_VERSION,
            "workers": coordinator.list_workers(),
            "tasks": coordinator.list_tasks(),
            "events": coordinator.events(limit=50),
        }

    @router.post("/pairings", status_code=201)
    def issue_pairing(payload: PairingCreateRequest):
        return coordinator.create_pairing_code(
            allowed_runtime=payload.allowed_runtime,
            scopes=payload.scopes,
            ttl=payload.ttl,
        )

    @router.post("/revoke")
    @router.post("/unregister")
    def revoke_credentials(
        payload: RevokeRequest,
        authorization: str | None = Header(default=None),
        x_orbit_worker_token: str | None = Header(default=None),
    ):
        revoked_tokens = 0
        revoked_pairing = False
        tok = _extract_token(authorization, x_orbit_worker_token, payload.worker_token)
        if tok and not payload.worker_id:
            try:
                w = coordinator.authenticate_worker(tok)
                revoked_tokens = coordinator.revoke_worker_tokens(w["worker_id"])
            except WorkerAuthError:
                pass
        if payload.worker_id:
            revoked_tokens = coordinator.revoke_worker_tokens(payload.worker_id)
        if payload.pairing_id:
            revoked_pairing = coordinator.revoke_pairing(payload.pairing_id)
        return {"revoked": bool(revoked_tokens or revoked_pairing), "revoked_tokens": revoked_tokens, "revoked_pairing": revoked_pairing}

    @router.post("/register", status_code=201)
    def register(payload: RegisterRequest, x_orbit_nonce: str | None = Header(default=None)):
        try:
            manifest_dict = payload.manifest or {
                "worker_id": payload.worker_id or payload.name or f"worker-{uuid.uuid4().hex[:8]}",
                "runtime_name": payload.runtime_name or payload.kind or "antigravity",
                "host_id": payload.host_id or payload.host or socket.gethostname(),
                "model_id": payload.model_id,
                "capabilities": payload.capabilities or ["orbit.plan.deliver"],
                "tool_allowlist": payload.tool_allowlist or [],
                "skills": payload.skills or [],
                "status": payload.status,
                "version": payload.version,
            }
            return coordinator.register_worker(
                payload.pairing_code,
                manifest_dict,
                nonce=payload.nonce or x_orbit_nonce,
            )
        except WorkerAuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        except (WorkerProtocolError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/heartbeat")
    def heartbeat(
        payload: HeartbeatRequest,
        authorization: str | None = Header(default=None),
        x_orbit_worker_token: str | None = Header(default=None),
        x_orbit_nonce: str | None = Header(default=None),
    ):
        try:
            worker = coordinator.authenticate_worker(
                _extract_token(authorization, x_orbit_worker_token, payload.worker_token),
                expected_worker_id=payload.worker_id,
                required_scope="worker:heartbeat",
                nonce=x_orbit_nonce,
            )
            res = coordinator.heartbeat(
                worker["worker_id"],
                status=payload.status,
                model_id=payload.model_id,
                resources=payload.resources,
            )
            offers = coordinator.poll_tasks(worker["worker_id"])
            res["assigned_task"] = offers[0] if offers else None
            return res
        except WorkerAuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        except WorkerProtocolError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/poll")
    @router.post("/poll")
    def poll(
        payload: PollRequest | None = None,
        authorization: str | None = Header(default=None),
        x_orbit_worker_token: str | None = Header(default=None),
        x_orbit_nonce: str | None = Header(default=None),
    ):
        try:
            body_tok = payload.worker_token if payload else None
            worker = coordinator.authenticate_worker(
                _extract_token(authorization, x_orbit_worker_token, body_tok),
                required_scope="worker:poll",
                nonce=x_orbit_nonce,
            )
            offers = coordinator.poll_tasks(worker["worker_id"])
            return {
                "worker_id": worker["worker_id"],
                "offers": offers,
                "tasks": offers,
                "assigned_task": offers[0] if offers else None,
            }
        except WorkerAuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        except WorkerProtocolError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/claim")
    def claim(
        payload: ClaimRequest,
        authorization: str | None = Header(default=None),
        x_orbit_worker_token: str | None = Header(default=None),
        x_orbit_nonce: str | None = Header(default=None),
    ):
        try:
            worker = coordinator.authenticate_worker(
                _extract_token(authorization, x_orbit_worker_token, payload.worker_token),
                expected_worker_id=payload.worker_id,
                required_scope="worker:claim",
                nonce=x_orbit_nonce,
            )
            task_id = payload.task_id
            if not task_id:
                offers = coordinator.poll_tasks(worker["worker_id"])
                if not offers:
                    raise WorkerProtocolError("no ASSIGNED/PENDING task for this worker")
                task_id = offers[0]["task_id"]
            claimed = coordinator.claim_task(task_id, worker["worker_id"])
            return {"claimed": True, **claimed}
        except WorkerAuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        except WorkerProtocolError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/progress")
    def progress(
        payload: ProgressRequest,
        authorization: str | None = Header(default=None),
        x_orbit_worker_token: str | None = Header(default=None),
        x_orbit_nonce: str | None = Header(default=None),
    ):
        try:
            worker = coordinator.authenticate_worker(
                _extract_token(authorization, x_orbit_worker_token, payload.worker_token),
                expected_worker_id=payload.worker_id,
                required_scope="worker:report",
                nonce=x_orbit_nonce,
            )
            return coordinator.record_progress({
                "task_id": payload.task_id,
                "lease_id": payload.lease_id,
                "worker_id": worker["worker_id"],
                "stage": payload.stage,
                "percent": payload.percent,
                "message": payload.message,
            })
        except WorkerAuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        except WorkerProtocolError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/report")
    def report(
        payload: ReportRequest,
        authorization: str | None = Header(default=None),
        x_orbit_worker_token: str | None = Header(default=None),
        x_orbit_nonce: str | None = Header(default=None),
    ):
        try:
            worker = coordinator.authenticate_worker(
                _extract_token(authorization, x_orbit_worker_token, payload.worker_token),
                expected_worker_id=payload.worker_id,
                required_scope="worker:report",
                nonce=x_orbit_nonce,
            )
            task_id = payload.task_id or worker.get("assigned_task_id")
            if not task_id:
                raise WorkerProtocolError("Missing task_id and worker has no active assigned_task_id")
            artifacts = payload.artifact_details or payload.artifacts
            reported = coordinator.report_result(
                {
                    "task_id": task_id,
                    "lease_id": payload.lease_id,
                    "worker_id": worker["worker_id"],
                    "status": payload.status,
                    "summary": payload.summary or payload.notes,
                    "artifacts": artifacts,
                    "logs": payload.logs,
                },
                worker_id=worker["worker_id"],
            )
            return {"reported": True, "task": reported, **reported}
        except WorkerAuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        except WorkerCancelledError as exc:
            raise HTTPException(409, str(exc)) from exc
        except WorkerProtocolError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/tasks", status_code=201)
    def enqueue_task(payload: EnqueueTaskRequest):
        return coordinator.enqueue_task(
            payload.title,
            payload.spec,
            mission_id=payload.mission_id,
            capability_required=payload.capability_required,
            grant=payload.grant,
        )

    @router.post("/cancel")
    def cancel(payload: CancelTaskRequest):
        try:
            return coordinator.cancel_task(payload.task_id, reason=payload.reason)
        except WorkerProtocolError as exc:
            raise HTTPException(404, str(exc)) from exc

    return router


def mount_worker_bridge(app: FastAPI, coordinator: LocalWorkerCoordinator) -> None:
    """Mount both `/api/workers/v0.1` and `/api/workers` on a FastAPI app."""
    router = create_worker_router(coordinator)
    app.include_router(router, prefix="/api/workers/v0.1", tags=["workers-v0.1"])
    app.include_router(router, prefix="/api/workers", tags=["workers"])
    app.state.worker_coordinator = coordinator


# ---------------------------------------------------------------------------
# Authenticated Worker Adapter Client (Antigravity / Hermes / OpenCode / LUNA)
# ---------------------------------------------------------------------------

class OrbitWorkerAdapter:
    """Client adapter connecting a real local agent runtime to the ORBIT coordinator."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        manifest: WorkerManifest | None = None,
        *,
        client: httpx.Client | None = None,
        api_prefix: str = "/api/workers/v0.1",
        default_workspace: Path | None = None,
        model_available_check: Callable[[str], bool] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_prefix = "/" + api_prefix.strip("/")
        self.manifest = manifest or inspect_antigravity_runtime()
        self._client = client or httpx.Client(base_url=self.base_url, timeout=10.0)
        self._owns_client = client is None
        self.default_workspace = (default_workspace or Path.home() / "Projects").resolve()
        self.worker_token: str | None = None
        self.token_hint: str = "<unpaired>"
        self.registered_worker_id: str = self.manifest.worker_id
        self.cancelled_tasks: set[str] = set()
        self.model_available_check = model_available_check

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _url(self, path: str) -> str:
        rel = f"{self.api_prefix}/{path.lstrip('/')}"
        if self._client.base_url and str(self._client.base_url).rstrip("/"):
            return rel
        return f"{self.base_url}{rel}"

    def _headers(self, *, use_nonce: bool = True) -> dict[str, str]:
        if not self.worker_token:
            raise WorkerAuthError("Adapter is not paired yet (no worker_token)")
        headers = {"Authorization": f"Bearer {self.worker_token}"}
        if use_nonce:
            headers["X-Orbit-Nonce"] = secrets.token_hex(12)
        return headers

    def pair_and_register(self, pairing_code: str, *, nonce: str | None = None) -> dict[str, Any]:
        """Exchange a short-lived pairing code for a scoped worker bearer token."""
        if self.manifest.status == "OFFLINE":
            raise WorkerProtocolError(f"Cannot register OFFLINE runtime '{self.manifest.runtime_name}'")
        chosen_nonce = nonce or secrets.token_hex(12)
        # Issue #12 fleet.py ALLOWED_KINDS = ("mock", "hermes", "opencode", "custom")
        compat_kind = self.manifest.runtime_name if self.manifest.runtime_name in ("mock", "hermes", "opencode") else "custom"
        payload = {
            "pairing_code": pairing_code,
            "nonce": chosen_nonce,
            "manifest": self.manifest.to_dict(),
            "worker_id": self.manifest.worker_id,
            "name": self.manifest.worker_id[:64],
            "runtime_name": self.manifest.runtime_name,
            "kind": compat_kind,
            "host_id": self.manifest.host_id,
            "host": self.manifest.host_id[:120],
            "model_id": self.manifest.model_id,
            "capabilities": list(self.manifest.capabilities)[:24],
            "tool_allowlist": list(self.manifest.tool_allowlist),
            "skills": list(self.manifest.skills),
            "status": self.manifest.status,
            "version": self.manifest.version[:40],
        }
        resp = self._client.post(self._url("register"), json=payload)
        if resp.status_code in (401, 403):
            raise WorkerAuthError(resp.json().get("detail", "Authentication failed"))
        if resp.status_code >= 400:
            raise WorkerProtocolError(resp.json().get("detail", f"Registration failed ({resp.status_code})"))
        data = resp.json()
        token = data.get("worker_token") or data.get("token")
        if not token:
            raise WorkerProtocolError("Coordinator response did not include worker_token")
        self.worker_token = str(token)
        self.token_hint = mask_secret(self.worker_token)
        if data.get("worker_id"):
            self.registered_worker_id = str(data["worker_id"])
        return data

    def send_heartbeat(self, *, status: str | None = None) -> dict[str, Any]:
        """Send authenticated liveness heartbeat and record any cancellation signals."""
        headers = self._headers()
        payload = {
            "worker_id": self.registered_worker_id,
            "worker_token": self.worker_token,
            "status": status or self.manifest.status,
            "model_id": self.manifest.model_id,
            "resources": inspect_host_resources(),
        }
        resp = self._client.post(self._url("heartbeat"), json=payload, headers=headers)
        if resp.status_code in (401, 403):
            raise WorkerAuthError(resp.json().get("detail", "Unauthorized heartbeat"))
        if resp.status_code >= 400:
            raise WorkerProtocolError(resp.json().get("detail", f"Heartbeat failed ({resp.status_code})"))
        data = resp.json()
        for c in data.get("cancellations") or []:
            tid = c.get("task_id")
            if tid:
                self.cancelled_tasks.add(str(tid))
        return data

    def poll_offers(self) -> list[dict[str, Any]]:
        """Poll coordinator for eligible tasks matching this worker's declared capabilities."""
        headers = self._headers()
        resp = self._client.post(
            self._url("poll"),
            json={"worker_id": self.registered_worker_id, "worker_token": self.worker_token},
            headers=headers,
        )
        if resp.status_code in (401, 403):
            raise WorkerAuthError(resp.json().get("detail", "Unauthorized poll"))
        if resp.status_code >= 400:
            raise WorkerProtocolError(resp.json().get("detail", f"Poll failed ({resp.status_code})"))
        data = resp.json()
        offers = list(data.get("offers") or data.get("tasks") or [])
        if not offers and data.get("assigned_task"):
            offers = [data["assigned_task"]]
        return offers

    def claim(self, task_id: str) -> dict[str, Any]:
        """Acquire an exclusive lease on `task_id`."""
        headers = self._headers()
        resp = self._client.post(
            self._url("claim"),
            json={
                "task_id": task_id,
                "worker_id": self.registered_worker_id,
                "worker_token": self.worker_token,
            },
            headers=headers,
        )
        if resp.status_code in (401, 403):
            raise WorkerAuthError(resp.json().get("detail", "Unauthorized claim"))
        if resp.status_code >= 400:
            raise WorkerProtocolError(resp.json().get("detail", f"Claim denied ({resp.status_code})"))
        return resp.json()

    def send_progress(self, task_id: str, lease_id: str, *, stage: str, percent: int, message: str) -> dict[str, Any]:
        """Send task progress event and check for mid-flight cancellation."""
        headers = self._headers()
        resp = self._client.post(
            self._url("progress"),
            json={
                "task_id": task_id,
                "lease_id": lease_id,
                "worker_id": self.registered_worker_id,
                "worker_token": self.worker_token,
                "stage": stage,
                "percent": percent,
                "message": message,
            },
            headers=headers,
        )
        if resp.status_code == 404:
            # Issue #12 minimal router does not expose /progress; fall back to /heartbeat
            return self.send_heartbeat(status="LIVE")
        if resp.status_code in (401, 403):
            raise WorkerAuthError(resp.json().get("detail", "Unauthorized progress"))
        if resp.status_code >= 400:
            raise WorkerProtocolError(resp.json().get("detail", f"Progress failed ({resp.status_code})"))
        data = resp.json()
        if data.get("status") == "cancelled" or data.get("cancel"):
            self.cancelled_tasks.add(task_id)
            reason = (data.get("cancel") or {}).get("reason") or "Task cancelled"
            raise WorkerCancelledError(reason)
        return data

    def submit_result(self, result: TaskResult) -> dict[str, Any]:
        """Report task completion or failure to coordinator."""
        headers = self._headers()
        artifact_paths = [
            str(a.get("path")) if isinstance(a, dict) and a.get("path") else str(a)
            for a in result.artifacts
        ]
        payload = {
            **result.to_dict(),
            "worker_id": self.registered_worker_id,
            "worker_token": self.worker_token,
            "notes": result.summary,
            "artifacts": artifact_paths,
            "artifact_details": result.artifacts,
        }
        resp = self._client.post(self._url("report"), json=payload, headers=headers)
        if resp.status_code in (401, 403):
            raise WorkerAuthError(resp.json().get("detail", "Unauthorized report"))
        if resp.status_code == 409:
            raise WorkerCancelledError(resp.json().get("detail", "Task cancelled"))
        if resp.status_code >= 400:
            raise WorkerProtocolError(resp.json().get("detail", f"Report failed ({resp.status_code})"))
        return resp.json()

    def unregister(self) -> dict[str, Any]:
        """Revoke this worker's active token cleanly."""
        headers = self._headers()
        resp = self._client.post(
            self._url("unregister"),
            json={"worker_id": self.registered_worker_id, "worker_token": self.worker_token},
            headers=headers,
        )
        if resp.status_code >= 400:
            raise WorkerProtocolError(resp.json().get("detail", f"Unregister failed ({resp.status_code})"))
        return resp.json()

    def execute_scoped_task(
        self,
        task: dict[str, Any],
        lease: dict[str, Any],
        *,
        on_step_hook: Callable[[], None] | None = None,
    ) -> TaskResult:
        """Execute a single leased task strictly within its MissionGrant without arbitrary shell execution."""
        task_id = str(task.get("task_id") or task.get("id"))
        lease_id = str(lease.get("lease_id") or "")
        spec = dict(task.get("spec") or {})
        grant = dict(task.get("grant") or {})
        if not grant and task.get("mission_id"):
            try:
                m_url = f"/api/missions/{task['mission_id']}"
                if not (self._client.base_url and str(self._client.base_url).rstrip("/")):
                    m_url = f"{self.base_url}{m_url}"
                m_resp = self._client.get(m_url)
                if m_resp.status_code == 200:
                    m_data = m_resp.json()
                    if isinstance(m_data.get("grant"), dict):
                        grant = dict(m_data["grant"])
            except Exception:
                pass
        logs: list[str] = [f"Worker {self.registered_worker_id} ({self.manifest.runtime_name}) starting task {task_id[:8]}"]

        if task_id in self.cancelled_tasks:
            raise WorkerCancelledError("Task was cancelled prior to execution")

        # 1. Check model requirement if specified by task
        required_model = spec.get("require_model")
        if required_model:
            is_avail = (
                self.model_available_check(str(required_model))
                if self.model_available_check is not None
                else (self.manifest.model_id == required_model)
            )
            if not is_avail:
                msg = f"MODEL_UNAVAILABLE: required model '{required_model}' is not online on {self.registered_worker_id}"
                logs.append(msg)
                return TaskResult(
                    task_id=task_id,
                    lease_id=lease_id,
                    worker_id=self.registered_worker_id,
                    status="DEGRADED",
                    summary=msg,
                    artifacts=[],
                    logs=logs,
                )

        # 2. Enforce operation allowlist (never execute arbitrary shell commands)
        op = str(spec.get("op") or "deliver").strip()
        if op not in ALLOWED_TASK_OPS:
            msg = f"GRANT DENIED: operation '{op}' is not in worker allowlist (arbitrary shell/command execution prohibited)"
            logs.append(msg)
            return TaskResult(
                task_id=task_id,
                lease_id=lease_id,
                worker_id=self.registered_worker_id,
                status="BLOCKED",
                summary=msg,
                artifacts=[],
                logs=logs,
            )

        # 3. Validate workspace grant root and path confinement
        root_str = grant.get("root") or spec.get("workspace_root") or str(self.default_workspace)
        root = Path(root_str).expanduser().resolve()
        rel_path = str(spec.get("path") or ".").strip()
        denied_paths = [str(p).rstrip("/") for p in (grant.get("denied_paths") or [])]

        for denied in denied_paths:
            if rel_path == denied or rel_path.startswith(denied + "/"):
                msg = f"GRANT DENIED: path '{rel_path}' is explicitly excluded by mission grant"
                logs.append(msg)
                return TaskResult(
                    task_id=task_id,
                    lease_id=lease_id,
                    worker_id=self.registered_worker_id,
                    status="BLOCKED",
                    summary=msg,
                    artifacts=[],
                    logs=logs,
                )

        try:
            target = checked_path(root, rel_path) if rel_path != "." else root
        except (PermissionError, ValueError) as exc:
            msg = f"GRANT DENIED: {exc}"
            logs.append(msg)
            return TaskResult(
                task_id=task_id,
                lease_id=lease_id,
                worker_id=self.registered_worker_id,
                status="BLOCKED",
                summary=msg,
                artifacts=[],
                logs=logs,
            )

        self.send_progress(task_id, lease_id, stage="PREPARE", percent=25, message=f"Validated grant path {rel_path}")
        if on_step_hook:
            on_step_hook()
        if task_id in self.cancelled_tasks:
            raise WorkerCancelledError("Task cancelled during preparation")

        # 4. Perform the allowlisted non-destructive operation
        if op in ("inspect", "workspace.inspect"):
            if not target.exists():
                return TaskResult(
                    task_id=task_id,
                    lease_id=lease_id,
                    worker_id=self.registered_worker_id,
                    status="FAILED",
                    summary=f"Target path does not exist: {rel_path}",
                    artifacts=[],
                    logs=logs,
                )
            entries = sorted(p.name for p in target.iterdir() if p.name not in DENIED_NAMES)[:50] if target.is_dir() else [target.name]
            self.send_progress(task_id, lease_id, stage="VERIFY", percent=90, message=f"Inspected {len(entries)} entries")
            logs.append(f"Inspected {rel_path}: {len(entries)} item(s)")
            return TaskResult(
                task_id=task_id,
                lease_id=lease_id,
                worker_id=self.registered_worker_id,
                status="SUBMITTED",
                summary=f"Inspected {rel_path} ({len(entries)} visible entries)",
                artifacts=[{"path": rel_path, "entries": entries}],
                logs=logs,
            )

        if op in ("read", "workspace.read"):
            if not target.is_file():
                return TaskResult(
                    task_id=task_id,
                    lease_id=lease_id,
                    worker_id=self.registered_worker_id,
                    status="FAILED",
                    summary=f"File not found: {rel_path}",
                    artifacts=[],
                    logs=logs,
                )
            digest = _file_sha256(target)
            size_b = target.stat().st_size
            self.send_progress(task_id, lease_id, stage="VERIFY", percent=90, message=f"Read {rel_path} ({size_b}B)")
            return TaskResult(
                task_id=task_id,
                lease_id=lease_id,
                worker_id=self.registered_worker_id,
                status="SUBMITTED",
                summary=f"Read {rel_path} (sha256={digest[:16]}...)",
                artifacts=[{"path": rel_path, "sha256": digest, "size_bytes": size_b}],
                logs=logs,
            )

        # op in ('deliver', 'retry-deliver', 'workspace.write_scoped')
        needles = [str(n) for n in (spec.get("must_contain") or []) if n]
        explicit_content = spec.get("content")
        if isinstance(explicit_content, str) and explicit_content:
            content = explicit_content
            for n in needles:
                if n not in content:
                    content = content.rstrip("\n") + f"\n{n}\n"
        else:
            lines = [
                f"# ORBIT Verified Artifact — {rel_path}",
                f"worker_id: {self.registered_worker_id}",
                f"runtime_name: {self.manifest.runtime_name}",
                f"host_id: {self.manifest.host_id}",
                f"model_id: {self.manifest.model_id or 'none'}",
                f"task_id: {task_id}",
            ]
            for n in needles:
                lines.append(str(n))
            content = "\n".join(lines) + "\n"

        if len(content) > MAX_WRITE_CHARS:
            return TaskResult(
                task_id=task_id,
                lease_id=lease_id,
                worker_id=self.registered_worker_id,
                status="BLOCKED",
                summary=f"Content exceeds MAX_WRITE_CHARS ({MAX_WRITE_CHARS})",
                artifacts=[],
                logs=logs,
            )

        self.send_progress(task_id, lease_id, stage="WRITE", percent=65, message=f"Writing scoped artifact {rel_path}")
        if on_step_hook:
            on_step_hook()
        if task_id in self.cancelled_tasks:
            raise WorkerCancelledError("Task cancelled before atomic write commit")

        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".orbit-worker-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

        digest = _file_sha256(target)
        size_b = target.stat().st_size
        self.send_progress(task_id, lease_id, stage="VERIFY", percent=95, message=f"Computed artifact sha256={digest[:16]}...")
        logs.append(f"Delivered {rel_path} ({size_b} bytes, sha256={digest})")
        return TaskResult(
            task_id=task_id,
            lease_id=lease_id,
            worker_id=self.registered_worker_id,
            status="SUBMITTED",
            summary=f"Delivered {rel_path} ({size_b} bytes, sha256={digest[:16]}...)",
            artifacts=[{"path": rel_path, "sha256": digest, "size_bytes": size_b}],
            logs=logs,
        )

    def run_once(self, *, on_step_hook: Callable[[], None] | None = None) -> dict[str, Any] | None:
        """Send heartbeat, poll for one eligible task, claim lease, execute within grant, and report result."""
        beat = self.send_heartbeat(status="IDLE")
        offers = self.poll_offers()
        if not offers and beat.get("assigned_task"):
            offers = [beat["assigned_task"]]
        if not offers:
            return None
        offer = offers[0]
        task_id = str(offer.get("task_id") or offer.get("id"))
        claimed = self.claim(task_id)
        lease = claimed.get("lease") or {"lease_id": ""}
        task_obj = dict(claimed.get("task") or offer)
        if "grant" not in task_obj and "grant" in offer:
            task_obj["grant"] = offer["grant"]
        try:
            result = self.execute_scoped_task(task_obj, lease, on_step_hook=on_step_hook)
        except WorkerCancelledError as exc:
            return {"task_id": task_id, "status": "CANCELLED", "reason": str(exc)}
        return self.submit_result(result)

