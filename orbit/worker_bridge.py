"""Unified ORBIT Worker Protocol (orbit.workers.v0.1) Broker & Antigravity Adapter.

Reconciles PR #14 (Antigravity / host runtime discovery) and PR #15 (Grok
`orbit.workers.v0.1` broker) into a single canonical wire protocol and shared
SQLite broker (`WorkerBroker`) mounted at `/api/workers`.

Security invariants:
1. Loopback-only bind (`127.0.0.1` / `localhost`). Remote hosts must use
   authenticated SSH / Tailscale loopback forwarding.
2. Operator endpoints (`POST /api/workers/pairing`, `/revoke`, `/tasks`,
   `/tasks/{id}/cancel`) require a scoped Operator bearer token
   (`Authorization: Bearer <operator_token>` or `X-Orbit-Operator-Token`).
   Worker bearer tokens are rejected with 403 on operator endpoints, and
   operator tokens are rejected with 403 on worker endpoints. Requests carrying
   proxy/forwarding headers (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`) are
   denied on operator routes.
3. Worker endpoints (`POST /api/workers/heartbeat`, `/poll`,
   `/tasks/{id}/progress`, `/tasks/{id}/result`) require a paired worker bearer
   token plus a unique per-request `X-Orbit-Nonce` and `X-Orbit-Timestamp`
   within the clock skew window. Nonce replay detection is atomic in SQLite
   (concurrent collisions return 409, never 500).
4. Pairing codes, worker tokens, and operator tokens are stored exclusively as
   SHA-256 digests in SQLite. Secret/state files on disk are created with mode
   `0600` via `os.open(..., O_CREAT | O_EXCL | O_WRONLY, 0o600)` before writing
   any bytes.
5. Runtime discovery (`GET /api/workers/discovery`) strictly separates observed
   local processes/tools from connected, authenticated workers with verified
   ORBIT-executable task handlers.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

PROTOCOL = "orbit.workers.v0.1"
PROTOCOL_VERSION = PROTOCOL
ADAPTER_VERSION = "0.1.0"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "testserver"})
WORKER_STATUSES = frozenset({"IDLE", "BUSY", "ERROR", "BLOCKED", "OFFLINE"})
RESULT_STATUSES = frozenset({"completed", "rejected", "cancelled", "error"})
PROGRESS_STATUSES = frozenset({"accepted", "running", "cancelling"})
TERMINAL_TASK_STATUSES = frozenset({"completed", "rejected", "cancelled", "error"})

EXECUTABLE_CAPABILITIES = ("orbit.inventory", "orbit.ping", "orbit.smoke_check")

ANTIGRAVITY_OBSERVED_TOOLS = (
    "run_command",
    "view_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "grep_search",
    "find_by_name",
    "list_dir",
    "ask_question",
    "search_web",
    "read_url_content",
    "invoke_subagent",
    "schedule",
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_CAP_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_GRANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv"}
_FORWARDED_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host")


class ProtocolError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class WorkerClientError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"ORBIT worker request failed ({status}): {detail}")
        self.status = status
        self.detail = detail


class ManualClock:
    """Injectable deterministic clock for unit and integration tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def assert_loopback_host(host: str) -> str:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("worker broker refuses a non-loopback bind")
    return "127.0.0.1"


def assert_loopback_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("ORBIT_URL must be an http loopback origin")
    if parsed.hostname not in {"127.0.0.1", "localhost", "testserver"}:
        raise ValueError("ORBIT_URL must be loopback; use an SSH local forward for a remote host")
    if parsed.path not in {"", "/"}:
        raise ValueError("ORBIT_URL must be an origin")
    return url.rstrip("/")


def scrub(text: str, secrets_list: list[str] | None = None) -> str:
    cleaned = _BEARER_RE.sub("Bearer [redacted]", text or "")
    for item in secrets_list or []:
        if item:
            cleaned = cleaned.replace(item, "[redacted]")
    return cleaned


def bounded_json(value: Any, limit: int = 16000) -> str:
    try:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(400, "value is not JSON") from exc
    if len(raw.encode("utf-8")) > limit:
        raise ProtocolError(400, "JSON value is too large")
    return raw


def valid_nonce(value: str | None) -> str:
    if not value or not _NONCE_RE.fullmatch(value):
        raise ProtocolError(401, "nonce rejected")
    return value


def write_secret_file(path: Path, content: str) -> None:
    """Create a secret file with 0600 permissions before writing any bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_secret_file(path: Path) -> str:
    """Read a secret file after verifying it is not group/world accessible."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Secret file not found: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"Refusing to read secret file with insecure permissions ({oct(mode)}): {path}")
    return path.read_text(encoding="utf-8").strip()


def save_state(path: Path, data: dict[str, Any]) -> None:
    write_secret_file(Path(path), json.dumps(data, indent=2, sort_keys=True))


def load_state(path: Path) -> dict[str, Any]:
    raw = read_secret_file(Path(path))
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Canonical orbit.workers.v0.1 Pydantic Models & Dataclasses
# ---------------------------------------------------------------------------


class Manifest(BaseModel):
    worker_id: str = Field(pattern=_ID_RE.pattern)
    runtime_name: str = Field(min_length=1, max_length=80)
    host_id: str = Field(min_length=1, max_length=80)
    model_id: str | None = Field(default=None, max_length=120)
    model_id_source: str | None = Field(default=None, max_length=80)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    status: str = "IDLE"
    version: str = Field(default=ADAPTER_VERSION, min_length=1, max_length=40)
    inventory: dict[str, Any] = Field(default_factory=dict)

    @field_validator("status")
    @classmethod
    def known_status(cls, value: str) -> str:
        if value not in WORKER_STATUSES:
            raise ValueError("unknown worker status")
        return value

    @field_validator("capabilities")
    @classmethod
    def known_caps(cls, value: list[str]) -> list[str]:
        for item in value:
            if not _CAP_RE.fullmatch(item):
                raise ValueError("invalid capability")
        return value


class PairRequest(BaseModel):
    protocol: str = PROTOCOL
    pairing_code: str = Field(min_length=8, max_length=200)
    manifest: Manifest


class HeartbeatRequest(BaseModel):
    manifest: Manifest


class TaskCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    task_type: str = Field(alias="type", pattern=_CAP_RE.pattern)
    payload: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list, max_length=64)
    grant: str = Field(default="local", pattern=_GRANT_RE.pattern)
    task_id: str | None = Field(default=None, pattern=_ID_RE.pattern)

    @field_validator("required_capabilities")
    @classmethod
    def known_required(cls, value: list[str]) -> list[str]:
        for item in value:
            if not _CAP_RE.fullmatch(item):
                raise ValueError("invalid capability")
        return value


