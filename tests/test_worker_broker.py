import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orbit.config import Settings
from orbit.worker_broker import WorkerBroker, create_worker_app, mount_worker_api
from orbit.worker_protocol import PROTOCOL, ManualClock, assert_loopback_host


def manifest(worker_id="grok-test", capabilities=None, status="IDLE"):
    return {
        "worker_id": worker_id,
        "runtime_name": "grok-build",
        "host_id": "NightKnightPC",
        "model_id": "grok-4.7",
        "model_id_source": "harness_session_identity",
        "capabilities": ["orbit.ping", "orbit.inventory"] if capabilities is None else capabilities,
        "status": status,
        "version": "0.1.0",
        "inventory": {
            "arbitrary_shell_from_dashboard": False,
            "model_ready": False,
            "model_endpoint_probed": False,
            "skill_count": 0,
            "mcp_invoked": False,
        },
    }


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = ManualClock()
        self.database = Path(self.temp.name) / "broker.sqlite3"
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
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.addCleanup(self.app.state.broker.close)

    def headers(self, token, nonce):
        return {
            "Authorization": f"Bearer {token}",
            "X-Orbit-Nonce": nonce,
            "X-Orbit-Timestamp": str(int(self.clock.now().timestamp())),
        }

    def issue_code(self):
        response = self.client.post("/api/workers/pairing")
        self.assertEqual(response.status_code, 201)
        return response.json()["pairing_code"]

    def pair(self, body=None, code=None):
        pairing_code = code or self.issue_code()
        response = self.client.post(
            "/api/workers/pair",
            json={"protocol": PROTOCOL, "pairing_code": pairing_code, "manifest": body or manifest()},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json(), pairing_code

    def test_pair_hides_token_after_issue_and_dashboard_shows_offline_until_heartbeat(self):
        paired, code = self.pair()
        token = paired["token"]
        self.assertEqual(paired["protocol"], PROTOCOL)
        listed = self.client.get("/api/workers").json()
        blob = json.dumps(listed) + self.client.get("/").text + json.dumps(self.client.get("/api/health").json())
        self.assertNotIn(token, blob)
        self.assertNotIn(code, blob)
        self.assertNotIn("token_hash", blob)
        self.assertEqual(listed["workers"][0]["presence"], "OFFLINE")
        self.assertEqual(self.client.get("/api/health").json()["executes_payloads"], False)
        beat = self.client.post("/api/workers/heartbeat", headers=self.headers(token, "nonce-heart-1"), json={"manifest": manifest()})
        self.assertEqual(beat.status_code, 200, beat.text)
        self.assertEqual(beat.json()["presence"], "LIVE")
        page = self.client.get("/").text
        self.assertIn("grok-test", page)
        self.assertIn("LIVE", page)
        self.assertNotIn(token, page)

    def test_invalid_reused_and_expired_pairing_codes(self):
        code = self.issue_code()
        bad = self.client.post(
            "/api/workers/pair",
            json={"protocol": PROTOCOL, "pairing_code": "not-a-real-code", "manifest": manifest()},
        )
        self.assertEqual(bad.status_code, 401)
        first, _ = self.pair(code=code)
        self.assertTrue(first["token"])
        reused = self.client.post(
            "/api/workers/pair",
            json={"protocol": PROTOCOL, "pairing_code": code, "manifest": manifest(worker_id="grok-other")},
        )
        self.assertEqual(reused.status_code, 401)
        fresh = self.issue_code()
        self.clock.advance(61)
        expired = self.client.post(
            "/api/workers/pair",
            json={"protocol": PROTOCOL, "pairing_code": fresh, "manifest": manifest(worker_id="grok-late")},
        )
        self.assertEqual(expired.status_code, 401)

    def test_protocol_mismatch_does_not_consume_code(self):
        code = self.issue_code()
        mismatch = self.client.post(
            "/api/workers/pair",
            json={"protocol": "orbit.workers.v9", "pairing_code": code, "manifest": manifest()},
        )
        self.assertEqual(mismatch.status_code, 409)
        ok, _ = self.pair(code=code)
        self.assertIn("token", ok)

    def test_pairing_attempt_limit(self):
        code = self.issue_code()
        for _ in range(3):
            rejected = self.client.post(
                "/api/workers/pair",
                json={"protocol": PROTOCOL, "pairing_code": "bad-code-value", "manifest": manifest()},
            )
            self.assertEqual(rejected.status_code, 401)
        locked = self.client.post(
            "/api/workers/pair",
            json={"protocol": PROTOCOL, "pairing_code": code, "manifest": manifest()},
        )
        self.assertEqual(locked.status_code, 429)
        self.clock.advance(61)
        ok, _ = self.pair()
        self.assertEqual(ok["worker_id"], "grok-test")

    def test_missing_revoked_expired_and_replayed_credentials(self):
        paired, _ = self.pair()
        token = paired["token"]
        missing = self.client.post("/api/workers/poll")
        self.assertEqual(missing.status_code, 401)
        beat = self.client.post("/api/workers/heartbeat", headers=self.headers(token, "nonce-once"), json={"manifest": manifest()})
        self.assertEqual(beat.status_code, 200, beat.text)
        replay = self.client.post("/api/workers/heartbeat", headers=self.headers(token, "nonce-once"), json={"manifest": manifest()})
        self.assertEqual(replay.status_code, 409)
        skewed = self.headers(token, "nonce-skew")
        skewed["X-Orbit-Timestamp"] = str(int(self.clock.now().timestamp()) + 600)
        self.assertEqual(
            self.client.post("/api/workers/heartbeat", headers=skewed, json={"manifest": manifest()}).status_code,
            401,
        )
        revoked = self.client.post("/api/workers/revoke", json={"worker_id": "grok-test"})
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json()["presence"], "OFFLINE")
        after = self.client.post("/api/workers/poll", headers=self.headers(token, "nonce-after-revoke"))
        self.assertEqual(after.status_code, 401)
        paired_again, _ = self.pair(body=manifest(worker_id="grok-expire"))
        self.clock.advance(121)
        expired = self.client.post(
            "/api/workers/poll",
            headers=self.headers(paired_again["token"], "nonce-expired"),
        )
        self.assertEqual(expired.status_code, 401)

    def test_nonce_replay_survives_restart(self):
        paired, _ = self.pair()
        token = paired["token"]
        nonce = "nonce-restart"
        first = self.client.post("/api/workers/heartbeat", headers=self.headers(token, nonce), json={"manifest": manifest()})
        self.assertEqual(first.status_code, 200, first.text)
        self.client.close()
        self.app.state.broker.close()
        restarted = create_worker_app(self.database, clock=self.clock, offline_after=20, token_ttl=120, timestamp_skew=60)
        self.addCleanup(restarted.state.broker.close)
        client = TestClient(restarted)
        self.addCleanup(client.close)
        replay = client.post("/api/workers/heartbeat", headers=self.headers(token, nonce), json={"manifest": manifest()})
        self.assertEqual(replay.status_code, 409)
        fresh = client.post("/api/workers/heartbeat", headers=self.headers(token, "nonce-new-life"), json={"manifest": manifest()})
        self.assertEqual(fresh.status_code, 200, fresh.text)

    def test_duplicate_lease_capability_denial_and_model_gap_do_not_stop_other_work(self):
        paired, _ = self.pair()
        other, _ = self.pair(body=manifest(worker_id="grok-other"))
        blocked = self.client.post("/api/workers/tasks", json={"type": "model.ready", "payload": {"note": "unavailable"}, "grant": "local"})
        allowed = self.client.post("/api/workers/tasks", json={"type": "orbit.ping", "payload": {"message": "hello"}, "grant": "local-test"})
        self.assertEqual(blocked.status_code, 201, blocked.text)
        self.assertEqual(allowed.status_code, 201, allowed.text)
        token = paired["token"]
        self.client.post("/api/workers/heartbeat", headers=self.headers(token, "nonce-live"), json={"manifest": manifest()})
        first = self.client.post("/api/workers/poll", headers=self.headers(token, "nonce-poll-1")).json()
        second = self.client.post("/api/workers/poll", headers=self.headers(token, "nonce-poll-2")).json()
        self.assertEqual(first["lease"]["offer"]["type"], "orbit.ping")
        self.assertEqual(second["lease"]["lease_id"], first["lease"]["lease_id"])
        other_poll = self.client.post("/api/workers/poll", headers=self.headers(other["token"], "nonce-other")).json()
        self.assertIsNone(other_poll["lease"])
        tasks = self.client.get("/api/workers/tasks").json()["tasks"]
        model_task = next(item for item in tasks if item["type"] == "model.ready")
        self.assertEqual(model_task["status"], "queued")
        def denials_for(worker_id):
            events = self.app.state.broker.snapshot()["events"]
            return [item for item in events if item["kind"] == "capability_denied" and item["worker_id"] == worker_id]

        self.assertEqual(len(denials_for("grok-test")), 1)
        self.assertEqual(len(denials_for("grok-other")), 1)
        self.client.post("/api/workers/poll", headers=self.headers(token, "nonce-poll-3"))
        self.assertEqual(len(denials_for("grok-test")), 1)
        self.assertEqual(len(denials_for("grok-other")), 1)
        lease = first["lease"]
        progress = self.client.post(
            f"/api/workers/tasks/{lease['task_id']}/progress",
            headers=self.headers(token, "nonce-progress"),
            json={"lease_id": lease["lease_id"], "event_id": "event1", "status": "accepted", "message": "accepted", "progress": 0.4},
        )
        self.assertEqual(progress.status_code, 200, progress.text)
        stolen = self.client.post(
            f"/api/workers/tasks/{lease['task_id']}/result",
            headers=self.headers(other["token"], "nonce-steal"),
            json={"lease_id": lease["lease_id"], "status": "completed", "result": {"pong": "nope"}, "log_excerpt": ""},
        )
        self.assertEqual(stolen.status_code, 403)
        done = self.client.post(
            f"/api/workers/tasks/{lease['task_id']}/result",
            headers=self.headers(token, "nonce-result"),
            json={"lease_id": lease["lease_id"], "status": "completed", "result": {"pong": "hello", "executed_shell": False}, "log_excerpt": "ok"},
        )
        self.assertEqual(done.status_code, 200, done.text)
        duplicate = self.client.post(
            f"/api/workers/tasks/{lease['task_id']}/result",
            headers=self.headers(token, "nonce-dup"),
            json={"lease_id": lease["lease_id"], "status": "completed", "result": {"pong": "hello"}, "log_excerpt": ""},
        )
        self.assertEqual(duplicate.status_code, 409)
        page = self.client.get("/").text
        self.assertIn("hello", page)
        self.assertEqual(self.client.get("/api/health").json()["status"], "ok")
        detail = json.dumps(self.client.get(f"/api/workers/tasks/{lease['task_id']}").json())
        self.assertNotIn(token, detail)

    def test_cancel_rejects_late_success_and_lost_heartbeat_releases_lease(self):
        paired, _ = self.pair()
        token = paired["token"]
        self.client.post("/api/workers/heartbeat", headers=self.headers(token, "nonce-up"), json={"manifest": manifest(status="BUSY")})
        created = self.client.post("/api/workers/tasks", json={"type": "orbit.ping", "payload": {"message": "x"}, "task_id": "taskping01"}).json()
        lease = self.client.post("/api/workers/poll", headers=self.headers(token, "nonce-lease")).json()["lease"]
        self.assertEqual(lease["task_id"], created["task_id"])
        cancel = self.client.post(f"/api/workers/tasks/{lease['task_id']}/cancel")
        self.assertEqual(cancel.json()["status"], "cancel_requested")
        late = self.client.post(
            f"/api/workers/tasks/{lease['task_id']}/result",
            headers=self.headers(token, "nonce-late"),
            json={"lease_id": lease["lease_id"], "status": "completed", "result": {"pong": "x"}, "log_excerpt": ""},
        )
        self.assertEqual(late.status_code, 409)
        seen = self.client.post("/api/workers/poll", headers=self.headers(token, "nonce-see-cancel")).json()
        self.assertIn(lease["task_id"], seen["cancels"])
        self.assertTrue(seen["lease"]["cancel_requested"])
        ack = self.client.post(
            f"/api/workers/tasks/{lease['task_id']}/result",
            headers=self.headers(token, "nonce-ack"),
            json={"lease_id": lease["lease_id"], "status": "cancelled", "result": {"cancelled": True, "executed_shell": False}, "log_excerpt": "cancel acknowledged"},
        )
        self.assertEqual(ack.status_code, 200, ack.text)
        queued = self.client.post("/api/workers/tasks", json={"type": "orbit.ping", "payload": {"message": "again"}, "task_id": "taskping02"})
        self.assertEqual(queued.status_code, 201)
        self.client.post("/api/workers/heartbeat", headers=self.headers(token, "nonce-hold"), json={"manifest": manifest()})
        held = self.client.post("/api/workers/poll", headers=self.headers(token, "nonce-held")).json()["lease"]
        self.assertEqual(held["task_id"], "taskping02")
        self.clock.advance(21)
        replacement, _ = self.pair(body=manifest(worker_id="grok-backup"))
        self.client.post("/api/workers/heartbeat", headers=self.headers(replacement["token"], "nonce-backup"), json={"manifest": manifest(worker_id="grok-backup")})
        moved = self.client.post("/api/workers/poll", headers=self.headers(replacement["token"], "nonce-moved")).json()["lease"]
        self.assertEqual(moved["task_id"], "taskping02")
        self.assertNotEqual(moved["lease_id"], held["lease_id"])
        stale = self.client.post(
            "/api/workers/tasks/taskping02/result",
            headers=self.headers(token, "nonce-stale-result"),
            json={"lease_id": held["lease_id"], "status": "completed", "result": {"pong": "again"}, "log_excerpt": ""},
        )
        self.assertEqual(stale.status_code, 403)
        listed = self.client.get("/api/workers").json()["workers"]
        original = next(item for item in listed if item["worker_id"] == "grok-test")
        self.assertEqual(original["presence"], "OFFLINE")

    def test_queued_cancel_and_worker_mismatch(self):
        created = self.client.post("/api/workers/tasks", json={"type": "orbit.ping", "task_id": "queued01"}).json()
        cancelled = self.client.post("/api/workers/tasks/queued01/cancel")
        self.assertEqual(cancelled.json()["status"], "cancelled")
        paired, _ = self.pair()
        self.client.post("/api/workers/heartbeat", headers=self.headers(paired["token"], "nonce-q"), json={"manifest": manifest()})
        self.assertIsNone(self.client.post("/api/workers/poll", headers=self.headers(paired["token"], "nonce-empty")).json()["lease"])
        mismatch = self.client.post(
            "/api/workers/heartbeat",
            headers=self.headers(paired["token"], "nonce-mismatch"),
            json={"manifest": manifest(worker_id="someone-else")},
        )
        self.assertEqual(mismatch.status_code, 403)
        self.assertEqual(created["required_capabilities"], ["orbit.ping"])

    def test_cross_origin_denied_and_public_bind_refused(self):
        denied = self.client.post("/api/workers/pairing", headers={"Origin": "https://evil.example"})
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.post("/api/workers/pairing", headers={"Origin": "http://testserver"})
        self.assertEqual(allowed.status_code, 201, allowed.text)
        with self.assertRaises(ValueError):
            assert_loopback_host("0.0.0.0")
        with self.assertRaises(ValueError):
            assert_loopback_host("100.115.247.63")
        self.assertEqual(assert_loopback_host("localhost"), "127.0.0.1")

    def test_mount_does_not_enable_worker_routes_on_stock_orbit(self):
        from orbit.app import create_app

        base = Path(self.temp.name)
        workspace = base / "workspace"
        workspace.mkdir()
        settings = Settings(
            model="mock-local",
            base_url="http://test.invalid/v1",
            api_key="",
            workspace=workspace,
            database=base / "orbit.sqlite3",
            bind="127.0.0.1",
            port=8765,
        )
        model = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
        self.addCleanup(model.close)
        stock = create_app(settings, model)
        with TestClient(stock) as client:
            self.assertEqual(client.get("/api/workers/protocol").status_code, 404)
        mounted = FastAPI()
        mount_worker_api(mounted, WorkerBroker(base / "mounted.sqlite3", clock=self.clock))
        self.addCleanup(mounted.state.worker_broker.close)
        with TestClient(mounted) as client:
            self.assertEqual(client.get("/api/workers/protocol").json()["protocol"], PROTOCOL)
            self.assertEqual(client.get("/api/workers/protocol").json()["executes_payloads"], False)

    def test_shell_payload_remains_stored_and_unexecuted(self):
        marker = Path(self.temp.name) / "pwned"
        paired, _ = self.pair(body=manifest(capabilities=["shell"]))
        token = paired["token"]
        self.client.post("/api/workers/heartbeat", headers=self.headers(token, "nonce-shell-h"), json={"manifest": manifest(capabilities=["shell"])})
        created = self.client.post(
            "/api/workers/tasks",
            json={"type": "shell", "payload": {"command": f"touch {marker}"}, "grant": "denied"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        lease = self.client.post("/api/workers/poll", headers=self.headers(token, "nonce-shell-p")).json()["lease"]
        self.assertEqual(lease["offer"]["type"], "shell")
        self.assertFalse(marker.exists())
        details = self.app.state.broker._conn.execute("SELECT detail FROM events").fetchall()
        blob = " ".join(row["detail"] for row in details)
        self.assertNotIn(token, blob)


if __name__ == "__main__":
    unittest.main()
