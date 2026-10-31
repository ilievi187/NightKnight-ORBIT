"""Versioned local worker contract for ORBIT (orbit.workers.v0.1).

The broker stores token and pairing-code hashes only. Callers must not log
raw bearer tokens, pairing codes, or Authorization headers.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROTOCOL = "orbit.workers.v0.1"
ADAPTER_VERSION = "0.1.0"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
WORKER_STATUSES = frozenset({"IDLE", "BUSY", "ERROR", "BLOCKED", "OFFLINE"})
RESULT_STATUSES = frozenset({"completed", "rejected", "cancelled", "error"})
PROGRESS_STATUSES = frozenset({"accepted", "running", "cancelling"})
TERMINAL_TASK_STATUSES = frozenset({"completed", "rejected", "cancelled", "error"})

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_CAP_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_GRANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")


class ProtocolError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class ManualClock:
    """Injectable clock so expiry and heartbeat loss do not sleep in tests."""

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
    if host not in LOOPBACK_HOSTS:
        raise ValueError("worker broker refuses a non-loopback bind")
    return "127.0.0.1"


def scrub(text: str, secrets: list[str] | None = None) -> str:
    cleaned = _BEARER_RE.sub("Bearer [redacted]", text or "")
    for secret in secrets or []:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def bounded_json(value: Any, limit: int = 16000) -> str:
    import json

    try:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(400, "value is not JSON") from exc
    if len(raw.encode("utf-8")) > limit:
        raise ProtocolError(400, "JSON value is too large")
    return raw


class Manifest(BaseModel):
    worker_id: str = Field(pattern=_ID_RE.pattern)
    runtime_name: str = Field(min_length=1, max_length=80)
    host_id: str = Field(min_length=1, max_length=80)
    model_id: str | None = Field(default=None, max_length=120)
    model_id_source: str | None = Field(default=None, max_length=80)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    status: str = "IDLE"
    version: str = Field(min_length=1, max_length=40)
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
    protocol: str
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


def valid_nonce(value: str | None) -> str:
    if not value or not _NONCE_RE.fullmatch(value):
        raise ProtocolError(401, "nonce rejected")
    return value
