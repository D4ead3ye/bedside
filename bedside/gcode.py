"""G-code -> toolpath geometry.

The point of recording a byte offset per segment: OctoPrint reports job
progress as `filepos`, a byte offset into the very same file. Bisecting the
offsets turns that into an exact segment index, so the printed/unprinted
boundary in the 3D view is the real one rather than a guess from percent
complete.

Parsing runs on a worker thread — a 40MB file is a couple of seconds, and a
frame loop cannot afford that.
"""

from __future__ import annotations

import math
import threading
from array import array

import numpy as np

MAX_BYTES = 400 * 1024 * 1024

# Feature classes, normalised across slicer vocabularies. Every major slicer
# annotates the file with `;TYPE:` (PrusaSlicer, Cura, Orca) or `;FEATURE:`
# (Bambu), which is far better than guessing from geometry: sparse infill is
# made of long straight passes, so a "keep the longest runs" heuristic keeps
# infill and throws the perimeters away.
OTHER, PERIMETER, SOLID, INFILL, SUPPORT, BRIDGE, EXTERNAL, SKIRT = \
    range(8)

FEATURE_NAMES = {
    OTHER: "other", PERIMETER: "inner walls", SOLID: "solid",
    INFILL: "infill", SUPPORT: "support", BRIDGE: "bridges",
    EXTERNAL: "outer wall", SKIRT: "brim / skirt",
}

# Ordered: the first match wins, so "solid infill" is not caught by "infill"
# and "support interface" is not caught by "interface".
_FEATURE_RULES = (
    (b"bridge", BRIDGE),
    # Brim, skirt and raft are all "first layer, around the part, thrown
    # away afterwards" — a different thing from support, which stands up
    # through the print and is what you look at to see whether it is
    # holding. They shared a class until it turned out you often want one
    # without the other.
    (b"skirt", SKIRT), (b"brim", SKIRT), (b"raft", SKIRT),
    (b"support", SUPPORT), (b"tower", SUPPORT), (b"wipe", SUPPORT),
    (b"solid", SOLID), (b"skin", SOLID), (b"top surface", SOLID),
    (b"bottom surface", SOLID), (b"ironing", SOLID),
    # The outer wall is the silhouette: if it survives on every layer the
    # shell reads as solid, and if it does not you see straight through the
    # gaps. It gets its own class so it can outrank everything else for
    # budget. PrusaSlicer/Orca say "External perimeter", Cura "WALL-OUTER".
    (b"external perimeter", EXTERNAL), (b"outer perimeter", EXTERNAL),
    (b"wall-outer", EXTERNAL), (b"outer wall", EXTERNAL),
    (b"perimeter", PERIMETER), (b"wall", PERIMETER),
    (b"infill", INFILL), (b"fill", INFILL),
)


def _classify(comment: bytes) -> int:
    low = comment.lower()
    for needle, kind in _FEATURE_RULES:
        if needle in low:
            return kind
    return OTHER


# Target chord length when flattening an arc, in mm. Small enough that a
# curve reads as a curve, large enough not to explode the point count.
ARC_CHORD = 0.35
ARC_MAX_STEPS = 400


