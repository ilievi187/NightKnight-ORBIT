"""Minimal HTTP API and bundled UI. Keep it loopback-only until authentication exists."""
import os
import sys
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from .config import Settings
from .storage import Storage
from .harness import Harness, PROFILES, ProviderError
from .runtime import build_runtime, ProviderChatError
from .brain_control import BrainControl, SetupError
from .fleet import Fleet, FleetStore
from .planmode import PlanStore, Supervisor
from .mission_api import build_mission_router, create_worker_hooks, demo_root

WEB_DIR = Path(__file__).parent.parent / "web"


def _bundled_web_dir() -> Path | None:
    """Frozen (PyInstaller) builds keep web assets under _MEIPASS; env wins."""
    env = os.getenv("ORBIT_WEB_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "web"
        if bundled.is_dir():
            return bundled
    return None


if not WEB_DIR.is_dir():
    _candidate = _bundled_web_dir()
    if _candidate:
        WEB_DIR = _candidate


class NewSession(BaseModel):
    profile: str = "ALEX"
    title: str = Field(default="Нова мисия", max_length=120)


class ChatMessage(BaseModel):
    content: str = Field(min_length=1, max_length=10000)


class ApprovalDecision(BaseModel):
    approve: bool


class ProbeRequest(BaseModel):
    providers: list[str] | None = None


class BenchmarkRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    prompt: str = Field(default="Reply with a single short word.", max_length=400)
    max_tokens: int = Field(default=32, ge=1, le=512)


class BrainChoice(BaseModel):
    id: str = Field(min_length=1, max_length=64)


class TargetChoice(BaseModel):
    worker_ids: list[str] = Field(default_factory=list, max_length=25)


def create_app(settings: Settings | None = None, client: httpx.Client | None = None) -> FastAPI:
    settings = settings or Settings.from_environment()
    store = Storage(settings.database)
    engine = Harness(settings, store, client=client)
    runtime = build_runtime(settings, client=client)
    app = FastAPI(title="NightKnight ORBIT", version="0.1.0", docs_url="/api/docs", redoc_url=None)
    app.state.engine = engine
    app.state.runtime = runtime
    # Plan Mode kernel + worker fleet (issue #12). Additive; the loopback and
    # TrustedHost boundary below keeps this exactly as local-facing as the core API.
    plan_store = PlanStore(settings.database)
    hooks = create_worker_hooks(settings.database)
    fleet_registry = Fleet(FleetStore(settings.database), running_task_ids=hooks[0])
    supervisor = Supervisor(plan_store, default_root=settings.workspace, fleet=fleet_registry)
    app.state.missions = supervisor
    app.state.fleet = fleet_registry
    brains = BrainControl(settings, fleet_registry, client=client)
    engine.brain_route = brains.chat_route
    supervisor.command_targets = brains
    app.state.brains = brains
    missions_enabled = os.getenv("ORBIT_MISSIONS", "1") != "0"
    if missions_enabled:
        app.include_router(build_mission_router(
            supervisor, fleet_registry,
            enable_tick=os.getenv("ORBIT_MISSION_TICK", "0") == "1",
            task_of_worker=hooks[1]))
    # Reject DNS rebinding attempts against the loopback-only service.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    @app.middleware("http")
    async def browser_safety(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                # Deny mutations from arbitrary websites opening local URLs.
                from urllib.parse import urlsplit
                parsed = urlsplit(origin)
                if parsed.scheme not in {"http", "https"} or parsed.netloc != request.headers.get("host", ""):
                    return JSONResponse(status_code=403, content={"detail": "Cross-origin mutation denied"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:"
        return response

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/health")
    def health():
        diag = runtime.status()  # cached snapshot only, no network activity
        setup = brains.status()
        return {"status": "ok", "version": "0.1.0", "setup": setup, "provider_status": diag.get("selected_status") or "not_checked",
                "model": settings.model, "base_url": settings.base_url, "workspace": str(settings.workspace),
                "write_policy": "human_approval", "runtime": {
                    "probed_provider": diag.get("selected"),
                    "probed_model": diag.get("selected_model"),
                    "probe_status": diag.get("selected_status"),
                    "probe_latency_ms": diag.get("selected_latency_ms"),
                    # Backward-compatible keys for M3 clients. All remain probe-only,
                    # never imply the Harness chat route has switched providers.
                    "selected_provider": diag.get("selected"),
                    "selected_status": diag.get("selected_status"),
                    "selected_latency_ms": diag.get("selected_latency_ms"),
                    "provider_count": diag.get("provider_count"),
                    "note": "Probe metadata only; ORBIT chat still uses configured Harness provider.",
                }}

    @app.get("/api/setup/status")
    def setup_status():
        return brains.status()

    @app.get("/api/setup/brains")
    def list_brains():
        return brains.catalog()

    @app.post("/api/setup/brain")
    def set_brain(payload: BrainChoice):
        try:
            return brains.select(payload.id)
        except SetupError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/setup/workers")
    def list_worker_targets():
        return brains.workers()

    @app.post("/api/setup/workers")
    def set_worker_targets(payload: TargetChoice):
        try:
            return brains.select_workers(payload.worker_ids)
        except SetupError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/agents")
    def agents():
        return [{"id": key, "description": val} for key, val in PROFILES.items()]

    @app.get("/api/runtime")
    def runtime_status():
        """Cached model endpoint metadata; not the selected active chat transport."""
        return runtime.status()

    @app.post("/api/runtime/probe")
    def runtime_probe(payload: ProbeRequest):
        """Explicit read-only probe. READY means model-list reachable, not weights loaded."""
        try:
            return runtime.probe(payload.providers)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/runtime/benchmark")
    def runtime_benchmark(payload: BenchmarkRequest):
        """One explicitly requested completion; usage-derived tokens/s when available."""
        try:
            return runtime.benchmark(payload.provider, payload.model,
                                     prompt=payload.prompt, max_tokens=payload.max_tokens)
        except ProviderChatError as exc:
            raise HTTPException(502, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/sessions")
    def sessions():
        return store.sessions()

    @app.post("/api/sessions", status_code=201)
    def new_session(payload: NewSession):
        try:
            return engine.start(payload.profile, payload.title)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/sessions/{session_id}")
    def session(session_id: str):
        if not store.get_session(session_id):
            raise HTTPException(404, "Session not found")
        return engine._result(session_id, "approval_required" if store.pending(session_id) else "ready")

    @app.post("/api/sessions/{session_id}/chat")
    def chat(session_id: str, payload: ChatMessage):
        if not store.get_session(session_id):
            raise HTTPException(404, "Session not found")
        try:
            return engine.chat(session_id, payload.content)
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/approvals/{approval_id}")
    def approve(approval_id: str, payload: ApprovalDecision):
        try:
            return engine.approve(approval_id, payload.approve)
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return app


app = create_app()
