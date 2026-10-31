#!/usr/bin/env python3
"""Companion CLI worker agent for connecting Antigravity, Hermes, OpenCode, and LUNA to ORBIT.

Usage examples:
  # 1. Inspect real local runtime, skills, tools, model, and hardware capabilities
  python tools/orbit_worker_agent.py --inspect

  # 2. Run an offline/loopback end-to-end self-test (pairing -> heartbeat -> task -> verify)
  python tools/orbit_worker_agent.py --self-test

  # 3. Connect to a live local ORBIT dashboard with a short-lived pairing code
  python tools/orbit_worker_agent.py --api http://127.0.0.1:8765 --pairing <CODE> --runtime antigravity --once

  # 4. Print authenticated SSH / Tailscale loopback tunnel guide for remote hosts
  python tools/orbit_worker_agent.py --print-tunnel-guide
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orbit.worker_bridge import (
    LocalWorkerCoordinator,
    OrbitWorkerAdapter,
    WorkerManifest,
    build_remote_tunnel_spec,
    inspect_all_runtimes,
    inspect_antigravity_runtime,
    inspect_hermes_runtime,
    inspect_luna_runtime,
    inspect_opencode_runtime,
    mask_secret,
    mount_worker_bridge,
)


def select_manifest(runtime: str, *, worker_id: str | None = None, tunnel_url: str | None = None) -> WorkerManifest:
    kind = runtime.lower().strip()
    if kind in ("antigravity", "agy"):
        return inspect_antigravity_runtime(worker_id=worker_id)
    if kind == "hermes":
        return inspect_hermes_runtime(worker_id=worker_id)
    if kind in ("opencode", "alex"):
        return inspect_opencode_runtime(worker_id=worker_id)
    if kind == "luna":
        return inspect_luna_runtime(worker_id=worker_id or "luna-ubuntu-remote", tunnel_health_url=tunnel_url)
    raise ValueError(f"Unsupported runtime '{runtime}' (choose: antigravity, hermes, opencode, luna)")


def run_self_test(runtime: str = "antigravity") -> dict:
    """Execute a complete, deterministic verification against an ephemeral ORBIT coordinator."""
    with tempfile.TemporaryDirectory(prefix="orbit-worker-selftest-") as tmp:
        tmp_path = Path(tmp)
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        coord = LocalWorkerCoordinator(tmp_path / "orbit.sqlite3", workspace)
        app = FastAPI(title="ORBIT Worker Self-Test")
        mount_worker_bridge(app, coord)
        client = TestClient(app)

        manifest = select_manifest(runtime)
        pairing = coord.create_pairing_code(allowed_runtime=manifest.runtime_name)
        adapter = OrbitWorkerAdapter(
            base_url="http://testserver",
            manifest=manifest,
            client=client,  # type: ignore[arg-type]
            default_workspace=workspace,
        )
        reg = adapter.pair_and_register(pairing["pairing_code"])
        beat = adapter.send_heartbeat(status="IDLE")

        task = coord.enqueue_task(
            "Verify real Antigravity worker connection & artifact delivery",
            {
                "op": "deliver",
                "path": "proof/antigravity-worker-verified.md",
                "must_contain": [
                    "ORBIT_WORKER_VERIFIED=true",
                    f"runtime={manifest.runtime_name}",
                    f"host={manifest.host_id}",
                ],
            },
            mission_id="mission-issue-13-selftest",
            capability_required="orbit.plan.deliver",
            grant={"root": str(workspace), "denied_paths": ["secrets/"]},
        )
        outcome = adapter.run_once()
        fleet = client.get("/api/workers/v0.1/fleet").json()
        return {
            "self_test": "PASSED",
            "pairing_code_masked": mask_secret(pairing["pairing_code"]),
            "worker_token_masked": adapter.token_hint,
            "registered_worker_id": reg["worker_id"],
            "heartbeat_status": beat["worker"]["status"],
            "enqueued_task_id": task["task_id"],
            "task_outcome": outcome,
            "fleet_workers": fleet["workers"],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ORBIT Worker Protocol v0.1 Companion Adapter")
    parser.add_argument("--inspect", action="store_true", help="Inspect and print real local runtime & host capabilities")
    parser.add_argument("--print-tunnel-guide", action="store_true", help="Print authenticated SSH/Tailscale tunnel specs")
    parser.add_argument("--self-test", action="store_true", help="Run an end-to-end verification against an ephemeral coordinator")
    parser.add_argument("--api", default="http://127.0.0.1:8765", help="Loopback ORBIT API base URL (default: http://127.0.0.1:8765)")
    parser.add_argument("--pairing", default="", help="One-time pairing credential from the ORBIT dashboard")
    parser.add_argument("--runtime", default="antigravity", choices=["antigravity", "hermes", "opencode", "luna"])
    parser.add_argument("--worker-id", default=None, help="Optional explicit worker_id override")
    parser.add_argument("--tunnel-url", default=None, help="Optional loopback tunnel health URL for remote LUNA")
    parser.add_argument("--workspace", default=str(Path.home() / "Projects"), help="Default workspace root for scoped tasks")
    parser.add_argument("--once", action="store_true", help="Pair, heartbeat, run at most one task, and exit")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Seconds between poll/heartbeat cycles")
    parser.add_argument("--max-cycles", type=int, default=30, help="Maximum poll cycles when not using --once")
    args = parser.parse_args(argv)

    if args.inspect:
        manifests = [m.to_dict() for m in inspect_all_runtimes()]
        print(json.dumps({"protocol_version": "v0.1", "runtimes": manifests}, indent=2))
        return 0

    if args.print_tunnel_guide:
        specs = [
            build_remote_tunnel_spec("ubuntu-hermes", tailscale_ip="100.98.90.67"),
            build_remote_tunnel_spec("detti", tailscale_ip="100.95.239.126"),
        ]
        print(json.dumps({"tunnels": specs}, indent=2))
        return 0

    if args.self_test:
        report = run_self_test(args.runtime)
        print(json.dumps(report, indent=2))
        return 0

    if not args.pairing:
        parser.error("--pairing <CODE> is required when connecting to a live ORBIT server (or use --inspect / --self-test)")

    manifest = select_manifest(args.runtime, worker_id=args.worker_id, tunnel_url=args.tunnel_url)
    adapter = OrbitWorkerAdapter(
        base_url=args.api,
        manifest=manifest,
        default_workspace=Path(args.workspace).expanduser().resolve(),
    )
    try:
        reg = adapter.pair_and_register(args.pairing)
        print(json.dumps({
            "event": "paired",
            "worker_id": adapter.registered_worker_id,
            "runtime": manifest.runtime_name,
            "model_id": manifest.model_id,
            "token_hint": adapter.token_hint,
            "heartbeat_ttl": reg.get("heartbeat_ttl"),
        }))
        if args.once:
            res = adapter.run_once()
            print(json.dumps({"event": "run_once_complete", "result": res}, indent=2))
            return 0

        for _ in range(args.max_cycles):
            res = adapter.run_once()
            if res is not None:
                print(json.dumps({"event": "task_completed", "result": res}))
            time.sleep(max(0.2, args.poll_interval))
        return 0
    finally:
        adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
