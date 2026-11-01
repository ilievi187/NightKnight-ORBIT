"""User-selected model brain and explicitly chosen real worker targets.

This is onboarding state, *not* a manufactured worker roster. A discoverable
model endpoint is not an executing worker. No GPU processes are launched.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class SetupError(ValueError):
    pass


class BrainControl:
    """Persist one selected brain and target allowlist, without storing secrets."""

    def __init__(self, settings, fleet, client: httpx.Client | None = None):
        self.settings = settings
        self.fleet = fleet
        self.client = client or httpx.Client(timeout=httpx.Timeout(0.7, connect=0.4), trust_env=False)
        self.filename = Path(settings.database)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS orbit_setup (
                slot INTEGER PRIMARY KEY CHECK (slot=1),
                base_url TEXT,
                model TEXT,
                selected_at REAL,
                selected_workers TEXT NOT NULL DEFAULT '[]',
                confirmed INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute("INSERT OR IGNORE INTO orbit_setup(slot) VALUES(1)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.filename), timeout=20)
        try:
            with db:
                yield db
        finally:
            db.close()

    def status(self) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT base_url,model,selected_workers,confirmed FROM orbit_setup WHERE slot=1"
            ).fetchone()
        url, model, workers_json, confirmed = row
        return {
            "brain": {"base_url": url, "model": model} if url and model else None,
            "worker_ids": json.loads(workers_json or "[]"),
            "targets_confirmed": bool(confirmed),
        }

    def reset_worker_authority(self) -> None:
        """On every backend start, require an explicit new worker permission."""
        with self.connect() as db:
            db.execute("UPDATE orbit_setup SET selected_workers='[]', confirmed=0 WHERE slot=1")

    @staticmethod
    def _key(base_url: str, model: str) -> str:
        return hashlib.sha256((base_url + "\0" + model).encode()).hexdigest()[:24]

    def _configured_urls(self):
        out = [(self.settings.base_url.rstrip("/"), self.settings.model, "Configured")]
        for cfg in getattr(self.settings, "providers", ()) or ():
            u = str(cfg.get("base_url") or "").rstrip("/")
            if u:
                out.append((u, cfg.get("model"), str(cfg.get("name") or "Configured")))
        # Explicitly scoped local discovery; no subnet scan, no arbitrary URL supplied by browser.
        out.extend([
            ("http://127.0.0.1:18879/v1", None, "Local llama.cpp"),
            ("http://127.0.0.1:8080/v1", None, "Local OpenAI API"),
            ("http://127.0.0.1:11434/v1", None, "Local Ollama"),
        ])
        dedup = {}
        for u, model, origin in out:
            parsed = urlsplit(u)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            # No credentials in URL are accepted in the catalog.
            if parsed.username or parsed.password:
                continue
            dedup.setdefault(u, (u, model, origin))
        return list(dedup.values())[:12]

    def _api_key_for(self, url: str) -> str:
        if url == self.settings.base_url:
            return self.settings.api_key
        for cfg in getattr(self.settings, "providers", ()) or ():
            if str(cfg.get("base_url") or "").rstrip("/") == url:
                return str(cfg.get("api_key") or "")
        return ""

    def catalog(self) -> dict:
        choices = []
        for url, configured_model, origin in self._configured_urls():
            models = []
            error = None
            headers = {}
            api_key = self._api_key_for(url)
            if api_key:
                headers["Authorization"] = "Bearer " + api_key
            try:
                # All requests are to explicit server configuration or fixed loopback seeds.
                response = self.client.get(url + "/models", headers=headers, timeout=0.7)
                response.raise_for_status()
                payload = response.json()
                entries = payload.get("data") or payload.get("models") or []
                if not isinstance(entries, list):
                    raise ValueError("Model list is not an array")
                models = [str(x.get("id") or x.get("name"))
                          for x in entries[:64] if isinstance(x, dict)
                          and (x.get("id") or x.get("name"))]
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                error = str(exc)[:120]
            for model in dict.fromkeys(models):
                choices.append({"id": self._key(url, model), "model": model,
                                "base_url": url, "origin": origin, "online": True})
            if configured_model and configured_model not in models:
                choices.append({"id": self._key(url, str(configured_model)),
                                "model": str(configured_model), "base_url": url,
                                "origin": origin, "online": False,
                                "note": "Не е потвърден в model API" if not error else "API offline"})
        return {"brains": choices, "selection": self.status()}

    def select(self, brain_id: str) -> dict:
        choices = self.catalog()["brains"]
        match = next((m for m in choices if m["id"] == brain_id and m["online"]), None)
        if match is None:
            raise SetupError("Моделът не е наличен. Обнови списъка и избери работещ API.")
        with self.connect() as db:
            db.execute("""UPDATE orbit_setup
                          SET base_url=?,model=?,selected_at=?,selected_workers='[]',confirmed=0
                          WHERE slot=1""", (match["base_url"], match["model"], time.time()))
        return self.status()

    def chat_route(self) -> tuple[str, str, str]:
        chosen = self.status()["brain"]
        if not chosen:
            # Preserves the existing API contract for old integrations, while the
            # user-facing setup screen does not allow chats before choosing a brain.
            return self.settings.base_url, self.settings.model, self.settings.api_key
        return chosen["base_url"], chosen["model"], self._api_key_for(chosen["base_url"])

    def workers(self) -> dict:
        now = time.time()
        workers = self.fleet.public_workers(now)
        ready = [w for w in workers if w.get("status") in ("LIVE", "IDLE")
                 and self.fleet.is_heartbeat_fresh(w, now)]
        return {"workers": ready, "selection": self.status(),
                "note": "Показват се само workers с истинска регистрация и свеж heartbeat."}

    def select_workers(self, worker_ids: list[str]) -> dict:
        if self.status()["brain"] is None:
            raise SetupError("Първо избери мозък.")
        if len(worker_ids) > 25 or len(set(worker_ids)) != len(worker_ids):
            raise SetupError("Невалиден списък с агенти.")
        online = {w["id"] if "id" in w else w.get("worker_id") for w in self.workers()["workers"]}
        if not set(worker_ids).issubset(online):
            raise SetupError("Може да се разрешават само реално свързани и активни workers.")
        with self.connect() as db:
            db.execute("""UPDATE orbit_setup SET selected_workers=?,confirmed=1 WHERE slot=1""",
                       (json.dumps(worker_ids),))
        return self.status()

    def allowed_workers(self, now: float) -> set[str] | None:
        state = self.status()
        if not state["targets_confirmed"]:
            # Keep legacy demo tests functional until onboarding is completed.
            return None
        return set(state["worker_ids"])
