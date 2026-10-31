"""HTTP integration tests: worker loop v0.1 through the FastAPI app.

Follows the existing suite's conventions (unittest + TestClient). The Plan
Mode router is additive; the pre-existing contract tests in test_orbit.py
must keep passing unchanged.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from orbit.planmode import PlanStore, Supervisor
from orbit.app import create_app, Settings


class MissionApiBase(unittest.TestCase):
    """Boots the real app with a temp environment (no background tick)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orbit-mapi-"))
        self.env = {
            "ORBIT_WORKSPACE": str(self.tmp / "workspace"),
            "ORBIT_DATA_DIR": str(self.tmp / "data"),
            "ORBIT_MISSION_TICK": "0",
        }
        (self.tmp / "workspace").mkdir(parents=True)
        self._saved_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.app = create_app()
        self.client = TestClient(self.app)
        self.now = [time.time()]

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def demo_mission(self):
        response = self.client.post("/api/lab/demo/mission", json={})
        self.assertEqual(response.status_code, 201)
        return response.json()

    def register_worker_via_api(self, name="MockA"):
        pairing = self.client.post("/api/lab/pairing").json()
        response = self.client.post("/api/workers/v0.1/register", json={
            "pairing_code": pairing["pairing_code"], "name": name, "host": "nightknight",
            "kind": "mock", "capabilities": ["orbit.plan.deliver"], "version": "0.1"})
        self.assertEqual(response.status_code, 201, response.text)
        worker = response.json()
        beat = self.client.post("/api/workers/v0.1/heartbeat", json={"worker_token": worker["worker_token"]})
        self.assertEqual(beat.status_code, 200)
        return worker


class HealthAndContractTests(MissionApiBase):
    def test_existing_contract_untouched(self):
        data = self.client.get("/api/health").json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["version"], "0.1.0")
        self.assertEqual(data["write_policy"], "human_approval")

    def test_profiles_still_separate(self):
        agents = self.client.get("/api/agents").json()
        self.assertTrue(all("id" in a and "description" in a for a in agents))


class RegistrationFlowTests(MissionApiBase):
    def test_register_requires_valid_pairing(self):
        response = self.client.post("/api/workers/v0.1/register", json={
            "pairing_code": "NOPE-NOPE-NOPE", "name": "X", "host": "h",
            "kind": "mock", "capabilities": ["c"]})
        self.assertEqual(response.status_code, 403)

    def test_register_burns_pairing(self):
        worker = self.register_worker_via_api()
        replay = self.client.post("/api/workers/v0.1/register", json={
            "pairing_code": "NOPE", "name": "Y", "host": "h", "kind": "mock",
            "capabilities": ["orbit.plan.deliver"]})
        self.assertEqual(replay.status_code, 403)

    def test_worker_token_guard_all_routes(self):
        for path, payload in [
            ("/api/workers/v0.1/heartbeat", {"worker_token": "bogus-token-value-123456"}),
            ("/api/workers/v0.1/poll", {"worker_token": "bogus-token-value-123456"}),
            ("/api/workers/v0.1/claim", {"worker_token": "bogus-token-value-123456"}),
            ("/api/workers/v0.1/report", {"worker_token": "bogus-token-value-123456"}),
        ]:
            response = self.client.post(path, json=payload)
            self.assertEqual(response.status_code, 403, path)

    def test_fleet_listing_no_secrets(self):
        self.register_worker_via_api()
        workers = self.client.get("/api/workers").json()["workers"]
        self.assertEqual(len(workers), 1)
        blob = self.client.get("/api/workers").text
        self.assertNotIn("token", blob.lower().replace("history.context", ""))  # no token material


