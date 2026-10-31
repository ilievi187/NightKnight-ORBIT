# ORBIT worker protocol v0.1

Protocol id: `orbit.workers.v0.1`

This is the reference contract for issue #12 worker enrollment and issue #13 live adapters. The implementation in this branch is a loopback broker plus a Grok adapter. It is not mounted in the stock `create_app` yet, because `orbit/app.py` is owned by the open runtime PR.

The broker stores pairing codes and bearer tokens as SHA-256 hashes. It leases task records. It does not execute payloads, start shells, or call a model.

## Authentication

Operator routes (create a pairing code, enqueue, cancel, revoke, list) are authorized only by binding to `127.0.0.1` and by the same cross-origin POST denial as the rest of ORBIT. Do not publish them.

Worker routes require:

- `Authorization: Bearer <token>`
- `X-Orbit-Nonce`: unique per request, 8–128 URL-safe characters
- `X-Orbit-Timestamp`: unix seconds within 60 seconds of the broker clock

Reusing a nonce returns 409. A revoked, unknown, or expired token returns 401. The raw token is returned once from `POST /api/workers/pair` and is never included in later responses, the status page, or event details.

Pairing codes are single-use, expire in 10 minutes, and are rejected after 8 failures in a minute. A protocol mismatch does not consume the code.

## Routes

| Method | Path | Caller | Purpose |
|---|---|---|---|
| GET | `/api/workers/protocol` | either | Version probe. `executes_payloads` is false. |
| POST | `/api/workers/pairing` | operator | Issue a one-time pairing code. |
| POST | `/api/workers/pair` | worker | Exchange the code and a manifest for a bearer token. |
| POST | `/api/workers/heartbeat` | worker | Refresh presence, capabilities, and lease expiry. |
| POST | `/api/workers/poll` | worker | Return the current lease or a new one, plus cancel ids. |
| POST | `/api/workers/tasks` | operator | Queue a task. Required capabilities default to `[type]`. |
| POST | `/api/workers/tasks/{id}/progress` | worker | Record an accepted, running, or cancelling event. |
| POST | `/api/workers/tasks/{id}/result` | worker | Complete, reject, cancel, or fail the leased task. |
| POST | `/api/workers/tasks/{id}/cancel` | operator | Cancel a queued task, or mark a lease cancel-requested. |
| POST | `/api/workers/revoke` | operator | Revoke a worker token and release its leases. |
| GET | `/api/workers` | operator | Public worker rows. No secrets. |
| GET | `/api/workers/{worker_id}` | operator | Public row plus the inventory JSON. |
| GET | `/api/workers/tasks` | operator | Task rows and results. |
| GET | `/api/workers/tasks/{id}` | operator | One task, including the stored payload. |

## Objects

`WorkerManifest`: `worker_id`, `runtime_name`, `host_id`, `model_id`, `model_id_source`, `capabilities`, `status`, `version`, `inventory`.

Status values the worker may report: `IDLE`, `BUSY`, `ERROR`, `BLOCKED`, `OFFLINE`.

`presence` is computed by the broker. `LIVE` means a non-revoked worker whose last heartbeat is inside the freshness window (20 seconds by default) and whose reported status is not `OFFLINE`. A freshly paired worker stays `OFFLINE` until the first heartbeat. The broker does not invent `LIVE`.

`TaskOffer` is inside a lease: `type`, `payload`, `required_capabilities`, `grant`.

`Lease`: `lease_id`, `task_id`, `expires_at`, `cancel_requested`, `offer`.

`TaskProgress`: `lease_id`, `event_id`, `status`, `message`, `progress`.

`TaskResult`: `lease_id`, `status` (`completed`, `rejected`, `cancelled`, `error`), `result`, `log_excerpt`.

## Scheduling rules

- A second poll while a lease is active returns the same lease. Another worker does not receive it.
- A task is offered only when every required capability is in the worker manifest. A denial is recorded once for that worker and task. Other tasks continue.
- A heartbeat older than the freshness window, or a passed lease expiry, returns a normal lease to the queue. A cancel that was in progress becomes `cancelled`.
- After restart, token hashes, nonces, and tasks remain in SQLite. The raw token is not in the database.
- A successful result after `cancel_requested` is rejected with 409. The worker must send `cancelled`.
- Revoking a worker returns its open lease to the queue and makes the old token fail closed.

## What the dashboard still has to do

Mount `mount_worker_api(app, broker)` inside `create_app` when `orbit/app.py` is free. Keep `TrustedHostMiddleware` and the loopback bind. Add a fleet panel that reads `GET /api/workers` and `GET /api/workers/tasks`. Show the pairing code only in the operator response that created it.

Until that mount lands, run the standalone broker:

```bash
python -m orbit.worker_broker --host 127.0.0.1 --port 8766
```

The status page is `http://127.0.0.1:8766/`. It is the worker coordinator, not the ORBIT chat UI.
