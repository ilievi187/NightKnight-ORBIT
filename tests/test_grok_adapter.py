import json
import stat
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from orbit.grok_adapter import (
    EXECUTABLE_CAPABILITIES,
    GrokWorker,
    assert_loopback_url,
    build_inventory,
    main,
    scan_skill_names,
)
from orbit.worker_broker import create_worker_app
from orbit.worker_protocol import PROTOCOL


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "broker.sqlite3"
        self.app = create_worker_app(self.database, offline_after=30, token_ttl=600, pairing_ttl=600)
        self.http = TestClient(self.app)
        self.addCleanup(self.http.close)
        self.addCleanup(self.app.state.broker.close)
        self.logs = []
        self.inventory = build_inventory(skill_roots=[], host_id="NightKnightPC")
        self.worker = GrokWorker(
            "http://127.0.0.1",
            client=self.http,
            inventory=self.inventory,
            log=self.logs.append,
        )

    def pair(self, **kwargs):
        code = self.http.post("/api/workers/pairing").json()["pairing_code"]
        if kwargs:
            self.worker = GrokWorker(
                "http://127.0.0.1",
                client=self.http,
                inventory=self.inventory,
                log=self.logs.append,
                **kwargs,
            )
        public = self.worker.pair(code, worker_id="grok-NightKnightPC")
        self.assertNotIn("token", public)
        self.assertTrue(all(self.worker.token not in line for line in self.logs))
        return public

    def test_inventory_is_honest_about_what_orbit_can_execute(self):
        self.assertEqual(self.inventory["model_id"], "grok-4.7")
        self.assertFalse(self.inventory["model_endpoint_probed"])
        self.assertFalse(self.inventory["model_ready"])
        self.assertFalse(self.inventory["arbitrary_shell_from_dashboard"])
        self.assertFalse(self.inventory["observed_session_tools_executable_through_orbit"])
        self.assertFalse(self.inventory["mcp_tools_invoked"])
        self.assertEqual(self.inventory["executable_capabilities"], list(EXECUTABLE_CAPABILITIES))
        self.assertIn("read_file", self.inventory["observed_session_tools"])
        self.assertNotIn("read_file", self.inventory["executable_capabilities"])
        self.assertIn("tasks", self.inventory["mcp_servers_connected"])

    def test_skill_scan_names_directories_and_does_not_read_contents(self):
        root = Path(self.temp.name) / "skills"
        secret = "SECRET-SKILL-BODY"
        (root / "alpha").mkdir(parents=True)
        (root / "alpha" / "SKILL.md").write_text(secret)
        (root / "group" / "beta").mkdir(parents=True)
        (root / "group" / "beta" / "SKILL.md").write_text(secret)
        (root / "node_modules" / "hidden").mkdir(parents=True)
        (root / "node_modules" / "hidden" / "SKILL.md").write_text(secret)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text(secret)
        link = root / "linked"
        link.symlink_to(outside, target_is_directory=True)
        names = scan_skill_names([root])
        self.assertEqual(names, ["alpha", "group/beta", "linked"])
        self.assertNotIn(secret, json.dumps(names))

    def test_ping_round_trip_is_visible_and_shell_task_is_not_executed(self):
        self.pair()
        created = self.http.post("/api/workers/tasks", json={"type": "orbit.ping", "payload": {"message": "orbit-ping"}, "grant": "local-test"})
        self.assertEqual(created.status_code, 201, created.text)
        outcome = self.worker.handle_once()
        self.assertEqual(outcome["action"], "completed")
        self.assertEqual(outcome["result"]["pong"], "orbit-ping")
        self.assertFalse(outcome["executed_shell"])
        with TestClient(self.app) as client:
            page = client.get("/").text
            tasks = client.get("/api/workers/tasks").json()["tasks"]
        self.assertIn("grok-NightKnightPC", page)
        self.assertIn("orbit-ping", page)
        self.assertNotIn(self.worker.token, page)
        self.assertEqual(tasks[0]["status"], "completed")
        self.assertEqual(tasks[0]["executed_shell"], False)
        workers = self.http.get("/api/workers").json()["workers"]
        self.assertEqual(workers[0]["presence"], "LIVE")
        self.assertEqual(workers[0]["model_id"], "grok-4.7")
        self.assertFalse(workers[0]["inventory_summary"]["model_ready"])

    def test_cancel_and_unsupported_shell_do_not_run_a_handler(self):
        self.pair()
        self.http.post("/api/workers/tasks", json={"type": "orbit.ping", "payload": {"message": "stop"}, "task_id": "cancelme01"})
        self.worker.heartbeat()
        leased = self.worker.poll()["lease"]
        called = {"count": 0}

        def forbidden(_lease):
            called["count"] += 1
            return {"pong": "no", "executed_shell": False}

        self.worker.handlers["orbit.ping"] = forbidden
        self.http.post("/api/workers/tasks/cancelme01/cancel")
        outcome = self.worker.handle_once()
        self.assertEqual(outcome["action"], "cancelled")
        self.assertEqual(called["count"], 0)
        self.assertEqual(leased["task_id"], "cancelme01")

        marker = Path(self.temp.name) / "should-not-exist"
        shell_worker = GrokWorker(
            "http://127.0.0.1",
            client=self.http,
            inventory=self.inventory,
            capabilities=["shell"],
            log=self.logs.append,
        )
        code = self.http.post("/api/workers/pairing").json()["pairing_code"]
        shell_worker.pair(code, worker_id="grok-shell-test")
        self.http.post("/api/workers/tasks", json={"type": "shell", "payload": {"command": f"touch {marker}"}, "grant": "no"})
        rejected = shell_worker.handle_once()
        self.assertEqual(rejected["action"], "rejected")
        self.assertFalse(rejected["executed_shell"])
        self.assertFalse(marker.exists())
        detail = self.http.get("/api/workers/tasks").json()
        shell_task = next(item for item in detail["tasks"] if item["type"] == "shell")
        self.assertEqual(shell_task["status"], "rejected")
        self.assertNotIn("touch", json.dumps(shell_task["result"]))

    def test_state_file_permissions_and_loopback_url_policy(self):
        state = Path(self.temp.name) / "grok-worker.json"
        self.worker.state_path = state
        self.pair()
        self.assertTrue(state.is_file())
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        stored = json.loads(state.read_text())
        self.assertEqual(stored["protocol"], PROTOCOL)
        self.assertNotIn(stored["token"], "\n".join(self.logs))
        with self.assertRaises(ValueError):
            assert_loopback_url("http://100.115.247.63:8766")
        with self.assertRaises(ValueError):
            assert_loopback_url("http://0.0.0.0:8766")
        with self.assertRaises(ValueError):
            assert_loopback_url("https://127.0.0.1:8766")
        self.assertEqual(assert_loopback_url("http://localhost:8766"), "http://localhost:8766")
        with self.assertRaises(SystemExit):
            main(["pair", "--url", "http://10.0.0.8:8766", "--state", str(state)])

    def test_revoked_worker_is_not_reported_ready(self):
        self.pair()
        self.http.post("/api/workers/revoke", json={"worker_id": "grok-NightKnightPC"})
        with self.assertRaises(Exception) as caught:
            self.worker.heartbeat()
        self.assertEqual(caught.exception.status, 401)
        listed = self.http.get("/api/workers").json()["workers"][0]
        self.assertEqual(listed["presence"], "OFFLINE")
        self.assertTrue(listed["revoked"])


if __name__ == "__main__":
    unittest.main()
