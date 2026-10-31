# ORBIT Agent Missions — Sprint 1

The tasks below are independent starting points; each gets its own PR. Do not claim agents are running until a real runner/job is visible.

## NOVA — M1: Architecture & contracts
**Goal**: propose a versioned provider interface and host capability protocol, including failure/retry semantics and least-privilege boundaries.
**Deliverables**: `docs/agents/provider-contract.md`, ADR for multi-host broker, threat model, executable fixtures or API schema where relevant.
**Acceptance**: covers local OpenAI-compatible servers, timeouts, cancellations, tool-call limits and how pending approvals survive restart. Does not grant arbitrary desktop/shell access.

## ALEX — M2: Provider adapter & observable tasks
**Goal**: separate the current HTTP provider from session orchestration; introduce controlled timeouts, cancellation, and structured activity/status APIs.
**Deliverables**: provider interface, fake provider for tests, task progress states and tests.
**Acceptance**: old 6 tests remain green, new provider tests pass, cancellation cannot execute already rejected actions.

## HERMES — M3: Local inference integration
**Goal**: detect and validate explicit Ollama/llama.cpp model endpoints and expose honest readiness/latency, without automatically loading weights or claiming unavailable VRAM.
**Deliverables**: optional adapter/config, health probe with timeout, docs for NightKnightPC and sample model runs.
**Acceptance**: mocked offline tests; integration test guarded behind opt-in env var; clear error states; no network calls at import time.

## LUNA — M4: Security & CI
**Goal**: harden approvals, unauthorized origin rejection, workspace traversal, symlink revalidation, and accidental secret exposure.
**Deliverables**: additional negative tests, GitHub workflow and local safety checklist.
**Acceptance**: test suite passes on Python 3.11 and 3.13; no test requires production credentials; write approvals cannot be replayed or swapped between sessions.

## Next sprint (blocked until Sprint 1 contracts reviewed)
- Adapter bridging real ALEX / NOVA / Hermes processes via authenticated local broker.
- Live agent status, queue and reproducible activity log.
- Multi-host worker leases, Tailscale identity checks and signed capabilities.
- Sci-Fi UI animation, voice avatar and accessibility / reduced-motion controls.

## Coordination protocol
One GitHub issue per mission with a named owner or agent runner. Every PR links its issue, includes tests run, and states security implications. Integrate only after green CI and human review of permissions.
