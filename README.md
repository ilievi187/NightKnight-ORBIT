# NightKnight ORBIT — Universal AI Agent Harness

> Product direction: [ORBIT North Star](docs/ORBIT-NORTH-STAR.md) | [Supervisor epic #9](https://github.com/ilievi187/NightKnight-ORBIT/issues/9). This is a first-step prototype, not the full autonomous supervisor.

**Prototype v0.2 (agent-ready)** · Local-first · OpenAI-compatible models · SQLite sessions · Explicit tool approvals · Sci-Fi mission-control UI

ORBIT is the proposed independent orchestration layer for **ALEX**, **NOVA** and **HERMES**. It **does not replace or modify** the existing local `NightKnight-ALEX` installation or the private Hermes backup. The profiles in v0.1 are prompt-defined personalities, **not yet integrations** with those running services.

## What works now

- Browser-based, responsive dark mission-control UI with interactive agent constellation.
- Switch between profiles (starts a fresh session for that profile).
- Persistent chat transcripts and action audit in local SQLite.
- OpenAI-compatible `chat/completions` tool-calling loop (`llama.cpp`, Ollama-compatible endpoints, LM Studio, or cloud endpoints speaking this API).
- `list_files` and `read_file` tools strictly confined to `ORBIT_WORKSPACE`.
- `write_file` tool pauses for human approval. Approve or deny in the UI; the model continues after the decision.
- No terminal commands, browser automation, cross-device actions, API key editor, cloud auth, voice, real streaming, multi-model router, autonomous skills installer, or remote user accounts yet.

## Run on Linux (Arch / Omarchy)

```bash
cd NightKnight-ORBIT
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env to match your actual local inference server & workspace
set -a; source .env; set +a
python -m orbit
```

Open **http://127.0.0.1:8765** in your browser.

Default model API is `http://127.0.0.1:8080/v1`; this does **not** launch any model server. Start a compatible model server first and set `ORBIT_MODEL` to the **exact exposed model ID**. For Ollama, the URL is typically `http://127.0.0.1:11434/v1`. For cloud models, set the corresponding HTTPS endpoint and `ORBIT_API_KEY` (some cloud APIs require an adapter or specific protocol). Never add a real API key to version control.

If your local model cannot reliably perform structured tool calls, chats may work but file tools will not. Check that your specific model template and endpoint support OpenAI-compatible tool calling.

**Do not serve this app to the internet or bind it to `0.0.0.0`.** There is no user authentication in v0.1. Run one process/worker only. For remote access, wait for authenticated access controls; a tunnel by itself is not authentication.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Directory structure

```
orbit/                  FastAPI service, policy engine, provider adapter, SQLite
web/                    static, build-free frontend
  index.html            shell / chat / agent constellation
  style.css             responsive dark theme
  app.js                session, chat & approval frontend
 tests/                  mocked provider and safety tests
 docs/                   architecture and roadmap
```

## Safety & known limitations

- Read calls may send **workspace file contents** to the selected model provider. Choose your provider and workspace accordingly.
- Sensitive filenames and key formats are blocked; this is defense-in-depth, **not** a complete data-loss-prevention system.
- Model-proposed writes require a click before execution; path escape and obvious credentials paths are blocked. A write can overwrite an existing file after approval, so review the entire proposed change and use git before important edits.
- The audit is local and does not constitute tamper-proof logging.
- No command-execution tool exists yet. Desktop and multi-host control require an isolated capability broker with a stricter permission model.
- The local model can fail on long conversations; context compaction, token accounting, provider failover and streaming are future work.

## Development principles

1. Own the session, state, routing and permissions, never the model weights.
2. Capability-first plugins: every skill/tool declares and asks for scope.
3. Separate **ORBIT core** from **ALEX/NOVA/HERMES profiles** and the separate model inference workers.
4. Record every tool action; support reproducible runs and recovery.
5. Test against real tasks and published baselines before saying "better than OpenCode / other harnesses".

See [Architecture](docs/ARCHITECTURE.md) and [Roadmap](docs/ROADMAP.md).

## Where this project should live

The current ALEX source of truth is on NightKnightPC at `/home/nightknight/Projects/NightKnight-ALEX`. Keep ORBIT as a **sibling** directory (`/home/nightknight/Projects/NightKnight-ORBIT`), not inside the Hermes private backup. Later, attach ALEX as a real backend via an explicit bridge adapter, after inspecting the actual code and auth surfaces.

## Parallel agent development (Sprint 1)

See [`AGENTS.md`](AGENTS.md) and [`docs/agents/MISSIONS.md`](docs/agents/MISSIONS.md). These are *instructions and task definitions*, not active autonomous processes. Agent execution requires a separately installed/authorized runner and access to the dedicated ORBIT repository. GitHub Actions runs tests only.

To launch GitHub coding agents in a dedicated repository, see [GitHub Launch](docs/agents/GITHUB-LAUNCH.md). Custom agent profiles are in `.github/agents/`; they are not running processes.
