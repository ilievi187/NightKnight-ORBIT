# ORBIT Desktop — User Test Checklist

Run this against a downloaded AppImage or portable build.

## Setup
- [ ] Download artifact from GitHub Actions (workflow `ORBIT Desktop ...`, artifact `NightKnight-ORBIT-Desktop-Linux`)
- [ ] Verify checksum against `SHA256SUMS`
- [ ] Launch app (AppImage: `chmod +x && ./...AppImage` / Portable: `./orbit-desktop`)
- [ ] Dashboard loads at `http://127.0.0.1:8765` (only loopback)

## Plan Mode
- [ ] **MISSIONS** tab visible with pipeline (GOAL→DISCOVER→MATCH→SCAFFOLD→ASSIGN→RUN→VERIFY→GAP→REPLAN→COMPLETE)
- [ ] **LAUNCH DEMO MISSION** creates a mission with persistent task graph
- [ ] Tasks show statuses (PENDING/ASSIGNED/RUNNING/SUBMITTED/VERIFIED/FAILED/BLOCKED)

## Worker Pairing & Fleet
- [ ] **FLEET** tab: **GENERATE PAIRING** shows a **one-shot** code with expiry countdown
- [ ] Pairing stored hashed (not visible in logs/journal). Journal shows event without code/hash.
- [ ] Run real mock worker: `orbit-mock-worker --api http://127.0.0.1:8765 --pairing <CODE> ...`
- [ ] **FLEET** lists worker as **LIVE/IDLE** (only real registered worker; profiles ALEX/NOVA/HERMES remain separate)
- [ ] Heartbeats update; no fabricated metrics

## Verified Execution
- [ ] Worker claims tasks and writes artifacts into the mission's grant workspace
- [ ] Supervisor **verifies files on disk** (criteria show evidence, satisfied=true/false)
- [ ] Demo mission exercises **flaky retry**: first attempt fails verification → **GAP → REPLAN** → iteration 2 completes
- [ ] A task with scoped grant denial remains **BLOCKED** but does **not** gate mission completion
- [ ] Mission reaches **COMPLETE** with all required criteria satisfied

## Persistence & Safety
- [ ] Close and reopen app — mission/task state persists (SQLite)
- [ ] No secrets in `/api/health` or fleet listing
- [ ] Worker API is versioned (`/api/workers/v0.1`); unauthorized token → 403
- [ ] Port busy shows clear error (`ORBIT_PORT_BUSY:`) if port already taken

## Pass/Fail
- Tester: _______
- Result: ☐ PASS  ☐ FAIL
- Notes: _____________________________
- Screenshots attached: ☐ yes