class MissionFlowThroughApiTests(MissionApiBase):
    def test_full_demo_mission_completion(self):
        """Mirror the real mock-worker loop: heartbeat -> claim what arrives ->
        write artifacts -> report, until terminal. First engine.py attempt is
        intentionally broken (flaky demo knob) so the verifier FAILS it and
        REPLAN recovers at iteration 2."""
        mission = self.demo_mission()
        worker = self.register_worker_via_api()
        mid = mission["id"]
        token = worker["worker_token"]
        grant_root = Path(mission["grant"]["root"])
        flaky_state = {"engine_broken": False}

        import hashlib

        def _good_content(path):
            base = "# ORBIT demo artifact\n"
            if path.endswith("engine.py"):
                return base + "/* engine */ class DemoEngine:\n    def run(self): pass\n"
            if path.endswith("report.md"):
                return base + "VERIFIED\n"
            if path.endswith("README.md"):
                return base + "mentions DemoEngine\n"
            return base

        def worker_cycle():
            data = self.client.post("/api/workers/v0.1/heartbeat",
                                    json={"worker_token": token}).json()
            task = data.get("assigned_task")
            if not task or task["status"] != "ASSIGNED":
                return None
            claim = self.client.post("/api/workers/v0.1/claim", json={"worker_token": token})
            if claim.status_code != 200:
                return None
            claimed = claim.json()["task"]
            path = claimed["spec"].get("path")
            broken = path.endswith("engine.py") and claimed["attempt"] == 1 and not flaky_state["engine_broken"]
            if broken:
                flaky_state["engine_broken"] = True
            content = "# broken first attempt" if broken else _good_content(path)
            (grant_root / path).parent.mkdir(parents=True, exist_ok=True)
            (grant_root / path).write_text(content, encoding="utf-8")
            response = self.client.post("/api/workers/v0.1/report", json={
                "worker_token": token, "status": "done",
                "notes": "flaky demo attempt" if broken else "submitted",
                "artifacts": [f"{path}@{hashlib.sha256(content.encode()).hexdigest()[:12]}"]})
            self.assertEqual(response.status_code, 200)
            return claimed

        stage = None
        for _ in range(40):
            result = worker_cycle()
            if result is None:
                self.client.post(f"/api/missions/{mid}/advance")
            stage = self.client.get(f"/api/missions/{mid}").json()["stage"]
            if stage in ("COMPLETE", "BLOCKED", "CANCELLED"):
                break

        view = self.client.get(f"/api/missions/{mid}").json()
        self.assertEqual(view["stage"], "COMPLETE",
                         [e["message"][:90] for e in view["events"][:6]])
        self.assertEqual(view["iteration"], 2, "flaky first attempt must drive one replan")
        self.assertTrue(flaky_state["engine_broken"], "flaky knob must have been exercised")
        self.assertTrue(all(c["satisfied"] for c in view["criteria"]))
        statuses = [t["status"] for t in view["tasks"]]
        self.assertIn("BLOCKED", statuses)   # scoped denial present but non-gating
        self.assertIn("VERIFIED", statuses)
        self.assertTrue(any(t["attempt"] == 2 and t["status"] == "VERIFIED"
                            for t in view["tasks"]), [t["status"] for t in view["tasks"]])
        self.assertTrue(any(t["attempt"] == 1 and t["status"] == "FAILED"
                            for t in view["tasks"]), statuses)

    def test_worker_listing_shows_live_worker(self):
        self.demo_mission()
        worker = self.register_worker_via_api(name="FleetViewer")
        workers = self.client.get("/api/workers").json()["workers"]
        entry = workers[0]
        self.assertEqual(entry["name"], "FleetViewer")
        self.assertIn(entry["status"], ("LIVE", "IDLE", "OFFLINE"))
        self.assertNotIn("worker_token", entry)
        self.assertNotIn("token_hash", entry)

    def test_unassigned_claim_rejected(self):
        self.demo_mission()
        pairing = self.client.post("/api/lab/pairing").json()
        worker = self.client.post("/api/workers/v0.1/register", json={
            "pairing_code": pairing["pairing_code"], "name": "NoBeat", "host": "h",
            "kind": "mock", "capabilities": ["orbit.plan.deliver"]}).json()
        # never heartbeated -> not eligible -> nothing to claim
        response = self.client.post("/api/workers/v0.1/claim", json={"worker_token": worker["worker_token"]})
        self.assertEqual(response.status_code, 409)

    def test_foreign_worker_cannot_claim_others_task(self):
        self.demo_mission()
        worker_a = self.register_worker_via_api("WorkerA")
        worker_b = self.register_worker_via_api("WorkerB")
        # A claims; B should fail (its own queue is empty after A took the task)
        got = self.client.post("/api/workers/v0.1/claim", json={"worker_token": worker_a["worker_token"]})
        self.assertEqual(got.status_code, 200, got.text)
        response = self.client.post("/api/workers/v0.1/claim", json={"worker_token": worker_b["worker_token"]})
        self.assertEqual(response.status_code, 409)

    def test_report_before_claim_rejected(self):
        self.demo_mission()
        worker = self.register_worker_via_api()
        response = self.client.post("/api/workers/v0.1/report", json={
            "worker_token": worker["worker_token"], "status": "done", "notes": "", "artifacts": []})
        self.assertEqual(response.status_code, 409)

    def test_mission_endpoints_404(self):
        self.assertEqual(self.client.get("/api/missions/does-not-exist").status_code, 404)
        self.assertEqual(self.client.post("/api/missions/does-not-exist/cancel").status_code, 404)

    def test_demo_worker_binary_hint(self):
        from orbit import mission_api
        with unittest.mock.patch.object(mission_api, "find_mock_worker_bundle", return_value=None):
            response = self.client.post("/api/lab/demo/worker")
        self.assertEqual(response.status_code, 503)
        self.assertIn("no capacity invented", response.json()["detail"])


class DemoLaunchTests(MissionApiBase):
    def test_trusted_demo_launcher_uses_bundled_script(self):
        """The demo launcher must only ever run the bundled tools/mock-worker.py."""
        script = self.tmp / "workspace" / "tools-standin.py"
        script.write_text("# stand-in for bundled worker", encoding="utf-8")
        os.environ["ORBIT_MOCK_WORKER"] = str(script)
        with unittest.mock.patch("subprocess.Popen") as popen:
            popen.return_value.pid = 4242
            response = self.client.post("/api/lab/demo/worker")
        self.assertEqual(response.status_code, 201, response.text)
        args = popen.call_args[0][0]
        self.assertEqual(args[1], str(script), "launcher must not run user-supplied commands")
        self.assertTrue(any(a == "--pairing" for a in args))


import unittest.mock  # noqa: E402  (placed here intentionally after imports)


if __name__ == "__main__":
    unittest.main()
