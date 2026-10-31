# NIGHTKNIGHT ORBIT — North Star (Harness of Harnesses)

**Status:** Product/architecture direction, not an implementation claim.
**Scope:** ORBIT is the supervisory control plane connecting existing agent harnesses, model providers, skills/tools, project workspaces, and authorized devices.
**Tracking:** [Epic #9](https://github.com/ilievi187/NightKnight-ORBIT/issues/9).

## One-sentence mission

**One user objective in; ORBIT discovers what it can actually do, plans, delegates, observes, corrects, resumes, and reports — while the user grants sensible boundaries once instead of approving every ordinary step.**

The product is neither a new standalone coding model, nor a wrapper that forwards prompts blindly to several LLMs, nor a GUI for a single runtime. It is a *meta-harness*: a planner/supervisor and a capability-governed operating system for AI work.

The supervisor **does not have to do the actual coding or browsing**. It is responsible for making sure the right available workers do the job, verifying their results, and recovering when they fail.

## Vocabulary: don't conflate these entities

| Concept | Examples | ORBIT responsibility |
|---|---|---|
| Brain / model | local Heretic via llama.cpp/Ollama; OpenAI API; later Gemini/Grok | Capability discovery, benchmarking, cost/context and error-aware routing |
| Worker / harness | OpenCode, Hermes, ALEX, LUNA, coding or research agents | Lifecycle, adapters, delegated task execution and reported progress |
| Skill / tool | Agent Skills (SKILL.md), MCP, Git, filesystem, tests, browser | Discovery, permission checks, input/output contracts |
| Host | NightKnightPC, Ubuntu, detti | Identity, resource telemetry, authenticated execution, heartbeats |
| Project | ORBIT, AURORA, others | Workspace root, branches/worktrees, state, granted authority |
| Supervisor | ORBIT | Plan Mode, DAG scheduling, monitoring, arbitration, recovery |

**"All brains as one" means shared coordination, memory and tool contracts, not merging model weights or pretending different subscriptions are interchangeable.** Maintain explicit identities and measured capabilities for each provider/agent.

## The mandatory self-improving mission loop (user-defined product invariant)

**Not a one-shot plan.** Every mission executes the entire cycle repeatedly until objective acceptance criteria are met or a bounded, explicit blocker requires intervention.

```text
USER GOAL
   ↓
1. ANALYZE GOAL            → observable scope, requirements, Definition of Done
   ↓
2. INVENTORY AVAILABILITY  → real models/APIs, agent processes, hosts, tools,
                             context, permissions, VRAM, spend limits
   ↓
3. EVALUATE CAPABILITIES   → tested/observed skills of each worker + brain;
                             capability-confidence and failure history
   ↓
4. BUILD PROJECT SKELETON → workspace/repo structure, interfaces, phases,
                             dependencies, acceptance tests and deliverables
   ↓
5. ASSIGN + EXECUTE        → schedule eligible agents, isolated worktrees,
                             verified task inputs/outputs
   ↓
6. VERIFY PROGRESS         → run acceptance tests, inspect artifacts,
                             compare whole-project state against goal
   ↓
7. GAP ANALYSIS            → missing requirements? bugs? integration failures?
   ├─ YES → revise plan, re-score available agents, reassign gaps,
   │         execute new tasks, verify AGAIN (repeat from 2/3 as needed)
   ├─ BLOCKED → keep independent tasks running; request ONLY the specific
   │            new grant/decision/resource needed for blocked tasks
   └─ NO  → final independent project-level verification → COMPLETE
```

**Project readiness, not just task completion:** an agent finishing its assigned work does **not** mean the project is done. The supervisor re-inspects the combined artifact, interfaces, tests, UX and all originally requested features after every iteration. Project skeleton is established early and evolves when evidence exposes new constraints.

**Completion contract:** `readiness = 100%` means all tracked, testable acceptance requirements are satisfied by evidence (not an LLM's self-rating); mandatory security and integration gates pass; there are no critical unresolved gaps; results are reproducible. Show a truthful partial score, `BLOCKED`, or `UNKNOWN` when proof is missing. There is no guarantee of literal perfection.

**Anti-stall control:** each iteration has an ID, plan revision, work budget, retry cap, success/failure events and changed-state diff. Detect no-progress loops, repeated failed approaches, expired permits, offline workers and depleted quotas. Replan or escalate **one concrete blocker** instead of endlessly retrying or pausing unrelated work. Persist the whole mission across restarts, including approvals.

**Example:** User requests a complete AURORA site. ALEX finishes backend, HERMES fixes model integration, NOVA verifies information architecture, LUNA runs independent E2E/security checks; supervisor discovers case pages lack embedded video, creates focused follow-up tasks, and loops until the defined video, language, data and UX acceptance tests pass — not until a worker merely says “done”.

## Core experience: autonomous Plan Mode

1. **Understand the goal** and inspect the authorized project, its tests and current task state.
2. **Discover actual resources:** which models and runtimes are online, tool/skill availability, verified context window, practical coding/tool competence, GPU/RAM headroom, endpoint latency, quotas, prices, and known limitations. Unknown means *unknown*.
3. **Produce an executable plan:** a persistent DAG with milestones, dependencies, acceptance criteria, rollback options, priorities, estimated cost and matching worker requirements. The user can edit the plan, but need not micromanage it.
4. **Delegate:** choose workers/models based on observed evidence and policy. Run independent tasks concurrently in isolated branches/worktrees/containers; cap concurrency, spend and VRAM.
5. **Observe and steer:** durable status events, heartbeat, logs, tests and artifacts. Detect slow, failed, missing, stuck or blocked workers; retry, reassign, reprioritize or request the minimum necessary decision.
6. **Verify:** tests and independent review; only count work complete when outputs exist and acceptance conditions pass. A worker's success claim is not proof.
7. **Recover:** checkpoint after each transition; resume after restart, model unload or network loss without repeating completed work or re-executing previously denied writes.
8. **Report:** concise human summary with evidence, commits/PRs, remaining risks, measured cost and the exact blocked scope if any.

The supervisor may start/wake a worker **only within preconfigured user-approved host/resource permissions**, with rate limits, budget caps, and auditable lifecycle commands. It cannot promise to perform tasks in the background unless a running ORBIT service/runner really exists.

## Autonomous permissions: one approval for a bounded mission

The prototype's per-write approval is **a starting safety gate**, not the intended UX.

Proposed opt-in mission grant:
- **Scope:** one or more explicit project directory subtrees on named paired hosts; isolated worktree; tool and operation allowlist.
- **Routine permissions:** inspect/read, navigate nested folders, create/edit non-sensitive files, execute project-local tests and tooling, make local commits — without asking for each file.
- **Limits:** expiry, wall-clock/runtime, CPU/GPU/VRAM quotas, token/API spend ceiling, concurrent task count, project identifiers and auditable operations.
- **Escalation:** ask only when moving outside grant, accessing private data/credentials, changing host or permission settings, destructive operations, merging into protected branches, external publication/deployment, payments, or other consequential side effects.
- **After a denial:** pause that action and its dependents; **keep unrelated task DAG branches running**. Never globally deadlock a mission.
- **Enforcement:** scoped capabilities and sandbox isolation in the tool/host layer; no model can authorize itself; every operation has a decision receipt. Keep transparent diffs, checkpoints and rollback.
- **Migration:** current per-write gate stays in place until the new policy engine, revocation, audit and test matrix are implemented and explicitly enabled.

## Initial provider priority

**Now:** (1) local LLMs over OpenAI-compatible APIs, including Heretic (no assumptions about exact live model names/ports), (2) OpenAI API with an official configured API key and cost controls, (3) OpenCode as an *agent runtime/worker*, not mislabelled as an LLM provider.

**Later:** Gemini, Grok and other supported endpoints; more cloud subscriptions only when an officially supported integration exists. A ChatGPT Plus/Pro consumer subscription is not, by itself, an OpenAI API key or API entitlement. No session-token extraction, ToS bypass or unofficial credential sharing.

Prioritize stability and capabilities over a long list of logos.

## Target components

```text
User goal / mission board / voice
            |
     ORBIT SUPERVISOR (Plan Mode)
     - task graph / budget / policy / audit
     - observe, delegate, verify, retry, recover
            |
     ┌──────┴───────────┬──────────────┐
     |                  |              |
 Brain registry    Worker registry   Skill/tool registry
 local LLM         OpenCode/Hermes    MCP, files, tests,
 OpenAI API        ALEX/NOVA/LUNA     Git, browser...
 later others      other harnesses
     |                  |              |
     └──────────────────┴──────────────┘
            |
   Capability-enforced host gateway
   (paired NightKnightPC / Ubuntu / detti)
            |
   isolated worktrees, event log, checkpoints,
   resource telemetry, test/PR evidence
```

The UI is a *truthful visual projection of system state*, not an independent source of truth. Agent planets should indicate real running, idle, blocked, failed, delegated and verified work; no fabricated metrics.

## Non-negotiable acceptance scenario

Given a user-authorized project root, run one test mission:
- Detect at least two mocked workers and two brain/provider profiles, including one offline or unavailable option.
- Score candidate assignments using recorded evidence; route tasks to eligible workers, not simply the cheapest/name-matched model.
- Persist a plan with at least three tasks, including parallel work and a dependency.
- Demonstrate scoped editing without a prompt per nested folder or normal file.
- Simulate one worker failure; reassign only its unfinished steps.
- Simulate a request outside scope; block that action and notify the user while independent work continues.
- Restart the supervisor; prove completed steps are not repeated and pending approvals are not silently assumed granted.
- Verify tests/artifact and produce a truthful, audited completion report.
- Include time, token/spend and resource budget assertions, without invented metrics.

## Delivery order (not seven unrelated rewrites)

**P0: Supervisor kernel** — persistent task DAG, event log, state machine, planning artifact, mock worker/brain registries, observation/retry/restart tests.

**P0: Capability grants** — mission-level approval, path/host/tool scopes, isolation, revocation, no global deadlocks, audit, regression tests.

**P1: Real adapters** — local OpenAI-compatible + OpenAI API first; OpenCode and Hermes worker adapters; measured provider selection, real task lifecycle.

**P1: Worker fleet** — isolated Git worktrees, authenticated multi-host runtime, resource-aware scheduling, waking authorized workers.

**P2: Experience** — 3D/solar-system mission control, voice/avatar, richer integrations and optional Gemini/Grok.

## Integration rules

- Reuse carefully chosen, licensed components from Orca, OpenHands, OpenCode, Hermes, etc. through clean adapters where practical, instead of copying whole repositories into ORBIT.
- Keep existing PR #6 (skills) and PR #7 (runtime) independent; the supervisor must integrate them without destroying working code.
- Ship narrowly scoped PRs, offline/mock tests and real-host validation before declaring hardware/runtime features completed.
- A multi-agent work result is accepted because tests/artifacts pass, not because an LLM reported "done".
- No public bind of local services before authentication, and no unauthorized cloud spend, destructive changes or unattended deployments.

**North Star test:** the user gives one meaningful task and a bounded grant; ORBIT figures out who should do what, watches the whole mission, and asks only when a real new human decision is needed.
