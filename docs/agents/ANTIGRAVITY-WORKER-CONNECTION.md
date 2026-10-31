# Google Antigravity — ORBIT Worker Connection Guide (`orbit.workers.v0.1`)

## 1. Unified Protocol & Coordination Summary (PR #14 + PR #15)

This branch reconciles **PR #14** (Antigravity worker adapter & local runtime discovery) with **PR #15** (`orbit.workers.v0.1` loopback broker & Grok worker adapter) onto a single canonical wire protocol (`protocol = "orbit.workers.v0.1"`), mounted on the ORBIT FastAPI dashboard (`orbit/app.py`) via `mount_worker_api(app, broker)`.

### Canonical Routes (`/api/workers`)

| Method | Route | Caller | Auth Required | Purpose |
|---|---|---|---|---|
| `GET` | `/api/workers/protocol` | Public | None | Version & auth capability probe (`executes_payloads: false`). |
| `GET` | `/api/workers/discovery` | Public | None | Local runtime discovery (`discovered` vs `connected` / `authorized_to_execute`). |
| `POST` | `/api/workers/pairing` | **Operator** | `Bearer <operator_token>` or `X-Orbit-Operator-Token` | Mint a single-use pairing code (10m TTL, hashed at rest). |
| `POST` | `/api/workers/pair` | **Worker** | Single-use `pairing_code` | Exchange pairing code + `Manifest` for a worker bearer token. |
| `POST` | `/api/workers/heartbeat` | **Worker** | `Bearer <worker_token>` + `X-Orbit-Nonce` + `X-Orbit-Timestamp` | Refresh worker `LIVE` presence and active lease TTL. |
| `POST` | `/api/workers/poll` | **Worker** | `Bearer <worker_token>` + `X-Orbit-Nonce` + `X-Orbit-Timestamp` | Lease one capability-matched task (`TaskOffer`) and receive cancel IDs. |
| `POST` | `/api/workers/tasks/{id}/progress` | **Worker** | `Bearer <worker_token>` + `X-Orbit-Nonce` + `X-Orbit-Timestamp` | Record `accepted`, `running`, or `cancelling` progress. |
| `POST` | `/api/workers/tasks/{id}/result` | **Worker** | `Bearer <worker_token>` + `X-Orbit-Nonce` + `X-Orbit-Timestamp` | Report `completed`, `rejected`, `cancelled`, or `error` result. |
| `POST` | `/api/workers/tasks` | **Operator** | `Bearer <operator_token>` or `X-Orbit-Operator-Token` | Queue a scoped task (`orbit.ping`, `orbit.inventory`, `orbit.smoke_check`). |
| `POST` | `/api/workers/tasks/{id}/cancel` | **Operator** | `Bearer <operator_token>` or `X-Orbit-Operator-Token` | Cancel a queued task or mark an active lease `cancel_requested`. |
| `POST` | `/api/workers/revoke` | **Operator** | `Bearer <operator_token>` or `X-Orbit-Operator-Token` | Immediately revoke a worker token and release its active leases. |
| `GET` | `/api/workers` | Public | None | List registered workers and live presence (no secrets/hashes). |
| `GET` | `/api/workers/tasks` | Public | None | List tasks and sanitized results. |
| `GET` | `/workers` | Public | None | HTML status view rendered from `broker.snapshot()`. |

---

## 2. Security Architecture

