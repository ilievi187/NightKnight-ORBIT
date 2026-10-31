#!/usr/bin/env python3
"""Install the six original COSMOS PNG assets without adding large binaries to git.

Usage:
  python scripts/install_cosmos_assets.py
  python scripts/install_cosmos_assets.py ~/Downloads
  python scripts/install_cosmos_assets.py ~/Downloads/NightKnight-COSMOS-ALEX-NOVA-assets.zip

This copies only exact expected filenames and verifies PNG dimensions + SHA-256.
"""
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "web" / "cosmos"
ARCHIVE = "NightKnight-COSMOS-ALEX-NOVA-assets.zip"
ASSETS = {
    "cosmos_alex_background.png": ((3840, 2160), "9f38606ea87fef4c3aa247d4f786b6898d836a97d775df89a12e0efc0a8c20aa"),
    "cosmos_nova_background.png": ((3840, 2160), "14f3634a49979240362d73401a208a61894a343fb1214325fc55f97a36ec805f"),
    "planet_violet_texture.png": ((4096, 2048), "6468ec9166e571c5ef5d62798d4a4fbfe0513833d7bb42474ccde4c942771ea5"),
    "planet_blue_texture.png": ((4096, 2048), "dbce2c6fe014a7d0281e95cf4f7377e4e772181f745323eb57b1ba169119d00d"),
    "planet_amber_texture.png": ((4096, 2048), "fa850a4416e6c8159fc6ba8e90d5da6955fc0f91ac16e5206966d17f39b869ea"),
    "planet_moon_texture.png": ((4096, 2048), "e9f57fd15cf6f63d6b8300576d9c72d4e527578841239bfc99bc0c49379302f3"),
}
MAX_FILE_SIZE = 12 * 1024 * 1024


def resolve_source(arg=None):
    if arg:
        source = Path(arg).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(source)
        return source
    for folder in (Path.home() / "Downloads", Path.home() / "Изтеглени"):
        archive = folder / ARCHIVE
        if archive.is_file():
            return archive
        if all((folder / name).is_file() for name in ASSETS):
            return folder
    raise FileNotFoundError("No COSMOS ZIP or six PNG files in Downloads. Pass the download path explicitly.")


def validate_file(path, expected_size, expected_digest):
    data_size = path.stat().st_size
    if data_size > MAX_FILE_SIZE or data_size < 24:
        raise ValueError(f"{path.name}: unexpected file size {data_size}")
    with path.open("rb") as handle:
        header = handle.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"{path.name}: not a valid PNG header")
    dimensions = struct.unpack(">II", header[16:24])
    if dimensions != expected_size:
        raise ValueError(f"{path.name}: expected {expected_size}, got {dimensions}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected_digest:
        raise ValueError(f"{path.name}: SHA-256 mismatch; the file is not the expected asset")


def install(source):
    TARGET.mkdir(parents=True, exist_ok=True)
    archive = zipfile.ZipFile(source) if source.is_file() else None
    try:
        with tempfile.TemporaryDirectory(prefix=".cosmos-", dir=TARGET) as workspace:
            staging = Path(workspace)
            for name, (dimensions, expected_hash) in ASSETS.items():
                target = staging / name
                if archive:
                    # zip.open(name) selects exact entries; never extract arbitrary archive paths.
                    info = archive.getinfo(name)
                    if info.file_size > MAX_FILE_SIZE:
                        raise ValueError(f"{name}: oversized archive entry")
                    reader = archive.open(info)
                else:
                    reader = (source / name).open("rb")
                with reader, target.open("wb") as writer:
                    total = 0
                    while True:
                        chunk = reader.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_FILE_SIZE:
                            raise ValueError(f"{name}: exceeds {MAX_FILE_SIZE} bytes")
                        writer.write(chunk)
                validate_file(target, dimensions, expected_hash)
                print(f"VERIFIED {name}")
            # Only replace the files after all six passed verification.
            for name in ASSETS:
                (staging / name).replace(TARGET / name)
    finally:
        if archive:
            archive.close()
    print(f"Installed 6 COSMOS assets into {TARGET}")
    print("Reload ORBIT at http://127.0.0.1:8765 (Ctrl+Shift+R).")


if __name__ == "__main__":
    try:
        install(resolve_source(sys.argv[1] if len(sys.argv) > 1 else None))
    except (OSError, ValueError, zipfile.BadZipFile, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
