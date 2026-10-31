# ORBIT Architecture v0.1

```text
Browser (Mission Control UI)
        | same-origin HTTP
        v
ORBIT Gateway (FastAPI, loopback only)
        |
        +-- Agent Profiles: ALEX / NOVA / HERMES
        +-- Model Adapter: OpenAI-compatible /chat/completions
        +-- Tool Loop: fixed 8-step budget per turn
        +-- Capability Policy: auto-read / user-approved write
        +-- SQLite: sessions / messages / approvals / audit
        +-- Workspace Tools: list / read / write
        |
        v
User-owned project folder (single selected root)

Later:
  Model Router     -> local llama.cpp / Ollama / cloud adapters
  Host Agent       -> NightKnightPC / detti via authenticated Tailscale
  Capability Gate  -> shell/browser/GUI skills with durable approvals
  Context Engine   -> scoped memory, RAG, compaction, provenance
  Task Scheduler   -> resumable jobs and multi-agent coordination
  Voice & Avatar   -> speech-to-text + TTS + reactive face
```

### Core contract

- Session and permission state is not held in model context; SQLite is source of truth.
- Each tool invocation gets a stable model call ID and an audit entry.
- Model responses are untrusted proposals. They may not grant themselves permission.
- Workspace path traversal and symlink escapes are prohibited.
- Every write is paused until the human approves it, including when the model calls tools autonomously.
- The API only listens on loopback in default configuration.

### Threat model for next milestone

Before enabling arbitrary shell or cross-host execution: OS-level sandbox, process isolation, capability tokens, approval receipts, resource limits, timeouts, durable job IDs, secret vault, explicit host trust and a full security review are necessary. A prompt that says "be safe" is not a security boundary.
