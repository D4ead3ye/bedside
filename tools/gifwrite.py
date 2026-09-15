"""A minimal animated-GIF writer.

Pillow, imageio and ffmpeg are all absent from this environment and GIF is
the only format a GitHub README animates from a repo file with no doubt
attached, so the encoder lives here. Nothing in it is exotic — a shared
palette, ordered dithering, and the LZW variant the GIF spec describes.
"""

from __future__ import annotations

import numpy as np

# 8x8 Bayer matrix, normalised to [-0.5, 0.5). 256 colours over a dark
# purple gradient bands visibly without this; with it the banding turns
# into noise fine enough to disappear at README size.
_BAYER = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21],
], np.float32) / 64.0 - 0.5


def build_palette(frames, colours=256, sample=60000, seed=7):
    """k-means over a sample of every frame, via OpenCV."""
    import cv2
    rng = np.random.default_rng(seed)
    flat = np.concatenate([f.reshape(-1, 3) for f in frames])
    idx = rng.choice(len(flat), size=min(sample, len(flat)), replace=False)
    data = flat[idx].astype(np.float32)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 12, 1.0)
    _, _, centres = cv2.kmeans(data, colours, None, crit, 3,
                               cv2.KMEANS_PP_CENTERS)
    return np.clip(centres, 0, 255).astype(np.uint8)


def _lut(palette, bits=6):
    """Nearest palette index for every cell of a quantised RGB cube.

    Mapping 16M pixels against 256 colours directly is 4 billion distance
    terms. Doing it once per cube cell and then indexing is the same answer
    for a fraction of the work.
    """
    n = 1 << bits
    step = 256 // n
    grid = (np.arange(n, dtype=np.float32) * step + step / 2.0)
    r, g, b = np.meshgrid(grid, grid, grid, indexing="ij")
    cells = np.stack([r, g, b], axis=-1).reshape(-1, 3)
    pal = palette.astype(np.float32)
    out = np.empty(len(cells), np.uint8)
    for i in range(0, len(cells), 4096):
        chunk = cells[i:i + 4096]
        d = ((chunk[:, None, :] - pal[None, :, :]) ** 2).sum(-1)
        out[i:i + 4096] = d.argmin(1).astype(np.uint8)
    return out, bits, step


def quantise(frame, lut, bits, step, dither=True):
    a = frame.astype(np.float32)
    if dither:
        h, w = a.shape[:2]
        tile = np.tile(_BAYER, (h // 8 + 1, w // 8 + 1))[:h, :w]
        a = a + tile[:, :, None] * step
    q = np.clip(a, 0, 255).astype(np.uint8) >> (8 - bits)
    n = 1 << bits
    return lut[(q[:, :, 0].astype(np.int32) * n + q[:, :, 1]) * n
               + q[:, :, 2]]


def lzw_encode(data: bytes, min_code_size: int = 8) -> bytes:
    """The GIF flavour: LSB-first packing, a reset when the table fills."""
    clear_code = 1 << min_code_size
    end_code = clear_code + 1
    code_size = min_code_size + 1
    table: dict[tuple[int, int], int] = {}
    next_code = end_code + 1
    out = bytearray()
    buf = 0
    nbits = 0

    def write(code):
        nonlocal buf, nbits
        buf |= code << nbits
        nbits += code_size
        while nbits >= 8:
            out.append(buf & 0xFF)
            buf >>= 8
            nbits -= 8

    write(clear_code)
    if data:
        w = data[0]
        for k in data[1:]:
            key = (w, k)
            if key in table:
                w = table[key]
                continue
            write(w)
            if next_code == 4096:
                write(clear_code)
                table.clear()
                next_code = end_code + 1
                code_size = min_code_size + 1
            else:
                table[key] = next_code
                next_code += 1
                # One code LATER than feels right. The decoder's table lags
                # the encoder's by exactly one entry, so the encoder has to
                # hold the narrow width for one extra code to stay in step.
                # Widening at 2^size instead of 2^size+1 decodes as noise
                # from the 512th string onward — verified against .NET's
                # GIF decoder, which is the only oracle here that is not
                # this file's own mirror.
                if next_code == (1 << code_size) + 1 and code_size < 12:
                    code_size += 1
            w = k
        write(w)
    write(end_code)
    if nbits:
        out.append(buf & 0xFF)
    return bytes(out)


def lzw_decode(data: bytes, min_code_size: int = 8) -> bytes:
    """Only used to prove the encoder round-trips."""
    clear_code = 1 << min_code_size
    end_code = clear_code + 1
    code_size = min_code_size + 1
    table = {i: bytes([i]) for i in range(clear_code)}
    next_code = end_code + 1
    out = bytearray()
    prev = None
    buf = 0
    nbits = 0
    pos = 0
    while True:
        while nbits < code_size and pos < len(data):
            buf |= data[pos] << nbits
            nbits += 8
            pos += 1
        if nbits < code_size:
            break
        code = buf & ((1 << code_size) - 1)
        buf >>= code_size
        nbits -= code_size
        if code == clear_code:
            table = {i: bytes([i]) for i in range(clear_code)}
            next_code = end_code + 1
            code_size = min_code_size + 1
            prev = None
            continue
        if code == end_code:
            break
        if code in table:
            entry = table[code]
        elif prev is not None:
            entry = prev + prev[:1]
        else:
            break
        out += entry
        if prev is not None:
            table[next_code] = prev + entry[:1]
            next_code += 1
            if next_code == (1 << code_size) and code_size < 12:
                code_size += 1        # one behind the encoder, by design
        prev = entry
    return bytes(out)


def _blocks(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 255):
        chunk = data[i:i + 255]
        out.append(len(chunk))
        out += chunk
    out.append(0)
    return bytes(out)


def write_gif(path, frames, palette, indices, delay_cs=8, loop=0):
    """`indices` is one uint8 index image per frame, `palette` 256x3."""
    h, w = indices[0].shape
    pal = np.zeros((256, 3), np.uint8)
    pal[:len(palette)] = palette
    out = bytearray(b"GIF89a")
    out += np.array([w, h], "<u2").tobytes()
    out += bytes([0xF7, 0, 0])                 # global table, 256 entries
    out += pal.tobytes()
    # NETSCAPE2.0 is what makes it loop forever rather than play once.
    out += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01" + np.array([loop], "<u2").tobytes() + b"\x00"
    for idx in indices:
        out += b"\x21\xF9\x04\x00" + np.array([delay_cs], "<u2").tobytes() + b"\x00\x00"
        out += b"\x2C" + np.array([0, 0, w, h], "<u2").tobytes() + b"\x00"
        out += bytes([8])
        out += _blocks(lzw_encode(idx.tobytes(), 8))
    out += b"\x3B"
    open(path, "wb").write(bytes(out))
    return len(out)
