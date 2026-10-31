"""SQLite-backed durable transcripts, tool approvals and audit trail."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Storage:
    def __init__(self, filename: Path):
        self.filename = filename
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_calls_json TEXT,
                    tool_call_id TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result TEXT,
                    created_at REAL NOT NULL,
                    resolved_at REAL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    args_summary TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    outcome TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
                CREATE INDEX IF NOT EXISTS approvals_session ON approvals(session_id, state);
                CREATE INDEX IF NOT EXISTS audit_session ON audit(session_id, id);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.filename, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA busy_timeout = 30000")
            with db:
                yield db
        finally:
            db.close()

    def new_session(self, profile: str, title: str = "Нова мисия") -> dict:
        session_id = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (session_id, profile, title[:120], now, now))
        return self.get_session(session_id)

    def get_session(self, session_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def sessions(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]

    def add_message(self, session_id: str, role: str, content: str | None = None, *, tool_calls=None, tool_call_id=None):
        with self.connect() as db:
            db.execute("INSERT INTO messages(session_id,role,content,tool_calls_json,tool_call_id,created_at) VALUES(?,?,?,?,?,?)",
                       (session_id, role, content, json.dumps(tool_calls) if tool_calls is not None else None, tool_call_id, time.time()))
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id))

    def messages(self, session_id: str):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        out = []
        for r in rows:
            o = dict(r)
            if o["tool_calls_json"]:
                o["tool_calls"] = json.loads(o.pop("tool_calls_json"))
            else:
                o.pop("tool_calls_json")
            out.append(o)
        return out

    def new_approval(self, session_id: str, call_id: str, name: str, arguments: dict):
        approval_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO approvals(id,session_id,call_id,name,args_json,state,created_at) VALUES(?,?,?,?,?,?,?)",
                       (approval_id, session_id, call_id, name, json.dumps(arguments), "pending", time.time()))
        return approval_id

    def approval(self, approval_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return self._approval_row(row) if row else None

    @staticmethod
    def _approval_row(row):
        value = dict(row)
        value["args"] = json.loads(value.pop("args_json"))
        return value

    def pending(self, session_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM approvals WHERE session_id=? AND state='pending' ORDER BY created_at", (session_id,)).fetchall()
        return [self._approval_row(row) for row in rows]

    def resolve_approval(self, approval_id: str, decision: str, result: str) -> bool:
        with self.connect() as db:
            result_db = db.execute("UPDATE approvals SET state=?,result=?,resolved_at=? WHERE id=? AND state='pending'",
                                   (decision, result, time.time(), approval_id))
        return result_db.rowcount == 1

    def audit(self, session_id: str, name: str, args: dict, decision: str, outcome: str):
        # Never log file content or API secrets: paths / sizes only.
        safe = {k: (f"<{len(str(v))} chars>" if k == "content" else str(v)[:200]) for k,v in args.items()}
        with self.connect() as db:
            db.execute("INSERT INTO audit(session_id,name,args_summary,decision,outcome,created_at) VALUES(?,?,?,?,?,?)",
                       (session_id, name, json.dumps(safe), decision, outcome[:500], time.time()))

    def audit_events(self, session_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM audit WHERE session_id=? ORDER BY id DESC LIMIT 30", (session_id,)).fetchall()
        return [dict(row) for row in rows]
