"""First-run contract: actual model selection and no synthetic workers."""
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from orbit.app import create_app
from orbit.brain_control import BrainControl, SetupError
from orbit.config import Settings
from orbit.fleet import Fleet, FleetStore


class BrainFirstTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        workspace = root / "Projects"
        workspace.mkdir()
        self.settings = Settings(
            model="old-model", base_url="http://127.0.0.1:8080/v1", api_key="",
            workspace=workspace, database=root / "orbit.sqlite3",
            bind="127.0.0.1", port=8765,
        )
        self.requests = []

        def transport(request):
            self.requests.append(request)
            if request.method == "GET" and request.url.path == "/v1/models" and request.url.port == 18879:
                return httpx.Response(200, json={"object": "list",
                                                 "data": [{"id": "heretic27-fastlink-65k"}]})
            if request.method == "POST" and request.url.path.endswith("/chat/completions"):
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": "Hello from the selected brain",
                }}]})
            return httpx.Response(503, json={"detail": "offline"})

        self.http = httpx.Client(transport=httpx.MockTransport(transport))
        self.addCleanup(self.http.close)
        self.fleet = Fleet(FleetStore(self.settings.database))
        self.brains = BrainControl(self.settings, self.fleet, self.http)

    def choose_heretic(self, control=None):
        ctrl = control or self.brains
        live = [c for c in ctrl.catalog()["brains"] if c["online"]]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["model"], "heretic27-fastlink-65k")
        ctrl.select(live[0]["id"])
        return live[0]

    def test_first_run_empty_and_no_implicit_worker(self):
        state = self.brains.status()
        self.assertIsNone(state["brain"])
        self.assertEqual(state["worker_ids"], [])
        self.assertFalse(state["targets_confirmed"])
        self.assertEqual(self.brains.workers()["workers"], [])

    def test_choice_persists_and_chat_route_changes(self):
        self.choose_heretic()
        chosen = self.brains.chat_route()
        self.assertEqual(chosen, ("http://127.0.0.1:18879/v1", "heretic27-fastlink-65k", ""))
        again = BrainControl(self.settings, self.fleet, self.http)
        self.assertEqual(again.chat_route(), chosen)

    def test_unknown_offline_brain_refused(self):
        offline = next(c for c in self.brains.catalog()["brains"] if c["base_url"].endswith(":8080/v1"))
        self.assertFalse(offline["online"])
        with self.assertRaises(SetupError):
            self.brains.select(offline["id"])
        with self.assertRaises(SetupError):
            self.brains.select("phantom")
        self.assertIsNone(self.brains.status()["brain"])

    def test_no_fake_workers_and_zero_targets_are_explicit(self):
        self.choose_heretic()
        with self.assertRaises(SetupError):
            self.brains.select_workers(["phantom-worker"])
        self.assertFalse(self.brains.status()["targets_confirmed"])
        self.brains.select_workers([])
        self.assertEqual(self.brains.allowed_workers(0), set())

    def test_worker_requires_registration_and_heartbeat(self):
        self.choose_heretic()
        pairing = self.fleet.create_pairing()
        worker = self.fleet.register(pairing["pairing_code"], "Worker A",
                                     "NightKnightPC", "mock", ["orbit.plan.deliver"])
        wid = worker["worker_id"]
        with self.assertRaises(SetupError):
            self.brains.select_workers([wid])
        self.fleet.heartbeat(wid)
        self.assertEqual([x["id"] for x in self.brains.workers()["workers"]], [wid])
        self.assertEqual(self.brains.select_workers([wid])["worker_ids"], [wid])
        self.choose_heretic()
        self.assertEqual(self.brains.status()["worker_ids"], [])
        self.assertFalse(self.brains.status()["targets_confirmed"])

    def test_full_api_uses_selected_model_for_real_chat_request(self):
        app = create_app(self.settings, self.http)
        with TestClient(app) as client:
            self.assertIsNone(client.get("/api/setup/status").json()["brain"])
            self.assertEqual(client.get("/api/setup/workers").json()["workers"], [])
            live = next(c for c in client.get("/api/setup/brains").json()["brains"] if c["online"])
            self.assertEqual(client.post("/api/setup/brain",json={"id":live["id"]}).status_code,200)
            confirmed = client.post("/api/setup/workers",json={"worker_ids":[]})
            self.assertTrue(confirmed.json()["targets_confirmed"])
            session = client.post("/api/sessions",json={"profile":"ORBIT"})
            self.assertEqual(session.status_code,201,session.text)
            chat = client.post("/api/sessions/"+session.json()["id"]+"/chat",json={"content":"hello"})
            self.assertEqual(chat.status_code,200,chat.text)
            self.assertIn("selected brain",chat.json()["assistant"])
            chats = [r for r in self.requests if r.method=="POST" and r.url.path.endswith("/chat/completions")]
            self.assertEqual(chats[-1].url.port,18879)
            self.assertEqual(json.loads(chats[-1].content)["model"],"heretic27-fastlink-65k")

    def test_no_hardcoded_agents_in_first_run_ui(self):
        html = (Path(__file__).resolve().parents[1]/"web"/"index.html").read_text()
        self.assertIn('id="setupScreen"',html)
        self.assertIn('id="setupBrainList"',html)
        self.assertIn('id="setupTargetList"',html)
        for profile in ("ALEX","NOVA","HERMES"):
            self.assertNotIn('data-profile="'+profile+'"',html)


if __name__ == "__main__":
    unittest.main()
