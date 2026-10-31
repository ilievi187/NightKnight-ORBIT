# Connect the Grok worker

The adapter is `python -m orbit.grok_adapter`. It speaks `orbit.workers.v0.1` to a loopback ORBIT worker broker. It does not attach to the chat profiles in the ORBIT UI, and it does not register Hermes, OpenCode, or LUNA.

## What this runtime actually does

Executable task types:

- `orbit.ping` returns the message and `executed_shell: false`
- `orbit.inventory` returns the same inventory the worker advertised

Every other task type, including anything with a command string, is rejected. The process does not start a shell for a dashboard task.

The inventory is split on purpose:

- `executable_capabilities` is only the two handlers above.
- `observed_session_tools` is the Grok Build tool list seen while this adapter was written on 2026-10-31. Those tools are not callable through ORBIT.
- `model_id` is `grok-4.7` because that is the harness session identity. `model_endpoint_probed` and `model_ready` stay false. The adapter does not claim a local model server is up.
- Skill names are directory names found under the resolved skill roots. Skill files are not read.
- The `tasks` MCP server was connected in the authoring session. Its tools were searched, not invoked. `mcp_tools_invoked` stays false.

Hermes and OpenCode are installed on NightKnightPC and were running during the probe. Their CLIs have no ORBIT registration command. Those adapters are pending. LUNA was not given a registration bridge.

## Pair and run

Start the broker on loopback. Port 8765 on this machine is already taken by an unrelated review server bound to a Tailscale address. Do not retarget that process. The worker broker defaults to `127.0.0.1:8766`.

```bash
python -m orbit.worker_broker --host 127.0.0.1 --port 8766
```

In another shell, from the repository root:

```bash
umask 077
curl -sS -X POST http://127.0.0.1:8766/api/workers/pairing -o "$HOME/.local/share/nightknight-orbit/pairing.json"
export ORBIT_URL=http://127.0.0.1:8766
export ORBIT_PAIRING_CODE_FILE="$HOME/.local/share/nightknight-orbit/pairing.json"
python - <<'PY'
import json, os
from pathlib import Path
body = json.loads(Path(os.environ["ORBIT_PAIRING_CODE_FILE"]).read_text())
Path(os.environ["ORBIT_PAIRING_CODE_FILE"]).write_text(body["pairing_code"])
PY
python -m orbit.grok_adapter pair
rm -f "$ORBIT_PAIRING_CODE_FILE"
python -m orbit.grok_adapter once
```

The pairing code is read from the environment or from a file. It is not accepted as a process argument. `pair` writes `~/.local/share/nightknight-orbit/grok-worker.json` with mode `0600` and prints the worker id only. `once` heartbeats, takes at most one lease, reports progress, and posts the result.

`python -m orbit.grok_adapter inventory` prints the manifest facts without contacting the broker. `run` repeats `once` until the process is stopped. Stop it with Ctrl-C. It does not kill other agents or model servers.

Give the worker a harmless task:

```bash
curl -sS -X POST http://127.0.0.1:8766/api/workers/tasks \
  -H 'Content-Type: application/json' \
  -d '{"type":"orbit.ping","payload":{"message":"hello from orbit"},"grant":"local-test"}'
python -m orbit.grok_adapter once
```

Open `http://127.0.0.1:8766/` and confirm the worker presence is `LIVE` only after a heartbeat, the model source is the harness identity, and the ping result is visible. `OFFLINE` after a missed heartbeat is a real timeout, not a placeholder.

## Revoke

```bash
curl -sS -X POST http://127.0.0.1:8766/api/workers/revoke \
  -H 'Content-Type: application/json' \
  -d '{"worker_id":"grok-NightKnightPC"}'
rm -f "$HOME/.local/share/nightknight-orbit/grok-worker.json"
```

The old token then fails with 401. Pair again with a new code when the worker should return. Revocation does not stop the broker and does not cancel unrelated tasks.

## Tests

```bash
python -m unittest discover -s tests -v
```

The stock ORBIT app still returns 404 for `/api/workers/protocol`. That is the live-dashboard gap.
