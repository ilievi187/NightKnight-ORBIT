# ORBIT Worker Connection — Antigravity & Multi-Runtime Bridge (Issue #13 / #12)

This document specifies the **ORBIT Worker Protocol v0.1** adapter implementation (`orbit/worker_bridge.py` and `tools/orbit_worker_agent.py`) that connects **Google Antigravity (AGY)** and sibling local runtimes (**Hermes**, **OpenCode / ALEX**, and **LUNA**) to the local NightKnight ORBIT coordinator.

---

## 1. Inspected Local Runtime & Host Inventory (`NightKnightPC`)

All values below are dynamically probed from the live host by `inspect_antigravity_runtime()`, `inspect_hermes_runtime()`, `inspect_opencode_runtime()`, `inspect_luna_runtime()`, and `inspect_host_resources()`. No capabilities or live statuses are fabricated.

### Host Resources (`NightKnightPC`)
- **OS / Kernel**: `Linux NightKnightPC 7.2.8-9-omarchy x86_64` (Arch / Omarchy + Hyprland)
- **CPU**: `AMD Ryzen 7 7800X3D 8-Core Processor` (8 cores / 16 threads)
- **System RAM**: `31,153 MiB` total (~30.4 GiB)
- **GPUs (via `/sys/class/drm` & ROCm)**:
  - `card1` (`gfx1100`): **AMD Radeon RX 7900 XT** — `20,464 MiB` VRAM (`~1,386 MiB` used)
  - `card0` (`gfx1036`): **AMD Radeon 610M** (iGPU) — `512 MiB` VRAM (`~16 MiB` used)
- **Tailscale Mesh (`tailscale0`)**:
  - `NightKnightPC`: `100.115.247.63` (local host)
  - `ilievi-b450m-steel-legend` (`ubuntu-hermes` / `nightknight-server`): `100.98.90.67` (Linux, active)
  - `detti`: `100.95.239.126` (Windows, idle)

### Primary Worker Runtime: Google Antigravity (`antigravity`)
- **Worker ID**: `antigravity-nightknightpc`
- **Worker Harness (`runtime_name`)**: `antigravity` (`/home/nightknight/.local/opt/Antigravity-x64/antigravity`, version `2.0-agy`)
- **Active Brain / Model (`model_id`)**: `Gemini 4 Argon (Medium)` (explicitly separated from the worker harness)
- **Declared Capabilities**:
  - `orbit.plan.deliver` (Plan Mode artifact delivery & verification)
  - `workspace.inspect` (read-only directory enumeration within `MissionGrant`)
  - `workspace.read` (read-only UTF-8 file inspection + SHA-256 hashing within `MissionGrant`)
  - `workspace.write_scoped` (atomic file creation/update confined to `MissionGrant.root`)
  - `unittest.run`
  - `reverse_engineering.arsenal` (49-skill NightKnight Reverse Engineering Arsenal installed locally)
- **Tool Allowlist (13 tools)**:
  `run_command`, `view_file`, `write_to_file`, `replace_file_content`, `search_web`, `read_url_content`, `invoke_subagent`, `define_subagent`, `manage_subagents`, `send_message`, `schedule`, `manage_task`, `ask_question`
- **Verified Local Skills (57 `SKILL.md` packages)**:
  - 49 `reverse-*` skills in `/home/nightknight/.gemini/config/skills/`
  - 8 builtin/config skills (`agy-customizations`, `antigravity_guide`, `automation`, `generative_ui`, `migrate-workflows`, `permissioned-github`, `plugin`, `ui-plugin-navigation`)

