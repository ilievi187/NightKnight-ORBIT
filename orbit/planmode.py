"""Plan Mode supervisor kernel (issue #11 / #12).

Deterministic, restart-safe mission state machine:

    GOAL -> DISCOVER -> MATCH -> SCAFFOLD -> ASSIGN -> RUN -> VERIFY
         -> GAP -> REPLAN / COMPLETE / BLOCKED

Every transition is persisted to SQLite and produces a visible event, so the
dashboard shows verified evidence rather than worker self-claims. Completion
checks compare FILE ARTIFACTS on disk against the mission's acceptance
criteria; worker report text is recorded as evidence metadata only and never
decides pass/fail.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

TOKEN_TERMINAL_STAGES = ("COMPLETE", "BLOCKED", "CANCELLED")
CRITERIA_KINDS = ("file_exists", "file_contains", "sha256")

MAX_ARTIFACT_BYTES = 262_144  # verifier reads at most 256 KiB per criterion check
DEFAULT_MAX_ITERATIONS = 4
DEFAULT_MAX_ATTEMPTS = 3
MAX_ADVANCE_STEPS = 64


class PlanError(RuntimeError):
    """Raised on invalid mission state requests."""


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class MissionGrant:
    """Where mission artifacts may live and which paths are explicitly denied.

    Denials are scoped: a denied path affects only tasks touching it; unrelated
    tasks continue (issue #11 requirement).
    """
    root: Path
    denied_paths: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps({"root": str(self.root), "denied_paths": list(self.denied_paths)})

    @classmethod
    def from_json(cls, raw: str) -> "MissionGrant":
        data = json.loads(raw or "{}")
        return cls(root=Path(str(data.get("root")) or "."), denied_paths=tuple(str(p) for p in (data.get("denied_paths") or ())))


@dataclass
class Criterion:
    cid: str
    text: str
    kind: str  # file_exists | file_contains | sha256
    path: str
    needle: str | None = None
    expected_sha256: str | None = None

    def as_dict(self) -> dict:
        out = {"id": self.cid, "text": self.text, "kind": self.kind, "path": self.path}
        if self.needle is not None:
            out["needle"] = self.needle
        if self.expected_sha256 is not None:
            out["sha256"] = self.expected_sha256
        return out


def parse_criteria(raw) -> list[Criterion]:
    """Validate + normalize acceptance criteria supplied by API clients."""
    if not isinstance(raw, list) or not raw:
        raise PlanError("At least one acceptance criterion is required")
    out: list[Criterion] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise PlanError(f"Criterion {index} must be an object")
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in CRITERIA_KINDS:
            raise PlanError(f"Criterion {index}: unknown kind {kind!r} (expected one of {CRITERIA_KINDS})")
        path = str(item.get("path") or "").strip()
        if not path or path.startswith("/") or ".." in Path(path).parts:
            raise PlanError(f"Criterion {index}: path must be a relative path without '..'")
        text = str(item.get("text") or "").strip() or f"Artifact {path}"
        needle = item.get("needle")
        expected = item.get("sha256")
        if kind == "file_contains":
            if not isinstance(needle, str) or not needle.strip():
                raise PlanError(f"Criterion {index}: file_contains requires a non-empty 'needle'")
        if kind == "sha256":
            if not isinstance(expected, str) or len(expected) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in expected):
                raise PlanError(f"Criterion {index}: sha256 requires a 64-character hex digest")
            expected = expected.lower()
        out.append(Criterion(cid=f"crit-{index}", text=text, kind=kind, path=path,
                             needle=str(needle) if kind == "file_contains" else None,
                             expected_sha256=str(expected) if kind == "sha256" else None))
    return out


class PlanStore:
    """SQLite persistence for missions, tasks and events. Additive to Storage."""

    def __init__(self, filename: Path):
        self.filename = Path(filename)
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS missions (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    iteration INTEGER NOT NULL DEFAULT 1,
                    max_iterations INTEGER NOT NULL DEFAULT 4,
                    grant_json TEXT NOT NULL,
                    criteria_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    blocked_note TEXT
                );
                CREATE TABLE IF NOT EXISTS mission_tasks (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    seq INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_id TEXT,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    evidence_json TEXT,
                    result_summary TEXT
                );
                CREATE INDEX IF NOT EXISTS mission_tasks_mission ON mission_tasks(mission_id, seq);
                CREATE TABLE IF NOT EXISTS mission_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id TEXT NOT NULL,
                    task_id TEXT,
                    stage TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS mission_events_mission ON mission_events(mission_id, id);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.filename, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout = 30000")
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db
        finally:
            db.close()

    # --- row helpers -------------------------------------------------------
    @staticmethod
    def _row(row) -> dict | None:
        return dict(row) if row is not None else None

    def missions(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM missions ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def mission(self, mission_id: str) -> dict | None:
        with self.connect() as db:
            return self._row(db.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone())

    def set_mission(self, mission_id: str, *, stage: str | None = None, iteration: int | None = None,
                    blocked_note: str | None = None) -> None:
        sets, args = ["updated_at=?"], [time.time()]
        if stage is not None:
            sets.append("stage=?"); args.append(stage)
        if iteration is not None:
            sets.append("iteration=?"); args.append(iteration)
        if blocked_note is not None:
            sets.append("blocked_note=?"); args.append(blocked_note)
        args.append(mission_id)
        with self.connect() as db:
            db.execute(f"UPDATE missions SET {', '.join(sets)} WHERE id=?", args)

    def insert_mission(self, mission: dict) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO missions(id,goal,stage,iteration,max_iterations,grant_json,criteria_json,created_at,updated_at,blocked_note) "
                "VALUES(:id,:goal,:stage,:iteration,:max_iterations,:grant_json,:criteria_json,:created_at,:updated_at,:blocked_note)", mission)

    def tasks(self, mission_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM mission_tasks WHERE mission_id=? ORDER BY seq", (mission_id,)).fetchall()
        return [dict(r) for r in rows]

    def task(self, task_id: str) -> dict | None:
        with self.connect() as db:
            return self._row(db.execute("SELECT * FROM mission_tasks WHERE id=?", (task_id,)).fetchone())

    def insert_task(self, task: dict) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO mission_tasks(id,mission_id,seq,title,spec_json,status,worker_id,attempt,created_at,updated_at,evidence_json,result_summary) "
                "VALUES(:id,:mission_id,:seq,:title,:spec_json,:status,:worker_id,:attempt,:created_at,:updated_at,:evidence_json,:result_summary)", task)

    def update_task(self, task_id: str, **changes) -> None:
        allowed = ("status", "worker_id", "attempt", "evidence_json", "result_summary", "spec_json", "title")
        sets, args = ["updated_at=?"], [time.time()]
        for key, value in changes.items():
            if key not in allowed or value is None:
                continue
            sets.append(f"{key}=?")
            args.append(str(value) if not isinstance(value, str) else value)
        if len(sets) == 1:
            return
        args.append(task_id)
        with self.connect() as db:
            db.execute(f"UPDATE mission_tasks SET {', '.join(sets)} WHERE id=?", args)

    def append_event(self, mission_id: str, stage: str, message: str, *, kind: str = "info",
                     task_id: str | None = None, created_at: float | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO mission_events(mission_id,task_id,stage,kind,message,created_at) VALUES(?,?,?,?,?,?)",
                (mission_id, task_id, stage, kind, message[:500], created_at if created_at is not None else time.time()))

    def events(self, mission_id: str, limit: int = 300) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM mission_events WHERE mission_id=? ORDER BY id DESC LIMIT ?", (mission_id, limit)).fetchall()
        return [dict(r) for r in rows]


class Verifier:
    """File-artifact based verification. Worker text never decides pass/fail."""

    def __init__(self, max_bytes: int = MAX_ARTIFACT_BYTES):
        self.max_bytes = max_bytes

    def within_grant(self, root: Path, supplied: str) -> Path:
        if not isinstance(supplied, str) or not supplied.strip() or "\x00" in supplied:
            raise PlanError("Invalid artifact path")
        rel = Path(supplied)
        if rel.is_absolute() or not rel.parts or ".." in rel.parts:
            raise PlanError("Artifact paths must be relative to the mission workspace")
        candidate = (root / rel).resolve()
        if not candidate.is_relative_to(root.resolve()):
            raise PlanError("Artifact path escapes mission workspace")
        return candidate

    def _read(self, candidate: Path) -> bytes:
        if not candidate.is_file():
            raise FileNotFoundError(str(candidate))
        size = candidate.stat().st_size
        if size > self.max_bytes:
            raise PlanError(f"Artifact exceeds verification limit ({size} bytes > {self.max_bytes})")
        return candidate.read_bytes()

    def check(self, criterion: Criterion, root: Path) -> tuple[bool, str]:
        """Return (satisfied, evidence message). All errors become honest failures."""
        try:
            candidate = self.within_grant(root, criterion.path)
        except (PlanError, OSError, ValueError) as exc:
            return False, f"REJECTED: {str(exc)[:200]}"
        if criterion.kind == "file_exists":
            if candidate.is_file():
                return True, f"EXISTS: {criterion.path}"
            return False, f"MISSING: {criterion.path}"
        try:
            blob = self._read(candidate)
        except FileNotFoundError:
            return False, f"MISSING: {criterion.path}"
        except PlanError as exc:
            return False, f"REJECTED: {str(exc)[:200]}"
        if criterion.kind == "file_contains":
            needle = (criterion.needle or "").encode("utf-8")
            if needle and needle in blob:
                return True, f"CONTAINS: {criterion.path} includes required content"
            return False, f"NOT SATISFIED: required content absent from {criterion.path}"
        if criterion.kind == "sha256":
            digest = hashlib.sha256(blob).hexdigest()
            if digest == (criterion.expected_sha256 or "").lower():
                return True, f"SHA256 MATCH: {criterion.path} = {digest[:16]}..."
            return False, f"SHA256 MISMATCH: {criterion.path} observed {digest[:16]}..."
        return False, f"UNKNOWN KIND: {criterion.kind}"

    def check_all(self, criteria: list[Criterion], root: Path) -> list[tuple[Criterion, bool, str]]:
        return [(c, *self.check(c, root)) for c in criteria]


class Supervisor:
    """Drives missions through the stage machine using persisted facts only."""

    def __init__(self, store: PlanStore, default_root: Path, heartbeat_ttl: float = 20.0,
                 max_iterations: int = DEFAULT_MAX_ITERATIONS, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 fleet=None, verifier: Verifier | None = None):
        self.store = store
        self.default_root = Path(default_root)
        self.heartbeat_ttl = heartbeat_ttl
        self.default_max_iterations = max_iterations
        self.max_attempts = max_attempts
        self.fleet = fleet  # orbit.fleet.Fleet; optional for small unit tests
        self.verifier = verifier or Verifier()

    # --- creation -----------------------------------------------------------
    def create_mission(self, goal: str, criteria_raw, *, grant: MissionGrant | None = None,
                       max_iterations: int | None = None, now: float | None = None,
                       extra_specs: list[tuple[str, dict]] | None = None) -> dict:
        g = grant or MissionGrant(root=self.default_root)
        if not g.root.is_dir():
            raise PlanError(f"Mission workspace root does not exist: {g.root}")
        criteria = parse_criteria(criteria_raw)
        now = time.time() if now is None else now
        mission = {
            "id": new_id(),
            "goal": (goal.strip() or "ORBIT mission")[:300],
            "stage": "GOAL",
            "iteration": 1,
            "max_iterations": max(1, min(int(max_iterations or self.default_max_iterations), 20)),
            "grant_json": g.to_json(),
            "criteria_json": json.dumps([c.as_dict() for c in criteria]),
            "created_at": now,
            "updated_at": now,
            "blocked_note": None,
        }
        self.store.insert_mission(mission)
        self.store.append_event(mission["id"], "GOAL", f"GOAL ANALYSIS: {mission['goal']}", kind="stage", created_at=now)
        for c in criteria:
            self.store.append_event(mission["id"], "GOAL", f"ACCEPTANCE: [{c.kind}] {c.path}", kind="criterion", created_at=now)
        if g.denied_paths:
            self.store.append_event(mission["id"], "GOAL",
                                    "SCOPED GRANT: root=" + str(g.root) + " denied=" + ", ".join(g.denied_paths),
                                    kind="grant", created_at=now)
        for title, spec in (extra_specs or []):
            self.add_spec_task(mission["id"], title, spec, now=now)
        self.advance(mission["id"], now=now)
        return self.mission_view(mission["id"])

    def add_spec_task(self, mission_id: str, title: str, spec: dict, *, now: float | None = None) -> dict:
        mission = self.store.mission(mission_id)
        if not mission:
            raise PlanError(f"Mission not found: {mission_id}")
        now = time.time() if now is None else now
        seq = max((t["seq"] for t in self.store.tasks(mission_id)), default=0) + 1
        task = {"id": new_id(), "mission_id": mission_id, "seq": seq, "title": title[:120],
                "spec_json": json.dumps(spec), "status": "PENDING", "worker_id": None, "attempt": 1,
                "created_at": now, "updated_at": now, "evidence_json": None, "result_summary": None}
        self.store.insert_task(task)
        self.store.append_event(mission_id, mission["stage"], f"SCAFFOLD: {title}", kind="scaffold",
                                task_id=task["id"], created_at=now)
        return task

    # --- views ---------------------------------------------------------------
    def criteria_of(self, mission: dict) -> list[Criterion]:
        out = []
        for d in json.loads(mission["criteria_json"]):
            out.append(Criterion(cid=d["id"], text=d["text"], kind=d["kind"], path=d["path"],
                                 needle=d.get("needle"), expected_sha256=d.get("sha256")))
        return out

    def task_view(self, task: dict) -> dict:
        out = {key: task[key] for key in ("id", "mission_id", "seq", "title", "status", "worker_id",
                                          "attempt", "created_at", "updated_at", "result_summary")}
        out["spec"] = json.loads(task["spec_json"]) if task["spec_json"] else {}
        out["evidence"] = json.loads(task["evidence_json"]) if task["evidence_json"] else None
        return out

    def mission_view(self, mission_id: str) -> dict:
        mission = self.store.mission(mission_id)
        if not mission:
            raise PlanError(f"Mission not found: {mission_id}")
        grant = MissionGrant.from_json(mission["grant_json"])
        checks = self.verifier.check_all(self.criteria_of(mission), grant.root)
        return {
            "id": mission["id"],
            "goal": mission["goal"],
            "stage": mission["stage"],
            "iteration": mission["iteration"],
            "max_iterations": mission["max_iterations"],
            "created_at": mission["created_at"],
            "updated_at": mission["updated_at"],
            "blocked_note": mission["blocked_note"],
            "grant": {"root": str(grant.root), "denied_paths": list(grant.denied_paths)},
            "criteria": [dict(c.as_dict(), satisfied=ok, evidence=message) for c, ok, message in checks],
            "tasks": [self.task_view(t) for t in self.store.tasks(mission_id)],
            "events": list(reversed(self.store.events(mission_id))),
        }

    # --- fleet-facing helpers -------------------------------------------------
    def capability_name(self) -> str:
        return "orbit.plan.deliver"

    def eligible_workers(self, now: float) -> list[dict]:
        if not self.fleet:
            return []
        eligible = list(self.fleet.capable_workers(self.capability_name(), now=now))
        policy = getattr(self, "command_targets", None)
        if policy is not None:
            allowed = policy.allowed_workers(now)
            if allowed is not None:
                eligible = [w for w in eligible if (w.get("worker_id") or w.get("id")) in allowed]
        return eligible

    def blocked_reason(self, spec: dict, grant: MissionGrant) -> str | None:
        path = str(spec.get("path") or "")
        for denied in grant.denied_paths:
            clean = denied.rstrip("/")
            if path == clean or path.startswith(clean + "/"):
                return f"GRANT DENIED: '{denied}' is outside this mission's approved write scope"
        return None

    def _worker_live(self, task: dict, now: float) -> bool:
        worker = self.fleet.worker(task["worker_id"]) if self.fleet else None
        if worker is None:
            return self.fleet is None  # without a fleet registry we cannot judge; be conservative
        return self.fleet.is_heartbeat_fresh(worker, now)

    # --- the stage machine -----------------------------------------------------
    def advance(self, mission_id: str, now: float | None = None) -> dict:
        """Progress the mission as far as facts allow. Bounded passes; restart-safe."""
        now = time.time() if now is None else now
        for _ in range(MAX_ADVANCE_STEPS):
            mission = self.store.mission(mission_id)
            if not mission:
                raise PlanError(f"Mission not found: {mission_id}")
            stage = mission["stage"]
            if stage in TOKEN_TERMINAL_STAGES:
                return self.mission_view(mission_id)
            tasks = self.store.tasks(mission_id)

            # Lost workers invalidate RUNNING tasks (heartbeat TTL exceeded).
            self._fail_lost_tasks(mission, tasks, now)
            tasks = self.store.tasks(mission_id)

            if stage == "GOAL":
                self.store.set_mission(mission_id, stage="DISCOVER")
                self.store.append_event(mission_id, "DISCOVER", "DISCOVER: inventorying registered workers",
                                        kind="stage", created_at=now)
                continue
            if stage == "DISCOVER":
                eligible = self.eligible_workers(now)
                self.store.append_event(
                    mission_id, "DISCOVER",
                    f"DISCOVER: {len(eligible)} eligible worker(s) with capability '{self.capability_name()}'",
                    kind="inventory", created_at=now)
                for w in eligible[:8]:
                    self.store.append_event(mission_id, "DISCOVER",
                                            f"  candidate {w['name']} host={w['host']} kind={w['kind']}",
                                            kind="inventory", created_at=now)
                self.store.set_mission(mission_id, stage="MATCH")
                continue
            if stage == "MATCH":
                eligible = self.eligible_workers(now)
                if eligible:
                    self.store.append_event(mission_id, "MATCH",
                                            f"MATCH: worker pool ready ({len(eligible)} eligible)", kind="stage", created_at=now)
                else:
                    self.store.append_event(mission_id, "MATCH",
                                            "MATCH: no eligible worker registered yet — mission waits (no fabricated capacity)",
                                            kind="wait", created_at=now)
                self.store.set_mission(mission_id, stage="SCAFFOLD")
                continue
            if stage == "SCAFFOLD":
                created = self._ensure_deliver_tasks(mission_id, self.criteria_of(mission), now=now)
                if created:
                    self.store.append_event(mission_id, "SCAFFOLD",
                                            f"SCAFFOLD: planned {len(created)} deliver task(s) from acceptance criteria",
                                            kind="stage", created_at=now)
                else:
                    self.store.append_event(mission_id, "SCAFFOLD", "SCAFFOLD: task plan intact",
                                            kind="stage", created_at=now)
                self.store.set_mission(mission_id, stage="ASSIGN")
                continue
            if stage == "ASSIGN":
                if self._assign_pending(mission, tasks, now):
                    continue
                if any(t["status"] == "PENDING" for t in self.store.tasks(mission_id)):
                    if not self.eligible_workers(now):
                        self.store.append_event(mission_id, "ASSIGN",
                                                "ASSIGN: waiting for an eligible worker — " +
                                                f"{sum(1 for t in self.store.tasks(mission_id) if t['status'] == 'PENDING')} task(s) pending",
                                                kind="wait", created_at=now)
                        return self.mission_view(mission_id)
                self.store.set_mission(mission_id, stage="RUN")
                continue
            if stage == "RUN":
                fresh = self.store.tasks(mission_id)
                if self._mid_flight(fresh, now):
                    # Stay in RUN until fully quiescent: never verify while claims
                    # may still complete — premature verification multiplies replans.
                    return self.mission_view(mission_id)
                self.store.set_mission(mission_id, stage="VERIFY")
                self.store.append_event(mission_id, "VERIFY", "VERIFY: workers quiescent — verifying acceptance criteria",
                                        kind="stage", created_at=now)
                continue
            if stage == "VERIFY":
                view = self._evaluate(mission, now)
                mission = self.store.mission(mission_id)
                if mission["stage"] in TOKEN_TERMINAL_STAGES:
                    return view
                continue  # GAP stage handled in the next loop pass within this call
            if stage == "GAP":
                return self._replan(mission, now)
        raise PlanError("Mission state machine did not converge (loop guard)")

    # --- stage details ----------------------------------------------------------
    def _ensure_deliver_tasks(self, mission_id, criteria: list[Criterion], *, now: float) -> list[dict]:
        existing = self.store.tasks(mission_id)
        live_paths = set()
        for t in existing:
            spec = json.loads(t["spec_json"] or "{}")
            if spec.get("op") in ("deliver", "retry-deliver") and t["status"] not in ("SKIPPED",):
                live_paths.add(spec.get("path"))
        created = []
        seq = max((t["seq"] for t in existing), default=0)
        grouped: dict[str, list[Criterion]] = {}
        for c in criteria:
            grouped.setdefault(c.path, []).append(c)
        for path, group in sorted(grouped.items()):
            if path in live_paths:
                continue
            seq += 1
            needles = [c.needle for c in group if c.kind == "file_contains" and c.needle]
            spec = {"op": "deliver", "path": path, "must_contain": needles,
                    "criterion_ids": sorted(c.cid for c in group)}
            task = {"id": new_id(), "mission_id": mission_id, "seq": seq,
                    "title": f"Deliver {path}", "spec_json": json.dumps(spec), "status": "PENDING",
                    "worker_id": None, "attempt": 1, "created_at": now, "updated_at": now,
                    "evidence_json": None, "result_summary": None}
            self.store.insert_task(task)
            created.append(task)
        return created

    def _assign_pending(self, mission, tasks, now: float) -> bool:
        """Assign PENDING tasks to eligible workers; deny blocked paths immediately."""
        grant = MissionGrant.from_json(mission["grant_json"])
        moved = False
        for t in tasks:
            if t["status"] != "PENDING":
                continue
            spec = json.loads(t["spec_json"] or "{}")
            if reason := self.blocked_reason(spec, grant):
                self.store.update_task(t["id"], status="BLOCKED", result_summary=reason)
                self.store.append_event(mission["id"], mission["stage"],
                                        f"SCOPED DENIAL: {t['title']} — {reason} (unrelated tasks continue)",
                                        kind="scope-denied", task_id=t["id"], created_at=now)
                continue
        eligible = self.eligible_workers(now)
        pending = [t for t in self.store.tasks(mission["id"]) if t["status"] == "PENDING"]
        for offset, t in enumerate(pending):
            if not eligible:
                break
            worker = eligible[offset % len(eligible)]
            self.store.update_task(t["id"], status="ASSIGNED", worker_id=worker["id"])
            self.store.append_event(mission["id"], mission["stage"],
                                    f"ASSIGN: {t['title']} -> {worker['name']} (attempt {t['attempt']})",
                                    kind="assign", task_id=t["id"], created_at=now)
            moved = True
        self._refresh_worker_status(now)
        return moved

    def _mid_flight(self, tasks, now: float) -> bool:
        """True while any task may still make progress (wait instead of verifying)."""
        if any(t["status"] in ("PENDING", "ASSIGNED") for t in tasks):
            return True
        return any(t["status"] == "RUNNING" and self._worker_live(t, now) for t in tasks)

    # --- verification ---------------------------------------------------------------
    def _evaluate(self, mission, now: float) -> dict:
        mission_id = mission["id"]
        grant = MissionGrant.from_json(mission["grant_json"])
        checks = self.verifier.check_all(self.criteria_of(mission), grant.root)
        unsatisfied = [(c, msg) for c, ok, msg in checks if not ok]
        for c, ok, msg in checks:
            self.store.append_event(mission_id, "VERIFY",
                                    ("SATISFIED: " if ok else "UNSATISFIED: ") + f"{c.path} — {msg}",
                                    kind="verification", created_at=now)
        self._update_task_statuses(mission_id, unsatisfied, now)
        if not unsatisfied:
            blocked_count = sum(1 for t in self.store.tasks(mission_id) if t["status"] == "BLOCKED")
            note = f"ALL {len(checks)} acceptance criteria verified with file evidence"
            if blocked_count:
                note += f" ({blocked_count} task(s) blocked by scoped grant denials — none gate completion)"
            self.store.set_mission(mission_id, stage="COMPLETE")
            self.store.append_event(mission_id, "COMPLETE", f"COMPLETE: {note}", kind="final", created_at=now)
            return self.mission_view(mission_id)
        self.store.set_mission(mission_id, stage="GAP")
        failing = ", ".join(sorted({c.path for c, _ in unsatisfied}))
        self.store.append_event(mission_id, "GAP",
                                f"GAP ANALYSIS: {len(unsatisfied)} criterion/criteria unmet ({failing})",
                                kind="stage", created_at=now)
        return self.mission_view(mission_id)

    def _update_task_statuses(self, mission_id, unsatisfied: list[tuple[Criterion, str]], now: float) -> None:
        failing_paths = {c.path for c, _ in unsatisfied}
        tasks = self.store.tasks(mission_id)
        for t in tasks:
            spec = json.loads(t["spec_json"] or "{}")
            path = spec.get("path")
            if t["status"] in ("RUNNING",):
                # Quiescent verify: any RUNNING task reaching here is orphaned.
                if path in failing_paths or self.fleet is None or (self.fleet and self.fleet.worker(t["worker_id"]) is None):
                    self.store.update_task(t["id"], status="FAILED", result_summary="in-flight task never submitted; failed at verification")
            elif t["status"] == "SUBMITTED":
                if path in failing_paths:
                    self.store.update_task(t["id"], status="FAILED", result_summary="artifact failed acceptance verification")
                else:
                    self.store.update_task(t["id"], status="VERIFIED", result_summary="artifact verified against acceptance criteria")

    def _fail_lost_tasks(self, mission, tasks, now: float) -> None:
        if not self.fleet:
            return
        for t in tasks:
            if t["status"] != "RUNNING":
                continue
            worker = self.fleet.worker(t["worker_id"]) if t["worker_id"] else None
            if worker is None:
                self.store.update_task(t["id"], status="FAILED", result_summary="assigned worker record missing")
                self.store.append_event(mission["id"], mission["stage"],
                                        f"WORKER LOST: {t['title']} (no registered worker)", kind="worker-lost",
                                        task_id=t["id"], created_at=now)
            elif not self.fleet.is_heartbeat_fresh(worker, now):
                self.fleet.note_offline(worker, now)
                self.store.update_task(t["id"], status="FAILED", result_summary=f"worker '{worker['name']}' heartbeat lost")
                self.store.append_event(mission["id"], mission["stage"],
                                        f"WORKER LOST: {t['title']} — heartbeat TTL of {self.heartbeat_ttl:.0f}s exceeded; task failed (replan will recover)",
                                        kind="worker-lost", task_id=t["id"], created_at=now)

    def _replan(self, mission, now: float) -> dict:
        mission_id = mission["id"]
        iteration = int(mission["iteration"])
        tasks = self.store.tasks(mission_id)
        retry_needed = [t for t in tasks if t["status"] == "FAILED"]

        if not retry_needed:
            note = ("BLOCKED: nothing left to replan — no failed task has retry budget remaining. "
                    "Missions complete only on verified evidence; no progress is fabricated.")
            self.store.set_mission(mission_id, stage="BLOCKED", blocked_note=note)
            self.store.append_event(mission_id, "BLOCKED", note, kind="final", created_at=now)
            return self.mission_view(mission_id)

        if iteration >= int(mission["max_iterations"]):
            failing = ", ".join(sorted({(json.loads(t["spec_json"] or "{}").get("path") or "?") for t in retry_needed}))
            note = (f"BLOCKED: max iteration guard reached ({iteration}/{mission['max_iterations']}) with unmet paths: {failing}. "
                    "Honest stop — mission did not complete.")
            self.store.set_mission(mission_id, stage="BLOCKED", blocked_note=note)
            self.store.append_event(mission_id, "BLOCKED", note, kind="final", created_at=now)
            return self.mission_view(mission_id)

        retried = 0
        seq = max((t["seq"] for t in tasks), default=0)
        for t in retry_needed:
            if int(t["attempt"]) >= self.max_attempts:
                self.store.update_task(t["id"], status="SKIPPED", result_summary="retry budget exhausted")
                self.store.append_event(mission_id, "REPLAN",
                                        f"REPLAN: attempt budget exhausted for '{t['title']}' — SKIPPED",
                                        kind="replan", task_id=t["id"], created_at=now)
                continue
            spec = json.loads(t["spec_json"] or "{}")
            seq += 1
            retry_spec = dict(spec, op="retry-deliver", retry_of=t["id"])
            retry = {"id": new_id(), "mission_id": mission_id, "seq": seq,
                     "title": f"Retry {spec.get('path', 'task')} (attempt {int(t['attempt']) + 1})",
                     "spec_json": json.dumps(retry_spec), "status": "PENDING",
                     "worker_id": None, "attempt": int(t["attempt"]) + 1,
                     "created_at": now, "updated_at": now, "evidence_json": None, "result_summary": None}
            self.store.insert_task(retry)
            retried += 1
            self.store.append_event(mission_id, "REPLAN", f"REPLAN: created {retry['title']}",
                                    kind="replan", task_id=retry["id"], created_at=now)

        if retried == 0:
            note = (f"BLOCKED: every failed task exhausted its retry budget (max_attempts={self.max_attempts}). "
                    "Honest stop — mission did not complete.")
            self.store.set_mission(mission_id, stage="BLOCKED", blocked_note=note)
            self.store.append_event(mission_id, "BLOCKED", note, kind="final", created_at=now)
            return self.mission_view(mission_id)

        self.store.set_mission(mission_id, stage="ASSIGN", iteration=iteration + 1)
        self.store.append_event(mission_id, "REPLAN",
                                f"REPLAN: {retried} retry task(s) queued -> ASSIGN (iteration {iteration + 1}/{mission['max_iterations']})",
                                kind="stage", created_at=now)
        self.advance(mission_id, now=now)
        return self.mission_view(mission_id)

    # --- fleet status sync --------------------------------------------------------------
    def _refresh_worker_status(self, now: float) -> None:
        """LIVE = worker actively holds a RUNNING task; IDLE = fresh heartbeat, no claim."""
        if not self.fleet:
            return
        busy: set[str] = set()
        for mission in self.store.missions():
            if mission["stage"] in TOKEN_TERMINAL_STAGES:
                continue
            for t in self.store.tasks(mission["id"]):
                if t["status"] == "RUNNING" and t["worker_id"]:
                    busy.add(t["worker_id"])
        self.fleet.refresh_status(busy, now)

    # --- external transitions (worker-facing API) ------------------------------------
    def claim(self, task_id: str, worker: dict, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        t = self.store.task(task_id)
        if not t:
            raise PlanError("unknown task")
        if t["worker_id"] != worker["id"]:
            raise PlanError("task is assigned to another worker")
        if t["status"] == "RUNNING":
            return self.task_view(t)  # idempotent re-claim
        if t["status"] != "ASSIGNED":
            raise PlanError(f"task is {t['status']} — only ASSIGNED tasks can be claimed")
        mission = self.store.mission(t["mission_id"])
        grant = MissionGrant.from_json(mission["grant_json"])
        spec = json.loads(t["spec_json"] or "{}")
        if reason := self.blocked_reason(spec, grant):
            self.store.update_task(task_id, status="BLOCKED", result_summary=reason)
            self.store.append_event(t["mission_id"], mission["stage"],
                                    f"CLAIM DENIED: {t['title']} — {reason}", kind="scope-denied",
                                    task_id=task_id, created_at=now)
            raise PlanError(reason)
        self.store.update_task(task_id, status="RUNNING")
        self.store.append_event(t["mission_id"], mission["stage"], f"RUN: {worker['name']} claimed {t['title']}",
                                kind="run", task_id=task_id, created_at=now)
        self._refresh_worker_status(now)
        return self.task_view(self.store.task(task_id))

    def report(self, task_id: str, worker: dict, evidence: dict, now: float | None = None) -> dict:
        """Record a worker's submission. Pass/fail happens later at VERIFY against
        files only; the worker's text is informational evidence."""
        now = time.time() if now is None else now
        t = self.store.task(task_id)
        if not t:
            raise PlanError("unknown task")
        if t["worker_id"] != worker["id"]:
            raise PlanError("task is assigned to another worker")
        if t["status"] not in ("RUNNING", "SUBMITTED"):
            raise PlanError(f"task is {t['status']} — only RUNNING tasks can be reported")
        mission = self.store.mission(t["mission_id"])
        artifacts = evidence.get("artifacts") if isinstance(evidence.get("artifacts"), list) else []
        evidence_lite = {
            "artifacts": [str(a)[:200] for a in artifacts][:20],
            "notes": str(evidence.get("notes") or "")[:2000],
            "reported_by_worker": str(evidence.get("status") or "done")[:40],
        }
        self.store.update_task(task_id, status="SUBMITTED", evidence_json=json.dumps(evidence_lite),
                               result_summary=f"submitted by {worker['name']} — queued for file verification")
        self.store.append_event(t["mission_id"], mission["stage"],
                                f"SUBMITTED: {t['title']} by {worker['name']} — awaiting verification (worker text is not trusted for pass/fail)",
                                kind="submit", task_id=task_id, created_at=now)
        self.advance(t["mission_id"], now=now)
        return self.task_view(self.store.task(task_id))

    def cancel(self, mission_id: str, note: str = "", now: float | None = None) -> dict:
        now = time.time() if now is None else now
        mission = self.store.mission(mission_id)
        if not mission:
            raise PlanError(f"Mission not found: {mission_id}")
        if mission["stage"] in TOKEN_TERMINAL_STAGES:
            return self.mission_view(mission_id)
        message = f"CANCELLED: {note[:260]}" if note else "CANCELLED"
        self.store.set_mission(mission_id, stage="CANCELLED", blocked_note=message)
        self.store.append_event(mission_id, "CANCELLED", message, kind="final", created_at=now)
        return self.mission_view(mission_id)

    def tick(self, now: float | None = None) -> None:
        """Background scheduler step: age workers, fail lost, advance active missions."""
        now = time.time() if now is None else now
        if self.fleet:
            self.fleet.age(now)
        for mission in self.store.missions():
            if mission["stage"] in TOKEN_TERMINAL_STAGES:
                continue
            try:
                self.advance(mission["id"], now=now)
            except PlanError as exc:
                # One broken mission must not stop the scheduler.
                self.store.append_event(mission["id"], mission["stage"],
                                        f"SCHEDULER WARNING: {str(exc)[:200]}", kind="warning", created_at=now)


def failing_paths(tasks: list[dict]) -> set[str]:
    """Best-effort set of paths among failed deliver/retry tasks."""
    out = set()
    for t in tasks:
        spec = json.loads(t["spec_json"] or "{}")
        if spec.get("op") in ("deliver", "retry-deliver"):
            path = spec.get("path")
            if path:
                out.add(path)
    return out

    # --- fleet status sync --------------------------------------------------------------
    def _refresh_worker_status(self, now: float) -> None:
        if not self.fleet:
            return
        busy: set[str] = set()
        for mission in self.store.missions():
            if mission["stage"] in TOKEN_TERMINAL_STAGES:
                continue
            for t in self.store.tasks(mission["id"]):
                if t["status"] == "RUNNING" and t["worker_id"]:
                    busy.add(t["worker_id"])
        self.fleet.refresh_status(busy, now)


def unsatisfied_paths(supervisor, tasks) -> set[str]:
    paths = set()
    for t in tasks:
        spec = json.loads(t["spec_json"] or "{}")
        if spec.get("op") in ("deliver", "retry-deliver"):
            paths.add(spec.get("path"))
    return {p for p in paths if p}
