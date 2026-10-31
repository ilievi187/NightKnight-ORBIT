#!/usr/bin/env python3
"""ORBIT Worker Companion CLI (`orbit.workers.v0.1`) for Google Antigravity.

Security policy:
- Never accepts raw pairing codes or bearer tokens as CLI process arguments
  (`argv`), preventing leakage via `ps aux` or shell history.
- Reads pairing codes only from `--pairing-stdin`, a `0600`-protected file via
  `--pairing-file`, or `ORBIT_PAIRING_CODE_FILE` / `ORBIT_PAIRING_CODE`.
- Stores worker bearer credentials in a `0600`-protected state file created with
  `O_CREAT | O_EXCL` mode `0600` before writing any bytes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orbit.worker_bridge import (  # noqa: E402
    PROTOCOL,
    AntigravityWorker,
    WorkerBroker,
    assert_loopback_url,
    build_antigravity_inventory,
    build_remote_tunnel_spec,
    create_worker_app,
    inspect_antigravity_runtime,
    inspect_hermes_runtime,
    inspect_host_resources,
    inspect_luna_runtime,
    inspect_opencode_runtime,
    load_state,
    read_secret_file,
)


def default_state_path() -> Path:
    return Path.home() / ".local" / "share" / "nightknight-orbit" / "antigravity-worker.json"


def _read_pairing_code(
    *,
    pairing_stdin: bool = False,
    pairing_file: str | None = None,
    stdin_stream: Any | None = None,
) -> str:
    stream = stdin_stream or sys.stdin
    if pairing_stdin:
        code = stream.read().strip()
        if not code:
            raise SystemExit("No pairing code received on stdin.")
        return code
    file_path = pairing_file or os.environ.get("ORBIT_PAIRING_CODE_FILE", "").strip()
    if file_path:
        try:
            code = read_secret_file(Path(file_path).expanduser())
        except (FileNotFoundError, PermissionError) as exc:
            raise SystemExit(str(exc)) from exc
        if not code:
            raise SystemExit(f"Pairing code file is empty: {file_path}")
        return code
    env_code = os.environ.get("ORBIT_PAIRING_CODE", "").strip()
    if env_code:
        return env_code
    raise SystemExit(
        "Pairing secret required via --pairing-stdin, --pairing-file <0600-path>, "
        "or ORBIT_PAIRING_CODE_FILE. Pairing codes are never accepted in CLI arguments."
    )


def run_self_test() -> dict[str, Any]:
    """Run an end-to-end self-test against an ephemeral loopback WorkerBroker."""
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory(prefix="orbit_worker_selftest_") as tmp:
        tmp_path = Path(tmp)
        app = create_worker_app(tmp_path / "broker.sqlite3")
        broker: WorkerBroker = app.state.worker_broker
        state_file = tmp_path / "antigravity-worker.json"

        with TestClient(app, base_url="http://127.0.0.1") as client:
            op_headers = {"Authorization": f"Bearer {broker.operator_token}"}

            # 1. Mint single-use pairing code using scoped operator token
            pair_resp = client.post("/api/workers/pairing", headers=op_headers)
            assert pair_resp.status_code == 201, pair_resp.text
            pairing_code = pair_resp.json()["pairing_code"]

            # 2. Pair Antigravity worker and send initial heartbeat
            worker = AntigravityWorker(
                "http://127.0.0.1",
                client=client,
                state_path=state_file,
                workspace_root=tmp_path,
            )
            paired = worker.pair(pairing_code, worker_id="antigravity-NightKnightPC")
            beat = worker.heartbeat("IDLE")

            # 3. Enqueue a harmless ping task and a smoke_check task via operator API
            t1 = client.post(
                "/api/workers/tasks",
                headers=op_headers,
                json={"type": "orbit.ping", "payload": {"message": "hello-orbit"}, "grant": "local"},
            )
            assert t1.status_code == 201, t1.text
            t2 = client.post(
                "/api/workers/tasks",
                headers=op_headers,
                json={
                    "type": "orbit.smoke_check",
                    "payload": {"output_path": "docs/ANTIGRAVITY-PROOF.md"},
                    "grant": "local",
                },
            )
            assert t2.status_code == 201, t2.text

            out1 = worker.handle_once()
            out2 = worker.handle_once()
            discovery = client.get("/api/workers/discovery").json()

            return {
                "self_test": "PASSED",
                "protocol": PROTOCOL,
                "paired": paired,
                "heartbeat_presence": beat["presence"],
                "ping_outcome": out1,
                "smoke_check_outcome": out2,
                "discovery_antigravity": discovery["runtimes"]["antigravity"],
                "discovery_hermes": discovery["runtimes"]["hermes"],
            }


def main(argv: list[str] | None = None, *, stdin_stream: Any | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    for token in raw_args:
        if token == "--pairing" or token.startswith("--pairing="):
            raise SystemExit(
                "ERROR: Passing pairing secrets in CLI arguments (--pairing) is forbidden "
                "to prevent process/ps/history leakage. Use --pairing-stdin, "
                "--pairing-file <0600-path>, or ORBIT_PAIRING_CODE_FILE."
            )

    parser = argparse.ArgumentParser(
        prog="orbit_worker_agent",
        description="ORBIT Worker Protocol v0.1 Companion CLI for Google Antigravity",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["inventory", "pair", "heartbeat", "once", "run"],
        help="Worker action (`inventory`, `pair`, `heartbeat`, `once`, `run`)",
    )
    parser.add_argument("--inspect", action="store_true", help="Inspect local host and runtime discovery state")
    parser.add_argument("--self-test", action="store_true", help="Run end-to-end self-test against ephemeral broker")
    parser.add_argument("--print-tunnel-guide", action="store_true", help="Print SSH/Tailscale loopback tunnel spec")
    parser.add_argument("--api", "--url", dest="url", default=os.environ.get("ORBIT_URL", "http://127.0.0.1:8765"))
    parser.add_argument(
        "--state",
        default=os.environ.get("ORBIT_WORKER_STATE", str(default_state_path())),
        help="Path to 0600 worker credential state file",
    )
    parser.add_argument("--pairing-stdin", action="store_true", help="Read single-use pairing code from stdin")
    parser.add_argument("--pairing-file", help="Read single-use pairing code from a 0600-permission file")
    parser.add_argument("--worker-id", default=None, help="Optional explicit worker_id")
    parser.add_argument("--workspace", default=str(REPO_ROOT), help="Workspace root for scoped smoke_check tasks")
    parser.add_argument("--once", action="store_true", help="Pair (if secret supplied) and handle one poll cycle")
    parser.add_argument("--interval", type=float, default=5.0, help="Poll interval in seconds for `run`")

    args = parser.parse_args(raw_args)

    if args.inspect or args.command == "inventory":
        payload = {
            "protocol": PROTOCOL,
            "host_resources": inspect_host_resources(),
            "antigravity_manifest": inspect_antigravity_runtime().to_dict(),
            "sibling_discovery": {
                "hermes": inspect_hermes_runtime(),
                "opencode": inspect_opencode_runtime(),
                "luna": inspect_luna_runtime(),
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.print_tunnel_guide:
        print(json.dumps(build_remote_tunnel_spec(), indent=2, sort_keys=True))
        return 0

    if args.self_test:
        print(json.dumps(run_self_test(), indent=2, sort_keys=True))
        return 0

    try:
        base_url = assert_loopback_url(args.url)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    state_path = Path(args.state).expanduser()
    workspace_root = Path(args.workspace).expanduser().resolve()
    worker = AntigravityWorker(
        base_url,
        state_path=state_path,
        workspace_root=workspace_root,
        inventory=build_antigravity_inventory(),
        worker_id=args.worker_id,
    )
    try:
        cmd = args.command or ("once" if args.once else None)
        if cmd is None:
            parser.print_help()
            return 2

        if cmd == "pair":
            code = _read_pairing_code(
                pairing_stdin=args.pairing_stdin,
                pairing_file=args.pairing_file,
                stdin_stream=stdin_stream,
            )
            paired = worker.pair(code, worker_id=args.worker_id)
            beat = worker.heartbeat("IDLE")
            print(json.dumps({**paired, "presence": beat.get("presence")}, indent=2, sort_keys=True))
            return 0

        if args.pairing_stdin or args.pairing_file or os.environ.get("ORBIT_PAIRING_CODE_FILE") or os.environ.get("ORBIT_PAIRING_CODE"):
            code = _read_pairing_code(
                pairing_stdin=args.pairing_stdin,
                pairing_file=args.pairing_file,
                stdin_stream=stdin_stream,
            )
            worker.pair(code, worker_id=args.worker_id)
        else:
            if not state_path.is_file():
                raise SystemExit("Worker state file is missing; pair first using `pair --pairing-stdin` or `--pairing-file`.")
            state = load_state(state_path)
            worker.token = state["token"]
            worker.worker_id = state["worker_id"]

        if cmd == "heartbeat":
            print(json.dumps(worker.heartbeat("IDLE"), indent=2, sort_keys=True))
            return 0

        if cmd == "once":
            print(json.dumps(worker.handle_once(), indent=2, sort_keys=True))
            return 0

        while True:
            print(json.dumps(worker.handle_once(), sort_keys=True))
            time.sleep(args.interval)
    finally:
        worker.close()


if __name__ == "__main__":
    raise SystemExit(main())
