"""Multi-provider selection and credential isolation, all with fake HTTP transports."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from orbit.app import create_app
from orbit.brain_control import BrainControl, SetupError
from orbit.config import Settings
from orbit.fleet import Fleet, FleetStore
from orbit.provider_sources import ProviderSources, ProviderSetupError, checked_url


class MultiProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        workspace = root / "Projects"
        workspace.mkdir()
        self.settings = Settings(model="old-local", base_url="http://127.0.0.1:8080/v1",
                                 api_key="", workspace=workspace,
                                 database=root / "data" / "orbit.sqlite3",
                                 bind="127.0.0.1", port=8765)
        self.settings.database.parent.mkdir()
        self.seen = []

        def handle(request):
            self.seen.append(request)
            host = request.url.host
            if request.method == "GET" and request.url.path == "/v1/models":
                if request.url.port == 18879:
                    return httpx.Response(200, json={
                        "data": [{"id": "heretic27-fastlink-65k"}, {"id": "another-local-model"}]})
                if request.url.port == 1234:
                    return httpx.Response(200, json={"data": [{"id": "lm-studio-loaded"}]})
            if host == "api.openai.com" and request.method == "GET":
                if request.headers.get("authorization") == "Bearer test-OPENAI-secret":
                    return httpx.Response(200, json={"data": [{"id": "gpt-test-model"}]})
                return httpx.Response(401)
            if request.url.port == 11434 and request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [
                    {"name": "ollama-model-a:latest"}, {"name": "ollama-model-b:latest"}]})
            if request.method == "POST" and request.url.path.endswith("/chat/completions"):
                return httpx.Response(200, json={"choices": [
                    {"message": {"role": "assistant", "content": "test connected"}}]})
            return httpx.Response(503)

        self.http = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(self.http.close)
        self.fleet = Fleet(FleetStore(self.settings.database))
        self.brains = BrainControl(self.settings, self.fleet, self.http)

    def test_local_discovery_multiple_models_and_native_ollama_tags(self):
        catalog = self.brains.catalog()
        local = [b for b in catalog["brains"] if b["category"] == "local" and b["online"]]
        found = {b["model"] for b in local}
        self.assertEqual(found, {"heretic27-fastlink-65k", "another-local-model",
                                 "lm-studio-loaded", "ollama-model-a:latest",
                                 "ollama-model-b:latest"})
        self.assertIn("sources", catalog)
        self.assertTrue(all("api_key" not in s for s in catalog["sources"]))
        # No GPU launch, no chat sampling merely to enumerate the models.
        self.assertTrue(all(r.method == "GET" for r in self.seen))

    def test_cloud_subscriptions_are_not_automatically_connected(self):
        result = self.brains.catalog()
        openai = next(s for s in result["sources"] if s["name"] == "OpenAI API")
        self.assertEqual(openai["status"], "needs_key")
        self.assertFalse(openai["has_key"])
        self.assertFalse(any(r.url.host == "api.openai.com" for r in self.seen))

    def test_cloud_key_stored_owner_only_and_never_returned(self):
        result = self.brains.add_provider("OpenAI API", "https://api.openai.com/v1",
                                          "test-OPENAI-secret")
        self.assertTrue(result["has_key"])
        private_file = self.settings.database.parent / "provider-sources.json"
        self.assertEqual(stat.S_IMODE(private_file.stat().st_mode), 0o600)
        self.assertIn("test-OPENAI-secret", private_file.read_text())
        catalog = self.brains.catalog()
        self.assertNotIn("test-OPENAI-secret", json.dumps(catalog))
        chosen = next(b for b in catalog["brains"] if b["model"] == "gpt-test-model")
        self.assertTrue(chosen["online"])
        self.brains.select(chosen["id"])
        self.assertEqual(self.brains.chat_route(),
                         ("https://api.openai.com/v1", "gpt-test-model", "test-OPENAI-secret"))
        self.assertNotIn("test-OPENAI-secret", json.dumps(self.brains.status()))
        self.brains.remove_provider(result["id"])
        self.assertIsNone(self.brains.status()["brain"])
        self.assertEqual(next(s for s in self.brains.catalog()["sources"]
                              if s["name"] == "OpenAI API")["status"], "needs_key")

    def test_local_manual_provider_custom_port(self):
        info = self.brains.add_provider("Private Local", "http://127.0.0.1:19999/v1", "")
        sources = self.brains.catalog()["sources"]
        self.assertTrue(any(s["id"] == info["id"] and s["category"] == "local" for s in sources))
        self.assertFalse(any(s.get("api_key") for s in sources))
        self.brains.remove_provider(info["id"])

    def test_https_allowlist_and_no_lan_ssrf(self):
        bad = ["http://192.168.1.42:1234/v1", "http://169.254.169.254/latest/meta-data",
               "http://0.0.0.0:8080/v1", "https://not-approved.example/v1",
               "https://api.openai.com:3333/v1", "http://api.openai.com/v1",
               "https://api.openai.com/v1?key=secret", "http://user:password@127.0.0.1:8888/v1",
               "http://127.0.0.1:0/v1"]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(ProviderSetupError):
                checked_url(url)
        self.assertEqual(checked_url("https://api.openai.com/v1")[1], "cloud")
        self.assertEqual(checked_url("http://127.0.0.1:1234/v1")[1], "local")

    def test_private_file_cannot_be_group_readable_or_symlink(self):
        self.brains.add_provider("Private", "http://127.0.0.1:19999/v1")
        private_file = self.settings.database.parent / "provider-sources.json"
        os.chmod(private_file, 0o644)
        with self.assertRaisesRegex(ProviderSetupError, "0600"):
            self.brains.catalog()
        os.chmod(private_file, 0o600)
        private_file.unlink()
        elsewhere = Path(self.temp.name) / "other.json"
        elsewhere.write_text(json.dumps({"version": 1, "providers": []}))
        private_file.symlink_to(elsewhere)
        with self.assertRaisesRegex(ProviderSetupError, "symlink"):
            self.brains.catalog()

    def test_ui_custom_provider_credential_not_echoed(self):
        app = create_app(self.settings, self.http)
        with TestClient(app) as client:
            posted = client.post("/api/setup/providers", json={
                "name": "OpenAI API", "base_url": "https://api.openai.com/v1",
                "api_key": "test-OPENAI-secret"})
            self.assertEqual(posted.status_code, 200, posted.text)
            self.assertNotIn("test-OPENAI-secret", posted.text)
            listing = client.get("/api/setup/brains")
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertNotIn("test-OPENAI-secret", listing.text)
            model = next(x for x in listing.json()["brains"] if x["model"] == "gpt-test-model")
            self.assertEqual(client.post("/api/setup/brain", json={"id": model["id"]}).status_code, 200)
            chat = client.post("/api/sessions", json={"profile": "ORBIT"})
            reply = client.post("/api/sessions/" + chat.json()["id"] + "/chat",
                                json={"content": "hello"})
            self.assertEqual(reply.status_code, 200, reply.text)
            post = [r for r in self.seen if r.method == "POST"
                    and r.url.host == "api.openai.com"][-1]
            self.assertEqual(post.headers.get("authorization"), "Bearer test-OPENAI-secret")
            self.assertEqual(json.loads(post.content)["model"], "gpt-test-model")
            self.assertEqual(client.delete("/api/setup/providers/"+posted.json()["id"]).status_code,200)

    def test_picker_has_local_cloud_add_and_search(self):
        root = Path(__file__).resolve().parents[1] / "web"
        html = (root / "index.html").read_text()
        js = (root / "app.js").read_text()
        for token in ['id="setupAddSource"', 'id="setupProviderForm"',
                      'data-source-filter="local"', 'data-source-filter="cloud"',
                      'id="setupModelSearch"']:
            self.assertIn(token, html)
        for token in ["renderBrainCatalog", "submitProvider", "deleteProvider"]:
            self.assertIn(token, js)


if __name__ == "__main__":
    unittest.main()
