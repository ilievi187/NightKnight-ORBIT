# Worker API v0.1 (localhost)

Versioned under `/api/workers/v0.1`. Only callable from loopback clients. Tokens are **256-bit**, generated on registration, **hashed at rest**. Pairing codes are single-use, short-lived, and **hashed at rest** (displayed once).

## Onboarding (pairing)
1. Generate a pairing code from the UI (FLEET tab) or `POST /api/lab/pairing` → `{pairing_id, pairing_code, expires_in, expires_at}` (shown once).
2. Run a worker with `--pairing-stdin` and supply the code through stdin, or an owner-only 0600 pairing file. Never pass secrets in process arguments (ps/history). The worker must call `register` with that code.

## Endpoints
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/workers/v0.1/register` | pairing code in body | Registers worker, consumes pairing, returns `{worker_id, worker_token, grant}`. Token shown once. |
| POST | `/api/workers/v0.1/heartbeat` | `worker_token` | Presence update; returns `{assigned_task, ...}` (may advance mission eligibility). |
| POST | `/api/workers/v0.1/poll` | `worker_token` | Poll for assignment. |
| POST | `/api/workers/v0.1/claim` | `worker_token` | Claim an `ASSIGNED` task → becomes `RUNNING`. Scoped by grant/root. |
| POST | `/api/workers/v0.1/report` | `worker_token` | Submit completion; **only artifact metadata** (`path@sha256[:12]`). Server verifies artifacts on disk against criteria. |
| POST | `/api/workers/v0.1/unregister` | `worker_token` | Revoke worker. |

## Security rules
- No command execution from worker traffic.
- Verifier checks **file artifacts on disk** (exists/contains/sha256). Worker `status` self-claims do **not** pass/fail missions.
- Scope grants (including `BLOCKED` scoped denial) never gate mission completion.
- Agent **profiles** (`/api/agents`: ALEX/NOVA/HERMES) are separate from connected **live workers** (`/api/workers`).
- Registry contains only really-registered workers; never fabricated.

## Example (mock worker)
```json
POST /api/workers/v0.1/register
{"pairing_code":"ABCD-EFGH-JKLM","name":"DEMO","kind":"mock","capabilities":["orbit.plan.deliver"],"host":"127.0.0.1","version":"0.1.0"}
→ 201 {"worker_id":"...","worker_token":"...","grant":{"root":"/path/to/workspace/orbit-demo","caps":["orbit.plan.deliver"]}}
```
