# Desktop Installation & Usage (Issue #12)

NightKnight ORBIT targets an **experimental local desktop application** for Arch/Omarchy Linux. Treat this as a test build until an AppImage is produced and successfully launched on NightKnightPC:
- **Tauri 2 AppImage** (preferred) — desktop shell launching the local FastAPI backend as a sidecar
- **Portable tar.gz** (fallback) — self-contained directory with the server + mock worker

## Download

Artifacts are built by GitHub Actions as **workflow artifacts** (not a public release) on the `agent/alex-orbit-desktop-v01` branch / PR.

After CI completes, download from:
**Actions → ORBIT Desktop (Linux AppImage + Portable) → Artifacts → NightKnight-ORBIT-Desktop-Linux**

Each artifact includes checksums in `SHA256SUMS`.

## Install (AppImage)

```bash
chmod +x NightKnight-ORBIT*.AppImage
./NightKnight-ORBIT*.AppImage
```

On some Wayland/Omarchy setups you may need FUSE:
```bash
# Arch/Omarchy
sudo pacman -S fuse2
```

## Install (Portable)

```bash
tar -xzf NightKnight-ORBIT-portable-linux-x86_64.tar.gz
cd orbit-desktop-portable
./orbit-desktop
```

The app opens the dashboard at `http://127.0.0.1:8765`. The backend **only binds to 127.0.0.1**.

## Plan Mode & Worker Pairing

1. Open the **MISSIONS** tab → **LAUNCH DEMO MISSION** (real supervisor, persistent SQLite state)
2. Open the **FLEET** tab → **GENERATE PAIRING** (single-use, short-lived, hashed at rest)
3. Run the real mock worker (supply its one-time pairing code on stdin; never in command arguments):
```bash
# In portable bundle:
./bin/orbit-mock-worker --api http://127.0.0.1:8765 --pairing-stdin --workdir ./orbit-demo --flaky-retry 1

# Or from source in this repo:
.venv/bin/python tools/mock-worker.py --api http://127.0.0.1:8765 --pairing-stdin --workdir ./orbit-demo --flaky-retry 1
```

The supervisor advances on registration/heartbeat. Verification checks **actual files on disk** (worker self-claims never pass/fail). A blocked task with a scoped grant denial does **not** gate completion.

## Uninstall

- **AppImage:** delete the `.AppImage` file.
- **Portable:** delete the extracted directory.
- **Data:** defaults in `~/.local/share/orbit/` or `$ORBIT_DATA_DIR`; workspace in `$ORBIT_WORKSPACE`. Remove those directories to clear state.

## Security

- Loopback only **is not equivalent to local operator authentication**. Do not enable forwarded/remote access until a scoped operator credential is enforced. The worker broker from PRs #14/#15 is not integrated in this desktop branch yet.
- Pairing codes are hashed at rest; shown once.
- Worker tokens are 256-bit, hashed at rest, never logged/exposed.
- Writing requires human approval in the chat flow (separate from worker artifact submission).
- CSP enforced by the server/UI.
