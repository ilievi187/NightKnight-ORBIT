"""Comprehensive unit, security, and cross-adapter integration tests for `orbit.workers.v0.1`."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import secrets
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from orbit.app import create_app
from orbit.config import Settings
from orbit.worker_bridge import (
    EXECUTABLE_CAPABILITIES,
    PROTOCOL,
    AntigravityWorker,
    ManualClock,
    WorkerBroker,
    WorkerClientError,
    assert_loopback_host,
    assert_loopback_url,
    build_antigravity_inventory,
    build_remote_tunnel_spec,
    create_worker_app,
    inspect_antigravity_runtime,
    inspect_hermes_runtime,
    inspect_host_resources,
    inspect_luna_runtime,
    inspect_opencode_runtime,
    read_secret_file,
    scan_skill_names,
    write_secret_file,
)
from tools import orbit_worker_agent


class GrokWireClient:
    """Reference Grok PR #15 wire client (`orbit.grok_adapter.GrokWorker` compatible)."""

    def __init__(self, client: Any, clock: ManualClock | None = None, worker_id: str = "grok-NightKnightPC") -> None:
        self.client = client
        self.clock = clock
        self.worker_id = worker_id
        self.token: str | None = None
        self.capabilities = ["orbit.inventory", "orbit.ping"]
        self.inventory = {
            "runtime_name": "grok-build",
            "adapter_version": "0.1.0",
            "model_id": "grok-4.7",
            "model_id_source": "harness_session_identity",
            "model_endpoint_probed": False,
            "model_ready": False,
            "host_id": "NightKnightPC",
            "executable_capabilities": ["orbit.inventory", "orbit.ping"],
            "arbitrary_shell_from_dashboard": False,
            "observed_session_tools": ["read_file", "search_replace"],
            "observed_session_tools_executable_through_orbit": False,
            "mcp_servers_connected": ["tasks"],
            "mcp_tools_invoked": False,
            "skill_count": 0,
        }

    def manifest(self, status: str = "IDLE") -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "runtime_name": "grok-build",
            "host_id": "NightKnightPC",
            "model_id": "grok-4.7",
            "model_id_source": "harness_session_identity",
            "capabilities": list(self.capabilities),
            "status": status,
            "version": "0.1.0",
            "inventory": self.inventory,
        }

    def _headers(self) -> dict[str, str]:
        ts = int(self.clock.now().timestamp()) if self.clock else int(time.time())
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Orbit-Nonce": secrets.token_hex(16),
            "X-Orbit-Timestamp": str(ts),
        }

    def pair(self, pairing_code: str) -> dict[str, Any]:
        proto = self.client.get("/api/workers/protocol").json()
        assert proto["protocol"] == PROTOCOL
        resp = self.client.post(
            "/api/workers/pair",
            json={"protocol": PROTOCOL, "pairing_code": pairing_code, "manifest": self.manifest()},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self.token = body["token"]
        return body

    def heartbeat(self, status: str = "IDLE") -> dict[str, Any]:
        resp = self.client.post(
            "/api/workers/heartbeat",
            headers=self._headers(),
            json={"manifest": self.manifest(status)},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def handle_once(self) -> dict[str, Any]:
        beat = self.heartbeat("IDLE")
        polled = self.client.post("/api/workers/poll", headers=self._headers()).json()
        lease = polled.get("lease")
        if not lease:
            return {"action": "idle", "presence": beat["presence"], "executed_shell": False}
        task_id = lease["task_id"]
        lease_id = lease["lease_id"]
        task_type = lease["offer"]["type"]
        self.client.post(
            f"/api/workers/tasks/{task_id}/progress",
            headers=self._headers(),
            json={
                "lease_id": lease_id,
                "event_id": uuid.uuid4().hex,
                "status": "accepted",
                "progress": 0.2,
                "message": "accepted",
            },
        )
        if task_type == "orbit.ping":
            msg = lease["offer"]["payload"].get("message", "")
            output = {"pong": msg, "executed_shell": False}
        elif task_type == "orbit.inventory":
            output = {"inventory": self.inventory, "executed_shell": False}
        else:
            self.client.post(
                f"/api/workers/tasks/{task_id}/result",
                headers=self._headers(),
                json={
                    "lease_id": lease_id,
                    "status": "rejected",
                    "result": {"reason": "unsupported_task", "executed_shell": False},
                    "log_excerpt": "not executed",
                },
            )
            return {"action": "rejected", "task_id": task_id, "executed_shell": False}
        self.client.post(
            f"/api/workers/tasks/{task_id}/progress",
            headers=self._headers(),
            json={
                "lease_id": lease_id,
                "event_id": uuid.uuid4().hex,
                "status": "running",
                "progress": 1.0,
                "message": "finished",
            },
        )
        self.client.post(
            f"/api/workers/tasks/{task_id}/result",
            headers=self._headers(),
            json={
                "lease_id": lease_id,
                "status": "completed",
                "result": output,
                "log_excerpt": "ok",
            },
        )
        self.heartbeat("IDLE")
        return {"action": "completed", "task_id": task_id, "result": output, "executed_shell": False}


class WorkerBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="orbit_worker_test_")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.clock = ManualClock()
        self.database = self.base / "broker.sqlite3"
        self.app = create_worker_app(
            self.database,
            clock=self.clock,
            offline_after=20,
            lease_ttl=30,
            token_ttl=120,
            pairing_ttl=60,
            failure_limit=3,
            failure_window=60,
            timestamp_skew=60,
        )
        self.broker: WorkerBroker = self.app.state.worker_broker
        self.op_token = self.broker.operator_token
        self.op_headers = {"Authorization": f"Bearer {self.op_token}"}
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.addCleanup(self.client.close)
        self.addCleanup(self.broker.close)

        self.fake_ag_bin = self.base / "antigravity"
        self.fake_ag_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        self.inventory = build_antigravity_inventory(
            skill_roots=[],
            host_id="NightKnightPC",
            binary_path=self.fake_ag_bin,
        )

    def issue_code(self) -> str:
        resp = self.client.post("/api/workers/pairing", headers=self.op_headers)
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["pairing_code"]

    def make_ag_worker(self, worker_id: str = "antigravity-NightKnightPC", **kwargs: Any) -> AntigravityWorker:
        return AntigravityWorker(
            "http://127.0.0.1",
            client=self.client,
            workspace_root=self.base,
            inventory=kwargs.pop("inventory", self.inventory),
            worker_id=worker_id,
            clock=self.clock,
            **kwargs,
        )

    def test_01_discovery_separates_observed_tools_and_runtimes_from_connected_workers(self) -> None:
        skill_root = self.base / "skills"
        secret_body = "TOP-SECRET-SKILL-BODY-DO-NOT-READ"
        (skill_root / "reverse-elf").mkdir(parents=True)
        (skill_root / "reverse-elf" / "SKILL.md").write_text(secret_body, encoding="utf-8")
        names = scan_skill_names([skill_root])
        self.assertEqual(names, ["reverse-elf"])
        self.assertNotIn(secret_body, json.dumps(names))

        ag = inspect_antigravity_runtime(binary_path=self.fake_ag_bin, skill_roots=[skill_root])
        self.assertTrue(ag.discovered)
        self.assertFalse(ag.connected)
        self.assertFalse(ag.authorized_to_execute)
        self.assertEqual(ag.model_id, "Gemini 4 Argon (Medium)")
        self.assertEqual(ag.capabilities, list(EXECUTABLE_CAPABILITIES))
        self.assertFalse(ag.inventory["observed_session_tools_executable_through_orbit"])
        self.assertFalse(ag.inventory["skills_executable_through_orbit"])
        self.assertFalse(ag.inventory["arbitrary_shell_from_dashboard"])
        self.assertFalse(ag.inventory["model_ready"])
        self.assertIn("run_command", ag.inventory["observed_session_tools"])
        self.assertNotIn("run_command", ag.capabilities)

        # Before pairing, discovery endpoint reports all runtimes as unconnected
        disc_before = self.client.get("/api/workers/discovery").json()
        for rt_name in ("antigravity", "hermes", "opencode", "luna"):
            self.assertFalse(disc_before["runtimes"][rt_name]["connected"])
            self.assertFalse(disc_before["runtimes"][rt_name]["authorized_to_execute"])
            self.assertEqual(disc_before["runtimes"][rt_name]["executable_capabilities"], [])

        # Pair Antigravity and send heartbeat -> only antigravity becomes connected & authorized
        worker = self.make_ag_worker()
        worker.pair(self.issue_code())
        worker.heartbeat("IDLE")
        disc_after = self.client.get("/api/workers/discovery").json()
        self.assertTrue(disc_after["runtimes"]["antigravity"]["connected"])
        self.assertTrue(disc_after["runtimes"]["antigravity"]["authorized_to_execute"])
        self.assertEqual(
            disc_after["runtimes"]["antigravity"]["executable_capabilities"],
            list(EXECUTABLE_CAPABILITIES),
        )
        # Sibling runtimes remain unconnected until their own adapters pair & heartbeat
        self.assertFalse(disc_after["runtimes"]["hermes"]["connected"])
        self.assertFalse(disc_after["runtimes"]["opencode"]["connected"])
        self.assertFalse(disc_after["runtimes"]["luna"]["connected"])

    def test_02_operator_endpoints_enforce_scoped_auth_and_reject_worker_or_forwarded_calls(self) -> None:
        # 1. Unauthenticated requests (without Origin, simulating curl/local untrusted process) -> 401
        self.assertEqual(self.client.post("/api/workers/pairing").status_code, 401)
        self.assertEqual(
            self.client.post("/api/workers/tasks", json={"type": "orbit.ping"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/api/workers/revoke", json={"worker_id": "any"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/api/workers/tasks/t1/cancel").status_code,
            401,
        )

        # 2. Pair a worker and verify worker bearer token is forbidden (403) on operator routes
        worker = self.make_ag_worker()
        worker.pair(self.issue_code())
        worker_headers = {"Authorization": f"Bearer {worker.token}"}
        self.assertEqual(
            self.client.post("/api/workers/pairing", headers=worker_headers).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/workers/tasks",
                headers=worker_headers,
                json={"type": "orbit.ping"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/workers/revoke",
                headers=worker_headers,
                json={"worker_id": worker.worker_id},
            ).status_code,
            403,
        )

        # 3. Verify operator bearer token is forbidden (403) on worker routes
        op_worker_headers = {
            "Authorization": f"Bearer {self.op_token}",
            "X-Orbit-Nonce": "nonce-op-12345",
            "X-Orbit-Timestamp": str(int(self.clock.now().timestamp())),
        }
        self.assertEqual(
            self.client.post("/api/workers/poll", headers=op_worker_headers).status_code,
            403,
        )

        # 4. Verify forwarded headers (SSH/proxy forwarding) are rejected (403) on operator routes
        for hdr, val in (
            ("X-Forwarded-For", "100.98.90.67"),
            ("Forwarded", "for=100.98.90.67"),
            ("X-Real-IP", "100.98.90.67"),
        ):
            self.assertEqual(
                self.client.post(
                    "/api/workers/pairing",
                    headers={**self.op_headers, hdr: val},
                ).status_code,
                403,
            )

        # 5. Cross-origin browser mutation denied (403)
        self.assertEqual(
            self.client.post(
                "/api/workers/pairing",
                headers={**self.op_headers, "Origin": "https://evil.example"},
            ).status_code,
            403,
        )

        # 6. Revoked operator token is rejected (401)
        temp_op = self.broker.issue_operator_token("temp-admin")
        self.assertEqual(
            self.client.post(
                "/api/workers/pairing",
                headers={"X-Orbit-Operator-Token": temp_op},
            ).status_code,
            201,
        )
        self.broker.revoke_operator_token(temp_op)
        self.assertEqual(
            self.client.post(
                "/api/workers/pairing",
                headers={"X-Orbit-Operator-Token": temp_op},
            ).status_code,
            401,
        )

    def test_03_pairing_and_worker_auth_lifecycle_and_hash_only_storage(self) -> None:
        code = self.issue_code()
        worker = self.make_ag_worker()

        # Protocol mismatch does not consume pairing code
        mismatch = self.client.post(
            "/api/workers/pair",
            json={
                "protocol": "orbit.workers.v9",
                "pairing_code": code,
                "manifest": worker.inventory | {"worker_id": "ag-1", "capabilities": ["orbit.ping"], "status": "IDLE", "version": "0.1.0"},
            },
        )
        self.assertEqual(mismatch.status_code, 409)

        # Valid pair succeeds and hides token from snapshot/HTML
        worker.pair(code)
        raw_token = worker.token
        self.assertIsNotNone(raw_token)

        snap_text = json.dumps(self.client.get("/api/workers").json()) + self.client.get("/").text
        self.assertNotIn(raw_token, snap_text)
        self.assertNotIn(code, snap_text)

        # Check SQLite stores only hashes, never raw secrets
        with sqlite3.connect(self.database) as conn:
            dump = "\n".join(conn.iterdump())
        self.assertNotIn(raw_token, dump)
        self.assertNotIn(code, dump)
        self.assertNotIn(self.op_token, dump)

        # Reusing single-use pairing code fails with 401
        with self.assertRaises(WorkerClientError) as ctx:
            self.make_ag_worker(worker_id="ag-2").pair(code)
        self.assertEqual(ctx.exception.status, 401)

        # Expired pairing code fails with 401
        exp_code = self.issue_code()
        self.clock.advance(61)
        with self.assertRaises(WorkerClientError) as ctx:
            self.make_ag_worker(worker_id="ag-late").pair(exp_code)
        self.assertEqual(ctx.exception.status, 401)

    def test_04_concurrent_nonce_replay_is_atomic_and_never_500s(self) -> None:
        worker = self.make_ag_worker()
        worker.pair(self.issue_code())
        shared_nonce = "concurrent-nonce-0001"
        ts = str(int(self.clock.now().timestamp()))
        headers = {
            "Authorization": f"Bearer {worker.token}",
            "X-Orbit-Nonce": shared_nonce,
            "X-Orbit-Timestamp": ts,
        }

        barrier = threading.Barrier(8)
        statuses: list[int] = []
        lock = threading.Lock()

        def poll_worker() -> None:
            barrier.wait()
            resp = self.client.post("/api/workers/poll", headers=headers)
            with lock:
                statuses.append(resp.status_code)

        threads = [threading.Thread(target=poll_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(statuses.count(200), 1, f"Expected exactly one 200, got {statuses}")
        self.assertEqual(statuses.count(409), 7, f"Expected seven 409s, got {statuses}")
        self.assertNotIn(500, statuses)

    def test_05_cross_adapter_antigravity_and_grok_on_unified_broker(self) -> None:
        ag_worker = self.make_ag_worker(worker_id="antigravity-NightKnightPC")
        ag_worker.pair(self.issue_code())
        ag_worker.heartbeat("IDLE")

        grok_worker = GrokWireClient(self.client, clock=self.clock, worker_id="grok-NightKnightPC")
        grok_worker.pair(self.issue_code())
        grok_worker.heartbeat("IDLE")

        # Also verify PR #15's GrokWorker class directly if present on the host
        grok_orbit_dir = Path("/home/nightknight/Projects/NightKnight-ORBIT-grok-worker/orbit")
        if (grok_orbit_dir / "grok_adapter.py").is_file():
            import orbit

            orbit.__path__.append(str(grok_orbit_dir))
            try:
                from orbit.grok_adapter import GrokWorker as PR15GrokWorker  # type: ignore

                real_app = create_worker_app(self.base / "real_clock_broker.sqlite3")
                with TestClient(real_app, base_url="http://127.0.0.1") as rc:
                    rc_op = {"Authorization": f"Bearer {real_app.state.worker_broker.operator_token}"}
                    rc_code = rc.post("/api/workers/pairing", headers=rc_op).json()["pairing_code"]
                    pr15_grok = PR15GrokWorker(
                        "http://127.0.0.1",
                        client=rc,
                        inventory=grok_worker.inventory,
                        worker_id="grok-pr15-live",
                    )
                    pr15_grok.pair(rc_code, worker_id="grok-pr15-live")
                    rc.post(
                        "/api/workers/tasks",
                        headers=rc_op,
                        json={"type": "orbit.ping", "payload": {"message": "from-pr15-grok"}},
                    )
                    pr15_out = pr15_grok.handle_once()
                    self.assertEqual(pr15_out["action"], "completed")
                    self.assertEqual(pr15_out["result"]["pong"], "from-pr15-grok")
            finally:
                if str(grok_orbit_dir) in orbit.__path__:
                    orbit.__path__.remove(str(grok_orbit_dir))

        workers = {w["worker_id"]: w for w in self.client.get("/api/workers").json()["workers"]}
        self.assertEqual(workers["antigravity-NightKnightPC"]["presence"], "LIVE")
        self.assertEqual(workers["grok-NightKnightPC"]["presence"], "LIVE")

        # Enqueue a task requiring `orbit.smoke_check` (supported by Antigravity, not Grok)
        # and a task requiring `orbit.ping` (supported by both)
        smoke_resp = self.client.post(
            "/api/workers/tasks",
            headers=self.op_headers,
            json={
                "type": "orbit.smoke_check",
                "payload": {"output_path": "docs/CROSS-PROOF.md"},
                "grant": "local",
                "task_id": "task-smoke-1",
            },
        )
        self.assertEqual(smoke_resp.status_code, 201)

        ping_resp = self.client.post(
            "/api/workers/tasks",
            headers=self.op_headers,
            json={
                "type": "orbit.ping",
                "payload": {"message": "cross-adapter-pong"},
                "grant": "local",
                "task_id": "task-ping-1",
            },
        )
        self.assertEqual(ping_resp.status_code, 201)

        # Grok polls first: skips `orbit.smoke_check` (records capability_denied) and claims `orbit.ping`!
        grok_out = grok_worker.handle_once()
        self.assertEqual(grok_out["action"], "completed")
        self.assertEqual(grok_out["task_id"], "task-ping-1")
        self.assertEqual(grok_out["result"]["pong"], "cross-adapter-pong")

        # Antigravity polls next: claims and completes `orbit.smoke_check`!
        ag_out = ag_worker.handle_once()
        self.assertEqual(ag_out["action"], "completed")
        self.assertEqual(ag_out["task_id"], "task-smoke-1")
        self.assertEqual(ag_out["result"]["status"], "VERIFIED")
        self.assertTrue((self.base / "docs" / "CROSS-PROOF.md").is_file())

    def test_06_cancel_heartbeat_loss_and_shell_or_traversal_rejection(self) -> None:
        worker = self.make_ag_worker(capabilities=["orbit.ping", "orbit.smoke_check", "shell"])
        worker.pair(self.issue_code())
        worker.heartbeat("IDLE")

        # 1. Cancel mid-flight prevents completion
        self.client.post(
            "/api/workers/tasks",
            headers=self.op_headers,
            json={"type": "orbit.ping", "payload": {"message": "cancel-me"}, "task_id": "t-cancel"},
        )
        polled = worker.poll()
        lease = polled["lease"]
        self.client.post("/api/workers/tasks/t-cancel/cancel", headers=self.op_headers)
        with self.assertRaises(WorkerClientError) as ctx:
            worker.result("t-cancel", lease["lease_id"], "completed", {"pong": "late"}, "late")
        self.assertEqual(ctx.exception.status, 409)
        out_cancel = worker.handle_once()
        self.assertEqual(out_cancel["action"], "cancelled")

        # 2. Shell task is rejected without executing any shell
        marker = self.base / "pwned"
        self.client.post(
            "/api/workers/tasks",
            headers=self.op_headers,
            json={"type": "shell", "payload": {"command": f"touch {marker}"}, "task_id": "t-shell"},
        )
        out_shell = worker.handle_once()
        self.assertEqual(out_shell["action"], "rejected")
        self.assertFalse(out_shell["executed_shell"])
        self.assertFalse(marker.exists())

        # 3. Path traversal in `orbit.smoke_check` is rejected
        self.client.post(
            "/api/workers/tasks",
            headers=self.op_headers,
            json={
                "type": "orbit.smoke_check",
                "payload": {"output_path": "../escape.md"},
                "task_id": "t-escape",
            },
        )
        out_escape = worker.handle_once()
        self.assertEqual(out_escape["action"], "rejected")

        # 4. Lost heartbeat releases lease to backup worker
        self.client.post(
            "/api/workers/tasks",
            headers=self.op_headers,
            json={"type": "orbit.ping", "payload": {"message": "failover"}, "task_id": "t-failover"},
        )
        worker.heartbeat("IDLE")
        held = worker.poll()["lease"]
        self.assertEqual(held["task_id"], "t-failover")
        self.clock.advance(25)

        backup = self.make_ag_worker(worker_id="antigravity-backup")
        backup.pair(self.issue_code())
        backup.heartbeat("IDLE")
        recovered = backup.handle_once()
        self.assertEqual(recovered["action"], "completed")
        self.assertEqual(recovered["task_id"], "t-failover")

    def test_07_cli_rejects_pairing_in_argv_and_enforces_0600_secret_files(self) -> None:
        # 1. Passing --pairing in argv is strictly rejected
        for bad_args in (["pair", "--pairing", "secret123"], ["pair", "--pairing=secret123"]):
            with self.assertRaises(SystemExit) as ctx:
                orbit_worker_agent.main(bad_args)
            self.assertIn("forbidden", str(ctx.exception))

        # 2. write_secret_file creates 0600 file; read_secret_file rejects 0644 file
        secret_file = self.base / "pairing.code"
        write_secret_file(secret_file, "code-12345678\n")
        self.assertEqual(stat.S_IMODE(secret_file.stat().st_mode), 0o600)
        self.assertEqual(read_secret_file(secret_file), "code-12345678")

        os.chmod(secret_file, 0o644)
        with self.assertRaises(PermissionError):
            read_secret_file(secret_file)
        with self.assertRaises(SystemExit):
            orbit_worker_agent.main(["pair", "--pairing-file", str(secret_file)])

        # 3. Reading from stdin works without argv exposure
        code = orbit_worker_agent._read_pairing_code(
            pairing_stdin=True,
            stdin_stream=io.StringIO("stdin-secret-code\n"),
        )
        self.assertEqual(code, "stdin-secret-code")

        # 4. Loopback host & URL policy
        with self.assertRaises(ValueError):
            assert_loopback_host("0.0.0.0")
        with self.assertRaises(ValueError):
            assert_loopback_url("http://100.115.247.63:8765")
        tunnel = build_remote_tunnel_spec()
        self.assertFalse(tunnel["public_exposure"])
        self.assertFalse(tunnel["operator_privileged_over_tunnel"])

    def test_08_actual_orbit_fastapi_dashboard_integration(self) -> None:
        workspace = self.base / "orbit_ws"
        workspace.mkdir()
        settings = Settings(
            model="mock-local",
            base_url="http://test.invalid/v1",
            api_key="",
            workspace=workspace,
            database=self.base / "orbit.sqlite3",
            bind="127.0.0.1",
            port=8765,
        )
        model_client = httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
            )
        )
        self.addCleanup(model_client.close)

        orbit_app = create_app(settings, model_client)
        with TestClient(orbit_app, base_url="http://127.0.0.1") as client:
            health0 = client.get("/api/health").json()
            self.assertEqual(health0["status"], "ok")
            self.assertEqual(health0["worker_protocol"], PROTOCOL)
            self.assertEqual(health0["workers_live"], 0)

            proto = client.get("/api/workers/protocol").json()
            self.assertEqual(proto["protocol"], PROTOCOL)
            self.assertEqual(proto["operator_authentication"], "bearer-operator-token")

            # Unauthenticated pairing attempt on the main ORBIT app is blocked (401)
            self.assertEqual(client.post("/api/workers/pairing").status_code, 401)

            # Use the broker's scoped operator token to mint a pairing code
            app_broker: WorkerBroker = orbit_app.state.worker_broker
            op_hdr = {"Authorization": f"Bearer {app_broker.operator_token}"}
            code = client.post("/api/workers/pairing", headers=op_hdr).json()["pairing_code"]

            worker = AntigravityWorker(
                "http://127.0.0.1",
                client=client,
                state_path=self.base / "ag-state.json",
                workspace_root=workspace,
                inventory=self.inventory,
                worker_id="antigravity-NightKnightPC",
            )
            worker.pair(code)
            beat = worker.heartbeat("IDLE")
            self.assertEqual(beat["presence"], "LIVE")

            health1 = client.get("/api/health").json()
            self.assertEqual(health1["workers_live"], 1)
            self.assertEqual(health1["live_worker_ids"], ["antigravity-NightKnightPC"])

            # Enqueue and execute a smoke_check task through the ORBIT dashboard
            enq = client.post(
                "/api/workers/tasks",
                headers=op_hdr,
                json={
                    "type": "orbit.smoke_check",
                    "payload": {"output_path": "DASHBOARD-PROOF.md"},
                    "grant": "local",
                },
            )
            self.assertEqual(enq.status_code, 201)
            outcome = worker.handle_once()
            self.assertEqual(outcome["action"], "completed")
            self.assertTrue((workspace / "DASHBOARD-PROOF.md").is_file())

            workers_html = client.get("/workers").text
            self.assertIn("antigravity-NightKnightPC", workers_html)
            self.assertIn("LIVE", workers_html)
            self.assertNotIn(worker.token, workers_html)


if __name__ == "__main__":
    unittest.main()
