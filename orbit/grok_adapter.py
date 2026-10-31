"""Opt-in Grok Build worker client for a loopback ORBIT broker.

The adapter reports the tools and skills it can see. The only tasks it runs
are the handlers in HANDLERS. A dashboard task cannot start a shell, load a
model, or restart another runtime.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from .worker_protocol import ADAPTER_VERSION, PROTOCOL, scrub

EXECUTABLE_CAPABILITIES = ("orbit.inventory", "orbit.ping")
SESSION_TOOLS = (
    "web_search",
    "open_page",
    "open_page_with_find",
    "x_user_search",
    "x_semantic_search",
    "x_keyword_search",
    "x_thread_fetch",
    "run_terminal_command",
    "read_file",
    "search_replace",
    "list_dir",
    "grep",
    "kill_command_or_subagent",
    "todo_write",
    "get_command_or_subagent_output",
    "spawn_subagent",
    "scheduler_create",
    "scheduler_delete",
    "scheduler_list",
    "monitor",
    "search_tool",
    "use_tool",
    "workflow",
    "enter_plan_mode",
    "exit_plan_mode",
    "ask_user_question",
    "send_feedback",
    "web_fetch",
    "image_gen",
    "image_edit",
    "image_to_video",
    "reference_to_video",
    "write",
)
MCP_TOOLS_RETURNED_BY_SEARCH = (
    "tasks__list",
    "tasks__get_results",
    "tasks__create",
    "tasks__update",
    "tasks__pause",
    "tasks__delete",
    "tasks__run_now",
    "tasks__validate",
    "tasks__list_trigger_catalog",
    "tasks__list_trigger_resources",
)
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv"}


class WorkerClientError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"ORBIT worker request failed ({status}): {detail}")
        self.status = status
        self.detail = detail


def default_state_path() -> Path:
    return Path.home() / ".local" / "share" / "nightknight-orbit" / "grok-worker.json"


def assert_loopback_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("ORBIT_URL must be an http loopback origin")
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("ORBIT_URL must be loopback; use an SSH local forward for a remote host")
    if parsed.path not in {"", "/"}:
        raise ValueError("ORBIT_URL must be an origin")
    return url.rstrip("/")


def scan_skill_names(roots: list[Path], limit: int = 200) -> list[str]:
    """Return skill directory names. Does not read skill file contents."""
    names: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        resolved = root.resolve()
        if not resolved.is_dir():
            continue
        for current, dirs, files in os.walk(resolved, followlinks=False):
            kept = []
            for name in dirs:
                path = Path(current) / name
                if name in _SKIP_DIRS or name.startswith("."):
                    continue
                if path.is_symlink():
                    target = Path(os.path.realpath(path))
                    if target.is_dir() and (target / "SKILL.md").is_file():
                        relative = Path(current, name).relative_to(resolved).as_posix()
                        names.append(relative)
                    continue
                kept.append(name)
            dirs[:] = kept
            if "SKILL.md" in files:
                relative = Path(current).relative_to(resolved).as_posix()
                names.append("." if relative == "." else relative)
    unique = sorted(set(names))
    return unique[:limit]


def default_skill_roots() -> list[Path]:
    home = Path.home()
    return [home / ".grok" / "skills", home / ".grok" / "bundled" / "skills"]


def build_inventory(skill_roots: list[Path] | None = None, host_id: str | None = None) -> dict[str, Any]:
    roots = default_skill_roots() if skill_roots is None else skill_roots
    discovered = scan_skill_names(roots, limit=10000)
    skill_names = discovered[:200]
    inventory: dict[str, Any] = {
        "runtime_name": "grok-build",
        "adapter_version": ADAPTER_VERSION,
        "model_id": "grok-4.7",
        "model_id_source": "harness_session_identity",
        "model_endpoint_probed": False,
        "model_ready": False,
        "host_id": host_id or socket.gethostname(),
        "os": platform.system(),
        "python": platform.python_version(),
        "executable_capabilities": list(EXECUTABLE_CAPABILITIES),
        "arbitrary_shell_from_dashboard": False,
        "observed_session_tools": list(SESSION_TOOLS),
        "observed_session_tools_executable_through_orbit": False,
        "observed_session_tools_source": "grok_build_session_2026-10-31",
        "mcp_servers_connected": ["tasks"],
        "mcp_tools_invoked": False,
        "mcp_search_returned": list(MCP_TOOLS_RETURNED_BY_SEARCH),
        "mcp_search_hidden_count": 10,
        "skill_count": len(discovered),
        "skill_names": skill_names,
        "skill_names_truncated": len(discovered) > len(skill_names),
        "pending_adapters": ["hermes", "opencode", "luna"],
    }
    if len(json.dumps(inventory).encode("utf-8")) > 12000:
        inventory["skill_names"] = skill_names[:40]
        inventory["skill_names_truncated"] = True
    return inventory


def manifest_from_inventory(
    inventory: dict[str, Any],
    worker_id: str | None = None,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    host = str(inventory.get("host_id") or "localhost")
    safe_host = "".join(ch if ch.isalnum() or ch in ".-" else "-" for ch in host)[:40] or "host"
    return {
        "worker_id": worker_id or f"grok-{safe_host}",
        "runtime_name": "grok-build",
        "host_id": host[:80],
        "model_id": inventory.get("model_id"),
        "model_id_source": inventory.get("model_id_source"),
        "capabilities": list(capabilities or EXECUTABLE_CAPABILITIES),
        "status": "IDLE",
        "version": ADAPTER_VERSION,
        "inventory": inventory,
    }


def handle_ping(lease: dict[str, Any]) -> dict[str, Any]:
    payload = lease.get("offer", {}).get("payload") or {}
    message = payload.get("message", "")
    if not isinstance(message, str) or len(message) > 500:
        raise ValueError("ping message rejected")
    return {"pong": message, "executed_shell": False}


def handle_inventory(lease: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    del lease
    return {"inventory": inventory, "executed_shell": False}


def save_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def load_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class GrokWorker:
    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        state_path: Path | None = None,
        inventory: dict[str, Any] | None = None,
        token: str | None = None,
        worker_id: str | None = None,
        capabilities: list[str] | None = None,
        enforce_loopback: bool = True,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.base_url = assert_loopback_url(base_url) if enforce_loopback else base_url.rstrip("/")
        self.client = client or httpx.Client(base_url=self.base_url, timeout=5.0)
        self.state_path = state_path
        self.inventory = inventory if inventory is not None else build_inventory()
        self.token = token
        self.worker_id = worker_id
        self.capabilities = list(capabilities or EXECUTABLE_CAPABILITIES)
        self.log = log or (lambda _line: None)
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "orbit.ping": handle_ping,
            "orbit.inventory": lambda lease: handle_inventory(lease, self.inventory),
        }
        self._owns_client = client is None

    def __repr__(self) -> str:
        return f"GrokWorker(worker_id={self.worker_id!r})"

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def check_protocol(self) -> dict[str, Any]:
        response = self.client.get("/api/workers/protocol")
        body = self._json(response)
        if body.get("protocol") != PROTOCOL:
            raise WorkerClientError(409, "protocol mismatch")
        return body

    def pair(self, pairing_code: str, worker_id: str | None = None) -> dict[str, Any]:
        self.check_protocol()
        manifest = manifest_from_inventory(
            self.inventory, worker_id=worker_id or self.worker_id, capabilities=self.capabilities
        )
        response = self.client.post(
            "/api/workers/pair",
            json={"protocol": PROTOCOL, "pairing_code": pairing_code, "manifest": manifest},
        )
        body = self._json(response)
        self.token = body["token"]
        self.worker_id = body["worker_id"]
        if self.state_path is not None:
            save_state(
                self.state_path,
                {
                    "protocol": PROTOCOL,
                    "url": self.base_url,
                    "worker_id": self.worker_id,
                    "token": self.token,
                },
            )
        self.log(f"paired worker_id={self.worker_id} token=stored")
        return {"worker_id": self.worker_id, "protocol": body["protocol"], "token_stored": self.state_path is not None}

    def heartbeat(self, status: str = "IDLE") -> dict[str, Any]:
        manifest = manifest_from_inventory(self.inventory, worker_id=self.worker_id, capabilities=self.capabilities)
        manifest["status"] = status
        response = self.client.post("/api/workers/heartbeat", headers=self._headers(), json={"manifest": manifest})
        return self._json(response)

    def poll(self) -> dict[str, Any]:
        response = self.client.post("/api/workers/poll", headers=self._headers())
        return self._json(response)

    def progress(self, task_id: str, lease_id: str, status: str, progress: float, message: str) -> dict[str, Any]:
        response = self.client.post(
            f"/api/workers/tasks/{task_id}/progress",
            headers=self._headers(),
            json={
                "lease_id": lease_id,
                "event_id": uuid.uuid4().hex,
                "status": status,
                "progress": progress,
                "message": scrub(message, [self.token or ""]),
            },
        )
        return self._json(response)

    def result(self, task_id: str, lease_id: str, status: str, result: dict[str, Any], log_excerpt: str) -> dict[str, Any]:
        response = self.client.post(
            f"/api/workers/tasks/{task_id}/result",
            headers=self._headers(),
            json={
                "lease_id": lease_id,
                "status": status,
                "result": result,
                "log_excerpt": scrub(log_excerpt, [self.token or ""]),
            },
        )
        return self._json(response)

    def handle_once(self) -> dict[str, Any]:
        beat = self.heartbeat("IDLE")
        polled = self.poll()
        lease = polled.get("lease")
        if not lease:
            return {"action": "idle", "presence": beat.get("presence"), "executed_shell": False}
        task_id = lease["task_id"]
        lease_id = lease["lease_id"]
        cancels = set(polled.get("cancels") or [])
        if lease.get("cancel_requested") or task_id in cancels:
            self.result(task_id, lease_id, "cancelled", {"cancelled": True, "executed_shell": False}, "cancel acknowledged")
            return {"action": "cancelled", "task_id": task_id, "executed_shell": False}
        task_type = lease["offer"]["type"]
        handler = self.handlers.get(task_type)
        self.progress(task_id, lease_id, "accepted", 0.2, "accepted")
        if handler is None:
            self.result(
                task_id,
                lease_id,
                "rejected",
                {"reason": "unsupported_task", "task_type": task_type, "executed_shell": False},
                "not executed",
            )
            return {"action": "rejected", "task_id": task_id, "executed_shell": False}
        self.heartbeat("BUSY")
        try:
            output = handler(lease)
        except Exception as exc:
            self.result(task_id, lease_id, "error", {"error": type(exc).__name__, "executed_shell": False}, "handler failed")
            self.heartbeat("ERROR")
            return {"action": "error", "task_id": task_id, "executed_shell": False}
        if not isinstance(output, dict):
            self.result(task_id, lease_id, "error", {"error": "non_object_result", "executed_shell": False}, "bad handler result")
            return {"action": "error", "task_id": task_id, "executed_shell": False}
        output["executed_shell"] = False
        self.progress(task_id, lease_id, "running", 1.0, "finished")
        self.result(task_id, lease_id, "completed", output, "ok")
        self.heartbeat("IDLE")
        return {"action": "completed", "task_id": task_id, "result": output, "executed_shell": False}

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise WorkerClientError(401, "worker is not paired")
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Orbit-Nonce": secrets.token_hex(16),
            "X-Orbit-Timestamp": str(int(time.time())),
        }

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = {"detail": "non-JSON response"}
        if response.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else "request rejected"
            raise WorkerClientError(response.status_code, str(detail))
        if not isinstance(body, dict):
            raise WorkerClientError(response.status_code, "expected a JSON object")
        return body


def _read_pairing_code() -> str:
    code = os.environ.get("ORBIT_PAIRING_CODE", "")
    code_file = os.environ.get("ORBIT_PAIRING_CODE_FILE", "")
    if not code and code_file:
        code = Path(code_file).read_text(encoding="utf-8").strip()
    if not code:
        raise SystemExit("Set ORBIT_PAIRING_CODE or ORBIT_PAIRING_CODE_FILE. The code is not accepted as a process argument.")
    return code


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="orbit.grok_adapter")
    parser.add_argument("command", choices=["inventory", "pair", "once", "run"])
    parser.add_argument("--url", default=os.environ.get("ORBIT_URL", "http://127.0.0.1:8766"))
    parser.add_argument("--state", default=os.environ.get("ORBIT_WORKER_STATE", str(default_state_path())))
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.command == "inventory":
        print(json.dumps(build_inventory(), indent=2, sort_keys=True))
        return
    try:
        base_url = assert_loopback_url(args.url)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    state_path = Path(args.state).expanduser()
    worker = GrokWorker(base_url, state_path=state_path, log=print)
    try:
        if args.command == "pair":
            public = worker.pair(_read_pairing_code())
            print(json.dumps(public))
            return
        if not state_path.is_file():
            raise SystemExit("worker state is missing; pair first")
        state = load_state(state_path)
        worker.token = state["token"]
        worker.worker_id = state["worker_id"]
        if args.command == "once":
            print(json.dumps(worker.handle_once()))
            return
        while True:
            print(json.dumps(worker.handle_once()))
            time.sleep(args.interval)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
