"""Safe, explicit provider sources for ORBIT's model picker.

One source is an OpenAI-compatible HTTP API; a source is *not* a worker.
Credentials remain in an owner-only local file, never in Git or API responses.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx


# Broad LOCAL discovery is limited to these known loopback application ports.
# A different local port can be added by the user without scanning the LAN.
LOCAL_PORTS = (18879, 8080, 11434, 1234, 5000, 8000, 8001, 3000, 3900)
CLOUD = (
    ("OpenAI API", "https://api.openai.com/v1"),
    ("OpenRouter API", "https://openrouter.ai/api/v1"),
    ("Groq API", "https://api.groq.com/openai/v1"),
    ("Gemini API (OpenAI compatible)", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("Mistral API", "https://api.mistral.ai/v1"),
    ("Cerebras API", "https://api.cerebras.ai/v1"),
)
REMOTE_HOSTS = frozenset(urlsplit(url).hostname for _, url in CLOUD)
LOCAL_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))
MAX_PROVIDERS = 24
MAX_MODELS_PER_SOURCE = 250


class ProviderSetupError(ValueError):
    pass


def source_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def checked_url(raw: str) -> tuple[str, str]:
    url = str(raw or "").strip().rstrip("/")
    if len(url) > 320 or not url or any(ord(c) < 33 for c in url):
        raise ProviderSetupError("Невалиден provider URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ProviderSetupError("Невалиден порт") from exc
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderSetupError("URL не може да съдържа парола, query или fragment")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ProviderSetupError("Изисква се валиден HTTP(S) адрес")
    host = parsed.hostname.lower()
    if host in LOCAL_HOSTS:
        if parsed.scheme != "http" or port is None or not 1 <= port <= 65535:
            raise ProviderSetupError("Локалните API-та се свързват с http://127.0.0.1:PORT/v1")
        return url, "local"
    if parsed.scheme != "https" or host not in REMOTE_HOSTS or port not in (None, 443):
        raise ProviderSetupError("Cloud URL не е поддържан. Избери OpenAI/OpenRouter/Groq/Gemini/Mistral/Cerebras.")
    if parsed.path not in {urlsplit(v).path for _, v in CLOUD if urlsplit(v).hostname == host}:
        raise ProviderSetupError("Cloud API адресът трябва да бъде официалният endpoint.")
    return url, "cloud"


class ProviderSources:
    def __init__(self, settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=httpx.Timeout(0.8, connect=0.4), trust_env=False)
        self.file = Path(settings.database).parent / "provider-sources.json"
        self.lock = threading.RLock()

    def _read(self) -> list[dict]:
        if not self.file.exists():
            return []
        if self.file.is_symlink():
            raise ProviderSetupError("Отказ: provider конфигурацията е symlink")
        info = self.file.stat()
        if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o077):
            raise ProviderSetupError("Provider файлът трябва да бъде private (0600)")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ProviderSetupError("Provider файлът има грешен собственик")
        try:
            with self.file.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderSetupError("Невалидна локална конфигурация за providers") from exc
        if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
            raise ProviderSetupError("Невалиден provider формат")
        return data["providers"][:MAX_PROVIDERS]

    def _write(self, providers: list[dict]) -> None:
        self.file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.file.parent / (".provider-" + uuid.uuid4().hex)
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as fp:
                json.dump({"version": 1, "providers": providers}, fp)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(path, self.file)
            os.chmod(self.file, 0o600)
        finally:
            if path.exists():
                path.unlink()

    def register(self, name: str, raw_url: str, api_key: str = "", model: str | None = None) -> dict:
        url, category = checked_url(raw_url)
        label = str(name or "").strip()
        if not 1 <= len(label) <= 70 or any(ord(c) < 32 for c in label):
            raise ProviderSetupError("Въведи име на provider (до 70 символа)")
        key = str(api_key or "").strip()
        if len(key) > 1024 or any(ord(c) < 33 for c in key):
            raise ProviderSetupError("Невалиден API key")
        hint = str(model or "").strip()[:120] or None
        if hint and any(ord(c) < 32 for c in hint):
            raise ProviderSetupError("Невалиден model ID")
        with self.lock:
            records = self._read()
            current = next((r for r in records if r["base_url"] == url), None)
            if not current and len(records) >= MAX_PROVIDERS:
                raise ProviderSetupError("Достигнат е лимитът от 24 запазени providers")
            if current:
                current["name"] = label
                current["model"] = hint
                if key:
                    current["api_key"] = key
            else:
                records.append({"name": label, "base_url": url,
                                "api_key": key, "model": hint, "category": category})
            self._write(records)
        return {"id": source_id(url), "name": label, "base_url": url, "category": category,
                "has_key": bool(key or (current and current.get("api_key")))}

    def remove(self, identifier: str) -> None:
        with self.lock:
            records = self._read()
            after = [r for r in records if source_id(r["base_url"]) != identifier]
            if len(after) == len(records):
                raise ProviderSetupError("Provider не е намерен или е системен")
            self._write(after)

    def sources(self) -> list[dict]:
        seed = [{"name": self.settings.model or "Configured", "base_url": self.settings.base_url,
                 "model": self.settings.model, "api_key": self.settings.api_key, "origin": "Configured"}]
        seed.extend({"name": str(x.get("name") or "Configured"),
                     "base_url": str(x.get("base_url") or ""),
                     "model": x.get("model"), "api_key": str(x.get("api_key") or ""),
                     "origin": "Environment"} for x in (self.settings.providers or ()))
        for port in LOCAL_PORTS:
            seed.append({"name": "Local API :%d" % port,
                         "base_url": "http://127.0.0.1:%d/v1" % port, "model": None,
                         "api_key": "", "origin": "Discovery"})
        for name, url in CLOUD:
            seed.append({"name": name, "base_url": url, "api_key": "", "model": None,
                         "origin": "Cloud template"})
        # Registered values override built-in templates and defaults, never vice versa.
        seed.extend(dict(record, origin="User-configured") for record in self._read())
        values = {}
        for raw in seed:
            try:
                url, category = checked_url(raw["base_url"])
            except ProviderSetupError:
                continue
            # Discovery defaults and blank cloud templates must never hide a
            # configured model hint or configured cloud API credential.
            if url in values and raw.get("origin") in ("Discovery", "Cloud template"):
                continue
            values[url] = dict(raw, base_url=url, category=category, id=source_id(url))
        return list(values.values())

    def api_key_for(self, url: str) -> str:
        # Never return keys from HTTP endpoints; used only for server-side model calls.
        return next((s["api_key"] for s in self.sources() if s["base_url"] == url), "")

    def _probe(self, source: dict) -> dict:
        data = {"id": source["id"], "name": source["name"],
                "base_url": source["base_url"], "category": source["category"],
                "origin": source.get("origin", ""), "has_key": bool(source.get("api_key")),
                "status": "offline", "models": [], "model_hint": source.get("model")}
        if source["category"] == "cloud" and not source.get("api_key"):
            data["status"] = "needs_key"
            return data
        headers = {"Authorization": "Bearer " + source["api_key"]} if source.get("api_key") else {}
        try:
            reply = self.client.get(source["base_url"] + "/models",
                                    headers=headers, timeout=0.8)
            reply.raise_for_status()
            payload = reply.json()
            entries = payload.get("data") or payload.get("models") or []
            if not isinstance(entries, list):
                raise ValueError("invalid models response")
            for entry in entries[:MAX_MODELS_PER_SOURCE]:
                model = entry.get("id") or entry.get("name") or entry.get("model") if isinstance(entry, dict) else entry
                if isinstance(model, str) and 0 < len(model) < 180:
                    data["models"].append(model)
        except (httpx.HTTPError, ValueError, TypeError):
            # Ollama's canonical API exposes /api/tags. Its models may not be
            # loaded into VRAM; distinguish available model from actual generation.
            if source["category"] == "local" and source["base_url"].endswith("/v1"):
                try:
                    native = self.client.get(source["base_url"][:-3] + "/api/tags", timeout=0.8)
                    native.raise_for_status()
                    payload = native.json()
                    data["models"] = [x["name"] for x in payload.get("models", [])[:MAX_MODELS_PER_SOURCE]
                                      if isinstance(x, dict) and isinstance(x.get("name"), str)]
                except (httpx.HTTPError, ValueError, TypeError, KeyError):
                    pass
        data["models"] = list(dict.fromkeys(data["models"]))
        if data["models"]:
            data["status"] = "models_listed"
        return data

    def catalog(self) -> list[dict]:
        sources = self.sources()
        with ThreadPoolExecutor(max_workers=10) as pool:
            return list(pool.map(self._probe, sources))
