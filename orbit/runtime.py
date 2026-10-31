"""Local inference runtime: provider abstraction, health probes, honest status.

Design goals (Mission M3):
- Abstraction over local OpenAI-compatible endpoints (llama.cpp server, Ollama /v1,
  LM Studio, any cloud endpoint speaking the same API) plus Ollama's native API.
- Detect available models from a configured endpoint without hardcoding model IDs.
- Report honest readiness: READY / OFFLINE / BUSY / ERROR / NOT_CHECKED.
- Measure real latency; never fabricate throughput, VRAM, or capabilities.
- No network activity at import time or construction time. I/O only happens when a
  probe/chat/benchmark is explicitly requested.
- Read-only: this module never starts, stops, pulls, or loads model weights, and never
  issues mutating calls against a provider.
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Sequence

import httpx

# --------------------------------------------------------------------------- #
# Status vocabulary
# --------------------------------------------------------------------------- #


class ModelStatus(str, Enum):
    READY = "READY"
    OFFLINE = "OFFLINE"
    BUSY = "BUSY"
    ERROR = "ERROR"
    NOT_CHECKED = "NOT_CHECKED"

    @property
    def tone(self) -> str:
        """Coarse UI tone. Not a semantic guarantee, just a hint for rendering."""
        return {
            ModelStatus.READY: "ok",
            ModelStatus.OFFLINE: "muted",
            ModelStatus.BUSY: "warn",
            ModelStatus.ERROR: "error",
            ModelStatus.NOT_CHECKED: "muted",
        }[self]


class ProviderProtocolError(RuntimeError):
    """Endpoint answered, but the payload was not the expected shape."""


class ProviderChatError(RuntimeError):
    """A chat completion could not be produced or parsed."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key: str = ""
    model: str | None = None
    timeout: float = 15.0
    kind: str = "openai_compatible"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ProviderConfig.name is required")
        if not self.base_url:
            raise ValueError("ProviderConfig.base_url is required")
        if not math.isfinite(self.timeout) or self.timeout <= 0 or self.timeout > 600:
            raise ValueError("provider timeout must be finite and between 0 and 600 seconds")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))


# --------------------------------------------------------------------------- #
# Per-provider state (what the UI displays)
# --------------------------------------------------------------------------- #


@dataclass
class ProviderState:
    provider: str
    kind: str
    model: str | None
    status: ModelStatus
    latency_ms: float | None = None
    error: str | None = None
    available_models: tuple[str, ...] = ()
    server_version: str | None = None
    last_checked: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "kind": self.kind,
            "model": self.model,
            "status": self.status.value,
            "tone": self.status.tone,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "available_models": list(self.available_models),
            "server_version": self.server_version,
            "last_checked": self.last_checked,
        }


# --------------------------------------------------------------------------- #
# Provider base + concrete providers
# --------------------------------------------------------------------------- #

Headers = Callable[[], dict[str, str]]


def _default_client(timeout: float) -> httpx.Client:
    connect = max(1.0, min(5.0, timeout))
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=connect), follow_redirects=True)


def _classify_http(status_code: int) -> ModelStatus:
    if status_code in (429, 503):
        return ModelStatus.BUSY
    return ModelStatus.ERROR


