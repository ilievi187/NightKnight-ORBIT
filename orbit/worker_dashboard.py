"""Small status page for the standalone worker broker.

The page is generated from the public snapshot. It has no pairing codes,
bearer tokens, or token hashes.
"""
from __future__ import annotations

import html
import json
from typing import Any


def render_dashboard(snapshot: dict[str, Any]) -> str:
    workers = snapshot.get("workers") or []
    tasks = snapshot.get("tasks") or []
    events = snapshot.get("events") or []
    worker_rows = "\n".join(_worker_row(item) for item in workers) or (
        "<tr><td colspan='7'>No paired workers. Presence is not simulated.</td></tr>"
    )
    task_rows = "\n".join(_task_row(item) for item in tasks) or (
        "<tr><td colspan='6'>No tasks.</td></tr>"
    )
    event_rows = "\n".join(_event_row(item) for item in events) or "<li>No events.</li>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <title>ORBIT worker broker</title>
  <link rel="stylesheet" href="/worker.css">
</head>
<body>
  <main>
    <p class="kicker">ORBIT worker broker · {html.escape(str(snapshot.get("protocol", "")))}</p>
    <h1>Local workers</h1>
    <p class="note">LIVE means a heartbeat arrived inside the freshness window. This page is the worker coordinator, not the ORBIT chat UI. Task payloads are not executed here.</p>
    <p class="meta">Server time {html.escape(str(snapshot.get("server_time", "")))}</p>
    <h2>Workers</h2>
    <table>
      <thead><tr><th>Presence</th><th>Status</th><th>Worker</th><th>Runtime</th><th>Host</th><th>Model</th><th>Capabilities</th></tr></thead>
      <tbody>
        {worker_rows}
      </tbody>
    </table>
    <h2>Tasks</h2>
    <table>
      <thead><tr><th>Status</th><th>Type</th><th>Worker</th><th>Shell</th><th>Result</th><th>Updated</th></tr></thead>
      <tbody>
        {task_rows}
      </tbody>
    </table>
    <h2>Events</h2>
    <ul>{event_rows}</ul>
  </main>
</body>
</html>
"""


def _worker_row(item: dict[str, Any]) -> str:
    caps = ", ".join(item.get("capabilities") or [])
    model = item.get("model_id") or "unknown"
    source = item.get("model_id_source") or "not probed"
    return (
        "<tr>"
        f"<td>{html.escape(str(item.get('presence', '')))}</td>"
        f"<td>{html.escape(str(item.get('status', '')))}</td>"
        f"<td>{html.escape(str(item.get('worker_id', '')))}</td>"
        f"<td>{html.escape(str(item.get('runtime_name', '')))}</td>"
        f"<td>{html.escape(str(item.get('host_id', '')))}</td>"
        f"<td>{html.escape(str(model))} <span>({html.escape(str(source))})</span></td>"
        f"<td>{html.escape(caps)}</td>"
        "</tr>"
    )


def _task_row(item: dict[str, Any]) -> str:
    result = item.get("result")
    rendered = html.escape(json.dumps(result, sort_keys=True)) if result is not None else ""
    executed = item.get("executed_shell")
    executed_text = "" if executed is None else str(bool(executed)).lower()
    return (
        "<tr>"
        f"<td>{html.escape(str(item.get('status', '')))}</td>"
        f"<td>{html.escape(str(item.get('type', '')))}</td>"
        f"<td>{html.escape(str(item.get('worker_id') or ''))}</td>"
        f"<td>{html.escape(executed_text)}</td>"
        f"<td>{rendered}</td>"
        f"<td>{html.escape(str(item.get('updated_at', '')))}</td>"
        "</tr>"
    )


def _event_row(item: dict[str, Any]) -> str:
    return (
        "<li>"
        f"{html.escape(str(item.get('ts', '')))} "
        f"{html.escape(str(item.get('kind', '')))} "
        f"{html.escape(str(item.get('worker_id') or ''))} "
        f"{html.escape(str(item.get('task_id') or ''))}"
        "</li>"
    )
