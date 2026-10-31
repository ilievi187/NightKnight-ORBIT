"""M3 local-inference runtime regression tests.

All tests run fully offline: every provider is exercised through httpx.MockTransport
(or the MockProvider), so nothing requires a GPU model or a live server. This mirrors
the CI gate (python 3.11/3.13, unittest discover).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from orbit.config import Settings
from orbit.runtime import (
    InferenceRuntime,
    MockProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    LlamaCppProvider,
    ProviderChatError,
    ProviderConfig,
    ModelStatus,
    build_providers,
    providers_from_settings,
    build_runtime,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def cfg(name="p", base_url="http://127.0.0.1:9999/v1", model=None, kind="openai_compatible", timeout=5.0):
    return ProviderConfig(name=name, base_url=base_url, model=model, kind=kind, timeout=timeout)


def client_with(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_models(*ids):
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
    return handler


def openai_provider(handler, model=None, name="p", base_url="http://127.0.0.1:9999/v1"):
    client = client_with(handler)
    p = OpenAICompatibleProvider(cfg(name=name, base_url=base_url, model=model), client=client)
    return p, client


def settings(tmp, providers=None):
    return Settings(
        model="heretic27-q4ks",
        base_url="http://127.0.0.1:9999/v1",
        api_key="",
        workspace=tmp / "ws",
        database=tmp / "db.sqlite3",
        bind="127.0.0.1",
        port=8765,
        timeout=5.0,
        providers=tuple(providers or ()),
    )


class RuntimeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        (self.tmp / "ws").mkdir()


# --------------------------------------------------------------------------- #
# OpenAI-compatible provider (llama.cpp / LM Studio / Ollama /v1 surface)
# --------------------------------------------------------------------------- #


class OpenAIProviderTests(RuntimeBase):
    def test_ready_latency_and_models(self):
        p, client = openai_provider(ok_models("heretic27-q4ks", "llama-3"), model="heretic27-q4ks")
        state = p.health()
        self.assertEqual(state.status, ModelStatus.READY)
        self.assertIsNotNone(state.latency_ms)
        self.assertGreaterEqual(float(state.latency_ms or 0.0), 0.0)
        self.assertIn("heretic27-q4ks", state.available_models)
        self.assertIn("llama-3", state.available_models)
        self.assertEqual(state.model, "heretic27-q4ks")
        client.close()

    def test_invalid_model_identifier(self):
        p, client = openai_provider(ok_models("llama-3"), model="heretic27-q4ks")
        state = p.health()
        self.assertEqual(state.status, ModelStatus.ERROR)
        self.assertIsNotNone(state.error)
        self.assertIn("not exposed", state.error)
        self.assertIn("llama-3", state.error)
        client.close()

    def test_offline_connection_refused(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")
        p, client = openai_provider(handler, model="m")
        state = p.health()
        self.assertEqual(state.status, ModelStatus.OFFLINE)
        self.assertIsNotNone(state.error)
        self.assertIn("connection failed", state.error)
        client.close()

    def test_connection_timeout(self):
        def handler(request):
            raise httpx.ConnectTimeout("timed out")
        p, client = openai_provider(handler, model="m")
        state = p.health()
        self.assertEqual(state.status, ModelStatus.OFFLINE)
        self.assertIsNotNone(state.error)
        self.assertIn("timed out", state.error)
        client.close()

    def test_malformed_models_payload(self):
        def handler(request):
            return httpx.Response(200, json={"foo": 1})  # missing 'data' list
        p, client = openai_provider(handler, model="m")
        state = p.health()
        self.assertEqual(state.status, ModelStatus.ERROR)
        self.assertIsNotNone(state.error)
        self.assertIn("malformed", state.error)
        client.close()

    def test_malformed_not_json(self):
        def handler(request):
            return httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"})
        p, client = openai_provider(handler, model="m")
        state = p.health()
        self.assertEqual(state.status, ModelStatus.ERROR)
        self.assertIsNotNone(state.error)
        self.assertIn("malformed", state.error)
        client.close()

    def test_failed_health_probe_http500(self):
        def handler(request):
            return httpx.Response(500, text="server error")
        p, client = openai_provider(handler, model="m")
        state = p.health()
        self.assertEqual(state.status, ModelStatus.ERROR)
        self.assertIsNotNone(state.error)
        self.assertIn("HTTP 500", state.error)
        client.close()

    def test_busy_http503(self):
        def handler(request):
            return httpx.Response(503, text="overloaded")
        p, client = openai_provider(handler, model="m")
        state = p.health()
        self.assertEqual(state.status, ModelStatus.BUSY)
        client.close()

    def test_chat_ok(self):
        def handler(request):
            if request.url.path.endswith("/chat/completions"):
                body = json.loads(request.content)
                self.assertEqual(body["model"], "heretic27-q4ks")
                return httpx.Response(200, json={
                    "model": "heretic27-q4ks",
                    "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                    "usage": {"completion_tokens": 5},
                })
            return httpx.Response(200, json={"data": [{"id": "heretic27-q4ks"}]})
        p, client = openai_provider(handler, model="heretic27-q4ks")
        data = p.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(data["choices"][0]["message"]["content"], "hello")
        client.close()

    def test_chat_malformed_raises(self):
        def handler(request):
            return httpx.Response(200, json={"foo": 1})
        p, client = openai_provider(handler, model="m")
        with self.assertRaises(ProviderChatError):
            p.chat([{"role": "user", "content": "hi"}])
        client.close()

    def test_chat_no_model_configured(self):
        p, client = openai_provider(ok_models("m"), model=None)
        with self.assertRaises(ProviderChatError):
            p.chat([{"role": "user", "content": "hi"}])
        client.close()


# --------------------------------------------------------------------------- #
# Ollama + llama.cpp specific providers
# --------------------------------------------------------------------------- #


class OllamaProviderTests(RuntimeBase):
    def test_ready_with_version_and_models(self):
        def handler(request):
            if request.url.path.endswith("/api/tags"):
                return httpx.Response(200, json={"models": [{"name": "heretic27:q4ks"}, {"name": "llama3:8b"}]})
            if request.url.path.endswith("/api/version"):
                return httpx.Response(200, json={"version": "0.6.2"})
            return httpx.Response(404)
        client = client_with(handler)
        p = OllamaProvider(cfg(name="ollama", base_url="http://127.0.0.1:11434", model="heretic27:q4ks"), client=client)
        self.assertEqual(p.kind, "ollama")
        state = p.health()
        self.assertEqual(state.status, ModelStatus.READY)
        self.assertEqual(state.server_version, "0.6.2")
        self.assertIn("heretic27:q4ks", state.available_models)
        client.close()

    def test_base_url_with_v1_stripped(self):
        client = client_with(lambda r: httpx.Response(404))
        p = OllamaProvider(cfg(base_url="http://127.0.0.1:11434/v1"), client=client)
        self.assertEqual(p._root(), "http://127.0.0.1:11434")
        client.close()

    def test_chat_via_v1(self):
        def handler(request):
            if request.url.path.endswith("/v1/chat/completions"):
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "yo"}}]})
            return httpx.Response(200, json={"models": [{"name": "m"}]})
        client = client_with(handler)
        p = OllamaProvider(cfg(name="ollama", base_url="http://127.0.0.1:11434", model="m"), client=client)
        data = p.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(data["choices"][0]["message"]["content"], "yo")
        client.close()


class LlamaCppProviderTests(RuntimeBase):
    def test_kind_label_and_health(self):
        p, client = openai_provider(ok_models("heretic27-q4ks"), model="heretic27-q4ks")
        p2 = LlamaCppProvider(cfg(name="llamacpp", base_url="http://127.0.0.1:8080/v1", model="heretic27-q4ks"),
                             client=client)
        self.assertEqual(p2.kind, "llama_cpp")
        state = p2.health()
        self.assertEqual(state.status, ModelStatus.READY)
        client.close()


# --------------------------------------------------------------------------- #
# Mock provider behaviours (offline status matrix)
# --------------------------------------------------------------------------- #


class MockProviderTests(RuntimeBase):
    def _probe(self, behaviour, model="m", models=("m",)):
        p = MockProvider(cfg(name="mock", model=model), behaviour=behaviour, models=models)
        return p

    def test_ready(self):
        p = self._probe("ready")
        s = p.health()
        self.assertEqual(s.status, ModelStatus.READY)
        self.assertIsNotNone(s.latency_ms)
        self.assertGreaterEqual(float(s.latency_ms or 0.0), 0.0)
        self.assertIn("m", s.available_models)

    def test_offline(self):
        s = self._probe("offline").health()
        self.assertEqual(s.status, ModelStatus.OFFLINE)

    def test_timeout(self):
        s = self._probe("timeout").health()
        self.assertEqual(s.status, ModelStatus.OFFLINE)
        self.assertIsNotNone(s.error)
        self.assertIn("timed out", s.error)

    def test_malformed(self):
        s = self._probe("malformed").health()
        self.assertEqual(s.status, ModelStatus.ERROR)

    def test_http500(self):
        s = self._probe("http-500").health()
        self.assertEqual(s.status, ModelStatus.ERROR)

    def test_http503_busy(self):
        s = self._probe("http-503").health()
        self.assertEqual(s.status, ModelStatus.BUSY)

    def test_bad_model(self):
        p = MockProvider(cfg(name="mock", model="missing-model"), behaviour="ready", models=("other",))
        s = p.health()
        self.assertEqual(s.status, ModelStatus.ERROR)
        self.assertIsNotNone(s.error)
        self.assertIn("not exposed", s.error)


# --------------------------------------------------------------------------- #
# Runtime: selection, recovery, multiple providers, benchmark, no-network
# --------------------------------------------------------------------------- #


class RuntimeTests(RuntimeBase):
    def test_multiple_providers_select_ready(self):
        a = MockProvider(cfg(name="good", model="m"), behaviour="ready", models=("m",))
        b = MockProvider(cfg(name="bad", model="m"), behaviour="offline")
        runtime = InferenceRuntime([a, b])
        diag = runtime.probe()
        by_name = {d["provider"]: d for d in diag["providers"]}
        self.assertEqual(by_name["good"]["status"], "READY")
        self.assertEqual(by_name["bad"]["status"], "OFFLINE")
        self.assertEqual(diag["selected"], "good")
        self.assertEqual(diag["selected_status"], "READY")
        self.assertEqual(runtime.select().name, "good")

    def test_recovery_offline_then_ready(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("down")
            return httpx.Response(200, json={"data": [{"id": "m"}]})

        p, client = openai_provider(handler, model="m")
        runtime = InferenceRuntime([p])
        first = runtime.probe()
        self.assertEqual(first["providers"][0]["status"], "OFFLINE")
        second = runtime.probe()
        self.assertEqual(second["providers"][0]["status"], "READY")
        client.close()

    def test_probe_unknown_provider_raises(self):
        runtime = InferenceRuntime([MockProvider(cfg(name="a"), behaviour="ready")])
        with self.assertRaises(ValueError):
            runtime.probe(["nope"])

    def test_benchmark_honest_with_usage(self):
        p = MockProvider(cfg(name="mock", model="m"), behaviour="ready", chat_tokens=10, chat_elapse=0.01)
        runtime = InferenceRuntime([p])
        runtime.probe()
        result = runtime.benchmark()
        self.assertEqual(result["completion_tokens"], 10)
        self.assertIsNotNone(result["tokens_per_s"])
        self.assertTrue(result["tokens_per_s_available"])
        self.assertGreater(result["tokens_per_s"], 0)

    def test_benchmark_honest_without_usage(self):
        p = MockProvider(cfg(name="mock", model="m"), behaviour="ready", chat_tokens=None)
        runtime = InferenceRuntime([p])
        runtime.probe()
        result = runtime.benchmark()
        self.assertIsNone(result["completion_tokens"])
        self.assertIsNone(result["tokens_per_s"])
        self.assertFalse(result["tokens_per_s_available"])

    def test_no_network_at_construction_or_status(self):
        def handler(request):  # pragma: no cover - must never be reached
            raise AssertionError("network access during construction/status is forbidden")
        client = client_with(handler)
        p = OpenAICompatibleProvider(cfg(name="p", model="m"), client=client)
        runtime = InferenceRuntime([p])
        # status() must not touch the network
        diag = runtime.status()
        self.assertEqual(diag["providers"][0]["status"], "NOT_CHECKED")
        client.close()


# --------------------------------------------------------------------------- #
# Config: backward compatibility + provider parsing
# --------------------------------------------------------------------------- #


class ConfigTests(RuntimeBase):
    def test_backward_compatible_defaults(self):
        s = Settings(model="m", base_url="http://x/v1", api_key="", workspace=self.tmp / "ws",
                     database=self.tmp / "d.sqlite3", bind="127.0.0.1", port=8765)
        self.assertEqual(s.timeout, 15.0)
        self.assertEqual(s.providers, ())
        providers = providers_from_settings(s)
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0].name, "primary")
        self.assertEqual(providers[0].default_model, "m")

    def test_providers_list_from_settings(self):
        s = settings(
            self.tmp,
            providers=(
                {"name": "ollama", "base_url": "http://127.0.0.1:11434", "model": "heretic27:q4ks", "kind": "ollama", "timeout": 8},
                {"name": "llamacpp", "base_url": "http://127.0.0.1:8080/v1", "model": "heretic27-q4ks", "kind": "llama_cpp", "timeout": 8},
            ),
        )
        providers = providers_from_settings(s)
        self.assertEqual([p.name for p in providers], ["ollama", "llamacpp"])
        self.assertEqual(providers[0].kind, "ollama")
        self.assertEqual(providers[1].kind, "llama_cpp")

    def test_from_environment_json_providers(self):
        import orbit.config as c
        old = os.environ.get("ORBIT_PROVIDERS")
        os.environ["ORBIT_PROVIDERS"] = json.dumps([
            {"name": "a", "base_url": "http://127.0.0.1:1/v1", "model": "m1", "kind": "openai_compatible"},
            {"name": "b", "base_url": "http://127.0.0.1:2/v1", "model": "m2", "kind": "ollama"},
        ])
        try:
            s = c.Settings.from_environment()
        finally:
            if old is None:
                os.environ.pop("ORBIT_PROVIDERS", None)
            else:
                os.environ["ORBIT_PROVIDERS"] = old
        self.assertEqual(len(s.providers), 2)
        self.assertEqual(s.providers[0]["name"], "a")
        self.assertEqual(s.providers[1]["kind"], "ollama")


# --------------------------------------------------------------------------- #
# FastAPI integration
# --------------------------------------------------------------------------- #


class AppIntegrationTests(RuntimeBase):
    def _app(self, model_handler):
        from orbit.app import create_app
        client = client_with(model_handler)
        self.addCleanup(client.close)
        s = settings(self.tmp)
        app = create_app(s, client)
        return app

    def test_runtime_endpoints_ready(self):
        def handler(request):
            if request.url.path.endswith("/chat/completions"):
                return httpx.Response(200, json={
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"completion_tokens": 3},
                })
            return httpx.Response(200, json={"data": [{"id": "heretic27-q4ks"}]})
        app = self._app(handler)
        with TestClient(app) as c:
            # before probe: not checked, no network
            before = c.get("/api/runtime").json()
            self.assertEqual(before["provider_count"], 1)
            self.assertIsNone(before["selected"])

            # active probe -> READY
            probe = c.post("/api/runtime/probe", json={}).json()
            self.assertEqual(probe["providers"][0]["status"], "READY")
            self.assertIn("heretic27-q4ks", probe["providers"][0]["available_models"])

            # health now reflects last-known READY
            health = c.get("/api/health").json()
            self.assertEqual(health["write_policy"], "human_approval")
            self.assertEqual(health["runtime"]["selected_status"], "READY")
            self.assertEqual(health["runtime"]["selected_provider"], "primary")

            # benchmark works and is honest
            bench = c.post("/api/runtime/benchmark", json={}).json()
            self.assertEqual(bench["completion_tokens"], 3)
            self.assertTrue(bench["tokens_per_s_available"])

    def test_probe_offline_still_200(self):
        def handler(request):
            raise httpx.ConnectError("refused")
        app = self._app(handler)
        with TestClient(app) as c:
            resp = c.post("/api/runtime/probe", json={})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["providers"][0]["status"], "OFFLINE")

    def test_probe_unknown_provider_400(self):
        app = self._app(lambda r: httpx.Response(200, json={"data": [{"id": "m"}]}))
        with TestClient(app) as c:
            resp = c.post("/api/runtime/probe", json={"providers": ["nope"]})
            self.assertEqual(resp.status_code, 400)

    def test_existing_health_contract_preserved(self):
        app = self._app(lambda r: httpx.Response(200, json={"data": [{"id": "m"}]}))
        with TestClient(app) as c:
            health = c.get("/api/health").json()
            for key in ("status", "version", "provider_status", "model", "base_url", "workspace", "write_policy"):
                self.assertIn(key, health)


# --------------------------------------------------------------------------- #
# Opt-in live integration (skipped unless explicitly enabled)
# --------------------------------------------------------------------------- #


class LiveIntegrationTests(unittest.TestCase):
    """Runs only when ORBIT_INTEGRATION=1 and a base URL is supplied. Never in CI."""

    def test_live_probe(self):
        if os.getenv("ORBIT_INTEGRATION") != "1":
            self.skipTest("live integration disabled (set ORBIT_INTEGRATION=1)")
        base_url = os.getenv("ORBIT_INTEGRATION_BASE_URL", "http://127.0.0.1:8080/v1")
        model = os.getenv("ORBIT_INTEGRATION_MODEL")
        client = httpx.Client(timeout=15.0)
        self.addCleanup(client.close)
        p = OpenAICompatibleProvider(ProviderConfig(name="live", base_url=base_url, model=model), client=client)
        state = p.health()
        self.assertIn(state.status.value, {s.value for s in ModelStatus})
        print("LIVE probe:", state.status.value, state.error, state.available_models[:5])


if __name__ == "__main__":
    unittest.main()
