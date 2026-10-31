"""Model-agnostic tool loop with persisted state, bounded steps, and approval gates."""
import json
import threading
import httpx

from .config import Settings
from .storage import Storage
from .tools import TOOLS, WRITE_TOOLS, execute_tool

PROFILES = {
    "ALEX": "You are ALEX, a capable and precise coding and system engineering agent. Work incrementally; confirm evidence before claiming success.",
    "NOVA": "You are NOVA, a creative but rigorous planning, research and architecture agent. Explain tradeoffs and avoid confident guesses.",
    "HERMES": "You are HERMES, a pragmatic local coding agent. Minimize waste, favor concise execution plans and verify file operations.",
}

MAX_STEPS = 8


class ProviderError(RuntimeError):
    pass


class Harness:
    def __init__(self, settings: Settings, storage: Storage, client: httpx.Client | None = None):
        self.settings = settings
        self.store = storage
        self.client = client or httpx.Client(timeout=httpx.Timeout(90, connect=8))
        self._locks_guard = threading.Lock()
        self._locks = {}

    def lock(self, session_id: str):
        with self._locks_guard:
            if session_id not in self._locks:
                self._locks[session_id] = threading.RLock()
            return self._locks[session_id]

    def start(self, profile: str, title: str):
        if profile not in PROFILES:
            raise ValueError("Unknown profile")
        return self.store.new_session(profile, title)

    def _model_messages(self, session_id):
        session = self.store.get_session(session_id)
        out = [{"role": "system", "content": (
            PROFILES[session["profile"]] + "\n" +
            f"You operate inside the user's sandboxed project workspace: {self.settings.workspace}. " +
            "Use tools for verifiable facts. Never claim a tool ran if it did not. " +
            "Files can be listed/read automatically, but writing needs explicit user approval. " +
            "No shell execution, browser automation, or cross-host access are available in this prototype. " +
            "Do not ask for secrets or attempt to bypass workspace restrictions."
        )}]
        for saved in self.store.messages(session_id):
            msg = {"role": saved["role"], "content": saved["content"]}
            if "tool_calls" in saved:
                msg["tool_calls"] = saved["tool_calls"]
            if saved["tool_call_id"]:
                msg["tool_call_id"] = saved["tool_call_id"]
            out.append(msg)
        return out

    def _completion(self, session_id):
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        payload = {
            "model": self.settings.model,
            "messages": self._model_messages(session_id),
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": 0.2,
            "stream": False,
        }
        try:
            response = self.client.post(self.settings.base_url + "/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Model endpoint unavailable or invalid response: {str(exc)[:400]}") from exc

    def chat(self, session_id: str, user_text: str):
        with self.lock(session_id):
            if not self.store.get_session(session_id):
                raise ValueError("Unknown session")
            if self.store.pending(session_id):
                return self._result(session_id, "approval_required")
            if not user_text.strip():
                raise ValueError("Message cannot be blank")
            self.store.add_message(session_id, "user", user_text[:10000])
            return self._advance(session_id)

    def _advance(self, session_id):
        if self.store.pending(session_id):
            return self._result(session_id, "approval_required")
        for _ in range(MAX_STEPS):
            msg = self._completion(session_id)
            tool_calls = msg.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                raise ProviderError("Invalid tool_calls response")
            self.store.add_message(session_id, "assistant", msg.get("content"), tool_calls=tool_calls or None)
            if not tool_calls:
                return self._result(session_id, "completed")
            for call in tool_calls:
                call_id = str(call.get("id", ""))
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                    if not isinstance(args, dict):
                        raise ValueError("Expected JSON object")
                except (ValueError, TypeError) as exc:
                    output = f"Tool arguments error: {exc}"
                    self.store.add_message(session_id, "tool", output, tool_call_id=call_id)
                    self.store.audit(session_id, name, {}, "rejected", output)
                    continue
                if name in WRITE_TOOLS:
                    self.store.new_approval(session_id, call_id, name, args)
                    self.store.audit(session_id, name, args, "pending", "Waiting for explicit approval")
                else:
                    try:
                        output = execute_tool(name, args, self.settings.workspace)
                        decision = "auto-read"
                    except (OSError, ValueError, PermissionError) as exc:
                        output, decision = f"Tool error: {exc}", "rejected"
                    self.store.add_message(session_id, "tool", output, tool_call_id=call_id)
                    self.store.audit(session_id, name, args, decision, output)
            if self.store.pending(session_id):
                return self._result(session_id, "approval_required")
        return self._result(session_id, "step_limit")

    def approve(self, approval_id: str, approved: bool):
        action = self.store.approval(approval_id)
        if not action:
            raise ValueError("Unknown approval")
        session_id = action["session_id"]
        with self.lock(session_id):
            action = self.store.approval(approval_id)
            if action["state"] != "pending":
                raise ValueError("Approval already resolved")
            if approved:
                try:
                    output = execute_tool(action["name"], action["args"], self.settings.workspace, approved=True)
                    decision = "approved"
                except (OSError, ValueError, PermissionError) as exc:
                    output, decision = f"Tool error: {exc}", "failed"
            else:
                output, decision = "User denied this change.", "denied"
            if not self.store.resolve_approval(approval_id, decision, output):
                raise ValueError("Approval already resolved")
            self.store.add_message(session_id, "tool", output, tool_call_id=action["call_id"])
            self.store.audit(session_id, action["name"], action["args"], decision, output)
            if self.store.pending(session_id):
                return self._result(session_id, "approval_required")
            return self._advance(session_id)

    def _result(self, session_id, status):
        messages = self.store.messages(session_id)
        assistants = [x for x in messages if x["role"] == "assistant" and x["content"]]
        return {
            "status": status,
            "assistant": assistants[-1]["content"] if assistants else "",
            "session": self.store.get_session(session_id),
            "messages": messages,
            "approvals": self.store.pending(session_id),
            "audit": self.store.audit_events(session_id),
        }
