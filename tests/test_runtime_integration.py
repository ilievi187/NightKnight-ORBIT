"""Desktop + M3 integration safety checks. All offline."""
import threading
import unittest
from unittest import mock

from orbit.runtime import InferenceRuntime, MockProvider, ProviderConfig, ProviderState, ModelStatus, build_providers


class ProviderConfigurationSafety(unittest.TestCase):
    def test_duplicate_provider_names_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate provider"):
            build_providers([
                ProviderConfig(name="LLM", base_url="http://127.0.0.1:8000/v1"),
                ProviderConfig(name="llm", base_url="http://127.0.0.1:8001/v1"),
            ])

    def test_bad_timeouts_rejected(self):
        for timeout in (float("inf"), float("nan"), 0, -1, 601):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                ProviderConfig(name="test", base_url="http://127.0.0.1:1234/v1", timeout=timeout)

    def test_runtime_status_not_blocked_by_slow_probe(self):
        provider = MockProvider(ProviderConfig(name="slow", base_url="http://127.0.0.1:5555/v1"))
        entered = threading.Event()
        release = threading.Event()

        def stalled_health():
            entered.set()
            release.wait(timeout=3)
            return ProviderState(provider="slow", kind="mock", model=None, status=ModelStatus.READY)

        runtime = InferenceRuntime([provider])
        with mock.patch.object(provider, "health", stalled_health):
            worker = threading.Thread(target=runtime.probe, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=1), "probe did not enter")
                finished = threading.Event()
                result = []
                def read_cached():
                    result.append(runtime.status())
                    finished.set()
                reader = threading.Thread(target=read_cached, daemon=True)
                reader.start()
                self.assertTrue(finished.wait(timeout=0.5), "dashboard status blocked by an I/O probe")
                self.assertEqual(result[0]["providers"][0]["status"], "NOT_CHECKED")
            finally:
                release.set()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
