# ORBIT Grok worker worklog

## 2026-10-31 — protocol gap and adapter

- Issue #12 describes worker enrollment. Issue #13 asks for real runtime bridges. `origin/main` has no `/api/workers` routes. Open PRs #6, #7, and #10 already claim the app, web UI, README, and skill-vault files, so this branch does not edit them.
- Nothing is listening on `127.0.0.1:8765`. `100.115.247.63:8765` is `review_server.py` and was not modified.
- Hermes gateway and OpenCode serve were running. Neither CLI help lists an ORBIT registration command. No adapter was wrapped around them. LUNA was not bridged.
- Added a loopback reference broker for `orbit.workers.v0.1` and a Grok adapter whose only handlers are `orbit.ping` and `orbit.inventory`.
- Local unittest discovery: 24 tests, OK, on Python 3.15.2. Disabling the nonce replay check made the replay tests fail with `sqlite3.IntegrityError` instead of HTTP 409. The check was restored.
- Live dashboard connection is blocked until `mount_worker_api` is called from `create_app` and an ORBIT process is actually running on loopback.
- 2026-10-31 live loopback proof against `python -m orbit.worker_broker` on `127.0.0.1:8766`: worker `grok-NightKnightPC` reached presence `LIVE`, completed `orbit.ping`, and the status page showed `hello from orbit`. `model_ready` was false, `mcp_invoked` was false, skill count was 258 with the published name list truncated, and the bearer token was not in the page or worker JSON. The broker was then stopped. The review server on `100.115.247.63:8765` was still pid 5670.