class BaseProvider:
    """Common surface every provider exposes. Subclasses implement the endpoints."""

    kind: str = "abstract"

    def __init__(
        self,
        config: ProviderConfig,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.config = config
        self.client = client or _default_client(config.timeout)
        self._clock = clock
        self._owns_client = client is None

    # -- identity --------------------------------------------------------- #
    @property
    def name(self) -> str:
        return self.config.name

    @property
    def base_url(self) -> str:
        return self.config.base_url

    @property
    def default_model(self) -> str | None:
        return self.config.model

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _timeout(self) -> float:
        return self.config.timeout

    # -- protocol --------------------------------------------------------- #
    def list_models(self) -> list[str]:
        raise NotImplementedError

    def server_version(self) -> str | None:
        """Optional. Providers without a version endpoint return None. Never raises."""
        return None

    def health(self) -> ProviderState:
        """Probe reachability + model availability. Returns a state, never raises for
        ordinary failures (offline / timeout / bad payload / bad model)."""
        started = self._clock()
        state = ProviderState(
            provider=self.name,
            kind=self.kind,
            model=self.default_model,
            status=ModelStatus.NOT_CHECKED,
        )
        try:
            models = self.list_models()
        except httpx.TimeoutException as exc:
            state.status = ModelStatus.OFFLINE
            state.error = f"timed out after {self._timeout():.1f}s ({type(exc).__name__})"
            state.last_checked = self._clock()
            return state
        except httpx.ConnectError as exc:
            state.status = ModelStatus.OFFLINE
            state.error = f"connection failed: {str(exc)[:200]}"
            state.last_checked = self._clock()
            return state
        except httpx.HTTPStatusError as exc:
            state.status = _classify_http(exc.response.status_code)
            state.error = f"HTTP {exc.response.status_code} from {self.base_url}"
            state.last_checked = self._clock()
            return state
        except (ProviderProtocolError, httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            state.status = ModelStatus.ERROR
            state.error = f"malformed or unreadable response: {str(exc)[:200]}"
            state.last_checked = self._clock()
            return state

        state.available_models = tuple(models)
        state.server_version = self._safe_server_version()
        state.latency_ms = round((self._clock() - started) * 1000.0, 2)
        state.last_checked = self._clock()

        requested = self.default_model
        if requested:
            if requested in models:
                state.status = ModelStatus.READY
            elif models:
                state.status = ModelStatus.ERROR
                state.error = (
                    f"configured model '{requested}' not exposed by server "
                    f"(available: {', '.join(models[:8])})"
                )
            else:
                state.status = ModelStatus.ERROR
                state.error = "server exposed no models"
        else:
            if models:
                state.status = ModelStatus.READY
                state.model = models[0]
            else:
                state.status = ModelStatus.ERROR
                state.error = "server exposed no models"
        return state

    def _safe_server_version(self) -> str | None:
        try:
            return self.server_version()
        except Exception:  # noqa: BLE001 - version is best-effort, never fatal
            return None

    # -- chat ------------------------------------------------------------- #
    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        model: str | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Return the full completion JSON body. Raises ProviderChatError on failure."""
        raise NotImplementedError

    # -- lifecycle -------------------------------------------------------- #
    def close(self) -> None:
        if self._owns_client:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001
                pass


class OpenAICompatibleProvider(BaseProvider):
    """Any server speaking the OpenAI `/v1/models` + `/v1/chat/completions` API.

    Covers llama.cpp `llama-server`, LM Studio, and Ollama's /v1 surface.
    """

    kind = "openai_compatible"

    def list_models(self) -> list[str]:
        response = self.client.get(
            f"{self.base_url}/models", headers=self._headers(), timeout=self._timeout()
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ProviderProtocolError("/models did not return a JSON object with a 'data' list")
        out: list[str] = []
        for entry in data["data"]:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                out.append(entry["id"])
        return out

    def server_version(self) -> str | None:
        response = self.client.get(
            f"{self.base_url}/version", headers=self._headers(), timeout=self._timeout()
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            for key in ("version", "server_version"):
                if isinstance(data.get(key), str):
                    return data[key]
        return None

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        model: str | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        model = model or self.default_model
        if not model:
            raise ProviderChatError("no model configured; pass model= or set ProviderConfig.model")
        payload: dict[str, Any] = {"model": model, "messages": list(messages)}
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        if temperature is not None:
            payload["temperature"] = float(temperature)
        payload["stream"] = False
        try:
            response = self.client.post(
                f"{self.base_url}/chat/completions",
                headers={**self._headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=self._timeout(),
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderChatError(f"chat failed: {str(exc)[:300]}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
            raise ProviderChatError("chat response missing 'choices'")
        first = data["choices"][0]
        if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
            raise ProviderChatError("chat response missing 'choices[0].message'")
        return data


class LlamaCppProvider(OpenAICompatibleProvider):
    """llama.cpp `llama-server`. Same OpenAI surface; label distinct for the UI."""

    kind = "llama_cpp"


class OllamaProvider(BaseProvider):
    """Ollama. Uses the native /api surface for model discovery + version, and the
    /v1 OpenAI-compatible endpoint for chat (matches what ORBIT already speaks)."""

    kind = "ollama"

    def _root(self) -> str:
        root = self.base_url
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        return root.rstrip("/")

    def list_models(self) -> list[str]:
        response = self.client.get(
            f"{self._root()}/api/tags", headers=self._headers(), timeout=self._timeout()
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise ProviderProtocolError("/api/tags did not return a JSON object with a 'models' list")
        out: list[str] = []
        for entry in data["models"]:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                out.append(entry["name"])
        return out

    def server_version(self) -> str | None:
        response = self.client.get(
            f"{self._root()}/api/version", headers=self._headers(), timeout=self._timeout()
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("version"), str):
            return data["version"]
        return None

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        model: str | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        model = model or self.default_model
        if not model:
            raise ProviderChatError("no model configured; pass model= or set ProviderConfig.model")
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": False,
        }
        if max_tokens is not None:
            payload["options"] = {"num_predict": int(max_tokens)}
        if temperature is not None:
            payload["options"] = {**payload.get("options", {}), "temperature": float(temperature)}
        try:
            response = self.client.post(
                f"{self._root()}/v1/chat/completions",
                headers={**self._headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=self._timeout(),
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderChatError(f"chat failed: {str(exc)[:300]}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
            raise ProviderChatError("chat response missing 'choices'")
        return data


class MockProvider(BaseProvider):
    """Offline test double. No network. Deterministic status, models, and chat.

    Behaviours are driven by `behaviour`:
      - "ready"      : list_models returns `models`, chat returns a canned completion.
      - "offline"    : list_models raises httpx.ConnectError.
      - "timeout"    : list_models raises httpx.ConnectTimeout.
      - "bad-model"  : list_models returns `models` (which do not include config.model).
      - "malformed"  : list_models returns a non-conforming payload.
      - "http-500"   : list_models raises httpx.HTTPStatusError(500).
      - "http-503"   : list_models raises httpx.HTTPStatusError(503) -> BUSY.
    """

    kind = "mock"

    def __init__(
        self,
        config: ProviderConfig,
        behaviour: str = "ready",
        models: Sequence[str] = ("mock-model-1",),
        server_version: str = "mock/0.0",
        chat_tokens: int | None = 4,
        chat_elapse: float = 0.001,
    ) -> None:
        super().__init__(config, client=httpx.Client(transport=httpx.MockTransport(self._noop)))
        self.behaviour = behaviour
        self.models = list(models)
        self._server_version = server_version
        self._chat_tokens = chat_tokens
        self._chat_elapse = chat_elapse
        self.list_calls = 0
        self.chat_calls = 0

    @staticmethod
    def _noop(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never used
        return httpx.Response(500, text="MockProvider: no network expected")

    # -- protocol --------------------------------------------------------- #
    def list_models(self) -> list[str]:
        self.list_calls += 1
        if self.behaviour == "offline":
            raise httpx.ConnectError("mock: connection refused")
        if self.behaviour == "timeout":
            raise httpx.ConnectTimeout("mock: timed out")
        if self.behaviour == "malformed":
            raise ProviderProtocolError("mock: malformed /models payload")
        if self.behaviour == "http-500":
            raise httpx.HTTPStatusError("mock: 500", request=httpx.Request("GET", "mock"),
                                       response=httpx.Response(500))
        if self.behaviour == "http-503":
            raise httpx.HTTPStatusError("mock: 503", request=httpx.Request("GET", "mock"),
                                       response=httpx.Response(503))
        return list(self.models)

    def server_version(self) -> str | None:
        if self.behaviour in ("offline", "timeout", "malformed", "http-500", "http-503"):
            return None
        return self._server_version

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        model: str | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        self.chat_calls += 1
        if self.behaviour in ("offline", "timeout"):
            raise ProviderChatError("mock: provider not reachable")
        if self.behaviour == "http-500":
            raise ProviderChatError("mock: HTTP 500")
        if self.behaviour == "malformed":
            raise ProviderChatError("mock: malformed chat payload")
        time.sleep(self._chat_elapse)
        model = model or self.default_model or (self.models[0] if self.models else "mock-model-1")
        body: dict[str, Any] = {
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": "mock reply"}}],
        }
        if self._chat_tokens is not None:
            body["usage"] = {
                "prompt_tokens": 3,
                "completion_tokens": self._chat_tokens,
                "total_tokens": 3 + self._chat_tokens,
            }
        return body


# --------------------------------------------------------------------------- #
# Runtime: multiple providers, selection, diagnostics, benchmark
# --------------------------------------------------------------------------- #


class InferenceRuntime:
    """Owns a set of providers and exposes honest, request-scoped status.

    No network is performed until an explicit probe/chat/benchmark call is made.
    """

    def __init__(
        self,
        providers: Sequence[BaseProvider],
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.providers: list[BaseProvider] = list(providers)
        if len({p.name.casefold() for p in self.providers}) != len(self.providers):
            raise ValueError("duplicate provider names in inference runtime")
        self._clock = clock
        self._lock = threading.RLock()
        self._state: dict[str, ProviderState] = {}
        for p in self.providers:
            self._state[p.name] = ProviderState(
                provider=p.name, kind=p.kind, model=p.default_model, status=ModelStatus.NOT_CHECKED
            )
        self._checked_at: float | None = None

    # -- probes ----------------------------------------------------------- #
    def probe(self, names: Sequence[str] | None = None) -> dict[str, Any]:
        """Actively probe the named providers (all if names is None). Read-only."""
        selected = self._select_providers(names)
        # Never hold the shared state lock while waiting on network I/O.
        # A timed-out provider must not block the live dashboard.
        for provider in selected:
            try:
                state = provider.health()
            except Exception as exc:  # noqa: BLE001 - probe must never crash the API
                state = ProviderState(
                    provider=provider.name,
                    kind=provider.kind,
                    model=provider.default_model,
                    status=ModelStatus.ERROR,
                    error=f"unexpected probe failure: {str(exc)[:200]}",
                    last_checked=self._clock(),
                )
            with self._lock:
                self._state[provider.name] = state
                self._checked_at = self._clock()
        return self.diagnostics()

    def _select_providers(self, names: Sequence[str] | None) -> list[BaseProvider]:
        if names is None:
            return list(self.providers)
        wanted = list(names)
        by_name = {p.name: p for p in self.providers}
        out: list[BaseProvider] = []
        for name in wanted:
            if name in by_name:
                out.append(by_name[name])
        if not out:
            raise ValueError(f"no such provider(s): {', '.join(wanted)}")
        return out

    def status(self) -> dict[str, Any]:
        """Last-known state; performs no network I/O."""
        with self._lock:
            return self.diagnostics()

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            providers = [self._state[p.name].as_dict() for p in self.providers]
            ready = [d for d in providers if d["status"] == ModelStatus.READY.value]
            selected = ready[0] if ready else None
            return {
                "checked_at": self._checked_at,
                "provider_count": len(providers),
                "selected": selected["provider"] if selected else None,
                "selected_model": selected["model"] if selected else None,
                "selected_status": selected["status"] if selected else None,
                "selected_latency_ms": selected["latency_ms"] if selected else None,
                "providers": providers,
            }

    def select(self) -> BaseProvider | None:
        """First READY provider by configuration order, else None."""
        with self._lock:
            for p in self.providers:
                if self._state.get(p.name, ProviderState(p.name, p.kind, p.default_model, ModelStatus.NOT_CHECKED)).status == ModelStatus.READY:
                    return p
        return None

    # -- chat via a provider (optional; Harness still has its own path) ---- #
    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        provider: str | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        target: BaseProvider | None = None
        if provider is not None:
            by_name = {p.name: p for p in self.providers}
            if provider not in by_name:
                raise ValueError(f"no such provider: {provider}")
            target = by_name[provider]
        else:
            target = self.select() or (self.providers[0] if self.providers else None)
        if target is None:
            raise ProviderChatError("no provider configured")
        return target.chat(messages, model, **kwargs)

    # -- benchmark (opt-in, honest) ---------------------------------------- #
    def benchmark(
        self,
        provider: str | None = None,
        model: str | None = None,
        *,
        prompt: str = "Reply with a single short word.",
        max_tokens: int = 32,
    ) -> dict[str, Any]:
        """One timed completion. tokens_per_s is computed ONLY if the server reports
        completion_tokens; otherwise it is reported as unavailable (None)."""
        target: BaseProvider | None = None
        if provider is not None:
            by_name = {p.name: p for p in self.providers}
            if provider not in by_name:
                raise ValueError(f"no such provider: {provider}")
            target = by_name[provider]
        else:
            target = self.select() or (self.providers[0] if self.providers else None)
        if target is None:
            raise ProviderChatError("no provider configured")
        resolved_model = model or target.default_model
        started = self._clock()
        data = target.chat(
            [{"role": "user", "content": prompt}], resolved_model, max_tokens=max_tokens
        )
        elapsed = max(self._clock() - started, 1e-9)
        usage = data.get("usage") if isinstance(data, dict) else None
        tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        tokens_per_s = round(float(tokens) / elapsed, 2) if isinstance(tokens, (int, float)) else None
        return {
            "provider": target.name,
            "model": resolved_model,
            "elapsed_s": round(elapsed, 4),
            "completion_tokens": tokens,
            "tokens_per_s": tokens_per_s,
            "tokens_per_s_available": tokens_per_s is not None,
            "note": "tokens_per_s is measured only when the server returns usage.completion_tokens; "
                    "it is left unavailable (null) otherwise. No hardware or capability is inferred.",
        }

    def close(self) -> None:
        for p in self.providers:
            p.close()


# --------------------------------------------------------------------------- #
# Factory: build providers from Settings (or an explicit config list)
# --------------------------------------------------------------------------- #


def _detect_kind(name: str, base_url: str, explicit: str | None) -> str:
    if explicit in {"openai_compatible", "ollama", "llama_cpp", "mock"}:
        return explicit
    haystack = f"{name} {base_url}".lower()
    if "ollama" in haystack:
        return "ollama"
    if "llama" in haystack or "llamacpp" in haystack or "llama-cpp" in haystack:
        return "llama_cpp"
    return "openai_compatible"


_PROVIDER_CLASSES = {
    "openai_compatible": OpenAICompatibleProvider,
    "llama_cpp": LlamaCppProvider,
    "ollama": OllamaProvider,
}


def build_providers(
    configs: Iterable[ProviderConfig],
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> list[BaseProvider]:
    providers: list[BaseProvider] = []
    seen: set[str] = set()
    for cfg in configs:
        kind = _detect_kind(cfg.name, cfg.base_url, cfg.kind)
        cls = _PROVIDER_CLASSES.get(kind, OpenAICompatibleProvider)
        normalized = cfg.name.casefold()
        if normalized in seen:
            raise ValueError(f"duplicate provider name: {cfg.name}")
        seen.add(normalized)
        providers.append(cls(cfg, client=client, clock=clock))
    return providers


def providers_from_settings(
    settings: Any,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> list[BaseProvider]:
    """Build providers from a Settings object.

    Honors `settings.providers` (a sequence of dicts) when present, otherwise
    falls back to the single ORBIT_MODEL / ORBIT_BASE_URL pair. No I/O.
    """
    timeout = float(getattr(settings, "timeout", 15.0) or 15.0)
    raw = getattr(settings, "providers", None)
    if raw:
        configs: list[ProviderConfig] = []
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise ValueError("Settings.providers entries must be objects")
            name = entry.get("name") or f"provider-{i + 1}"
            base_url = entry.get("base_url") or getattr(settings, "base_url", "")
            if not base_url:
                raise ValueError(f"provider '{name}' is missing base_url")
            configs.append(
                ProviderConfig(
                    name=name,
                    base_url=base_url,
                    api_key=entry.get("api_key", "") or getattr(settings, "api_key", ""),
                    model=entry.get("model") or getattr(settings, "model", None),
                    timeout=float(entry.get("timeout", timeout)),
                    kind=str(entry.get("kind", "openai_compatible")),
                )
            )
        return build_providers(configs, client=client, clock=clock)

    base_url = getattr(settings, "base_url", "")
    if not base_url:
        return []
    return build_providers(
        [
            ProviderConfig(
                name="primary",
                base_url=base_url,
                api_key=getattr(settings, "api_key", "") or "",
                model=getattr(settings, "model", None),
                timeout=timeout,
                kind="openai_compatible",
            )
        ],
        client=client,
        clock=clock,
    )


def build_runtime(
    settings: Any,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> InferenceRuntime:
    return InferenceRuntime(providers_from_settings(settings, client=client, clock=clock), clock=clock)
