# A real Tauri sidecar must carry the Python runtime AND its data in one executable.
# The original onedir spec remains for the portable .tar.gz fallback.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
datas = []
if (ROOT / "web").is_dir():
    datas.append((str(ROOT / "web"), "web"))

hiddenimports = collect_submodules('backports') + [
    'backports', 'backports.tarfile',
    "orbit.app", "orbit.config", "orbit.harness", "orbit.storage",
    "orbit.fleet", "orbit.planmode", "orbit.mission_api", "orbit.brain_control", "orbit.provider_sources",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "httptools", "uvloop", "websockets", "h11",
]
a = Analysis(
    [str(ROOT / "desktop" / "orbit_server_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="orbit-server", console=True, strip=False, upx=False,
)
