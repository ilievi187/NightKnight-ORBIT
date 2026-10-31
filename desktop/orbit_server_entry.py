"""ORBIT desktop sidecar entry (PyInstaller frozen or portable launcher).

Contract with the desktop shell (see docs/DESKTOP.md):
- Binds 127.0.0.1 ONLY (Settings.bind default);
- Exits with code 3 and a machine-readable first line when the port is busy
  ("ORBIT_PORT_BUSY") so the GUI can show a clear dialog;
- Streams uvicorn logs to stderr for the desktop log file;
- Deterministic shutdown: the shell kills the process; uvicorn handles SIGTERM.
"""
from __future__ import annotations

import os
import socket
import sys


def _port_open(bind: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        try:
            sock.bind((bind, port))
        except OSError:
            return False
    return True


def main() -> int:
    from orbit.config import Settings

    try:
        settings = Settings.from_environment()
    except ValueError as exc:
        print(f"ORBIT_BOOT_ERROR: {exc}", file=sys.stderr, flush=True)
        return 2

    if not _port_open(settings.bind, settings.port):
        print(f"ORBIT_PORT_BUSY: {settings.bind}:{settings.port} is already in use — "
              "another ORBIT instance appears to be running. "
              "Close it or set ORBIT_PORT to a free port.", file=sys.stderr, flush=True)
        return 3

    import uvicorn

    print(f"ORBIT_READY: http://{settings.bind}:{settings.port} workspace={settings.workspace}",
          file=sys.stderr, flush=True)
    uvicorn.run("orbit.app:app", host=settings.bind, port=settings.port,
                reload=False, workers=1, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
