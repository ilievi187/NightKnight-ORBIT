"""Configuration is read from the environment; no secrets are sent to the UI."""
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Settings:
    model: str
    base_url: str
    api_key: str
    workspace: Path
    database: Path
    bind: str
    port: int
    # M3 — local inference runtime. Both optional with safe defaults so existing
    # constructors (and tests) that pass only the original seven fields keep working.
    timeout: float = 15.0
    providers: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @classmethod
    def from_environment(cls) -> "Settings":
        default_workspace = Path.home() / "Projects"
        if not default_workspace.is_dir():
            default_workspace = Path.home()
        workspace = Path(os.getenv("ORBIT_WORKSPACE", str(default_workspace))).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"ORBIT_WORKSPACE does not exist: {workspace}")
        data_dir = Path(os.getenv("ORBIT_DATA_DIR", str(Path.home() / ".local" / "share" / "nightknight-orbit"))).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            timeout = float(os.getenv("ORBIT_TIMEOUT", "15"))
        except ValueError as exc:
            raise ValueError("ORBIT_TIMEOUT must be a number") from exc
        if not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ValueError("ORBIT_TIMEOUT must be finite and between 0 and 600 seconds")
        providers = _parse_providers(os.getenv("ORBIT_PROVIDERS", ""), default_timeout=timeout)
        return cls(
            model=os.getenv("ORBIT_MODEL", "heretic27-q4ks"),
            base_url=os.getenv("ORBIT_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/"),
            api_key=os.getenv("ORBIT_API_KEY", ""),
            workspace=workspace,
            database=data_dir / "orbit.sqlite3",
            bind=os.getenv("ORBIT_BIND", "127.0.0.1"),
            port=int(os.getenv("ORBIT_PORT", "8765")),
            timeout=timeout,
            providers=tuple(providers),
        )


def _parse_providers(raw: str, default_timeout: float = 15.0) -> list[dict[str, Any]]:
    """Parse ORBIT_PROVIDERS.

    Accepts either a JSON array of objects, or a semicolon-separated list of
    `name=base_url[|model][|kind][|timeout]` entries (URL-encoding not required).
    Returns a list of dicts suitable for orbit.runtime.providers_from_settings.
    Empty / invalid input yields an empty list (the single primary provider is
    still derived from ORBIT_BASE_URL / ORBIT_MODEL by the runtime).
    """
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            out.append({
                "name": str(entry.get("name") or "").strip(),
                "base_url": str(entry.get("base_url") or "").strip(),
                "api_key": str(entry.get("api_key") or ""),
                "model": str(entry.get("model") or "") or None,
                "kind": str(entry.get("kind") or "openai_compatible"),
                "timeout": _as_float(entry.get("timeout"), default_timeout),
            })
        return out

    out = []
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("|")]
        name = parts[0] if parts and parts[0] else ""
        base_url = parts[1] if len(parts) > 1 and parts[1] else ""
        if not name or not base_url:
            continue
        model = parts[2] if len(parts) > 2 and parts[2] else None
        kind = parts[3] if len(parts) > 3 and parts[3] else "openai_compatible"
        timeout = _as_float(parts[4], default_timeout) if len(parts) > 4 else default_timeout
        out.append({"name": name, "base_url": base_url, "api_key": "", "model": model,
                    "kind": kind, "timeout": timeout})
    return out


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
