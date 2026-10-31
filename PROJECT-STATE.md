# PROJECT STATE — ALEX / ORBIT Desktop MVP (issue #12)

Last checkpoint: 2026-10-31 (session start)

## Mission
Build downloadable ORBIT Desktop app (issue #12). Architecture per #9 + PR #10; supervisor loop per #11.
Definition of DONE: user downloads artifact from GitHub Actions, runs dashboard on NightKnightPC,
registers a real mock worker via secure local pairing, runs one demo Plan Mode mission through >=2
verify/replan iterations, closes/reopens app and sees saved task state. No fake agents/metrics.

## Durable pointers
- Repo: https://github.com/ilievi187/NightKnight-ORBIT
- Issue #12 = mission brief. Issue #9 = north star. Issue #11 = supervisor loop contract.
- PR #7 (heretic, OPEN): M3 runtime; touches orbit/{app,config}.py + runtime.py + web/* additions.
  -> Keep my changes orthogonal: my app.py diff = additive only (imports + router mount).
  -> Preserve web UI parity with M3 runtime panel in rebuilt UI.
- PR #6 (luna, OPEN): skills vault, own files. PR #10 docs North Star, no conflict.
- Worktree: /home/nightknight/Projects/NightKnight-ORBIT-alex, branch agent/alex-orbit-desktop-v01, base origin/main 1e10c46.
- Base tests green: 6/6 (venv py3.15; CI parity 3.11/3.13).
- Group chat coordination channel: NOT FOUND locally (no Nova dir) -> use excluded-file note + comment on issue.
- The scenario mission content from scenario.md: "4 acceptance criteria, 2 failing -> replan -> COMPLETE" + blocked permission case.
- VD: WhatsApp Screenshot at /home/nightknight/Pictures (per user instruction).
- gitignored project-local .state/PROJECT-BRIEF.md lives in NightKnight-ORBIT-alex repo.

## File ownership (my branch)
NEW: orbit/planmode.py orbit/fleet.py orbit/mission_api.py orbit/desktop_boot.py tools/mock-worker.py
     tests/test_planmode.py tests/test_fleet.py tests/test_mission_e2e.py
     src-tauri/** desktop/** .github/workflows/desktop.yml docs/DESKTOP.md docs/WORKER-API.md
     docs/USER-TEST-CHECKLIST.md
MODIFY(minimal): orbit/app.py (additive only) web/index.html web/style.css web/app.js (rebuild keeping all v0.2 features + M3 parity)
README.md (desktop install section). NOT touching: orbit/runtime.py orbit/config.py M3 bits tests/test_runtime.py skills files MISSIONS.md ROADMAP.md
STATE: project-local docs/WORKLOG-ALEX-DESKTOP.md

## Architecture decisions (ref导读手记 complete in docs/DESKTOP.md)
- Plan mode stages: GOAL -> DISCOVER -> MATCH -> SCAFFOLD -> ASSIGN -> RUN -> VERIFY -> GAP -> REPLAN -> COMPLETE/BLOCKED (max_iterations guard)
- Persistence: additive SQLite tables missions/mission_tasks/mission_events/workers/worker_pairings in existing orbit.sqlite3
- Worker API v0.1 (LOOPBACK ONLY): /api/workers/v0.1/register|poll|claim|heartbeat|report
- Pairing: short-lived single-use pairing credential displayed in UI, hashed at rest, TTL, rate-limited
- No worker command execution: verifier is server-side; it checks FILE ARTIFACTS (sha256/contains) under grant root
- Mock worker: real HTTP client process (tools/mock-worker.py), executable run via server-local spawn of bundled binary ONLY (trusted code, no user-supplied commands)
- Desktop: Tauri v2 + PyInstaller onedir sidecar backend (rust 1.99 local, webkit2gtk-4.1 present)
- Fallback portable tar.gz: server + launcher script, browser UI (honest limitation)

## Progress
- [x] Read issue #12, #9, #11, PRs #6/#7/#10, code, CI, AGENTS.md
- [x] Base test run green in isolated worktree (6/6)
- [x] Checked local toolchain (Tauri feasible, pyinstaller available)
- [x] Backend kernel + fleet + worker API wired additive into app.py
- [x] tools/mock-worker.py — real stdlib-only http worker subprocess
- [x] E2E validated locally: server 127.0.0.1:18077 + real worker subprocess
      -> flaky fail -> GAP -> REPLAN retry -> COMPLETE (iteration 2), scope-denied
      task BLOCKED without gating completion. Grant-root bug fixed (demo_root
      nesting), premature-VERIFY removed (quiescence gate), flaky knob
      tied to engine task attempt 1.
- [ ] Formal unit tests (test_planmode, test_fleet, test_mission_api)
- [ ] Web UI rebuild (sci-fi dashboard, missions/fleet/system/stream tabs)
- [ ] Tauri desktop shell + local build
- [ ] PyInstaller onedir sidecar bundles (server + mock worker)
- [ ] CI desktop.yml with artifacts (AppImage + portable tar.gz + checksums)
- [ ] Docs + README + user test checklist
- [ ] Local E2E UI screenshots
- [ ] PR opened, CI green, artifact link posted (NO merge/release w/o approval)

## Blockers / notes
- CI main currently on fastlane (flat config), ensure my desktop.yml has same codeql style concerns
- release/publication ONLY AFTER user confirmation
