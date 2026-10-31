"""Unit and integration tests for ORBIT Worker Protocol v0.1 & Antigravity/Hermes/OpenCode/LUNA adapters (Issue #13)."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from orbit.worker_bridge import (
    ANTIGRAVITY_TOOLS,
    LocalWorkerCoordinator,
    OrbitWorkerAdapter,
    TaskResult,
    WorkerAuthError,
    WorkerCancelledError,
    WorkerManifest,
    WorkerProtocolError,
    build_remote_tunnel_spec,
    inspect_antigravity_runtime,
    inspect_hermes_runtime,
    inspect_host_resources,
    inspect_luna_runtime,
    inspect_opencode_runtime,
    mask_secret,
    mount_worker_bridge,
)


class WorkerBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orbit-worker-test-")
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "orbit.sqlite3"
        self.coord = LocalWorkerCoordinator(
            self.db_path,
            self.workspace,
            heartbeat_ttl=20.0,
            lease_ttl=60.0,
            pairing_ttl=300.0,
        )
        self.app = FastAPI()
        mount_worker_bridge(self.app, self.coord)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_manifest(
        self,
        worker_id: str = "antigravity-test",
        runtime_name: str = "antigravity",
        status: str = "IDLE",
        model_id: str | None = "Gemini 4 Argon (Medium)",
    ) -> WorkerManifest:
        return WorkerManifest(
            worker_id=worker_id,
            runtime_name=runtime_name,
            host_id="NightKnightPC",
            model_id=model_id,
            capabilities=("orbit.plan.deliver", "workspace.inspect", "workspace.read", "workspace.write_scoped"),
            tool_allowlist=ANTIGRAVITY_TOOLS,
            skills=("reverse-engineering-arsenal", "agy-customizations"),
            status=status,
            version="2.0-agy",
        )

    def test_runtime_and_hardware_discovery_truthful(self):
        resources = inspect_host_resources()
        self.assertTrue(resources["hostname"])
        self.assertGreaterEqual(resources["cpu_threads"], 1)
        self.assertIn("gpus", resources)

        fake_agy_bin = self.root / "antigravity"
        fake_agy_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        skill_dir = self.root / "skills" / "reverse-engineering-arsenal"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: reverse-engineering-arsenal\n---\n", encoding="utf-8")

        agy = inspect_antigravity_runtime(binary_path=fake_agy_bin, skill_roots=(self.root / "skills",))
        self.assertEqual(agy.runtime_name, "antigravity")
        self.assertEqual(agy.status, "IDLE")
        self.assertEqual(agy.model_id, "Gemini 4 Argon (Medium)")
        self.assertIn("run_command", agy.tool_allowlist)
        self.assertIn("orbit.plan.deliver", agy.capabilities)
        self.assertIn("reverse_engineering.arsenal", agy.capabilities)

        # Missing binary/service must report OFFLINE, never fake IDLE/LIVE
        luna_offline = inspect_luna_runtime(tunnel_health_url="http://127.0.0.1:59998/health", timeout=0.1)
        self.assertEqual(luna_offline.status, "OFFLINE")
        self.assertEqual(luna_offline.capabilities, ())
        self.assertIsNone(luna_offline.model_id)

        # OpenCode must be identified as worker runtime, not an LLM model
        oc = inspect_opencode_runtime(timeout=0.2)
        self.assertEqual(oc.runtime_name, "opencode")
        self.assertIsNone(oc.model_id)

    def test_pairing_invalid_expired_revoked_and_replay(self):
        manifest = self._make_manifest()
        adapter = OrbitWorkerAdapter(
            base_url="http://testserver",
            manifest=manifest,
            client=self.client,  # type: ignore[arg-type]
            default_workspace=self.workspace,
        )

        # 1. Invalid pairing credential rejected
        with self.assertRaises(WorkerAuthError):
            adapter.pair_and_register("orb_pair_invalid_secret_code")

        # 2. Expired pairing credential rejected
        expired = self.coord.create_pairing_code(ttl=10.0, now=1000.0)
        with self.assertRaises(WorkerAuthError):
            self.coord.register_worker(expired["pairing_code"], manifest, now=1020.0)

        # 3. Revoked pairing credential rejected
        revoked_p = self.coord.create_pairing_code()
        self.assertTrue(self.coord.revoke_pairing(revoked_p["pairing_id"]))
        with self.assertRaises(WorkerAuthError):
            adapter.pair_and_register(revoked_p["pairing_code"])

        # 4. Valid pairing succeeds once, then replay of the same pairing code is blocked
        valid = self.coord.create_pairing_code(allowed_runtime="antigravity")
        reg = adapter.pair_and_register(valid["pairing_code"], nonce="unique-nonce-001")
        self.assertEqual(reg["worker_id"], "antigravity-test")
        self.assertTrue(adapter.worker_token)
        self.assertNotIn(adapter.worker_token, adapter.token_hint)

        with self.assertRaises(WorkerAuthError):
            adapter.pair_and_register(valid["pairing_code"], nonce="unique-nonce-002")

        # 5. Replayed nonce with another valid pairing code is blocked
        valid2 = self.coord.create_pairing_code(allowed_runtime="antigravity")
        with self.assertRaises(WorkerAuthError):
            adapter.pair_and_register(valid2["pairing_code"], nonce="unique-nonce-001")

        # 6. Verify SQLite never stores plaintext pairing code or worker token
        raw_db_bytes = self.db_path.read_bytes()
        self.assertNotIn(valid["pairing_code"].encode("utf-8"), raw_db_bytes)
        self.assertNotIn(adapter.worker_token.encode("utf-8"), raw_db_bytes)

        # 7. Revoking worker token immediately blocks heartbeat and polling
        self.coord.revoke_worker_tokens("antigravity-test")
        with self.assertRaises(WorkerAuthError):
            adapter.send_heartbeat()
        with self.assertRaises(WorkerAuthError):
            adapter.poll_offers()

        # 8. Offline runtime manifest cannot register as active worker
        offline_manifest = self._make_manifest(worker_id="offline-w", status="OFFLINE")
        valid3 = self.coord.create_pairing_code()
        offline_adapter = OrbitWorkerAdapter(
            base_url="http://testserver",
            manifest=offline_manifest,
            client=self.client,  # type: ignore[arg-type]
        )
        with self.assertRaises(WorkerProtocolError):
            offline_adapter.pair_and_register(valid3["pairing_code"])

    def test_heartbeat_liveness_and_lost_worker_requeue(self):
        p1 = self.coord.create_pairing_code(now=100.0)
        m1 = self._make_manifest(worker_id="worker-alpha")
        self.coord.register_worker(p1["pairing_code"], m1, now=100.0)
        self.coord.heartbeat("worker-alpha", status="IDLE", now=105.0)

        task = self.coord.enqueue_task(
            "Deliver alpha artifact",
            {"op": "deliver", "path": "alpha.txt", "must_contain": ["ALPHA_OK"]},
            now=106.0,
        )
        claimed = self.coord.claim_task(task["task_id"], "worker-alpha", now=107.0)
        self.assertEqual(claimed["lease"]["status"], "ACTIVE")
        self.assertEqual(self.coord.get_worker("worker-alpha", now=108.0)["status"], "BUSY")

        # Advance time past heartbeat_ttl (20s): worker-alpha becomes OFFLINE and task returns to PENDING
        worker_after = self.coord.get_worker("worker-alpha", now=140.0)
        self.assertEqual(worker_after["status"], "OFFLINE")
        requeued = self.coord.get_task(task["task_id"])
        self.assertEqual(requeued["status"], "PENDING")
        self.assertIsNone(requeued["worker_id"])

        # A second live worker can now claim and complete the requeued task
        p2 = self.coord.create_pairing_code(now=141.0)
        m2 = self._make_manifest(worker_id="worker-beta", runtime_name="hermes", model_id=None)
        self.coord.register_worker(p2["pairing_code"], m2, now=141.0)
        claimed2 = self.coord.claim_task(task["task_id"], "worker-beta", now=142.0)
        self.assertEqual(claimed2["lease"]["worker_id"], "worker-beta")

    def test_duplicate_task_lease_prevention(self):
        p1 = self.coord.create_pairing_code()
        p2 = self.coord.create_pairing_code()
        w1 = OrbitWorkerAdapter(
            base_url="http://testserver",
            manifest=self._make_manifest(worker_id="w-one"),
            client=self.client,  # type: ignore[arg-type]
            default_workspace=self.workspace,
        )
        w2 = OrbitWorkerAdapter(
            base_url="http://testserver",
            manifest=self._make_manifest(worker_id="w-two", runtime_name="opencode", model_id=None),
            client=self.client,  # type: ignore[arg-type]
            default_workspace=self.workspace,
        )
        w1.pair_and_register(p1["pairing_code"])
        w2.pair_and_register(p2["pairing_code"])

        task = self.coord.enqueue_task(
            "Single lease task",
            {"op": "deliver", "path": "single.txt", "must_contain": ["LEASE_OK"]},
        )

        # Worker 1 claims task
        claim1 = w1.claim(task["task_id"])
        lease_id = claim1["lease"]["lease_id"]

        # Idempotent re-claim by Worker 1 returns the same lease
        claim1_again = w1.claim(task["task_id"])
        self.assertEqual(claim1_again["lease"]["lease_id"], lease_id)

        # Worker 2 attempting to claim the same task is rejected (409)
        with self.assertRaises(WorkerProtocolError):
            w2.claim(task["task_id"])

        # Worker 2 also does not see the leased task in poll_offers()
        self.assertEqual(w2.poll_offers(), [])

    def test_end_to_end_task_delivery_and_lying_worker_detection(self):
        p = self.coord.create_pairing_code()
        adapter = OrbitWorkerAdapter(
            base_url="http://testserver",
            manifest=self._make_manifest(worker_id="agy-e2e"),
            client=self.client,  # type: ignore[arg-type]
            default_workspace=self.workspace,
        )
        adapter.pair_and_register(p["pairing_code"])

        # 1. Honest worker delivers artifact and server verifies SHA-256 + content
        t1 = self.coord.enqueue_task(
            "Create verified proof",
            {"op": "deliver", "path": "docs/proof.md", "must_contain": ["PROOF_TOKEN_42"]},
            grant={"root": str(self.workspace), "denied_paths": []},
        )
        res1 = adapter.run_once()
        self.assertIsNotNone(res1)
        self.assertEqual(res1["status"], "VERIFIED")
        self.assertTrue((self.workspace / "docs" / "proof.md").is_file())
        self.assertIn("PROOF_TOKEN_42", (self.workspace / "docs" / "proof.md").read_text(encoding="utf-8"))

        # 2. Lying worker submits 'SUBMITTED' without creating the required file -> coordinator marks FAILED
        t2 = self.coord.enqueue_task(
            "Unverified claim test",
            {"op": "deliver", "path": "docs/missing.md", "must_contain": ["MUST_EXIST"]},
            grant={"root": str(self.workspace), "denied_paths": []},
        )
        claimed2 = adapter.claim(t2["task_id"])
        fake_result = TaskResult(
            task_id=t2["task_id"],
            lease_id=claimed2["lease"]["lease_id"],
            worker_id="agy-e2e",
            status="SUBMITTED",
            summary="I promise I wrote docs/missing.md!",
            artifacts=[{"path": "docs/missing.md"}],
        )
        reported2 = adapter.submit_result(fake_result)
        self.assertEqual(reported2["status"], "FAILED")
        self.assertFalse(reported2["result"]["verified"])

    def test_deterministic_cancellation_mid_task(self):
        p = self.coord.create_pairing_code()
        adapter = OrbitWorkerAdapter(
            base_url="http://testserver",
            manifest=self._make_manifest(worker_id="agy-cancel"),
            client=self.client,  # type: ignore[arg-type]
            default_workspace=self.workspace,
        )
        adapter.pair_and_register(p["pairing_code"])

        task = self.coord.enqueue_task(
            "Task to cancel mid-flight",
            {"op": "deliver", "path": "cancelled.txt", "must_contain": ["NEVER_WRITTEN"]},
            grant={"root": str(self.workspace), "denied_paths": []},
        )

        def cancel_during_step():
            self.coord.cancel_task(task["task_id"], reason="User requested cancellation")
            adapter.send_heartbeat(status="BUSY")

        outcome = adapter.run_once(on_step_hook=cancel_during_step)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["status"], "CANCELLED")
        self.assertFalse((self.workspace / "cancelled.txt").exists())

        # Late result submission after cancellation must be rejected (409)
        with self.assertRaises(WorkerCancelledError):
            adapter.submit_result(
                TaskResult(
                    task_id=task["task_id"],
                    lease_id="",
                    worker_id="agy-cancel",
                    status="SUBMITTED",
                    summary="late result",
                )
            )

    def test_unavailable_model_and_permission_denial_do_not_halt_unrelated_tasks(self):
        p = self.coord.create_pairing_code()
        adapter = OrbitWorkerAdapter(
            base_url="http://testserver",
            manifest=self._make_manifest(worker_id="agy-policy"),
            client=self.client,  # type: ignore[arg-type]
            default_workspace=self.workspace,
            model_available_check=lambda m: m == "Gemini 4 Argon (Medium)",
        )
        adapter.pair_and_register(p["pairing_code"])

        grant = {"root": str(self.workspace), "denied_paths": ["restricted/"]}

        # Task 1: Requires offline model -> fails gracefully with MODEL_UNAVAILABLE
        t_model = self.coord.enqueue_task(
            "Needs offline 70B model",
            {"op": "deliver", "path": "model_out.txt", "require_model": "offline-heretic-70b"},
            grant=grant,
        )
        r_model = adapter.run_once()
        self.assertEqual(r_model["status"], "FAILED")
        self.assertIn("MODEL_UNAVAILABLE", r_model["result"]["summary"])

        # Task 2: Arbitrary shell execution attempt -> BLOCKED
        t_shell = self.coord.enqueue_task(
            "Untrusted shell op",
            {"op": "shell", "command": "cat /etc/shadow"},
            grant=grant,
        )
        r_shell = adapter.run_once()
        self.assertEqual(r_shell["status"], "BLOCKED")
        self.assertIn("GRANT DENIED", r_shell["result"]["summary"])

        # Task 3: Denied path in grant -> BLOCKED
        t_denied = self.coord.enqueue_task(
            "Write to denied folder",
            {"op": "deliver", "path": "restricted/secret.txt", "must_contain": ["NOPE"]},
            grant=grant,
        )
        r_denied = adapter.run_once()
        self.assertEqual(r_denied["status"], "BLOCKED")

        # Task 4: Sensitive filename (.env) -> BLOCKED
        t_env = self.coord.enqueue_task(
            "Write .env file",
            {"op": "deliver", "path": ".env", "must_contain": ["SECRET=1"]},
            grant=grant,
        )
        r_env = adapter.run_once()
        self.assertEqual(r_env["status"], "BLOCKED")

        # Task 5: Unrelated valid task in the same mission -> SUCCEEDS and is VERIFIED!
        t_ok = self.coord.enqueue_task(
            "Valid sibling task",
            {"op": "deliver", "path": "allowed/report.md", "must_contain": ["MISSION_CONTINUES"]},
            grant=grant,
        )
        r_ok = adapter.run_once()
        self.assertEqual(r_ok["status"], "VERIFIED")
        self.assertTrue((self.workspace / "allowed" / "report.md").is_file())

    def test_persistence_across_coordinator_restart(self):
        p = self.coord.create_pairing_code()
        adapter = OrbitWorkerAdapter(
            base_url="http://testserver",
            manifest=self._make_manifest(worker_id="agy-persist"),
            client=self.client,  # type: ignore[arg-type]
            default_workspace=self.workspace,
        )
        adapter.pair_and_register(p["pairing_code"])
        t1 = self.coord.enqueue_task(
            "Persisted task 1",
            {"op": "deliver", "path": "persisted.txt", "must_contain": ["SURVIVED_RESTART"]},
        )
        adapter.run_once()

        # Simulate coordinator restart on the same SQLite database
        restarted_coord = LocalWorkerCoordinator(self.db_path, self.workspace)
        restarted_app = FastAPI()
        mount_worker_bridge(restarted_app, restarted_coord)
        restarted_client = TestClient(restarted_app)

        # Existing worker token and verified task state remain intact after restart
        adapter._client = restarted_client  # type: ignore[assignment]
        beat = adapter.send_heartbeat(status="IDLE")
        self.assertEqual(beat["worker"]["worker_id"], "agy-persist")
        saved_task = restarted_coord.get_task(t1["task_id"])
        self.assertEqual(saved_task["status"], "VERIFIED")

    def test_issue_12_mission_api_wire_compatibility(self):
        """Verify OrbitWorkerAdapter speaks Issue #12's exact /api/workers/v0.1 wire format."""
        app12 = FastAPI()
        router12 = APIRouter()
        state: dict = {
            "pairing_code": "A1B2C3-D4E5F6-112233",
            "worker_token": "tok_issue12_secret_token_1234567890",
            "task": {
                "task_id": "task-12-demo",
                "title": "Deliver orbit-demo/report.md",
                "status": "ASSIGNED",
                "attempt": 1,
                "mission_id": "mission-12",
                "spec": {"op": "deliver", "path": "orbit-demo/report.md", "must_contain": ["VERIFIED"]},
            },
            "reported": None,
        }

        @router12.post("/api/workers/v0.1/register", status_code=201)
        def reg12(payload: dict):
            if payload.get("pairing_code") != state["pairing_code"]:
                raise HTTPException(403, "unknown pairing code")
            if payload.get("kind") not in ("mock", "hermes", "opencode", "custom"):
                raise HTTPException(403, "invalid kind")
            return {
                "worker_id": "w12-id",
                "worker_token": state["worker_token"],
                "heartbeat_ttl": 20.0,
                "capabilities": payload.get("capabilities") or [],
                "status": "OFFLINE",
            }

        @router12.post("/api/workers/v0.1/heartbeat")
        def beat12(payload: dict):
            if payload.get("worker_token") != state["worker_token"]:
                raise HTTPException(403, "invalid token")
            return {"worker_id": "w12-id", "status": "IDLE", "heartbeat_ttl": 20.0, "assigned_task": state["task"]}

        @router12.post("/api/workers/v0.1/poll")
        def poll12(payload: dict):
            if payload.get("worker_token") != state["worker_token"]:
                raise HTTPException(403, "invalid token")
            return {"assigned_task": state["task"]}

        @router12.post("/api/workers/v0.1/claim")
        def claim12(payload: dict):
            if payload.get("worker_token") != state["worker_token"]:
                raise HTTPException(403, "invalid token")
            state["task"]["status"] = "RUNNING"
            return {"claimed": True, "task": state["task"]}

        @router12.post("/api/workers/v0.1/report")
        def report12(payload: dict):
            if payload.get("worker_token") != state["worker_token"]:
                raise HTTPException(403, "invalid token")
            if not isinstance(payload.get("artifacts"), list) or not all(isinstance(x, str) for x in payload["artifacts"]):
                raise HTTPException(422, "artifacts must be list[str]")
            state["task"]["status"] = "SUBMITTED"
            state["reported"] = payload
            return {"reported": True, "task": state["task"]}

        app12.include_router(router12)
        client12 = TestClient(app12)

        adapter = OrbitWorkerAdapter(
            base_url="http://testserver",
            manifest=self._make_manifest(worker_id="Antigravity-NK", runtime_name="antigravity"),
            client=client12,  # type: ignore[arg-type]
            default_workspace=self.workspace,
        )
        reg = adapter.pair_and_register(state["pairing_code"])
        self.assertEqual(reg["worker_id"], "w12-id")
        out = adapter.run_once()
        self.assertTrue(out["reported"])
        self.assertEqual(state["reported"]["artifacts"], ["orbit-demo/report.md"])
        self.assertTrue((self.workspace / "orbit-demo" / "report.md").is_file())

    def test_remote_ssh_tailscale_tunnel_spec(self):
        spec = build_remote_tunnel_spec("ubuntu-hermes", tailscale_ip="100.98.90.67")
        self.assertFalse(spec["public_exposure"])
        self.assertFalse(spec["root_required"])
        self.assertIn("127.0.0.1:18765:127.0.0.1:8765", spec["ssh_command"])
        self.assertNotIn("0.0.0.0", spec["ssh_command"])


if __name__ == "__main__":
    unittest.main()
