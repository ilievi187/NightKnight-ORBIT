# PyInstaller spec for ORBIT server (sidecar) — onedir for fastest cold start
# Bundles web assets into a discoverable location for the frozen build.
# Build (local): pyinstaller packaging/orbit-server.spec --distpath dist

import os
import sys
from pathlib import Path

block_cipher = None
SPEC_DIR = Path(SPECPATH)
ROOT = SPEC_DIR.parent

# Web assets must be present at runtime for the bundled UI
datas = []
web_dir = ROOT / 'web'
if web_dir.is_dir():
    datas.append((str(web_dir), 'web'))
docs_dir = ROOT / 'docs'
if docs_dir.is_dir():
    datas.append((str(docs_dir), 'docs'))

hiddenimports = [
    'orbit',
    'orbit.app',
    'orbit.config',
    'orbit.harness',
    'orbit.storage',
    'orbit.fleet',
    'orbit.planmode',
    'orbit.mission_api',
    'orbit.server',
    'uvicorn.logging',
    'uvicorn.loops.auto',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan.on',
    'httptools',
    'uvloop',
    'watchfiles',
    'websockets',
    'h11',
]

a = Analysis(
    [str(ROOT / 'desktop' / 'orbit_server_entry.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'IPython'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='orbit-server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='orbit-server',
)