class ProgressRequest(BaseModel):
    lease_id: str = Field(min_length=8, max_length=80)
    event_id: str = Field(pattern=_ID_RE.pattern)
    status: str
    message: str = Field(default="", max_length=400)
    progress: float = Field(ge=0.0, le=1.0)

    @field_validator("status")
    @classmethod
    def known_progress(cls, value: str) -> str:
        if value not in PROGRESS_STATUSES:
            raise ValueError("unknown progress status")
        return value


class ResultRequest(BaseModel):
    lease_id: str = Field(min_length=8, max_length=80)
    status: str
    result: dict[str, Any] = Field(default_factory=dict)
    log_excerpt: str = Field(default="", max_length=2000)

    @field_validator("status")
    @classmethod
    def known_result(cls, value: str) -> str:
        if value not in RESULT_STATUSES:
            raise ValueError("unknown result status")
        return value


class RevokeRequest(BaseModel):
    worker_id: str = Field(pattern=_ID_RE.pattern)


@dataclass
class WorkerManifest:
    """Dataclass representation of a runtime manifest and discovery state."""

    worker_id: str
    runtime_name: str
    host_id: str
    model_id: str | None
    capabilities: list[str]
    status: str
    version: str = ADAPTER_VERSION
    model_id_source: str | None = "harness_session_identity"
    inventory: dict[str, Any] = field(default_factory=dict)
    discovered: bool = True
    connected: bool = False
    authorized_to_execute: bool = False

    def to_protocol_manifest(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "runtime_name": self.runtime_name,
            "host_id": self.host_id,
            "model_id": self.model_id,
            "model_id_source": self.model_id_source,
            "capabilities": list(self.capabilities),
            "status": self.status if self.status in WORKER_STATUSES else "IDLE",
            "version": self.version,
            "inventory": dict(self.inventory),
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskOffer:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    required_capabilities: list[str] = field(default_factory=list)
    grant: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Lease:
    lease_id: str
    task_id: str
    expires_at: str
    cancel_requested: bool
    offer: TaskOffer

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "task_id": self.task_id,
            "expires_at": self.expires_at,
            "cancel_requested": self.cancel_requested,
            "offer": self.offer.to_dict(),
        }


@dataclass
class TaskProgress:
    lease_id: str
    event_id: str
    status: str
    message: str
    progress: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskResult:
    lease_id: str
    status: str
    result: dict[str, Any] = field(default_factory=dict)
    log_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Cancel:
    task_id: str
    reason: str
    requested_at: str = field(default_factory=lambda: isoformat(utcnow()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Truthful Host & Runtime Discovery Probes
# ---------------------------------------------------------------------------


def scan_skill_names(roots: list[Path], limit: int = 200) -> list[str]:
    """Return skill directory names without reading SKILL.md file contents."""
    names: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        resolved = root.resolve()
        if not resolved.is_dir():
            continue
        for current, dirs, files in os.walk(resolved, followlinks=False):
            kept = []
            for name in dirs:
                path = Path(current) / name
                if name in _SKIP_DIRS or name.startswith("."):
                    continue
                if path.is_symlink():
                    target = Path(os.path.realpath(path))
                    if target.is_dir() and (target / "SKILL.md").is_file():
                        relative = Path(current, name).relative_to(resolved).as_posix()
                        names.append(relative)
                    continue
                kept.append(name)
            dirs[:] = kept
            if "SKILL.md" in files:
                relative = Path(current).relative_to(resolved).as_posix()
                names.append("." if relative == "." else relative)
    return sorted(set(names))[:limit]


def default_antigravity_skill_roots() -> list[Path]:
    home = Path.home()
    return [
        home / ".gemini" / "config" / "skills",
        home / ".gemini" / "antigravity" / "builtin" / "skills",
    ]


def inspect_host_resources() -> dict[str, Any]:
    """Read real CPU, RAM, and DRM GPU VRAM metrics from the local Linux host."""
    cpu_model = platform.processor() or "unknown"
    mem_total_mib = 0
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
        for line in cpuinfo.splitlines():
            if line.lower().startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass

    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore")
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                mem_total_mib = kb // 1024
                break
    except (OSError, ValueError, IndexError):
        pass

    gpus: list[dict[str, Any]] = []
    drm_root = Path("/sys/class/drm")
    if drm_root.is_dir():
        for card_dir in sorted(drm_root.glob("card[0-9]*")):
            if "-" in card_dir.name:
                continue
            vram_file = card_dir / "device" / "mem_info_vram_total"
            if vram_file.is_file():
                try:
                    vram_bytes = int(vram_file.read_text(encoding="utf-8").strip())
                    gpus.append(
                        {
                            "card": card_dir.name,
                            "vram_mib": vram_bytes // (1024 * 1024),
                        }
                    )
                except (OSError, ValueError):
                    pass

    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu_model": cpu_model,
        "cpu_threads": os.cpu_count() or 1,
        "ram_total_mib": mem_total_mib,
        "gpus": gpus,
    }


def _http_json_probe(url: str, timeout: float = 1.5) -> tuple[bool, dict[str, Any] | None]:
    parsed = urlsplit(url)
    if parsed.hostname not in LOOPBACK_HOSTS:
        return False, None
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                raw = resp.read(16384).decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw)
                    return True, data if isinstance(data, dict) else {"raw": raw[:200]}
                except json.JSONDecodeError:
                    return True, {"raw": raw[:200]}
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    return False, None


def inspect_hermes_runtime(health_url: str = "http://127.0.0.1:8642/health") -> dict[str, Any]:
    """Probe local Hermes gateway presence without marking it ORBIT-connected."""
    ok, payload = _http_json_probe(health_url)
    version = str(payload.get("version") or "0.22.5") if ok and isinstance(payload, dict) else "unknown"
    return {
        "runtime_name": "hermes",
        "discovered": ok,
        "probe_status": "OBSERVED_LOCAL" if ok else "OFFLINE",
        "connected": False,
        "authorized_to_execute": False,
        "executable_capabilities": [],
        "endpoint": health_url,
        "version": version,
    }


def inspect_opencode_runtime(doc_url: str | None = None) -> dict[str, Any]:
    """Probe local OpenCode service presence without marking it ORBIT-connected."""
    binary = shutil.which("opencode") or (
        str(Path.home() / ".opencode" / "bin" / "opencode")
        if (Path.home() / ".opencode" / "bin" / "opencode").is_file()
        else None
    )
    target_url = doc_url or os.getenv("OPENCODE_DOC_URL", "http://127.0.0.1:49374/doc")
    ok, _ = _http_json_probe(target_url)
    return {
        "runtime_name": "opencode",
        "discovered": bool(ok or binary),
        "probe_status": "OBSERVED_LOCAL" if ok else ("BINARY_ONLY" if binary else "OFFLINE"),
        "connected": False,
        "authorized_to_execute": False,
        "executable_capabilities": [],
        "endpoint": target_url,
        "binary_present": bool(binary),
    }


