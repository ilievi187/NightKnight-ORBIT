# Remote workers without exposing ORBIT

The broker and the Grok adapter both refuse a non-loopback bind. `0.0.0.0`, a LAN address, and a Tailscale address are rejected before a socket is opened. HTTPS URLs, credentials embedded in the URL, and a non-root path are rejected by the adapter.

For a worker on another host, forward the loopback port over an SSH session that is already authenticated. Tailscale SSH is the preferred path because it does not open a public listener.

From the remote host:

```bash
ssh -N -L 127.0.0.1:8766:127.0.0.1:8766 nightknight@nightknightpc
```

Then on that same remote host:

```bash
export ORBIT_URL=http://127.0.0.1:8766
python -m orbit.grok_adapter pair
```

The remote process talks to its own loopback. SSH carries the bytes to NightKnightPC's loopback. The broker never listens on the Tailscale or LAN address.

Do not:

- change the broker host to `0.0.0.0` or a Tailscale IP
- add a firewall allow rule for port 8766
- put the bearer token in the SSH command line, a git remote, or a log
- point the adapter at the process currently bound to `100.115.247.63:8765`

That process is `review_server.py` for the local review tool. It is not ORBIT. It was left running.

This document is a proposal for the later multi-host grant. The adapter does not open the tunnel by itself.