def _arc_points(x, y, nx, ny, i, j, r, clockwise):
    """Intermediate points along a G2/G3 arc, endpoint excluded.

    Slicers and post-processors (ArcWelder in particular) rewrite long runs
    of G1 into arcs, so a parser that treats G2/G3 as a straight move to the
    endpoint draws a chord straight across the part. A half-circle becomes a
    line through the middle of the model.
    """
    if i is None and j is None:
        if r is None:
            return []
        # R form: centre lies on the perpendicular bisector of start->end.
        dx, dy = nx - x, ny - y
        d = math.hypot(dx, dy)
        if d < 1e-9 or d > 2.0 * abs(r):
            return []
        h = math.sqrt(max(abs(r) * abs(r) - 0.25 * d * d, 0.0))
        mx, my = (x + nx) * 0.5, (y + ny) * 0.5
        ux, uy = -dy / d, dx / d
        # Sign picks the minor arc for R>0 and the major arc for R<0.
        sign = 1.0 if (r > 0) == clockwise else -1.0
        cx, cy = mx + sign * h * ux, my + sign * h * uy
    else:
        cx, cy = x + (i or 0.0), y + (j or 0.0)

    rad = math.hypot(x - cx, y - cy)
    if rad < 1e-9:
        return []

    a0 = math.atan2(y - cy, x - cx)
    a1 = math.atan2(ny - cy, nx - cx)
    sweep = a1 - a0
    if clockwise:
        while sweep >= 0.0:
            sweep -= 2.0 * math.pi
        while sweep < -2.0 * math.pi:
            sweep += 2.0 * math.pi
    else:
        while sweep <= 0.0:
            sweep += 2.0 * math.pi
        while sweep > 2.0 * math.pi:
            sweep -= 2.0 * math.pi
    # Start and end coincident means a full turn, not a zero-length arc.
    if abs(nx - x) < 1e-9 and abs(ny - y) < 1e-9:
        sweep = -2.0 * math.pi if clockwise else 2.0 * math.pi

    arc_len = abs(sweep) * rad
    steps = int(min(ARC_MAX_STEPS, max(2, math.ceil(arc_len / ARC_CHORD))))
    return [(cx + rad * math.cos(a0 + sweep * k / steps),
             cy + rad * math.sin(a0 + sweep * k / steps))
            for k in range(1, steps)]


class Toolpath:
    """Points, per-segment extrude flags, and per-segment byte offsets."""

    def __init__(self, xs, ys, zs, ext, offs, feat=None, has_features=False,
                 fan=None, arcs=0):
        self.pts = np.stack([xs, ys, zs], axis=1)          # (N, 3) float32
        self.extrude = ext.astype(bool)                    # (N-1,)
        self.offs = offs                                   # (N-1,) int64
        self.feat = (feat if feat is not None
                     else np.zeros(len(self.extrude), np.uint8))
        # False when the slicer stripped its annotations — callers must then
        # fall back to length, because every segment reads as OTHER.
        self.has_features = bool(has_features)
        # G2/G3 moves flattened during parsing; surfaced in the log so a
        # file full of arcs is visible rather than mysterious.
        self.arcs = int(arcs)
        # Fan duty at each segment, read straight from the file. M106 is
        # only emitted when the speed CHANGES, so watching live traffic
        # cannot recover a value set hours before we connected.
        self.fan = (fan if fan is not None
                    else np.zeros(len(self.extrude), np.uint8))
        self.runs = _contiguous_runs(self.extrude)

        # Median positive Z step, i.e. the layer height. Used to give each
        # layer a slightly different shade: a wall facing the camera has
        # every layer at the same height AND the same depth, so without a
        # per-layer band it renders as one flat slab.
        if len(self.pts) > 2:
            dz = np.diff(self.pts[:, 2])
            up = dz[(dz > 1e-4) & (dz < 5.0)]
            self.layer_h = float(np.median(up)) if len(up) else 0.2
        else:
            self.layer_h = 0.2

        if len(self.pts):
            e = self.pts[:-1][self.extrude] if len(self.extrude) else self.pts
            src = e if len(e) else self.pts
            self.lo = src.min(axis=0)
            self.hi = src.max(axis=0)
        else:
            self.lo = np.zeros(3, np.float32)
            self.hi = np.ones(3, np.float32)
        self.center = (self.lo + self.hi) * 0.5
        self.size = float(np.max(self.hi - self.lo)) or 1.0

    def __len__(self):
        return len(self.extrude)

    def index_at(self, filepos: int) -> int:
        """Segment index the printer is currently at, from a byte offset."""
        if not len(self.offs):
            return 0
        return int(np.searchsorted(self.offs, max(0, int(filepos)), "left"))

    def fan_at(self, seg: int) -> float:
        """Fan duty 0.0-1.0 at a segment index."""
        if not len(self.fan):
            return 0.0
        seg = max(0, min(seg, len(self.fan) - 1))
        return float(self.fan[seg]) / 255.0

    def z_at(self, seg: int) -> float:
        seg = max(0, min(seg, len(self.pts) - 1))
        return float(self.pts[seg, 2])


