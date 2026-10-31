"""HTTP surface for Plan Mode + worker fleet (issues #11 / #12).

Two route families:

1. Loopback dashboard control (same trust boundary as the existing API):
   /api/missions, /api/missions/{id}, /api/workers, /api/lab/* (pairing +
   trusted demo launcher — runs ONLY the locally bundled mock-worker binary;
   POST data can never choose an executable).

2. Versioned worker loop, first release v0.1:
   /api/workers/v0.1/register|heartbeat|poll|claim|report|unregister

Design rules:
- No command execution from worker traffic: reports accept ONLY evidence
  metadata (artifact paths, notes). The supervisor verifies artifact FILES
  on disk itself; worker self-claims never decide pass/fail.
- Loopback invariant holds: this router is served by app bound to 127.0.0.1
  with TrustedHost + cross-origin mutation denial (existing middleware).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .fleet import Fleet, FleetError
from .planmode import MissionGrant, PlanError, Supervisor

WORKER_API_VERSION = "0.1"


class MissionRequest(BaseModel):
    goal: str = Field(min_length=3, max_length=300)
    criteria: list[dict]
    max_iterations: int | None = Field(default=None, ge=1, le=20)


class DemoMissionRequest(BaseModel):
    flaky: bool = True  # first attempt of the engine task fails verification, then replan fixes it


class WorkerRegister(BaseModel):
    pairing_code: str = Field(min_length=4, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    host: str = Field(min_length=1, max_length=120)
    kind: str = "mock"
    capabilities: list[str]
    version: str | None = Field(default=None, max_length=40)


class WorkerAuth(BaseModel):
    worker_token: str = Field(min_length=20, max_length=200)


class TaskReport(BaseModel):
    worker_token: str = Field(min_length=20, max_length=200)
    status: str = Field(default="done", max_length=40)
    notes: str = Field(default="", max_length=2000)
    artifacts: list[str] = Field(default_factory=list)


CREATED = 201


def create_worker_hooks(database: Path) -> tuple:
    """Loopback-facing facts for Fleet status: running-task check + assignment view."""
    def running_task_ids(worker_id: str) -> bool:
        try:
            db = sqlite3.connect(database, timeout=30)
            try:
                row = db.execute(
                    "SELECT 1 FROM mission_tasks WHERE worker_id=? AND status='RUNNING' LIMIT 1",
                    (worker_id,)).fetchone()
            finally:
                db.close()
        except sqlite3.OperationalError:
            return False  # schema not initialized yet; no tasks can be running
        return row is not None

    def task_of_worker(worker_id: str) -> dict | None:
        try:
            db = sqlite3.connect(database, timeout=30)
            try:
                row = db.execute(
                    "SELECT id, title, status FROM mission_tasks WHERE worker_id=? "
                    "AND status IN ('ASSIGNED','RUNNING','SUBMITTED') ORDER BY updated_at DESC LIMIT 1",
                    (worker_id,)).fetchone()
            finally:
                db.close()
        except sqlite3.OperationalError:
            return None
        return {"task_id": row[0], "title": row[1], "status": row[2]} if row else None

    return running_task_ids, task_of_worker


def find_mock_worker_bundle() -> list[str] | None:
    """Executable mock worker command, if bundled. Absolute paths only; never user input."""
    candidates: list[Path] = []
    env = os.getenv("ORBIT_MOCK_WORKER")
    if env:
        candidates.append(Path(env))
    # Packaged layout: the worker binary sits next to the frozen server binary.
    candidates.append(Path(sys.executable).resolve().parent / "orbit-mock-worker")
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "orbit-mock-worker" / "orbit-mock-worker")
        candidates.append(Path(meipass) / "tools" / "mock-worker.py")
    root = Path(__file__).resolve().parent.parent
    candidates.append(root / "tools" / "mock-worker.py")
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file():
            if resolved.suffix == ".py":
                python = os.getenv("ORBIT_PYTHON") or sys.executable
                return [python, str(resolved)]
            return [str(resolved)]
    return None


def demo_root(default_workspace: Path) -> Path:
    env_root = os.getenv("ORBIT_DEMO_ROOT")
    base = Path(env_root).expanduser() if env_root else Path(default_workspace)
    root = (base / "orbit-demo").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def demo_mission_spec(root: Path) -> dict:
    """Offline demo per Definition of DONE: 4 verified deliverables across
    >=2 verify/replan iterations (mock worker 'flaky' knob is an explicit,
    documented demo behavior, not fabricated status)."""
    return {
        "goal": "Demo: build the ORBIT demo module and prove every artifact by file verification",
        "criteria": [
            {"kind": "file_contains", "path": "engine.py",
             "needle": "class DemoEngine", "text": "engine module declares DemoEngine"},
            {"kind": "file_contains", "path": "engine.py",
             "needle": "def run(", "text": "engine exposes run()"},
            {"kind": "file_contains", "path": "report.md",
             "needle": "VERIFIED", "text": "report marks verification"},
            {"kind": "file_contains", "path": "README.md",
             "needle": "DemoEngine", "text": "README documents the engine"},
        ],
        "extra_specs": [(
            "Write secret.md (scoped-denial demo)",
            {"op": "probe", "path": "secret.md",
             "note": "intentionally outside the granted paths; unrelated tasks must continue"},
        )],
        "grant": {"root": str(root), "denied_paths": ["secret.md"]},
        "flaky": True,
    }


def build_mission_router(supervisor: Supervisor, fleet: Fleet, *, enable_tick: bool = True,
                         task_of_worker=None) -> APIRouter:
    """Compose Plan Mode + worker-loop routes."""
    router = APIRouter()
    if task_of_worker is None:
        task_of_worker = create_worker_hooks(supervisor.store.filename)[1]

    def _spawn_tick():
        import threading

        def loop():
            while True:
                time.sleep(2.0)
                try:
                    supervisor.tick()
                except Exception:  # pragma: no cover — scheduler must survive
                    pass
        threading.Thread(target=loop, daemon=True, name="orbit-supervisor-tick").start()

    if enable_tick:
        _spawn_tick()

    helpers = create_worker_hooks(supervisor.store.filename)
    running_task_ids = helpers[0]

    def _assigned_task(worker_id: str) -> dict | None:
        db = sqlite3.connect(supervisor.store.filename, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM mission_tasks WHERE worker_id=? AND status IN ('ASSIGNED','RUNNING') "
                "ORDER BY updated_at ASC LIMIT 1", (worker_id,)).fetchone()
        finally:
            db.close()
        if row is None:
            return None
        task = dict(row)
        spec = json.loads(task["spec_json"] or "{}")
        return {"task_id": task["id"], "title": task["title"], "status": task["status"],
                "attempt": task["attempt"], "mission_id": task["mission_id"], "spec": spec}

    def _err(exc: Exception, status: int = 400) -> HTTPException:
        return HTTPException(status, str(exc))

    def _must_worker(payload: WorkerAuth | object) -> dict:
        worker = fleet.authorize(getattr(payload, "worker_token", None))
        if worker is None:
            raise HTTPException(403, "invalid or revoked worker token")
        return worker

    # ================= dashboard control =====================================

    @router.post("/api/missions", status_code=CREATED)
    def create_mission(payload: MissionRequest):
        try:
            return supervisor.create_mission(
                payload.goal, payload.criteria,
                grant=MissionGrant(root=demo_root(supervisor.default_root)),
                max_iterations=payload.max_iterations)
        except PlanError as exc:
            raise _err(exc) from exc

    @router.post("/api/lab/demo/mission", status_code=CREATED)
    def create_demo_mission(payload: DemoMissionRequest):
        spec = demo_mission_spec(demo_root(supervisor.default_root))
        grant = MissionGrant(root=Path(spec["grant"]["root"]),
                             denied_paths=tuple(spec["grant"]["denied_paths"] or ()))
        try:
            view = supervisor.create_mission(spec["goal"], spec["criteria"], grant=grant,
                                             extra_specs=spec["extra_specs"])
        except PlanError as exc:
            raise _err(exc) from exc
        return view

    @router.get("/api/missions")
    def list_missions():
        now = time.time()
        out = []
        for mission in supervisor.store.missions():
            tasks = supervisor.store.tasks(mission["id"])
            counts: dict[str, int] = {}
            for t in tasks:
                counts[t["status"]] = counts.get(t["status"], 0) + 1
            out.append({
                "id": mission["id"],
                "goal": mission["goal"],
                "stage": mission["stage"],
                "iteration": mission["iteration"],
                "max_iterations": mission["max_iterations"],
                "created_at": mission["created_at"],
                "updated_at": mission["updated_at"],
                "blocked_note": mission["blocked_note"],
                "task_counts": counts,
                "age": round(now - mission["created_at"], 1),
            })
        return {"missions": out}

    @router.get("/api/missions/{mission_id}")
    def mission_detail(mission_id: str):
        try:
            return supervisor.mission_view(mission_id)
        except PlanError as exc:
            raise _err(exc, 404) from exc

    @router.post("/api/missions/{mission_id}/cancel")
    def cancel_mission(mission_id: str):
        try:
            return supervisor.cancel(mission_id)
        except PlanError as exc:
            raise _err(exc, 404) from exc

    @router.post("/api/missions/{mission_id}/advance")
    def mission_advance(mission_id: str):
        try:
            return supervisor.advance(mission_id)
        except PlanError as exc:
            raise _err(exc, 404) from exc

    def _advance_active_missions() -> None:
        """Presence changes eligibility: re-run the stage machine for open missions."""
        for mission in supervisor.store.missions():
            if mission["stage"] in ("COMPLETE", "BLOCKED", "CANCELLED"):
                continue
            try:
                supervisor.advance(mission["id"])
            except PlanError:
                pass

    # ================= worker fleet ============================================

    @router.get("/api/workers")
    def list_workers():
        return {
            "workers": fleet.public_workers(time.time(), task_of_worker=task_of_worker),
            "note": "Registry holds only really-registered workers. Agent profiles are listed separately at /api/agents.",
        }

    # ---- pairing onboarding (issue #12 item 7) -------------------------------

    @router.post("/api/lab/pairing", status_code=CREATED)
    def create_pairing():
        return fleet.create_pairing(time.time())

    @router.get("/api/lab/pairings")
    def list_pairings():
        return {"pairings": fleet.store.pairings_recent()}

    # ---- trusted demo launcher ------------------------------------------------

    @router.post("/api/lab/demo/worker", status_code=CREATED)
    def launch_demo_worker():
        command = find_mock_worker_bundle()
        if command is None:
            raise HTTPException(503, "demo mock worker is not bundled in this build (no capacity invented)")
        pairing = fleet.create_pairing(time.time())
        args = [
            *command,
            "--api", f"http://127.0.0.1:{os.getenv('ORBIT_PORT', '8765')}",
            "--pairing", pairing["pairing_code"],
            "--name", "DEMO-MockWorker",
            "--kind", "mock",
            "--capability", supervisor.capability_name(),
            "--workdir", str(demo_root(supervisor.default_root)),
            "--flaky-retry", "1",
        ]
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        log_path = Path(supervisor.store.filename).parent / "demo-worker.log"
        try:
            with open(log_path, "ab") as log:
                proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                                        env=env, start_new_session=True)
        except OSError as exc:
            raise HTTPException(503, f"demo worker could not start: {exc}") from exc
        return {
            "demo_pid": proc.pid,
            "pairing_id": pairing["pairing_id"],
            "log": str(log_path),
            "note": ("The mock worker is a REAL separate process — it registers, heartbeats and submits "
                     "artifacts exactly like an on-boarded Hermes/OpenCode worker would."),
        }

    @router.get("/api/lab/demo/worker")
    def demo_worker_status():
        return {"note": "POST to this path launches the bundled demo worker; GET returns this hint."}

    # ============================================================
    # Worker loop — versioned /api/workers/v0.1 (registration, polling, reporting)
    # ============================================================

    version_path = f"/api/workers/v{WORKER_API_VERSION}"

    @router.post(f"{version_path}/register", status_code=CREATED)
    def register_worker(payload: WorkerRegister):
        try:
            result = fleet.register(payload.pairing_code, payload.name, payload.host,
                                    payload.kind, payload.capabilities, payload.version)
        except FleetError as exc:
            raise _err(exc, 403) from exc
        _advance_active_missions()
        return result

    @router.post(f"{version_path}/heartbeat")
    def heartbeat(payload: WorkerAuth):
        worker = _must_worker(payload)
        try:
            beat = fleet.heartbeat(worker["id"])
        except FleetError as exc:
            raise _err(exc, 409) from exc
        _advance_active_missions()
        task = _assigned_task(worker["id"])
        return {**beat, "assigned_task": task}

    @router.post(f"{version_path}/poll")
    def poll_tasks(payload: WorkerAuth):
        worker = _must_worker(payload)
        return {"assigned_task": _assigned_task(worker["id"])}

    def _assigned_row(worker_id: str, status: str) -> str | None:
        db = sqlite3.connect(supervisor.store.filename, timeout=30)
        try:
            row = db.execute(
                "SELECT id FROM mission_tasks WHERE worker_id=? AND status=? ORDER BY updated_at ASC LIMIT 1",
                (worker_id, status)).fetchone()
        finally:
            db.close()
        return row[0] if row else None

    @router.post(f"{version_path}/claim")
    def claim_task(payload: WorkerAuth):
        worker = _must_worker(payload)
        task_id = _assigned_row(worker["id"], "ASSIGNED")
        if task_id is None:
            raise _err("no ASSIGNED task for this worker", 409)
        try:
            task = supervisor.claim(task_id, worker)
        except PlanError as exc:
            if "another worker" in str(exc):
                raise _err(exc, 409) from exc
            raise _err(exc) from exc
        return {"claimed": True, "task": task}

    @router.post(f"{version_path}/report")
    def report_task(payload: TaskReport):
        worker = _must_worker(payload)
        task_id = _assigned_row(worker["id"], "RUNNING")
        if task_id is None:
            raise _err("no RUNNING task for this worker", 409)
        evidence = {"artifacts": payload.artifacts, "notes": payload.notes, "status": payload.status}
        try:
            task = supervisor.report(task_id, worker, evidence)
        except PlanError as exc:
            raise _err(exc) from exc
        return {"reported": True, "task": task}

    @router.post(f"{version_path}/unregister")
    def unregister(payload: WorkerAuth):
        worker = _must_worker(payload)
        fleet.revoke(worker["id"])
        return {"revoked": worker["id"]}

    return router
