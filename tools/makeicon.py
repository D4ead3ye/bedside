"""Generate icon.png / icon.ico from primitives — no image library needed.

Same reasoning as VertexUI's drawn icons: an asset you generate is one you
cannot lose track of in a frozen exe.
"""

import struct
import sys
import zlib
from pathlib import Path

import numpy as np

S = 256
OUT = Path(__file__).resolve().parent.parent / "assets"


def hex_rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32)


BG = hex_rgb("#0e0e11")
ACCENT = hex_rgb("#da2538")
BRIGHT = hex_rgb("#ff4355")
INFO = hex_rgb("#9aa3b3")


def render():
    y, x = np.mgrid[0:S, 0:S].astype(np.float32)
    cx = cy = (S - 1) / 2.0
    img = np.zeros((S, S, 4), np.float32)

    # rounded-square plate
    r = S * 0.21
    qx = np.clip(x, r, S - r)
    qy = np.clip(y, r, S - r)
    plate = np.clip(r - np.hypot(x - qx, y - qy) + 1.0, 0, 1)
    img[..., :3] = BG
    img[..., 3] = plate * 255.0

    d = np.hypot(x - cx, y - cy)

    def ring(radius, width, colour):
        a = np.clip((width * 0.5 - np.abs(d - radius)) * 1.5, 0, 1) * plate
        img[..., :3] = img[..., :3] * (1 - a[..., None]) + colour * a[..., None]
        img[..., 3] = np.maximum(img[..., 3], a * 255.0)

    # concentric extrusion rings, brightest at the nozzle
    ring(S * 0.335, S * 0.055, ACCENT)
    ring(S * 0.215, S * 0.043, BRIGHT)
    ring(S * 0.105, S * 0.034, INFO)

    return np.clip(img, 0, 255).astype(np.uint8)


def png_bytes(rgba):
    h, w, _ = rgba.shape
    raw = np.zeros((h, w * 4 + 1), np.uint8)
    raw[:, 1:] = rgba.reshape(h, w * 4)

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body +
                struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 9))
            + chunk(b"IEND", b""))


def ico_bytes(png):
    """ICO may hold a PNG payload directly; 0 in the size byte means 256."""
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    png = png_bytes(render())
    (OUT / "icon.png").write_bytes(png)
    (OUT / "icon.ico").write_bytes(ico_bytes(png))
    # hello_imgui reads the window/taskbar icon from this exact path inside
    # the assets folder, so it has to be a real file, not a reference.
    (OUT / "app_settings").mkdir(parents=True, exist_ok=True)
    (OUT / "app_settings" / "icon.png").write_bytes(png)
    print(f"wrote {OUT / 'icon.png'} ({len(png)} bytes), icon.ico "
          "and app_settings/icon.png")


if __name__ == "__main__":
    sys.exit(main())