def _contiguous_runs(mask: np.ndarray):
    """[(start, stop)] index ranges of consecutive extruding segments.

    Travels break the path, so drawing one polyline per run is what stops
    the model being laced with straight lines across the bed.
    """
    if not len(mask):
        return []
    m = mask.astype(np.int8)
    edges = np.diff(m)
    starts = list((np.flatnonzero(edges == 1) + 1))
    stops = list((np.flatnonzero(edges == -1) + 1))
    if m[0]:
        starts.insert(0, 0)
    if m[-1]:
        stops.append(len(m))
    return [(int(a), int(b)) for a, b in zip(starts, stops) if b - a >= 1]


def parse(data: bytes, progress_cb=None) -> Toolpath:
    """Parse G-code bytes into a Toolpath.

    Understands G0/G1 moves, G90/G91 positioning, M82/M83 extruder mode and
    G92 origin resets — which is everything a normal FDM slicer emits.
    G2/G3 arcs are flattened into short chords. Treating them as a single
    straight move to the endpoint draws a line clean across the part, which
    matters because post-processors like ArcWelder rewrite ordinary G1 runs
    into arcs to shrink the file.
    """
    if len(data) > MAX_BYTES:
        raise ValueError(f"file is {len(data) // 1048576}MB, over the {MAX_BYTES // 1048576}MB cap")

    xs, ys, zs = array("f"), array("f"), array("f")
    ext, offs, feats = array("B"), array("q"), array("B")
    fans = array("B")

    x = y = z = 0.0
    e = 0.0
    abs_pos = True
    abs_e = True
    feat = OTHER
    seen_features = False
    fan = 0            # 0-255, carried forward between M106/M107
    arcs = 0           # how many G2/G3 moves were flattened

    # seed the path at the origin so segment i spans pts[i] -> pts[i+1]
    xs.append(0.0); ys.append(0.0); zs.append(0.0)

    off = 0
    total = len(data) or 1
    next_report = total // 50

    for raw in data.split(b"\n"):
        line_start = off
        off += len(raw) + 1

        if progress_cb is not None and off >= next_report:
            next_report = off + total // 50
            progress_cb(off / total)

        # Read the feature annotation before discarding the comment.
        semi = raw.find(b";")
        if semi != -1:
            head = raw[semi + 1:semi + 9].upper()
            if head.startswith(b"TYPE:"):
                feat = _classify(raw[semi + 6:])
                seen_features = True
            elif head.startswith(b"FEATURE:"):
                feat = _classify(raw[semi + 9:])
                seen_features = True
            raw = raw[:semi]
        raw = raw.strip()
        if not raw or raw[0] not in (71, 77):  # 'G', 'M'
            continue

        parts = raw.split()
        cmd = parts[0].upper()

        if cmd == b"G90":
            abs_pos = True
            continue
        if cmd == b"G91":
            abs_pos = False
            continue
        if cmd == b"M106":
            fan = 255
            for pt in parts[1:]:
                if pt[0:1].upper() == b"S":
                    try:
                        fan = max(0, min(255, int(float(pt[1:]))))
                    except ValueError:
                        pass
            continue
        if cmd == b"M107":
            fan = 0
            continue
        if cmd == b"M82":
            abs_e = True
            continue
        if cmd == b"M83":
            abs_e = False
            continue

        if cmd == b"G92":
            for p in parts[1:]:
                try:
                    v = float(p[1:])
                except ValueError:
                    continue
                c = p[0:1].upper()
                if c == b"X": x = v
                elif c == b"Y": y = v
                elif c == b"Z": z = v
                elif c == b"E": e = v
            continue

        if cmd not in (b"G0", b"G1", b"G00", b"G01", b"G2", b"G3"):
            continue

        nx, ny, nz = x, y, z
        de = 0.0
        moved = False
        ai = aj = ar = None
        for p in parts[1:]:
            c = p[0:1].upper()
            if c in (b"I", b"J", b"R"):
                try:
                    v = float(p[1:])
                except ValueError:
                    continue
                if c == b"I":
                    ai = v
                elif c == b"J":
                    aj = v
                else:
                    ar = v
                continue
            if c not in (b"X", b"Y", b"Z", b"E"):
                continue
            try:
                v = float(p[1:])
            except ValueError:
                continue
            if c == b"X":
                nx = v if abs_pos else x + v
                moved = True
            elif c == b"Y":
                ny = v if abs_pos else y + v
                moved = True
            elif c == b"Z":
                nz = v if abs_pos else z + v
                moved = True
            else:
                if abs_e:
                    de = v - e
                    e = v
                else:
                    de = v
                    e += v

        if not moved:
            continue  # retraction or pure-E move: no geometry

        if cmd in (b"G2", b"G3"):
            mid = _arc_points(x, y, nx, ny, ai, aj, ar, cmd == b"G2")
            if mid:
                arcs += 1
                extruding = 1 if de > 0.0 else 0
                # Every sub-segment carries the source line's byte offset, so
                # filepos still bisects correctly; equal offsets are fine for
                # searchsorted.
                for (px, py) in mid:
                    xs.append(px); ys.append(py); zs.append(nz)
                    ext.append(extruding)
                    offs.append(line_start)
                    feats.append(feat)
                    fans.append(fan)

        x, y, z = nx, ny, nz
        xs.append(x); ys.append(y); zs.append(z)
        ext.append(1 if de > 0.0 else 0)
        offs.append(line_start)
        feats.append(feat)
        fans.append(fan)

    if progress_cb is not None:
        progress_cb(1.0)

    return Toolpath(
        np.frombuffer(xs, dtype=np.float32).copy(),
        np.frombuffer(ys, dtype=np.float32).copy(),
        np.frombuffer(zs, dtype=np.float32).copy(),
        np.frombuffer(ext, dtype=np.uint8).copy(),
        np.frombuffer(offs, dtype=np.int64).copy(),
        np.frombuffer(feats, dtype=np.uint8).copy(),
        seen_features,
        np.frombuffer(fans, dtype=np.uint8).copy(),
        arcs,
    )


class Loader:
    """Downloads and parses in the background; the GUI polls `.state`."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"      # idle | downloading | parsing | ready | error
        self.message = ""
        self.fraction = 0.0
        self.path = None
        self.toolpath = None
        self._thread = None

    def start(self, client, path, origin="local"):
        if self._thread and self._thread.is_alive():
            return
        with self.lock:
            self.state = "downloading"
            self.message = path
            self.fraction = 0.0
            self.path = path
            self.toolpath = None
        self._thread = threading.Thread(
            target=self._run, args=(client, path, origin),
            daemon=True, name="gcode-load")
        self._thread.start()

    def _set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def _run(self, client, path, origin):
        try:
            data = client.download_gcode(path, origin)
            self._set(state="parsing", fraction=0.0)
            tp = parse(data, progress_cb=lambda f: self._set(fraction=f))
            if len(tp) == 0:
                raise ValueError("no printable moves found")
            self._set(state="ready", toolpath=tp, fraction=1.0,
                      message=f"{len(tp):,} segments")
        except Exception as exc:
            self._set(state="error", message=str(exc), fraction=0.0)

    def snapshot(self):
        with self.lock:
            return self.state, self.message, self.fraction, self.toolpath
