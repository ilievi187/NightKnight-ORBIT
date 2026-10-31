"""Minimal HTTP API and bundled UI. Keep it loopback-only until authentication exists."""
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

WEB_DIR = Path(__file__).parent.parent / "web"


class NewSession(BaseModel):
    profile: str = "ALEX"
    title: str = Field(default="Нова мисия", max_length=120)


class ChatMessage(BaseModel):
    content: str = Field(min_length=1, max_length=10000)


class ApprovalDecision(BaseModel):
    approve: bool


def create_app(settings: Settings | None = None, client: httpx.Client | None = None) -> FastAPI:
    settings = settings or Settings.from_environment()
    store = Storage(settings.database)
    engine = Harness(settings, store, client=client)
    app = FastAPI(title="NightKnight ORBIT", version="0.1.0", docs_url="/api/docs", redoc_url=None)
    app.state.engine = engine
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
        return {"status": "ok", "version": "0.1.0", "provider_status": "not_checked", "model": settings.model,
                "base_url": settings.base_url, "workspace": str(settings.workspace), "write_policy": "human_approval"}

    @app.get("/api/agents")
    def agents():
        return [{"id": key, "description": val} for key, val in PROFILES.items()]

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
