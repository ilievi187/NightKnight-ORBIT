"""Configuration is read from the environment; no secrets are sent to the UI."""
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    model: str
    base_url: str
    api_key: str
    workspace: Path
    database: Path
    bind: str
    port: int

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
        return cls(
            model=os.getenv("ORBIT_MODEL", "heretic27-q4ks"),
            base_url=os.getenv("ORBIT_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/"),
            api_key=os.getenv("ORBIT_API_KEY", ""),
            workspace=workspace,
            database=data_dir / "orbit.sqlite3",
            bind=os.getenv("ORBIT_BIND", "127.0.0.1"),
            port=int(os.getenv("ORBIT_PORT", "8765")),
        )