### Sibling Runtimes Probed on Host
| Runtime (`runtime_name`) | Harness vs Model | Probed Endpoint / Binary | Observed Status on `NightKnightPC` |
| :--- | :--- | :--- | :--- |
| **`antigravity`** | Worker harness (`model_id`: `Gemini 4 Argon (Medium)`) | `/home/nightknight/.local/opt/Antigravity-x64/antigravity` | `IDLE` / `LIVE` (57 skills) |
| **`hermes`** | Worker harness (`model_id`: `None` — profile-dependent) | `http://127.0.0.1:8642/health` (`v0.22.5`) + `~/.local/bin/hermes` | `IDLE` (gateway live on `:8642`) |
| **`opencode`** | Worker harness (`model_id`: `None` — never mislabelled as LLM) | `http://127.0.0.1:49374/doc` (`v2.2.1`) + `~/.local/bin/opencode` | `IDLE` (service live on `:49374`) |
| **`luna`** | Remote/profile worker harness (`ubuntu-hermes`) | SSH/Tailscale tunnel (none active by default) | `OFFLINE` (honest until tunnel is started) |

---

## 2. ORBIT Worker Protocol v0.1 & Issue #12 Compatibility

The adapter in [`orbit/worker_bridge.py`](../../orbit/worker_bridge.py) implements the full contract from Issue #12 and Issue #13:

- **Data Contracts**:
  - `WorkerManifest`: `worker_id`, `runtime_name` (`kind`), `host_id` (`host`), `model_id`, `capabilities`, `tool_allowlist`, `skills`, `status`, `version`, `heartbeat_at`, `transport`, `resources`.
  - `TaskOffer`: `task_id`, `mission_id`, `title`, `capability_required`, `spec`, `grant`, `attempt`, `created_at`.
  - `Lease`: `lease_id`, `task_id`, `mission_id`, `worker_id`, `acquired_at`, `expires_at`, `status`.
  - `TaskProgress`: `task_id`, `lease_id`, `worker_id`, `stage`, `percent`, `message`, `timestamp`.
  - `TaskResult`: `task_id`, `lease_id`, `worker_id`, `status`, `summary`, `artifacts`, `logs`, `completed_at`.
  - `Cancel`: `task_id`, `lease_id`, `reason`, `cancelled_at`.