def inspect_luna_runtime(tunnel_url: str | None = None) -> dict[str, Any]:
    """Probe optional SSH/Tailscale loopback tunnel for remote Ubuntu/detti LUNA."""
    target_url = tunnel_url or os.getenv("ORBIT_LUNA_TUNNEL_URL", "http://127.0.0.1:18765/health")
    ok, _ = _http_json_probe(target_url, timeout=0.8)
    return {
        "runtime_name": "luna",
        "discovered": ok,
        "probe_status": "OBSERVED_TUNNEL" if ok else "OFFLINE",
        "connected": False,
        "authorized_to_execute": False,
        "executable_capabilities": [],
        "endpoint": target_url,
        "transport": "ssh-tailscale-loopback-forward",
    }


def build_antigravity_inventory(
    *,
    skill_roots: list[Path] | None = None,
    host_id: str | None = None,
    binary_path: Path | None = None,
    model_id: str = "Gemini 4 Argon (Medium)",
) -> dict[str, Any]:
    """Build truthful Antigravity inventory separating observed tools from ORBIT handlers."""
    roots = default_antigravity_skill_roots() if skill_roots is None else skill_roots
    discovered_skills = scan_skill_names(roots, limit=10000)
    skill_names = discovered_skills[:200]
    host_res = inspect_host_resources()
    resolved_host = host_id or host_res["hostname"]

    ag_bin = binary_path or (Path.home() / ".local" / "opt" / "Antigravity-x64" / "antigravity")
    ag_present = ag_bin.is_file() or bool(shutil.which("antigravity") or shutil.which("agy"))

    inventory: dict[str, Any] = {
        "runtime_name": "antigravity",
        "adapter_version": ADAPTER_VERSION,
        "binary_present": ag_present,
        "model_id": model_id if ag_present else None,
        "model_id_source": "harness_session_identity" if ag_present else None,
        "model_endpoint_probed": False,
        "model_ready": False,
        "host_id": resolved_host,
        "os": platform.system(),
        "python": platform.python_version(),
        "executable_capabilities": list(EXECUTABLE_CAPABILITIES) if ag_present else [],
        "arbitrary_shell_from_dashboard": False,
        "observed_session_tools": list(ANTIGRAVITY_OBSERVED_TOOLS) if ag_present else [],
        "observed_session_tools_executable_through_orbit": False,
        "observed_session_tools_source": "antigravity_session_2026-10-31",
        "mcp_tools_invoked": False,
        "skill_count": len(discovered_skills),
        "skill_names": skill_names,
        "skill_names_truncated": len(discovered_skills) > len(skill_names),
        "skills_executable_through_orbit": False,
        "host_resources": host_res,
        "discovered_sibling_runtimes": {
            "hermes": inspect_hermes_runtime(),
            "opencode": inspect_opencode_runtime(),
            "luna": inspect_luna_runtime(),
        },
    }
    if len(json.dumps(inventory).encode("utf-8")) > 12000:
        inventory["skill_names"] = skill_names[:40]
        inventory["skill_names_truncated"] = True
    return inventory


def inspect_antigravity_runtime(
    *,
    worker_id: str | None = None,
    model_id: str = "Gemini 4 Argon (Medium)",
    skill_roots: list[Path] | None = None,
    binary_path: Path | None = None,
) -> WorkerManifest:
    """Inspect the local Antigravity runtime.

    Note: `connected` and `authorized_to_execute` remain False until the worker
    pairs and sends a live heartbeat to the ORBIT WorkerBroker.
    """
    inv = build_antigravity_inventory(
        skill_roots=skill_roots,
        binary_path=binary_path,
        model_id=model_id,
    )
    ag_present = bool(inv.get("binary_present"))
    host = str(inv.get("host_id") or "localhost")
    safe_host = "".join(ch if ch.isalnum() or ch in ".-" else "-" for ch in host)[:40] or "host"
    return WorkerManifest(
        worker_id=worker_id or f"antigravity-{safe_host}",
        runtime_name="antigravity",
        host_id=host[:80],
        model_id=inv.get("model_id"),
        model_id_source=inv.get("model_id_source"),
        capabilities=list(EXECUTABLE_CAPABILITIES) if ag_present else [],
        status="IDLE" if ag_present else "OFFLINE",
        version=ADAPTER_VERSION,
        inventory=inv,
        discovered=ag_present,
        connected=False,
        authorized_to_execute=False,
    )


def manifest_from_inventory(
    inventory: dict[str, Any],
    worker_id: str | None = None,
    capabilities: list[str] | None = None,
    runtime_name: str | None = None,
) -> dict[str, Any]:
    host = str(inventory.get("host_id") or socket.gethostname() or "localhost")
    safe_host = "".join(ch if ch.isalnum() or ch in ".-" else "-" for ch in host)[:40] or "host"
    rt_name = runtime_name or str(inventory.get("runtime_name") or "antigravity")
    caps = list(capabilities) if capabilities is not None else list(inventory.get("executable_capabilities") or EXECUTABLE_CAPABILITIES)
    status = "IDLE" if inventory.get("binary_present", True) else "OFFLINE"
    return {
        "worker_id": worker_id or f"{rt_name}-{safe_host}",
        "runtime_name": rt_name,
        "host_id": host[:80],
        "model_id": inventory.get("model_id"),
        "model_id_source": inventory.get("model_id_source"),
        "capabilities": caps,
        "status": status,
        "version": str(inventory.get("adapter_version") or ADAPTER_VERSION),
        "inventory": inventory,
    }


def build_remote_tunnel_spec(
    remote_host: str = "ubuntu-hermes",
    remote_user: str = "nightknight",
    local_orbit_port: int = 8765,
    remote_loopback_port: int = 18765,
) -> dict[str, Any]:
    """Generate an authenticated SSH/Tailscale loopback tunnel specification.

    Important: Forwarding only exposes worker routes (`/api/workers/pair`,
    `/heartbeat`, `/poll`, `/tasks/{id}/progress`, `/tasks/{id}/result`).
    Operator routes require a scoped operator bearer token and reject forwarded
    headers.
    """
    ssh_cmd = [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-R",
        f"127.0.0.1:{remote_loopback_port}:127.0.0.1:{local_orbit_port}",
        f"{remote_user}@{remote_host}",
    ]
    return {
        "transport": "ssh-over-tailscale-loopback",
        "local_bind": f"127.0.0.1:{local_orbit_port}",
        "remote_loopback_bind": f"127.0.0.1:{remote_loopback_port}",
        "public_exposure": False,
        "operator_privileged_over_tunnel": False,
        "command": " ".join(ssh_cmd),
        "argv": ssh_cmd,
    }