1. **Scoped Operator Authentication**:
   * Privileged routes (`POST /api/workers/pairing`, `/revoke`, `/tasks`, `/tasks/{id}/cancel`) require a scoped operator token (`orb_op_...`), stored only as a SHA-256 hash in SQLite `operator_tokens` and bootstrapped to a `0600` local file (`~/.local/share/nightknight-orbit/worker-broker.operator.token`).
   * Unauthenticated callers (including local `curl` without `Origin`) receive `401`.
   * Worker bearer tokens presented on operator endpoints receive `403`, and operator tokens presented on worker endpoints receive `403`.
   * Requests carrying proxy/forwarding headers (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`) are rejected with `403` on operator endpoints.
2. **Atomic Nonce Replay Protection**:
   * Worker requests require a unique `X-Orbit-Nonce` and `X-Orbit-Timestamp` within 60 seconds.
   * Nonce insertion is atomic against SQLite's `PRIMARY KEY (token_hash, nonce)`; concurrent replay collisions deterministically return `409` (never `500`).
3. **No Secrets in CLI Arguments (`argv`) & `0600` State Files**:
   * `tools/orbit_worker_agent.py` rejects `--pairing` on `argv` to prevent leakage via `ps aux` or shell history.
   * Pairing codes are read only from `--pairing-stdin`, `--pairing-file <0600-path>`, or `ORBIT_PAIRING_CODE_FILE`.
   * Credential state files (`~/.local/share/nightknight-orbit/antigravity-worker.json`) are created with `os.open(..., O_CREAT | O_EXCL | O_WRONLY, 0o600)` before writing any bytes.
4. **Strict Separation of Discovered Runtimes vs. Connected Workers**:
   * `AntigravityWorker` advertises only its verified ORBIT task handlers: `["orbit.inventory", "orbit.ping", "orbit.smoke_check"]`.
   * Observed session tools (`13` tools) and local skill directories (`57` skills) are reported in `inventory` with `observed_session_tools_executable_through_orbit: false` and `skills_executable_through_orbit: false`.
   * Sibling runtimes (`hermes`, `opencode`, `luna`) are reported by `GET /api/workers/discovery` with `connected: false` and `authorized_to_execute: false` until a real worker for that runtime pairs and sends a fresh heartbeat.

---

## 3. Onboarding Commands (No Secrets in `argv`)

### Inspect Local Host & Runtime Discovery
```bash
/home/nightknight/Projects/NightKnight-ORBIT/.venv/bin/python \
  tools/orbit_worker_agent.py --inspect
```

### Run End-to-End Self-Test
```bash
/home/nightknight/Projects/NightKnight-ORBIT/.venv/bin/python \
  tools/orbit_worker_agent.py --self-test
```

### Connect to the Local ORBIT FastAPI Dashboard (`127.0.0.1:8765`)
```bash
# 1. Mint a single-use pairing code using the local 0600 operator token file
OP_TOKEN="$(cat ~/.local/share/nightknight-orbit/worker-broker.operator.token)"
curl -sS -X POST http://127.0.0.1:8765/api/workers/pairing \
  -H "Authorization: Bearer ${OP_TOKEN}" | jq -r '.pairing_code' | \
  /home/nightknight/Projects/NightKnight-ORBIT/.venv/bin/python \
    tools/orbit_worker_agent.py pair \
    --url http://127.0.0.1:8765 \
    --pairing-stdin \
    --worker-id antigravity-NightKnightPC

# 2. Poll and execute one leased task using the 0600 state file
/home/nightknight/Projects/NightKnight-ORBIT/.venv/bin/python \
  tools/orbit_worker_agent.py once \
  --url http://127.0.0.1:8765
```

### Revoke a Worker
```bash
OP_TOKEN="$(cat ~/.local/share/nightknight-orbit/worker-broker.operator.token)"
curl -sS -X POST http://127.0.0.1:8765/api/workers/revoke \
  -H "Authorization: Bearer ${OP_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"worker_id": "antigravity-NightKnightPC"}'
```

---

## 4. Remote Hosts (`ubuntu-hermes` / `detti`) via SSH/Tailscale Loopback Tunnel

ORBIT never binds to `0.0.0.0`. Remote workers connect through an authenticated loopback reverse tunnel:

```bash
ssh -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -R 127.0.0.1:18765:127.0.0.1:8765 \
  nightknight@ubuntu-hermes
```
