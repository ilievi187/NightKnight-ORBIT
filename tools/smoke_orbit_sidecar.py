#!/usr/bin/env python3
"""CI smoke test: execute the *actual* frozen onefile backend and fetch real UI assets.

A successful AppImage build is insufficient evidence that the Python sidecar boots.
No external network requests and no model/GPU calls.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_orbit_sidecar.py /path/to/frozen/orbit-server")
    executable = Path(sys.argv[1]).resolve()
    if not executable.is_file():
        raise SystemExit(f"sidecar missing: {executable}")
    with tempfile.TemporaryDirectory(prefix="orbit-sidecar-smoke-") as temp:
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        workspace = Path(temp) / "workspace"
        workspace.mkdir()
        env = os.environ.copy()
        env.update(ORBIT_BIND="127.0.0.1", ORBIT_PORT=str(port),
                   ORBIT_WORKSPACE=str(workspace),
                   ORBIT_DATA_DIR=str(Path(temp) / "data"),
                   ORBIT_BASE_URL="http://127.0.0.1:18879/v1",
                   ORBIT_MODEL="heretic27-fastlink-65k")
        log = Path(temp) / "backend.log"
        with log.open("w+b") as output:
            proc = subprocess.Popen([str(executable)], env=env, cwd=temp,
                                    stdout=output, stderr=subprocess.STDOUT)
            base = f"http://127.0.0.1:{port}"
            try:
                until = time.monotonic() + 45
                last = "not started"
                while time.monotonic() < until:
                    if proc.poll() is not None:
                        raise RuntimeError(f"frozen backend exited with code {proc.returncode}")
                    try:
                        with urllib.request.urlopen(base + "/api/health", timeout=1) as response:
                            payload = json.load(response)
                        if payload.get("status") == "ok":
                            break
                        last = str(payload)
                    except (urllib.error.URLError, OSError, ValueError) as exc:
                        last = str(exc)
                    time.sleep(0.25)
                else:
                    raise TimeoutError(f"backend did not become healthy: {last}")

                assert payload["base_url"] == "http://127.0.0.1:18879/v1", payload
                assert payload["model"] == "heretic27-fastlink-65k", payload
                for path, marker in (("/", b"NightKnight ORBIT"),
                                     ("/assets/style.css", b"{"),
                                     ("/assets/app.js", b"function")):
                    with urllib.request.urlopen(base + path, timeout=3) as response:
                        body = response.read()
                        if response.status != 200 or marker not in body:
                            raise RuntimeError(f"bad frontend asset: {path}, status={response.status}")
                print("PASS: real frozen backend serves health + HTML + CSS + JS")
            except BaseException:
                output.flush()
                print("FROZEN BACKEND LOG:\n" + log.read_text(errors="replace")[-18000:], file=sys.stderr)
                raise
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    main()