# ---------------------------------------------------------------------------
# Unified WorkerBroker (orbit.workers.v0.1 + Scoped Operator Authentication)
# ---------------------------------------------------------------------------

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
CREATE TABLE IF NOT EXISTS operator_tokens (
    token_hash TEXT PRIMARY KEY,
    label TEXT NOT NULL,
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


def default_database() -> Path:
    return Path.home() / ".local" / "share" / "nightknight-orbit" / "worker-broker.sqlite3"


class WorkerBroker:
    """Canonical `orbit.workers.v0.1` loopback coordinator with scoped operator auth."""

    def __init__(
        self,
        database: Path,
        clock: Any | None = None,
        *,
        operator_token: str | None = None,
        operator_token_path: Path | None = None,
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

        self.operator_token_path = Path(
            operator_token_path or self.database.with_suffix(".operator.token")
        )
        self.operator_token = self._bootstrap_operator_token(operator_token)
        import atexit

        atexit.register(self.close)

    def _bootstrap_operator_token(self, explicit_token: str | None) -> str:
        env_token = os.environ.get("ORBIT_OPERATOR_TOKEN", "").strip()
        env_file = os.environ.get("ORBIT_OPERATOR_TOKEN_FILE", "").strip()
        token = explicit_token or env_token
        if not token and env_file:
            token = read_secret_file(Path(env_file))
        if not token and self.operator_token_path.is_file():
            try:
                token = read_secret_file(self.operator_token_path)
            except (PermissionError, OSError):
                token = None
        if not token:
            token = f"orb_op_{secrets.token_urlsafe(32)}"
            write_secret_file(self.operator_token_path, token + "\n")
        else:
            write_secret_file(self.operator_token_path, token + "\n")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO operator_tokens(token_hash, label, created_at, revoked_at)
                VALUES (?, 'bootstrap', ?, NULL)
                ON CONFLICT(token_hash) DO UPDATE SET revoked_at = NULL
                """,
                (digest(token), isoformat(self.now())),
            )
            self._conn.commit()
        return token

    def issue_operator_token(self, label: str = "operator") -> str:
        with self._lock:
            token = f"orb_op_{secrets.token_urlsafe(32)}"
            self._conn.execute(
                "INSERT INTO operator_tokens(token_hash, label, created_at, revoked_at) VALUES (?, ?, ?, NULL)",
                (digest(token), label[:80], isoformat(self.now())),
            )
            self._conn.commit()
            return token

    def revoke_operator_token(self, token: str) -> None:
        with self._lock:
            updated = self._conn.execute(
                "UPDATE operator_tokens SET revoked_at = ? WHERE token_hash = ?",
                (isoformat(self.now()), digest(token)),
            )
            self._conn.commit()
            if updated.rowcount == 0:
                raise ProtocolError(404, "operator token not found")

    def authenticate_operator(
        self,
        authorization: str | None,
        operator_header: str | None = None,
        headers: Any | None = None,
    ) -> sqlite3.Row:
        with self._lock:
            if headers is not None:
                for hdr in _FORWARDED_HEADERS:
                    if headers.get(hdr):
                        raise ProtocolError(403, "operator endpoints refuse forwarded requests")
            raw_token: str | None = None
            if operator_header and operator_header.strip():
                raw_token = operator_header.strip()
            elif authorization:
                raw_token = _bearer(authorization)
            if not raw_token or " " in raw_token:
                raise ProtocolError(401, "operator credentials required")
            token_hash = digest(raw_token)
            worker_row = self._conn.execute(
                "SELECT worker_id FROM workers WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if worker_row is not None:
                raise ProtocolError(403, "worker credentials forbidden on operator endpoint")
            op_row = self._conn.execute(
                "SELECT * FROM operator_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if op_row is None or op_row["revoked_at"] is not None:
                raise ProtocolError(401, "operator credentials rejected")
            return op_row

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            self._conn.close()
            self._conn = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now()
        return utcnow()

    def create_pairing(self) -> dict[str, str]:
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
                self._event(
                    moment,
                    worker_id,
                    None,
                    "paired" if previous is None else "token_rotated",
                    "token stored as hash",
                )
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

    def authenticate(
        self,
        authorization: str | None,
        nonce: str | None,
        timestamp: str | None,
    ) -> sqlite3.Row:
        with self._lock:
            token = _bearer(authorization)
            token_hash = digest(token)
            op_row = self._conn.execute(
                "SELECT token_hash FROM operator_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if op_row is not None:
                raise ProtocolError(403, "operator credentials forbidden on worker endpoint")
            moment = self.now()
            row = self._conn.execute(
                "SELECT * FROM workers WHERE token_hash = ?", (token_hash,)
            ).fetchone()
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
            checked_nonce = valid_nonce(nonce)
            cutoff = isoformat(moment - timedelta(seconds=self.timestamp_skew * 2))
            self._conn.execute("DELETE FROM nonces WHERE seen_at < ?", (cutoff,))
            try:
                self._conn.execute(
                    "INSERT INTO nonces(token_hash, nonce, seen_at) VALUES (?, ?, ?)",
                    (token_hash, checked_nonce, isoformat(moment)),
                )
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise ProtocolError(409, "nonce replay") from exc
            self._conn.commit()
            return row

    def heartbeat(self, worker: sqlite3.Row, manifest: Manifest) -> dict[str, Any]:
        with self._lock:
            self._require_same_worker(worker, manifest.worker_id)
            moment = self.now()
            self._sweep(moment)
            self._write_manifest(manifest, moment)
            self._extend_leases(manifest.worker_id, moment)
            self._conn.commit()
            fresh = self._conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (manifest.worker_id,)
            ).fetchone()
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
            leased = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (chosen["task_id"],)
            ).fetchone()
            return self._poll_body(worker["worker_id"], leased)

    def progress(self, worker: sqlite3.Row, task_id: str, body: ProgressRequest) -> dict[str, str]:
        with self._lock:
            task = self._owned_lease(worker, task_id, body.lease_id)
            moment = self.now()
            self._event(
                moment,
                worker["worker_id"],
                task_id,
                "progress",
                scrub(body.message)[:400] or body.status,
            )
            self._conn.execute(
                "UPDATE tasks SET updated_at = ?, lease_expires = ? WHERE task_id = ?",
                (
                    isoformat(moment),
                    isoformat(moment + timedelta(seconds=self.lease_ttl)),
                    task["task_id"],
                ),
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
                (
                    task_id,
                    body.task_type,
                    payload,
                    json.dumps(required),
                    body.grant,
                    isoformat(moment),
                    isoformat(moment),
                ),
            )
            self._event(moment, None, task_id, "queued", body.task_type)
            self._conn.commit()
            return {
                "protocol": PROTOCOL,
                "task_id": task_id,
                "status": "queued",
                "required_capabilities": required,
            }

    def cancel(self, task_id: str) -> dict[str, str]:
        with self._lock:
            task = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None:
                raise ProtocolError(404, "task not found")
            moment = self.now()
            if task["status"] in TERMINAL_TASK_STATUSES:
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
            row = self._conn.execute(
                "SELECT worker_id FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if row is None:
                raise ProtocolError(404, "worker not found")
            self._conn.execute(
                "UPDATE workers SET revoked_at = ?, status = 'OFFLINE' WHERE worker_id = ?",
                (isoformat(moment), worker_id),
            )
            self._release_worker_tasks(worker_id, moment)
            self._event(moment, worker_id, None, "revoked", "token rejected from now")
            self._conn.commit()
            return {"ok": "true", "worker_id": worker_id, "presence": "OFFLINE"}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            moment = self.now()
            self._sweep(moment)
            self._conn.commit()
            workers = [
                self._public_worker(row, moment)
                for row in self._conn.execute("SELECT * FROM workers ORDER BY worker_id")
            ]
            tasks = [
                self._public_task(row)
                for row in self._conn.execute("SELECT * FROM tasks ORDER BY created_at, task_id")
            ]
            events = [
                {
                    "ts": row["ts"],
                    "worker_id": row["worker_id"],
                    "task_id": row["task_id"],
                    "kind": row["kind"],
                }
                for row in self._conn.execute(
                    "SELECT ts, worker_id, task_id, kind FROM events ORDER BY id DESC LIMIT 30"
                )
            ]
            return {
                "protocol": PROTOCOL,
                "server_time": isoformat(moment),
                "workers": workers,
                "tasks": tasks,
                "events": events,
            }

    def discovery_snapshot(self) -> dict[str, Any]:
        """Return local host runtime discovery correlated with live paired workers.

        Never reports a discovered runtime as `connected` or `authorized_to_execute`
        unless a non-revoked worker for that runtime has `presence == 'LIVE'`.
        """
        snap = self.snapshot()
        live_by_runtime: dict[str, list[dict[str, Any]]] = {}
        for w in snap["workers"]:
            if w.get("presence") == "LIVE" and not w.get("revoked"):
                live_by_runtime.setdefault(w["runtime_name"], []).append(w)

        ag_manifest = inspect_antigravity_runtime()
        hermes_probe = inspect_hermes_runtime()
        opencode_probe = inspect_opencode_runtime()
        luna_probe = inspect_luna_runtime()

        runtimes: dict[str, dict[str, Any]] = {
            "antigravity": {
                "runtime_name": "antigravity",
                "discovered": ag_manifest.discovered,
                "probe_status": "OBSERVED_LOCAL" if ag_manifest.discovered else "OFFLINE",
                "connected": bool(live_by_runtime.get("antigravity")),
                "authorized_to_execute": bool(live_by_runtime.get("antigravity")),
                "registered_workers": [w["worker_id"] for w in live_by_runtime.get("antigravity", [])],
                "executable_capabilities": (
                    live_by_runtime["antigravity"][0]["capabilities"]
                    if live_by_runtime.get("antigravity")
                    else []
                ),
                "observed_session_tools_executable_through_orbit": False,
            },
            "hermes": {
                **hermes_probe,
                "connected": bool(live_by_runtime.get("hermes")),
                "authorized_to_execute": bool(live_by_runtime.get("hermes")),
                "registered_workers": [w["worker_id"] for w in live_by_runtime.get("hermes", [])],
                "executable_capabilities": (
                    live_by_runtime["hermes"][0]["capabilities"]
                    if live_by_runtime.get("hermes")
                    else []
                ),
            },
            "opencode": {
                **opencode_probe,
                "connected": bool(live_by_runtime.get("opencode")),
                "authorized_to_execute": bool(live_by_runtime.get("opencode")),
                "registered_workers": [w["worker_id"] for w in live_by_runtime.get("opencode", [])],
                "executable_capabilities": (
                    live_by_runtime["opencode"][0]["capabilities"]
                    if live_by_runtime.get("opencode")
                    else []
                ),
            },
            "luna": {
                **luna_probe,
                "connected": bool(live_by_runtime.get("luna")),
                "authorized_to_execute": bool(live_by_runtime.get("luna")),
                "registered_workers": [w["worker_id"] for w in live_by_runtime.get("luna", [])],
                "executable_capabilities": (
                    live_by_runtime["luna"][0]["capabilities"]
                    if live_by_runtime.get("luna")
                    else []
                ),
            },
        }
        for rt_name, workers_list in live_by_runtime.items():
            if rt_name not in runtimes:
                runtimes[rt_name] = {
                    "runtime_name": rt_name,
                    "discovered": True,
                    "probe_status": "CONNECTED_WORKER",
                    "connected": True,
                    "authorized_to_execute": bool(workers_list[0].get("capabilities")),
                    "registered_workers": [w["worker_id"] for w in workers_list],
                    "executable_capabilities": workers_list[0].get("capabilities", []),
                }
        return {
            "protocol": PROTOCOL,
            "server_time": snap["server_time"],
            "host_resources": inspect_host_resources(),
            "runtimes": runtimes,
        }

    def worker_detail(self, worker_id: str) -> dict[str, Any]:
        with self._lock:
            moment = self.now()
            row = self._conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if row is None:
                raise ProtocolError(404, "worker not found")
            public = self._public_worker(row, moment)
            public["inventory"] = json.loads(row["inventory_json"])
            return public

    def task_detail(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ProtocolError(404, "task not found")
            public = self._public_task(row)
            public["payload"] = json.loads(row["payload_json"])
            public["log_excerpt"] = row["log_excerpt"]
            return public

    def _upsert_worker(self, manifest: Manifest, token_hash: str, moment: datetime) -> None:
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

    def _write_manifest(self, manifest: Manifest, moment: datetime) -> None:
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

    def _extend_leases(self, worker_id: str, moment: datetime) -> None:
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
        if task["status"] in TERMINAL_TASK_STATUSES:
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

    def _deny_once(self, moment: datetime, task: sqlite3.Row, worker_id: str, missing: set[str]) -> None:
        inserted = self._conn.execute(
            "INSERT OR IGNORE INTO capability_denials(task_id, worker_id) VALUES (?, ?)",
            (task["task_id"], worker_id),
        )
        if inserted.rowcount == 1:
            detail = "missing " + ",".join(sorted(missing))
            self._event(moment, worker_id, task["task_id"], "capability_denied", detail[:400])

    def _sweep(self, moment: datetime) -> None:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status IN ('leased', 'cancel_requested')"
        ).fetchall()
        for task in rows:
            worker = self._conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (task["worker_id"],)
            ).fetchone()
            stale = (
                worker is None
                or worker["revoked_at"] is not None
                or self._heartbeat_stale(worker, moment)
            )
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
                self._event(
                    moment,
                    task["worker_id"],
                    task["task_id"],
                    "lease_released",
                    "heartbeat lost or lease expired",
                )

    def _release_worker_tasks(self, worker_id: str, moment: datetime) -> None:
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

    def _heartbeat_stale(self, worker: sqlite3.Row, moment: datetime) -> bool:
        if worker["last_heartbeat"] is None:
            return True
        age = (moment - parse_time(worker["last_heartbeat"])).total_seconds()
        return age > self.offline_after

    def _presence(self, worker: sqlite3.Row, moment: datetime) -> str:
        if (
            worker["revoked_at"] is not None
            or worker["status"] == "OFFLINE"
            or self._heartbeat_stale(worker, moment)
        ):
            return "OFFLINE"
        return "LIVE"

    def _public_worker(self, row: sqlite3.Row, moment: datetime) -> dict[str, Any]:
        inventory = json.loads(row["inventory_json"])
        presence = self._presence(row, moment)
        caps = json.loads(row["capabilities_json"])
        return {
            "worker_id": row["worker_id"],
            "runtime_name": row["runtime_name"],
            "host_id": row["host_id"],
            "model_id": row["model_id"],
            "model_id_source": row["model_id_source"],
            "capabilities": caps,
            "status": "OFFLINE" if row["revoked_at"] else row["status"],
            "presence": presence,
            "connected": presence == "LIVE",
            "authorized_to_execute": presence == "LIVE" and bool(caps),
            "version": row["version"],
            "last_heartbeat": row["last_heartbeat"],
            "revoked": row["revoked_at"] is not None,
            "inventory_summary": {
                "arbitrary_shell_from_dashboard": inventory.get("arbitrary_shell_from_dashboard"),
                "model_ready": inventory.get("model_ready"),
                "model_endpoint_probed": inventory.get("model_endpoint_probed"),
                "observed_session_tools_executable_through_orbit": inventory.get(
                    "observed_session_tools_executable_through_orbit", False
                ),
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

    def _event(
        self,
        moment: datetime,
        worker_id: str | None,
        task_id: str | None,
        kind: str,
        detail: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO events(ts, worker_id, task_id, kind, detail) VALUES (?, ?, ?, ?, ?)",
            (isoformat(moment), worker_id, task_id, kind, scrub(detail)[:400]),
        )

    def _record_failure(self, moment: datetime) -> None:
        self._conn.execute("INSERT INTO pair_failures(ts) VALUES (?)", (isoformat(moment),))
        cutoff = isoformat(moment - timedelta(seconds=self.failure_window))
        self._conn.execute("DELETE FROM pair_failures WHERE ts < ?", (cutoff,))

    def _failure_count(self, moment: datetime) -> int:
        cutoff = isoformat(moment - timedelta(seconds=self.failure_window))
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM pair_failures WHERE ts >= ?", (cutoff,)
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def _require_same_worker(worker: sqlite3.Row, worker_id: str) -> None:
        if worker["worker_id"] != worker_id:
            raise ProtocolError(403, "worker id mismatch")


LocalWorkerCoordinator = WorkerBroker


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


def render_worker_dashboard(snapshot: dict[str, Any]) -> str:
    workers = snapshot.get("workers") or []
    tasks = snapshot.get("tasks") or []
    events = snapshot.get("events") or []
    worker_rows = "\n".join(
        (
            "<tr>"
            f"<td>{html.escape(str(item.get('presence', '')))}</td>"
            f"<td>{html.escape(str(item.get('status', '')))}</td>"
            f"<td>{html.escape(str(item.get('worker_id', '')))}</td>"
            f"<td>{html.escape(str(item.get('runtime_name', '')))}</td>"
            f"<td>{html.escape(str(item.get('host_id', '')))}</td>"
            f"<td>{html.escape(str(item.get('model_id') or 'unknown'))} "
            f"<span>({html.escape(str(item.get('model_id_source') or 'not probed'))})</span></td>"
            f"<td>{html.escape(', '.join(item.get('capabilities') or []))}</td>"
            "</tr>"
        )
        for item in workers
    ) or "<tr><td colspan='7'>No paired workers. Presence is not simulated.</td></tr>"
    task_rows = "\n".join(
        (
            "<tr>"
            f"<td>{html.escape(str(item.get('status', '')))}</td>"
            f"<td>{html.escape(str(item.get('type', '')))}</td>"
            f"<td>{html.escape(str(item.get('worker_id') or ''))}</td>"
            f"<td>{html.escape('' if item.get('executed_shell') is None else str(bool(item.get('executed_shell'))).lower())}</td>"
            f"<td>{html.escape(json.dumps(item.get('result'), sort_keys=True)) if item.get('result') is not None else ''}</td>"
            f"<td>{html.escape(str(item.get('updated_at', '')))}</td>"
            "</tr>"
        )
        for item in tasks
    ) or "<tr><td colspan='6'>No tasks.</td></tr>"
    event_rows = "\n".join(
        (
            "<li>"
            f"{html.escape(str(item.get('ts', '')))} "
            f"{html.escape(str(item.get('kind', '')))} "
            f"{html.escape(str(item.get('worker_id') or ''))} "
            f"{html.escape(str(item.get('task_id') or ''))}"
            "</li>"
        )
        for item in events
    ) or "<li>No events.</li>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>ORBIT worker broker</title>
</head>
<body>
  <main>
    <p class="kicker">ORBIT worker broker · {html.escape(str(snapshot.get("protocol", "")))}</p>
    <h1>Local workers</h1>
    <p class="note">LIVE means a heartbeat arrived inside the freshness window. Task payloads are not executed by the broker.</p>
    <p class="meta">Server time {html.escape(str(snapshot.get("server_time", "")))}</p>
    <h2>Workers</h2>
    <table>
      <thead><tr><th>Presence</th><th>Status</th><th>Worker</th><th>Runtime</th><th>Host</th><th>Model</th><th>Capabilities</th></tr></thead>
      <tbody>{worker_rows}</tbody>
    </table>
    <h2>Tasks</h2>
    <table>
      <thead><tr><th>Status</th><th>Type</th><th>Worker</th><th>Shell</th><th>Result</th><th>Updated</th></tr></thead>
      <tbody>{task_rows}</tbody>
    </table>
    <h2>Events</h2>
    <ul>{event_rows}</ul>
  </main>
</body>
</html>"""


def protocol_error_handler(_request: Request, exc: ProtocolError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


def build_router(broker: WorkerBroker) -> APIRouter:
    router = APIRouter(prefix="/api/workers")

    def worker_auth(request: Request) -> sqlite3.Row:
        return broker.authenticate(
            request.headers.get("authorization"),
            request.headers.get("x-orbit-nonce"),
            request.headers.get("x-orbit-timestamp"),
        )

    def operator_auth(request: Request) -> sqlite3.Row:
        return broker.authenticate_operator(
            request.headers.get("authorization"),
            request.headers.get("x-orbit-operator-token"),
            request.headers,
        )

    @router.get("/protocol")
    def protocol() -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "authentication": "bearer-token-plus-nonce",
            "operator_authentication": "bearer-operator-token",
            "bind": "loopback",
            "executes_payloads": False,
        }

    @router.get("/discovery")
    def discovery() -> dict[str, Any]:
        return broker.discovery_snapshot()

    @router.post("/pairing", status_code=201)
    def pairing(request: Request) -> dict[str, str]:
        operator_auth(request)
        return broker.create_pairing()

    @router.post("/pair", status_code=201)
    def pair(body: PairRequest) -> dict[str, str]:
        return broker.pair(body)

    @router.post("/heartbeat")
    def heartbeat(body: HeartbeatRequest, request: Request) -> dict[str, Any]:
        return broker.heartbeat(worker_auth(request), body.manifest)

    @router.post("/poll")
    def poll(request: Request) -> dict[str, Any]:
        return broker.poll(worker_auth(request))

    @router.post("/revoke")
    def revoke(body: RevokeRequest, request: Request) -> dict[str, str]:
        operator_auth(request)
        return broker.revoke(body.worker_id)

    @router.post("/tasks", status_code=201)
    def create_task(body: TaskCreate, request: Request) -> dict[str, Any]:
        operator_auth(request)
        return broker.enqueue(body)

    @router.get("/tasks")
    def list_tasks() -> dict[str, Any]:
        return {"protocol": PROTOCOL, "tasks": broker.snapshot()["tasks"]}

    @router.get("/tasks/{task_id}")
    def task_detail(task_id: str) -> dict[str, Any]:
        return broker.task_detail(task_id)

    @router.post("/tasks/{task_id}/progress")
    def progress(task_id: str, body: ProgressRequest, request: Request) -> dict[str, str]:
        return broker.progress(worker_auth(request), task_id, body)

    @router.post("/tasks/{task_id}/result")
    def result(task_id: str, body: ResultRequest, request: Request) -> dict[str, str]:
        return broker.result(worker_auth(request), task_id, body)

    @router.post("/tasks/{task_id}/cancel")
    def cancel(task_id: str, request: Request) -> dict[str, str]:
        operator_auth(request)
        return broker.cancel(task_id)

    @router.get("")
    def list_workers() -> dict[str, Any]:
        snap = broker.snapshot()
        return {"protocol": PROTOCOL, "workers": snap["workers"], "server_time": snap["server_time"]}

    @router.get("/{worker_id}")
    def worker_detail(worker_id: str) -> dict[str, Any]:
        return broker.worker_detail(worker_id)

    return router


def mount_worker_api(app: FastAPI, broker: WorkerBroker | None = None, db_path: Path | None = None) -> WorkerBroker:
    """Attach canonical `orbit.workers.v0.1` routes to a FastAPI app.

    Idempotent: if `app.state.worker_broker` is already mounted, returns the
    existing broker without registering duplicate routes.
    """
    existing = getattr(app.state, "worker_broker", None)
    if existing is not None:
        return existing
    resolved_broker = broker or WorkerBroker(db_path or default_database())
    app.state.worker_broker = resolved_broker
    app.state.broker = resolved_broker
    app.add_exception_handler(ProtocolError, protocol_error_handler)
    app.include_router(build_router(resolved_broker))

    @app.get("/workers", include_in_schema=False)
    def workers_status_page() -> HTMLResponse:
        return HTMLResponse(render_worker_dashboard(resolved_broker.snapshot()))

    app.router.on_shutdown.append(resolved_broker.close)
    return resolved_broker


mount_worker_bridge = mount_worker_api


def create_worker_app(database: Path, clock: Any | None = None, **broker_kwargs: Any) -> FastAPI:
    broker = WorkerBroker(database, clock=clock, **broker_kwargs)
    app = FastAPI(title="ORBIT Worker Broker", version=PROTOCOL, docs_url=None, redoc_url=None)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"],
    )
    mount_worker_api(app, broker)

    @app.middleware("http")
    async def browser_safety(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme not in {"http", "https"} or parsed.netloc != request.headers.get("host", ""):
                    return JSONResponse(status_code=403, content={"detail": "Cross-origin mutation denied"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(render_worker_dashboard(broker.snapshot()))

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "orbit-worker-broker",
            "protocol": PROTOCOL,
            "bind": "127.0.0.1",
            "auth": "worker_bearer",
            "operator_auth": "bearer_operator_token",
            "executes_payloads": False,
        }

    return app


# ---------------------------------------------------------------------------
# Antigravity Worker Client (`orbit.workers.v0.1` + Cross-Adapter Compatible)
# ---------------------------------------------------------------------------


def handle_ping(lease: dict[str, Any]) -> dict[str, Any]:
    payload = lease.get("offer", {}).get("payload") or {}
    message = payload.get("message", "")
    if not isinstance(message, str) or len(message) > 500:
        raise ValueError("ping message rejected")
    return {"pong": message, "executed_shell": False}


def handle_inventory(lease: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    del lease
    return {"inventory": inventory, "executed_shell": False}


def handle_smoke_check(
    lease: dict[str, Any],
    *,
    workspace_root: Path,
    worker_id: str,
    runtime_name: str,
    model_id: str | None,
) -> dict[str, Any]:
    offer = lease.get("offer") or {}
    payload = offer.get("payload") or {}
    for forbidden in ("shell_command", "raw_shell", "bash", "cmd", "command"):
        if forbidden in payload:
            raise PermissionError(f"Forbidden raw shell key '{forbidden}' in payload")

    root = workspace_root.resolve()
    output_rel = payload.get("output_path")
    written_files: list[str] = []
    if output_rel:
        if not isinstance(output_rel, str) or output_rel.startswith("/"):
            raise PermissionError("output_path must be a relative path inside workspace_root")
        target = (root / output_rel).resolve()
        if target != root and root not in target.parents:
            raise PermissionError("Path traversal outside workspace_root denied")
        for part in target.relative_to(root).parts:
            if part in {".git", ".env"} or part.startswith(".env.") or part.endswith((".pem", ".key")):
                raise PermissionError(f"Denied path segment '{part}'")
        target.parent.mkdir(parents=True, exist_ok=True)
        proof_text = (
            f"# ORBIT Worker Proof ({worker_id})\n\n"
            f"- **Protocol**: `{PROTOCOL}`\n"
            f"- **Runtime**: `{runtime_name}`\n"
            f"- **Model**: `{model_id}`\n"
            f"- **Task ID**: `{lease.get('task_id')}`\n"
            f"- **Lease ID**: `{lease.get('lease_id')}`\n"
            f"- **Verified At**: `{isoformat(utcnow())}`\n"
        )
        target.write_text(proof_text, encoding="utf-8")
        written_files.append(str(target.relative_to(root)))

    return {
        "status": "VERIFIED",
        "worker_id": worker_id,
        "runtime_name": runtime_name,
        "model_id": model_id,
        "written_files": written_files,
        "executed_shell": False,
    }


class AntigravityWorker:
    """`orbit.workers.v0.1` compliant worker client for Google Antigravity."""

    def __init__(
        self,
        base_url: str,
        *,
        client: Any | None = None,
        state_path: Path | None = None,
        workspace_root: Path | None = None,
        inventory: dict[str, Any] | None = None,
        token: str | None = None,
        worker_id: str | None = None,
        capabilities: list[str] | None = None,
        enforce_loopback: bool = True,
        clock: Any | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.base_url = assert_loopback_url(base_url) if enforce_loopback else base_url.rstrip("/")
        self.client = client or httpx.Client(base_url=self.base_url, timeout=5.0)
        self.state_path = state_path
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.inventory = inventory if inventory is not None else build_antigravity_inventory()
        self.token = token
        self.worker_id = worker_id
        self.capabilities = (
            list(capabilities)
            if capabilities is not None
            else list(self.inventory.get("executable_capabilities") or EXECUTABLE_CAPABILITIES)
        )
        self.clock = clock
        self.log = log or (lambda _line: None)
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "orbit.ping": handle_ping,
            "orbit.inventory": lambda lease: handle_inventory(lease, self.inventory),
            "orbit.smoke_check": lambda lease: handle_smoke_check(
                lease,
                workspace_root=self.workspace_root,
                worker_id=self.worker_id or "antigravity-worker",
                runtime_name=str(self.inventory.get("runtime_name") or "antigravity"),
                model_id=self.inventory.get("model_id"),
            ),
        }
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client and hasattr(self.client, "close"):
            self.client.close()

    def check_protocol(self) -> dict[str, Any]:
        response = self.client.get("/api/workers/protocol")
        body = self._json(response)
        if body.get("protocol") != PROTOCOL:
            raise WorkerClientError(409, "protocol mismatch")
        return body

    def pair(self, pairing_code: str, worker_id: str | None = None) -> dict[str, Any]:
        if not self.inventory.get("binary_present", True):
            raise WorkerClientError(400, "Cannot pair an OFFLINE runtime")
        self.check_protocol()
        manifest = manifest_from_inventory(
            self.inventory,
            worker_id=worker_id or self.worker_id,
            capabilities=self.capabilities,
        )
        response = self.client.post(
            "/api/workers/pair",
            json={"protocol": PROTOCOL, "pairing_code": pairing_code, "manifest": manifest},
        )
        body = self._json(response)
        self.token = body["token"]
        self.worker_id = body["worker_id"]
        if self.state_path is not None:
            save_state(
                self.state_path,
                {
                    "protocol": PROTOCOL,
                    "url": self.base_url,
                    "worker_id": self.worker_id,
                    "token": self.token,
                },
            )
        self.log(f"paired worker_id={self.worker_id} token=stored")
        return {
            "worker_id": self.worker_id,
            "protocol": body["protocol"],
            "token_stored": self.state_path is not None,
        }

    def heartbeat(self, status: str = "IDLE") -> dict[str, Any]:
        manifest = manifest_from_inventory(
            self.inventory,
            worker_id=self.worker_id,
            capabilities=self.capabilities,
        )
        manifest["status"] = status
        response = self.client.post(
            "/api/workers/heartbeat",
            headers=self._headers(),
            json={"manifest": manifest},
        )
        return self._json(response)

    def poll(self) -> dict[str, Any]:
        response = self.client.post("/api/workers/poll", headers=self._headers())
        return self._json(response)

    def progress(
        self,
        task_id: str,
        lease_id: str,
        status: str,
        progress: float,
        message: str,
    ) -> dict[str, Any]:
        response = self.client.post(
            f"/api/workers/tasks/{task_id}/progress",
            headers=self._headers(),
            json={
                "lease_id": lease_id,
                "event_id": uuid.uuid4().hex,
                "status": status,
                "progress": progress,
                "message": scrub(message, [self.token or ""]),
            },
        )
        return self._json(response)

    def result(
        self,
        task_id: str,
        lease_id: str,
        status: str,
        result: dict[str, Any],
        log_excerpt: str,
    ) -> dict[str, Any]:
        response = self.client.post(
            f"/api/workers/tasks/{task_id}/result",
            headers=self._headers(),
            json={
                "lease_id": lease_id,
                "status": status,
                "result": result,
                "log_excerpt": scrub(log_excerpt, [self.token or ""]),
            },
        )
        return self._json(response)

    def handle_once(self) -> dict[str, Any]:
        beat = self.heartbeat("IDLE")
        polled = self.poll()
        lease = polled.get("lease")
        if not lease:
            return {"action": "idle", "presence": beat.get("presence"), "executed_shell": False}
        task_id = lease["task_id"]
        lease_id = lease["lease_id"]
        cancels = set(polled.get("cancels") or [])
        if lease.get("cancel_requested") or task_id in cancels:
            self.result(
                task_id,
                lease_id,
                "cancelled",
                {"cancelled": True, "executed_shell": False},
                "cancel acknowledged",
            )
            return {"action": "cancelled", "task_id": task_id, "executed_shell": False}
        task_type = lease["offer"]["type"]
        handler = self.handlers.get(task_type)
        self.progress(task_id, lease_id, "accepted", 0.2, "accepted")
        if handler is None:
            self.result(
                task_id,
                lease_id,
                "rejected",
                {"reason": "unsupported_task", "task_type": task_type, "executed_shell": False},
                "not executed",
            )
            return {"action": "rejected", "task_id": task_id, "executed_shell": False}
        self.heartbeat("BUSY")
        try:
            output = handler(lease)
        except PermissionError as exc:
            self.result(
                task_id,
                lease_id,
                "rejected",
                {"reason": "permission_denied", "detail": str(exc), "executed_shell": False},
                "permission denied",
            )
            self.heartbeat("IDLE")
            return {"action": "rejected", "task_id": task_id, "executed_shell": False}
        except Exception as exc:
            self.result(
                task_id,
                lease_id,
                "error",
                {"error": type(exc).__name__, "executed_shell": False},
                "handler failed",
            )
            self.heartbeat("ERROR")
            return {"action": "error", "task_id": task_id, "executed_shell": False}
        if not isinstance(output, dict):
            self.result(
                task_id,
                lease_id,
                "error",
                {"error": "non_object_result", "executed_shell": False},
                "bad handler result",
            )
            return {"action": "error", "task_id": task_id, "executed_shell": False}
        output["executed_shell"] = False
        self.progress(task_id, lease_id, "running", 1.0, "finished")
        self.result(task_id, lease_id, "completed", output, "ok")
        self.heartbeat("IDLE")
        return {"action": "completed", "task_id": task_id, "result": output, "executed_shell": False}

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise WorkerClientError(401, "worker is not paired")
        ts = int(self.clock.now().timestamp()) if self.clock is not None else int(time.time())
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Orbit-Nonce": secrets.token_hex(16),
            "X-Orbit-Timestamp": str(ts),
        }

    @staticmethod
    def _json(response: Any) -> dict[str, Any]:
        try:
            body = response.json()
        except Exception:
            body = {"detail": "non-JSON response"}
        if response.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else "request rejected"
            raise WorkerClientError(response.status_code, str(detail))
        if not isinstance(body, dict):
            raise WorkerClientError(response.status_code, "expected a JSON object")
        return body


OrbitWorkerAdapter = AntigravityWorker