- **Authenticated Pairing & Anti-Replay**:
  - Coordinator issues a short-lived single-use `pairing_code` (`POST /api/workers/v0.1/pairings` or Issue #12's `POST /api/lab/pairing`). Only `sha256(pairing_code)` is stored in SQLite.
  - Worker calls `POST /api/workers/v0.1/register` with `pairing_code`, unique `nonce`, and `WorkerManifest`.
  - Coordinator verifies the code is valid, unexpired, unrevoked, and unused, verifies the nonce has not been replayed, burns the pairing code, and issues a 256-bit `worker_token` (persisted only as `sha256(worker_token)`).
  - All tokens and pairing codes are masked via `mask_secret()` in logs, audit events, and CLI output.
- **Wire-Level Compatibility with Issue #12 (`orbit/mission_api.py` & `orbit/fleet.py`)**:
  - Sends `worker_token` in both the `Authorization: Bearer <token>` header **and** the JSON request body (`WorkerAuth`).
  - Maps `kind` to `"custom"` when registering `"antigravity"` or `"luna"` against `Fleet.register()` (`ALLOWED_KINDS = ("mock", "hermes", "opencode", "custom")`) while preserving `"runtime_name": "antigravity"` in the manifest.
  - Supports both `offers` list and Issue #12's `assigned_task` object on `/poll` and `/heartbeat`.
  - Automatically looks up mission `grant` (`root` and `denied_paths`) from `GET /api/missions/{mission_id}` when `/poll` omits `grant`.
  - Formats `/report` `artifacts` as `list[str]` (relative paths) alongside `artifact_details` (`sha256`, `size_bytes`) so Issue #12's `TaskReport` model and server-side `Verifier` validate every delivered file on disk.

---

## 3. Quick-Connect Onboarding Commands

### Inspect Real Local Capabilities (No Network Mutation)
```bash
python tools/orbit_worker_agent.py --inspect
```

### Run Standalone End-to-End Verification (Mock/Embedded Coordinator)
```bash
python tools/orbit_worker_agent.py --self-test
```

### Connect to a Running Local ORBIT Dashboard (`127.0.0.1:8765`)
1. Generate a one-time pairing code from the ORBIT UI (**Quick Connect**) or via loopback CLI:
   ```bash
   curl -s -X POST http://127.0.0.1:8765/api/lab/pairing
   ```
2. Pair and run the worker adapter for your chosen runtime:
   ```bash
   # Connect Google Antigravity (AGY) worker
   python tools/orbit_worker_agent.py --api http://127.0.0.1:8765 --pairing <PAIRING_CODE> --runtime antigravity

   # Connect local Hermes worker
   python tools/orbit_worker_agent.py --api http://127.0.0.1:8765 --pairing <PAIRING_CODE> --runtime hermes

   # Connect local OpenCode (ALEX) worker
   python tools/orbit_worker_agent.py --api http://127.0.0.1:8765 --pairing <PAIRING_CODE> --runtime opencode
   ```
   *(Add `--once` to execute a single assigned task and exit deterministically.)*

### Revoke a Worker Pairing or Active Token
```bash
# Self-revoke from the worker using its active token:
curl -s -X POST http://127.0.0.1:8765/api/workers/v0.1/unregister \
  -H "Content-Type: application/json" \
  -d '{"worker_token": "<WORKER_TOKEN>"}'
```

---

## 4. Authenticated SSH / Tailscale Forwarding for Remote Hosts (`ubuntu-hermes` / `detti`)

ORBIT **never** binds to `0.0.0.0` or exposes unauthenticated HTTP on LAN/Tailscale. To connect a remote worker (such as **LUNA** on `ubuntu-hermes` / `100.98.90.67` or `detti` / `100.95.239.126`):

1. Print the hardened tunnel specification:
   ```bash
   python tools/orbit_worker_agent.py --print-tunnel-guide
   ```
2. When explicitly authorized by the operator, forward the remote loopback port `127.0.0.1:18765` to `NightKnightPC`'s loopback `127.0.0.1:8765` over SSH + Tailscale:
   ```bash
   ssh -NT -o BatchMode=yes -o ExitOnForwardFailure=yes \
     -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
     -R 127.0.0.1:18765:127.0.0.1:8765 100.98.90.67
   ```
3. On the remote host (`ubuntu-hermes`), run the worker adapter against its local tunnel endpoint `http://127.0.0.1:18765` using a short-lived pairing code:
   ```bash
   python tools/orbit_worker_agent.py --api http://127.0.0.1:18765 --pairing <PAIRING_CODE> --runtime luna --once
   ```

---

## 5. Tested vs Pending Adapters

| Adapter / Component | Status | Verification Evidence |
| :--- | :--- | :--- |
| **Antigravity (`antigravity`) Local Loopback Adapter** | **TESTED & VERIFIED** | Verified via `tests/test_worker_bridge.py`, `tools/orbit_worker_agent.py --self-test`, and live end-to-end execution against Issue #12's `create_app()` (`mission_stage: COMPLETE`, `status: VERIFIED`). |
| **Hermes (`hermes`) Local Loopback Probe & Adapter** | **TESTED & VERIFIED** | Probed live `http://127.0.0.1:8642/health` (`v0.22.5`) on `NightKnightPC`; pairing, lease, and scoped delivery verified in `tests/test_worker_bridge.py`. |
| **OpenCode (`opencode`) Local Loopback Probe & Adapter** | **TESTED & VERIFIED** | Probed live `http://127.0.0.1:49374/doc` (`v2.2.1`) on `NightKnightPC`; harness vs model separation and duplicate-lease prevention verified in `tests/test_worker_bridge.py`. |
| **LUNA (`luna`) Remote SSH/Tailscale Adapter** | **PENDING LIVE TUNNEL GRANT** | Offline state detection (`OFFLINE`, never fake `READY`) and SSH loopback tunnel spec (`127.0.0.1:18765 -> 127.0.0.1:8765`) verified in unit tests; live remote execution awaits operator opt-in tunnel startup. |
