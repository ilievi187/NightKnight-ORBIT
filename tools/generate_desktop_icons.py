#!/usr/bin/env python3
"""Generate small, offline, deterministic ORBIT PNG launcher icons with stdlib."""
import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src-tauri" / "icons"

def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

def png(size: int) -> bytes:
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")
        for x in range(size):
            cx = (x + .5) / size * 2 - 1
            cy = (y + .5) / size * 2 - 1
            d = math.hypot(cx, cy)
            ring = abs((cx * cx / .64 + cy * cy / .24) - 1.0) < .085
            core = d < .24
            star = math.hypot(cx + .60, cy + .58) < .065
            if core:
                color = (160, 135, 245, 255)
            elif ring:
                color = (104, 208, 235, 255)
            elif star:
                color = (236, 243, 255, 255)
            else:
                color = (9, 12, 27, 255)
            row.extend(color)
        rows.append(bytes(row))
    compressed = zlib.compress(b"".join(rows), 9)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", compressed) + chunk(b"IEND", b""))

def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in [("32x32.png", 32), ("128x128.png", 128), ("128x128@2x.png", 256), ("icon.png", 256)]:
        path = OUT / name
        path.write_bytes(png(size))
        print(path)

if __name__ == "__main__":
    main()
