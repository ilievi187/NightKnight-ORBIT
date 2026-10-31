# ORBIT Roadmap

## M0 — Bootable vertical slice (v0.1)

- [x] Reproducible standalone app + responsive mission control UI
- [x] Profiles ALEX/NOVA/HERMES (prompt definitions only)
- [x] Single OpenAI-compatible model adapter
- [x] Durable sessions, messages, approvals, audit via SQLite
- [x] Read-only workspace tools + write with user approval
- [x] Mocked provider and permission tests
- [ ] Test against actual NightKnightPC Heretic inference endpoint
- [ ] Integrate existing ALEX CLI and actual 173 skills

## M1 — Real harness

- [ ] Multi-model registry, local/cloud detection, routing, fallback and quotas
- [ ] SSE streaming with backpressure and cancellation
- [ ] Compact/retrieve context with source provenance and token budget
- [ ] Native CLI with scripted tasks; import existing ALEX sessions
- [ ] Declarative MCP/skills adapter (per-tool scopes and installed skills)
- [ ] Separate policy engine with audit and diff preview before writes
- [ ] Single-writer concurrency control / interruption recovery
- [ ] Security hardening and integration tests

## M2 — Distributed command center

- [ ] Paired host daemon (NightKnightPC, detti, Ubuntu server)
- [ ] Host GPU/CPU/VRAM discovery & workload-aware scheduling
- [ ] Capability-based command execution in a container / OS sandbox
- [ ] Resumable DAG tasks, child agents, event bus and rollback
- [ ] Authenticated local UI, least privilege, secret manager
- [ ] Metrics: tok/s, latency, model costs, tool success, VRAM

## M3 — Solar-system desktop

- [ ] Tauri desktop packaging for Arch, Windows
- [ ] Dark 3D orbital filesystem / live working agent planets
- [ ] Full-screen voice ALEX/NOVA, lip-synced avatar and STT/TTS
- [ ] Opt-in browser and desktop control with clear live indicators
- [ ] Screen/video playback / workstation mission board
- [ ] Benchmarks against OpenCode, OpenHands, Claude Agent SDK

**Release gate:** A feature is not declared complete without integration tests on the actual hosts and manual verification of OS permissions.
