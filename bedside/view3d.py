"""3D toolpath view drawn straight onto the ImGui draw list.

Two measurements shaped this file:

* `add_polyline` only accepts a sequence of `ImVec2` — a numpy array is
  rejected, and a list of tuples costs 14ms per call. Building the ImVec2
  lists is ~8ms per 25k points, so they are cached and rebuilt only when the
  camera actually moves. Idling on a static camera costs the draw alone.
* Per-call overhead is small: 6000 short polylines cost about the same as
  one long one. Run count is therefore not worth optimising; total point
  count is.
"""

from __future__ import annotations

import math

import numpy as np
from imgui_bundle import ImVec2, ImVec4, imgui

import vertexui as vui
from vertexui import anim, theme as theme_mod

from . import gcode, glview

POINT_BUDGET = 22_000
# Each run costs a Python loop iteration and an add_polyline call, so a real
# model with tens of thousands of short infill fragments is bound by run
# count long before it is bound by points. Dropping the shortest runs sheds
# that cost while leaving every layer represented — which matters, because
# dropping whole layers would make the printing edge jump.
# Measured: 6000 short polylines cost about 4.2ms to draw, against 3.1ms
# for one long one — so run count buys a lot of completeness cheaply. The
# old 2,600 drew 3.7% of a 70k-run model, and the missing runs read as an
# x-ray rather than as missing detail.
# Runs are cheap next to points, and dropping them punches holes in solid
# surfaces. This is a backstop for pathological files, not a working limit —
# the point budget is what actually paces the view.
RUN_BUDGET = 30_000

# Painter's sorting can only order whole pieces against each other, so a
# piece must be short enough that its own depth is coherent — a four-sided
# perimeter needs its front and back sides in different pieces. The chunk
# length is derived from this budget rather than fixed, so a coarse model
# gets split hard and a dense one is not shattered into needless calls.
PIECE_BUDGET = 14_000
MIN_CHUNK = 3

# Hard ceiling on the turn tolerance. A closed loop only turns 2*pi in
# total, so once the tolerance passes that, no interior point can trigger a
# keep and the whole loop collapses to a chord from its first point to its
# last. On a concave outline that chord leaves the material entirely, which
# is what put stray lines outside the model. Measured on a U-shaped
# perimeter: 0.9 rad still gives 0.00mm deviation, 3 rad gives 1.4mm, and
# 17 rad — which the old budget loop reached in three passes — gives 9mm.
# Deviation ceiling in millimetres. A typical extrusion is 0.4mm wide, so
# anything under about a third of that is invisible on screen; past it the
# drawn line starts visibly leaving the real toolpath.
# Deviation ceiling in millimetres. Note this is the per-pass tolerance,
# and error compounds across passes to roughly 1.7x it — measured, a 1.5mm
# ceiling put lines 2.5mm off the real path, which is visible as cut
# corners. 0.5 keeps the measured worst case near 0.85mm, about two
# extrusion widths, which is the point where shortcuts stop being visible.
TOL_MM_MAX = 0.5

# Tolerance for the coarse copy shown while the camera moves. It must stay
# inside what is visually acceptable: at 5mm it bought only a 48% point
# reduction and drew long chords straight across the model, which is worse
# than the frame it saved. Kept sub-millimetre so no drawn line can leave
# the real toolpath by more than about two extrusion widths.
LOD_TOL_MM = 0.6

# One rate for zoom AND pan; they must match. The correction that keeps a
# point under the cursor is affine in the zoom ratio, so easing them
# together holds the invariant at every frame of the animation. Ease them
# at different rates — pan was 22 against zoom's 14 — and the point slides
# towards the centre for the whole transition, landing correctly only once
# both have settled. That is the "still goes slightly to the center".
ZOOM_EASE = 14.0

# The key light follows the camera, offset by this angle so it reads as a
# light over your shoulder rather than a flat headlight. A light fixed in
# WORLD space is wrong: orbiting rotates the face you are looking at away
# from it, and the near surface — the one you most want to see — goes dark.
_LIGHT_OFFSET = 0.62          # radians
_LIGHT_MIX = 0.62             # share of the shade index vs camera distance

# The colour ramp is precomputed as [layer band][nearness][height], so the
# per-run lookup in the draw loop is three array indexes rather than three
# colour computations.
HEIGHT_STEPS = 24
# Nearness is quantised into this many rows. Seven was visibly blocky:
# adjacent pieces on one wall landed in different rows and the seam showed
# as a hard step. The ramp is precomputed once, so more rows cost nothing
# per frame.
DEPTH_STEPS = 28

# Which features survive the run budget first. Perimeters and bridges carry
# the silhouette, solid surfaces carry the top faces; sparse infill and
# support are the first things worth losing.
_RUN_PRIORITY = {
    gcode.EXTERNAL: 0,          # the silhouette — never sacrifice it
    gcode.PERIMETER: 1, gcode.BRIDGE: 1,
    gcode.SOLID: 2, gcode.OTHER: 3,
    gcode.INFILL: 4, gcode.SUPPORT: 5, gcode.SKIRT: 6,
}
# Dropped first, never a KeyError. A missing entry here used to take the
# whole 3D view down with it the moment a new feature id was added, which
# is a lot of damage for a table that only decides what to sacrifice.
_PRIO_DEFAULT = 9

# Sparse infill and support are hidden by default: they are what turned the
# view into a solid mass, and a shell reads far better.
DEFAULT_VISIBLE = (gcode.EXTERNAL, gcode.PERIMETER, gcode.SOLID,
                   gcode.BRIDGE, gcode.OTHER)


def _thin_runs(pts, runs, tol_mm, passes=6):
    """Thin every run at once, bounded by deviation from the real toolpath.

    Douglas-Peucker gives a hard guarantee and is the textbook answer, but
    per-run in Python it cost 4-5 seconds on a 313k-point model and this
    runs on the frame thread. The work is not the maths, it is doing it
    12,920 times; so the whole model is flattened into one array and thinned
    with a handful of numpy passes.

    Each pass drops points whose removal would move the line by less than
    `tol_mm`, alternating parity so two neighbours are never dropped
    together — the earlier attempt at that guard tested a stale mask and was
    not even monotonic in its tolerance.
    """
    if not runs:
        return []

    lens = np.fromiter((b - a + 1 for a, b in runs), np.int64, len(runs))
    idx_all = np.concatenate([np.arange(a, b + 1) for a, b in runs])
    runid = np.repeat(np.arange(len(runs), dtype=np.int64), lens)
    pos = np.concatenate(([0], np.cumsum(lens)))

    P = pts[idx_all]
    n = len(P)
    keep = np.ones(n, bool)
    protect = np.zeros(n, bool)
    protect[pos[:-1]] = True          # first point of every run
    protect[pos[1:] - 1] = True       # last point of every run

    for pi in range(passes):
        sel = np.flatnonzero(keep)
        if len(sel) < 3:
            break
        r = runid[sel]
        same = (r[:-2] == r[1:-1]) & (r[1:-1] == r[2:])
        a_, b_, c_ = P[sel[:-2]], P[sel[1:-1]], P[sel[2:]]
        d = c_ - a_
        L2 = (d * d).sum(1)
        t = np.clip(((b_ - a_) * d).sum(1) / np.maximum(L2, 1e-12), 0.0, 1.0)
        dist = np.linalg.norm(b_ - (a_ + t[:, None] * d), axis=1)
        cand = same & (dist < tol_mm) & ~protect[sel[1:-1]]
        cand &= (np.arange(len(cand)) % 2) == (pi % 2)
        if not cand.any():
            break
        keep[sel[1:-1][cand]] = False

    kept = np.flatnonzero(keep)
    bounds = np.searchsorted(runid[kept], np.arange(len(runs) + 1))
    out = []
    for k in range(len(runs)):
        m = idx_all[kept[bounds[k]:bounds[k + 1]]]
        if len(m) >= 2:
            out.append(m.tolist())
        else:
            out.append(None)
    return out


_SAMPLE = None


def sample_toolpath():
    """A small annotated cylinder for the settings preview.

    Emitted as real G-code and run through the real parser, so the preview
    exercises the same feature classification, decimation and depth shading
    as a live job — a preview drawn by a separate code path would be a
    picture of what the settings were supposed to do.
    """
    global _SAMPLE
    if _SAMPLE is not None:
        return _SAMPLE

    # Proportions matter: 46 layers at 15mm radius is a disc, and a disc
    # reads as a flat circle from any angle. Taller than wide puts the
    # side wall on screen, which is where extrusion width and opacity
    # are actually judged.
    layers, r, seg = 84, 9.0, 40
    out = ["G90", "M82", "G92 E0"]
    e = 0.0
    for i in range(layers):
        z = 0.2 * (i + 1)
        solid = i < 3 or i >= layers - 3
        for k, rad in enumerate((r, r - 0.55)):
            out.append(";TYPE:External perimeter" if k == 0 else ";TYPE:Perimeter")
            out.append(f"G0 X{rad:.3f} Y0.000 Z{z:.2f}")
            for s in range(1, seg + 1):
                a = 2.0 * math.pi * s / seg
                e += 0.05
                out.append(f"G1 X{rad * math.cos(a):.3f} "
                           f"Y{rad * math.sin(a):.3f} E{e:.4f}")
        out.append(";TYPE:Solid infill" if solid else ";TYPE:Internal infill")
        n = 24 if solid else 7
        for j in range(n):
            t = -r + 2.0 * r * (j + 0.5) / n
            half = math.sqrt(max(r * r - t * t, 0.0)) - 1.2
            if half <= 0.2:
                continue
            out.append(f"G0 X{t:.3f} Y{-half:.3f} Z{z:.2f}")
            e += 0.05
            out.append(f"G1 X{t:.3f} Y{half:.3f} E{e:.4f}")

    _SAMPLE = gcode.parse(("\n".join(out) + "\n").encode())
    return _SAMPLE


class Display:
    """Decimated, run-grouped geometry plus a cached screen projection."""

    def __init__(self, tp, budget: int = POINT_BUDGET, run_budget: int = RUN_BUDGET,
                 visible=None, tol=None):
        runs = tp.runs
        self.runs_total = len(runs)
        self.has_features = tp.has_features

        # Feature of each run, taken from its first segment.
        run_feat = (np.asarray([tp.feat[a] for a, _ in runs], np.uint8)
                    if runs else np.zeros(0, np.uint8))

        if tp.has_features and visible is not None:
            allow = np.isin(run_feat, np.asarray(sorted(visible), np.uint8))
            runs = [r for r, ok in zip(runs, allow) if ok]
            run_feat = run_feat[allow]
        self.runs_visible = len(runs)

        if len(runs) > run_budget:
            lengths = np.fromiter((b - a for a, b in runs), np.int64, len(runs))
            if tp.has_features:
                # Rank by what the feature *is*, then by length. Ranking on
                # length alone keeps sparse infill (long diagonal passes) and
                # discards perimeters (chopped short by seams) — which is
                # precisely backwards for reading the model's shape.
                prio = np.asarray(
                    [_RUN_PRIORITY.get(f, _PRIO_DEFAULT) for f in run_feat],
                    np.int64)
                order = np.lexsort((-lengths, prio))[:run_budget]
            else:
                order = np.argpartition(-lengths, run_budget)[:run_budget]
            order = np.sort(order)
            runs = [runs[i] for i in order]
            run_feat = run_feat[order]
        self.runs_kept = len(runs)
        self._slice_starts = np.zeros(0, np.int64)   # filled once slices exist

        total = sum(b - a for a, b in runs) or 1
        # Uniform index striding thins a dense curve and a straight line by
        # the same factor, so it spends its budget on stretches that needed
        # no points and rounds off the corners that did. Thinning by shape
        # keeps a point whenever the path has turned enough or run far
        # enough since the last one kept: measured on a rounded-rectangle
        # perimeter, worst-case deviation from the true path drops from
        # 0.23mm to 0.06mm at the same point count.
        # Start well below anything visible and open up only if the budget
        # demands it, never past TOL_MM_MAX. `tol` overrides the whole
        # negotiation — used for the coarse level-of-detail copy, where a
        # visible shortcut is fine because it is only on screen while the
        # camera is actually moving.
        scale = 1.0
        if tol is not None:
            tol_mm = float(tol)
        else:
            tol_mm = 0.04
            scale = max(1.0, total / max(1000.0, float(budget)))
            tol_mm = min(TOL_MM_MAX, tol_mm * scale)

        def thin_all(tol):
            got = _thin_runs(tp.pts, runs, tol)
            out, feats = [], []
            for sel, f in zip(got, run_feat):
                if sel:
                    out.append(sel)
                    feats.append(int(f))
            return out, feats

        # Thinning by shape gives no guarantee about the resulting count, so
        # the budget is enforced afterwards. Tolerance does not map linearly
        # to point count, so one corrective pass overshoots — iterate until
        # it fits. Without this a dense model came out at 2.2x the requested
        # points and orbiting paid for every one of them.
        sels, sel_feat = thin_all(tol_mm)
        passes = 0 if tol is not None else 3
        # Capped at three passes: each re-scans every run, and this runs on
        # the frame thread when detail or feature visibility change. The
        # tolerance is clamped, so the budget can lose — points are cheaper
        # than a model that no longer matches its own toolpath.
        for _ in range(passes):
            n_thinned = sum(len(x) for x in sels)
            if n_thinned <= budget:
                break
            over = max(1.05, (n_thinned / float(budget)) ** 0.75)
            new_tol = min(TOL_MM_MAX, tol_mm * over)
            if new_tol <= tol_mm * 1.001:
                break
            tol_mm = new_tol
            sels, sel_feat = thin_all(tol_mm)

        # Last-resort valve only. Dropping runs punches holes in solid
        # surfaces — on the real model it removed every one of 10,324
        # solid-infill runs, which took the floor out and let you see
        # through the part. Extra points only cost frames, so the threshold
        # is deliberately far out: this exists for pathological files, not
        # as a routine budget mechanism.
        n_thinned = sum(len(x) for x in sels)
        if n_thinned > max(budget * 4, 150_000) and len(sels) > 1:
            keep_frac = budget / float(n_thinned)
            # sel_feat is built alongside sels, so it stays aligned even
            # though runs with fewer than two points are skipped. Slicing
            # run_feat here instead silently shifted the priorities and this
            # fallback then dropped outer walls at random.
            prio_now = np.asarray(
                [_RUN_PRIORITY.get(f, 3) for f in sel_feat], np.int64)
            keep_n = max(1, int(len(sels) * keep_frac))
            order2 = np.lexsort((np.arange(len(sels)), prio_now))[:keep_n]
            order2.sort()
            sels = [sels[i] for i in order2]
            sel_feat = [sel_feat[i] for i in order2]

        # A perimeter loop wraps right around the part, so as one piece its
        # mean depth is the object's centre and it cannot be ordered against
        # any other loop — measured, a whole run spanned 83% of the model's
        # depth range. That is why back surfaces painted over front ones and
        # the shell looked like an x-ray. Pieces overlap by one point so the
        # drawn line stays unbroken.
        n_pts = sum(len(s) for s in sels) or 1
        chunk = max(MIN_CHUNK, math.ceil(n_pts / PIECE_BUDGET) + 1)

        idx: list[int] = []
        slices: list[tuple[int, int]] = []
        for sel in sels:
            for c in range(0, len(sel) - 1, chunk - 1):
                part = sel[c:c + chunk]
                if len(part) < 2:
                    continue
                slices.append((len(idx), len(idx) + len(part)))
                idx.extend(part)
        self.chunk = chunk
        stride = round(scale, 2)

        self.idx = np.asarray(idx, dtype=np.int64)
        self.pts = tp.pts[self.idx] if len(self.idx) else np.zeros((0, 3), np.float32)
        # strictly increasing, because runs are ordered and disjoint —
        # which lets one searchsorted split the whole model by progress
        self.seg = self.idx
        self.slices = slices
        # Bisected in head() to find which run a flat index falls in.
        self._slice_starts = np.asarray([s0 for s0, _ in slices], np.int64) \
            if slices else np.zeros(0, np.int64)
        # Height of each run, for depth shading. Without it every layer
        # draws at the same colour and 150 layers read as one solid block.
        self.run_z = np.asarray(
            [float(self.pts[s0, 2]) for s0, _ in slices], np.float32
        ) if slices else np.zeros(0, np.float32)
        self.stride = stride
        self.center = tp.center
        self.size = tp.size
        self.layer_h = max(getattr(tp, "layer_h", 0.2), 1e-3)
        # Horizontal direction of each piece. An extruded bead is a little
        # wall, so the surface normal is perpendicular to the path in XY.
        # Without this every face of a box shades identically and the model
        # reads as a flat slab from any angle where one face fills the view.
        if slices:
            a_pt = self.pts[[s0 for s0, _ in slices]]
            b_pt = self.pts[[s1 - 1 for _, s1 in slices]]
            dxy = (b_pt - a_pt)[:, :2]
            ln = np.linalg.norm(dxy, axis=1, keepdims=True)
            self.run_dir = np.divide(dxy, np.maximum(ln, 1e-9))
        else:
            self.run_dir = np.zeros((0, 2), np.float32)
        # Alternating band per layer, so stacked layers stay distinguishable
        # on a wall that faces the camera squarely.
        self.run_band = (np.round(self.run_z / self.layer_h).astype(np.int64) & 1
                         if len(self.run_z) else np.zeros(0, np.int64))

        self._key = None
        self._proj: list[list[ImVec2]] = []
        self._order = np.zeros(0, np.int64)
        self._depth_idx = np.zeros(0, np.int64)
        self._order_py = None

    def project(self, key, cx, cy, ppm, R, dist):
        """Rebuild the cached ImVec2 lists if the camera changed."""
        if key == self._key:
            return
        self._key = key
        if not len(self.pts):
            self._proj = []
            self._order = np.zeros(0, np.int64)
            self._depth_idx = np.zeros(0, np.int64)
            return

        v = (self.pts - self.center) @ R.T
        depth = dist - v[:, 2]
        np.maximum(depth, 1e-3, out=depth)
        s = (ppm * dist) / depth
        sx = cx + v[:, 0] * s
        sy = cy - v[:, 1] * s

        flat = np.empty((len(sx), 2), np.float32)
        flat[:, 0] = sx
        flat[:, 1] = sy
        lst = flat.tolist()
        self._proj = [[ImVec2(a, b) for a, b in lst[s0:s1]] for s0, s1 in self.slices]

        # Painter's algorithm. Drawing in print order means a run at the back
        # can be laid over one at the front, which is most of why the model
        # read as a tangle of lines rather than a solid object. Sorting by
        # camera depth once per camera change costs one argsort.
        if len(self._slice_starts):
            sums = np.add.reduceat(depth, self._slice_starts)
            counts = np.diff(np.append(self._slice_starts, len(depth)))
            mean_d = sums / np.maximum(counts, 1)
            # Far to near. Sorting by layer height instead was tried: it is
            # stable through an orbit, but it is only correct looking
            # straight down — at a shallow angle the far wall's upper layers
            # paint over the near wall's lower ones. Measured 586 order
            # inversions, one per layer. Reverted for want of evidence that
            # it helped the symptom it was aimed at.
            self._order = np.argsort(-mean_d)
            # Nearness 0..1 across the model, quantised into ramp rows. This
            # is what gives the model volume: without it every surface is lit
            # identically and the shape reads flat however it is rotated.
            lo, hi = float(mean_d.min()), float(mean_d.max())
            near = (hi - mean_d) / max(hi - lo, 1e-6)

            # Surface normal in XY, flipped to face the camera, lit by a
            # fixed world-space key light. This is what separates the front
            # of a box from its side: distance alone cannot, because a flat
            # wall is all at one distance.
            nrm = np.stack([self.run_dir[:, 1], -self.run_dir[:, 0]], axis=1)
            view_xy = np.array([R[2][0], R[2][1]], np.float32)
            vn = float(np.hypot(view_xy[0], view_xy[1]))
            if vn < 1e-6:
                view_xy = np.array([0.0, 1.0], np.float32)   # straight down
                vn = 1.0
            view_xy = view_xy / vn
            flip = (nrm @ view_xy) < 0.0
            nrm[flip] *= -1.0

            # Key light rotated off the view direction, so it orbits with
            # the camera and the near face is always lit.
            ca, sa = math.cos(_LIGHT_OFFSET), math.sin(_LIGHT_OFFSET)
            light = np.array([view_xy[0] * ca - view_xy[1] * sa,
                              view_xy[0] * sa + view_xy[1] * ca], np.float32)
            # Half-Lambert: never reaches zero, so a face turned away from
            # the light dims instead of vanishing.
            lit = np.clip(0.5 + 0.5 * (nrm @ light), 0.0, 1.0)
            # Flat runs (a dot, or a perfectly closed loop) get the average.
            lit[np.linalg.norm(self.run_dir, axis=1) < 0.5] = 0.5

            shade = np.clip(_LIGHT_MIX * lit + (1.0 - _LIGHT_MIX) * near,
                            0.0, 1.0)
            self._depth_idx = np.clip(
                (shade * (DEPTH_STEPS - 1)).astype(np.int64), 0, DEPTH_STEPS - 1)
        else:
            self._order = np.zeros(0, np.int64)
            self._depth_idx = np.zeros(0, np.int64)

        # The draw loop runs once per piece — 21k times on a real model — and
        # indexing a numpy array with a scalar costs about a microsecond
        # each time. That was ~27ms per frame of pure lookup overhead, more
        # than the drawing itself. Plain lists are ~50x cheaper per access.
        self._order_py = self._order.tolist()
        self._depth_py = self._depth_idx.tolist()
        self._band_py = self.run_band.tolist()
        self._runz_py = self.run_z.tolist()

    def draw(self, dl, cur_seg: float, ramp, col_todo: int, thickness: float,
             cur_z: float, fade_mm: float):
        """`ramp` is a precomputed list of colours, darkest first. Printed
        geometry is shaded by how far below the current layer it sits, which
        is what stops a tall model reading as one solid mass."""
        if not self._proj:
            return
        k = int(np.searchsorted(self.seg, cur_seg, "left"))
        top = HEIGHT_STEPS - 1
        ghost = min(thickness * 0.45, 2.0)
        order = getattr(self, "_order_py", None)
        if order is None:
            return
        depth_py, band_py, runz_py = self._depth_py, self._band_py, self._runz_py
        slices, proj = self.slices, self._proj
        inv_fade = 1.0 / fade_mm
        for i in order:
            s0, s1 = slices[i]
            pts = proj[i]
            if s0 >= k:
                dl.add_polyline(pts, col_todo, ghost, 0)
                continue
            # ramp[band][nearness][height below the nozzle]
            row = ramp[band_py[i]][depth_py[i]]
            f = 1.0 - (cur_z - runz_py[i]) * inv_fade
            if f < 0.0:
                f = 0.0
            elif f > 1.0:
                f = 1.0
            col = row[int(f * top)]
            if s1 <= k:
                dl.add_polyline(pts, col, thickness, 0)
            else:
                cut = k - s0
                if cut >= 2:
                    dl.add_polyline(pts[:cut], row[top], thickness, 0)
                if len(pts) - cut >= 2:
                    dl.add_polyline(pts[cut - 1:], col_todo, ghost, 0)

    def z_at_seg(self, cur_seg: float) -> float:
        """Nozzle height, looked up in display-point space rather than
        segment space — the two are not the same index after decimation."""
        if not len(self.pts):
            return 0.0
        k = int(np.searchsorted(self.seg, cur_seg, "left"))
        return float(self.pts[min(k, len(self.pts) - 1), 2])

    def head(self, cur_seg: float):
        """Screen position of the nozzle, or None.

        Interpolated *between* the two points either side of `cur_seg`.
        Snapping to the nearest stored point makes the marker stutter from
        vertex to vertex, and after decimation those vertices can be far
        apart — the eased segment index is smooth, so the position has to
        be too.
        """
        if not self._proj or not self.slices:
            return None
        n = len(self.seg)
        k = int(np.searchsorted(self.seg, cur_seg, "left"))
        k = max(0, min(k, n - 1))

        si = int(np.searchsorted(self._slice_starts, k, "right")) - 1
        si = max(0, min(si, len(self.slices) - 1))
        s0, _ = self.slices[si]
        pts = self._proj[si]
        if not pts:
            return None
        loc = k - s0
        if loc <= 0:
            return pts[0]
        if loc >= len(pts):
            return pts[-1]

        # loc > 0 guarantees k-1 sits in this same run, so this never
        # interpolates across a travel move.
        a, b = float(self.seg[k - 1]), float(self.seg[k])
        f = 0.0 if b <= a else (cur_seg - a) / (b - a)
        f = min(max(f, 0.0), 1.0)
        p0, p1 = pts[loc - 1], pts[loc]
        return ImVec2(p0.x + (p1.x - p0.x) * f, p0.y + (p1.y - p0.y) * f)


def _rot(yaw: float, pitch: float) -> np.ndarray:
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]], np.float32)
    rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]], np.float32)
    return (rx @ rz).astype(np.float32)


class ToolpathView:
    """Orbit camera + the progress sweep. One instance, reused per job."""

    def __init__(self, key: str = "v3d"):
        # Namespaces the anim keys and the hit-test ID. Two views exist
        # (the dashboard and the settings preview) and sharing either
        # would make them fight over one eased camera and one widget ID.
        self.key = key
        self.yaw = -0.9
        # Just under 30 degrees above the bed. The old -1.05 (60 degrees)
        # looked down on the print from almost directly overhead, which
        # flattens the walls into outlines and is a poor angle to watch a
        # print from — and because auto-orbit only turns the yaw, it was
        # also the angle the whole orbit ran at.
        self.pitch = -0.5
        self.zoom = 1.0
        # Pan, in pixels of the panel it is drawn into. Storing it in screen
        # units rather than world units means a drag moves the model exactly
        # as far as the cursor went, at any zoom.
        self.pan_x = 0.0
        self.pan_y = 0.0
        # Flick inertia. A drag that ends mid-motion should coast, not stop
        # dead; this is the yaw/pitch velocity carried out of the last drag.
        self._spin = [0.0, 0.0]
        self.orbit_speed = 0.16       # rad/s while auto-orbiting
        self.display: Display | None = None
        self.display_lo: Display | None = None
        self._last_key = None
        # GPU path. The CPU polyline renderer stays as a fallback for a
        # machine that cannot give us a GL 3.3 context.
        self.use_gl = True
        self.gl_error = ""
        self._gl = None
        self._mesh = None
        self._mesh_lw = None
        # Off by default on purpose: orbiting reprojects every frame
        # (10.5ms vs 2.7ms measured), and an always-spinning view would
        # stop the app ever idling while it sits open for a 9-hour print.
        self.auto_orbit = False
        self._drag = False

        # -- tunables, all driven from the settings panel
        self.budget = POINT_BUDGET
        self.visible = set(DEFAULT_VISIBLE)
        # Extrusion width in MILLIMETRES, converted to pixels against the
        # current zoom. A fixed pixel width is what made the model look
        # like a wireframe: zooming in gave you the same hairlines further
        # apart instead of thicker beads of plastic.
        self.line_mm = 0.45
        self.fade = 0.30          # depth-fade span as a fraction of the model
        # Cutaway, as a fraction of model height. 1.0 draws everything.
        # Anything the shell encloses — infill above all — cannot change a
        # single pixel while the outer wall is on, so a filter that says
        # "show infill" is a filter that does nothing until you can look
        # inside. This is what lets you.
        self.clip = 1.0
        self.ghost = 0.055        # alpha of not-yet-printed geometry
        # Opaque by default. Semi-transparent geometry made every wall
        # leak whatever was behind it, so the shell looked like an
        # x-ray from any angle where two surfaces overlapped. Depth
        # sorting, depth shading and layer banding now carry the depth
        # cue that the alpha was standing in for.
        self.floor_alpha = 1.0
        self.show_bed = True
        self.accent = None        # ImVec4 override, or None to follow theme
        self.head_color = None    # None -> white, the highest-contrast default
        self.head_size = 1.0
        self._tp = None

    HOME = (-0.9, -0.5, 1.0)

    def home(self):
        """Back to the framing the view opens with."""
        self.yaw, self.pitch, self.zoom = self.HOME
        self.pan_x = self.pan_y = 0.0
        self._spin = [0.0, 0.0]

    def set_toolpath(self, tp, budget: int | None = None):
        self._tp = tp
        if budget is not None:
            self.budget = int(budget)
        self.rebuild()

    def rebuild(self):
        """Re-decimate. Needed whenever detail or feature visibility change.

        Two versions are built. Full coverage of a real model costs ~110k
        points, which draws fine but costs ~36ms to re-project — so orbiting
        it ran at 11fps. The coarse copy is used only while the camera is
        actually moving; the moment it stops, the full one is projected once
        and cached. Detail where you can see it, speed where you cannot.
        """
        # One display, always full fidelity. A coarse copy for frames where
        # the camera is moving sounded right, but the tolerance it needed to
        # save anything meaningful (5mm) drew chords straight across the
        # model — visible as streaks, and as geometry appearing to vanish,
        # exactly while rotating. Held to a safe 0.6mm it saved 9% of points,
        # which does not pay for a second copy of the geometry.
        self.display = (Display(self._tp, self.budget, visible=self.visible)
                        if self._tp is not None else None)
        self.display_lo = None
        self._ramp_key = None
        self._last_key = None
        self._mesh = None          # geometry changed; re-upload on next draw

    # ------------------------------------------------------------ drawing

    def camera_input(self, hovered, active, io, centre=None):
        """Orbit, pan, zoom, coast and reset, from one frame of
        mouse state. Split out from draw() so it can be driven
        directly: a live GLFW backend overwrites injected mouse
        positions every frame, so testing it through the real
        event queue is not possible.
        """
        # Claim the wheel while the cursor is over the canvas. Without this
        # the wheel zooms AND scrolls the panel behind it, because ImGui
        # scrolls the hovered window inside NewFrame — before any of our
        # code runs, so there is nothing left to "consume" by then. Key
        # ownership is the supported way to say the wheel is spoken for;
        # it takes effect from the next frame, which is fine because the
        # cursor is always over the canvas for a frame before it scrolls.
        if hovered or active:
            imgui.set_item_key_owner(imgui.Key.mouse_wheel_y)

        panning = active and (io.mouse_down[1] or io.mouse_down[2]
                              or io.key_shift)
        if active:
            d = io.mouse_delta
            if abs(d.x) > 0.01 or abs(d.y) > 0.01:
                if panning:
                    self.pan_x += d.x
                    self.pan_y += d.y
                else:
                    self.yaw += d.x * 0.010
                    self.pitch = max(-1.55,
                                     min(1.55, self.pitch + d.y * 0.010))
                    # Velocity for the coast on release. Smoothed, or one
                    # stationary frame at the end of a fast drag reads as a
                    # dead stop and the flick is lost.
                    self._spin[0] += (d.x * 0.010 - self._spin[0]) * 0.35
                    self._spin[1] += (d.y * 0.010 - self._spin[1]) * 0.35
                self.auto_orbit = False
                anim.mark_busy()
            imgui.set_mouse_cursor(imgui.MouseCursor_.resize_all
                                   if panning else imgui.MouseCursor_.hand)
        elif hovered:
            imgui.set_mouse_cursor(imgui.MouseCursor_.hand)

        if hovered and imgui.is_mouse_double_clicked(0):
            self.home()
            anim.mark_busy()

        if hovered and abs(io.mouse_wheel) > 0.0:
            # Ctrl is the fine step: at the default rate one notch is a 12%
            # jump, which overshoots when lining up a close look at a wall.
            rate = 1.04 if io.key_ctrl else 1.12
            was = self.zoom
            self.zoom = max(0.12, min(14.0,
                                      self.zoom * (rate ** io.mouse_wheel)))
            k = self.zoom / was
            if centre is not None and abs(k - 1.0) > 1e-6:
                # Zoom towards the cursor, not towards the middle of the
                # bed. A point on screen sits at  C + pan + w*scale, and
                # scale is proportional to zoom, so holding the point under
                # the cursor still across a zoom of k needs
                #     pan' = (m - C)(1 - k) + k*pan
                # Without this, closing in on a corner of the print walks it
                # off the edge of the panel and you have to pan back every
                # notch.
                m = io.mouse_pos
                self.pan_x = (m.x - centre[0]) * (1.0 - k) + k * self.pan_x
                self.pan_y = (m.y - centre[1]) * (1.0 - k) + k * self.pan_y
            anim.mark_busy()

        if not active and (abs(self._spin[0]) > 1e-4
                           or abs(self._spin[1]) > 1e-4):
            # Frame-rate independent decay, so a coast lasts the same
            # wall-clock time at 9fps as at 120.
            self.yaw += self._spin[0]
            self.pitch = max(-1.55, min(1.55, self.pitch + self._spin[1]))
            k = 0.90 ** (io.delta_time * 60.0)
            self._spin[0] *= k
            self._spin[1] *= k
            anim.mark_busy()

        if self.auto_orbit and not active:
            self.yaw += io.delta_time * self.orbit_speed
            anim.mark_busy()

    def draw(self, size: ImVec2, cur_seg: float, loading=None, live: bool = False):
        t = theme_mod.current()
        dl = imgui.get_window_draw_list()
        p0 = imgui.get_cursor_screen_pos()
        p1 = ImVec2(p0.x + size.x, p0.y + size.y)

        dl.add_rect_filled(p0, p1, imgui.get_color_u32(t.bg), t.rounding_panel)
        # add_rect takes thickness *before* flags in imgui-bundle
        dl.add_rect(p0, p1, imgui.get_color_u32(t.border), t.rounding_panel,
                    t.border_width, 0)

        # Right and middle count as presses on the canvas too, so a pan
        # drag keeps the item active and we get mouse_delta for it.
        #
        # AllowOverlap because the dashboard paints small buttons on top of
        # this one. ImGui gives hover to the FIRST item submitted that
        # contains the cursor and stops looking; this flag is the supported
        # way to say "a later item may take it from me". The caller also
        # submits those buttons first, so either mechanism alone is enough
        # — belt and braces on a control that has read as dead once.
        imgui.set_next_item_allow_overlap()
        imgui.invisible_button(
            f"##{self.key}", size,
            imgui.ButtonFlags_.mouse_button_left.value
            | imgui.ButtonFlags_.mouse_button_right.value
            | imgui.ButtonFlags_.mouse_button_middle.value)
        hovered = imgui.is_item_hovered()
        active = imgui.is_item_active()
        self.camera_input(hovered, active, imgui.get_io(),
                          centre=(p0.x + size.x * 0.5, p0.y + size.y * 0.5))
        # Clamp the pan, but scale the bound with zoom. Keeping a point
        # under the cursor needs a pan that grows like the zoom ratio, so a
        # fixed bound of one panel width is reached after a handful of
        # notches and every notch after that quietly pulls the view back
        # towards the middle — exactly the drift the zoom-to-cursor was
        # meant to remove. A bound that grows with zoom still stops a stray
        # drag from losing the model entirely.
        lim = 1.0 + self.zoom
        self.pan_x = max(-size.x * lim, min(size.x * lim, self.pan_x))
        self.pan_y = max(-size.y * lim, min(size.y * lim, self.pan_y))

        if self.display is None or not len(self.display.pts):
            self._placeholder(dl, p0, p1, loading, t)
            return

        if self.use_gl and glview.HAVE_GL:
            try:
                self._draw_gl(dl, p0, p1, size, cur_seg, live, t, hovered)
                return
            except Exception as exc:                      # pragma: no cover
                # One failure is enough: fall back for the rest of the run
                # rather than throwing from a draw call every frame.
                self.gl_error = str(exc)
                self.use_gl = False
                if self._gl is not None:
                    try:
                        self._gl.release_target()
                    except Exception:
                        pass
                self._gl = None

        # Eased camera: the drag sets a target, the view chases it, so a
        # flick settles instead of stopping dead.
        yaw = anim.to(f"{self.key}:yaw", self.yaw, 20.0)
        pitch = anim.to(f"{self.key}:pitch", self.pitch, 20.0)
        zoom = anim.to(f"{self.key}:zoom", self.zoom, ZOOM_EASE)
        # The sweep is the whole point: ease the segment index so a once-a-
        # second progress push renders as continuous motion.
        shown = anim.to(f"{self.key}:prog", float(cur_seg), 3.5)

        pan_x = anim.to(f"{self.key}:panx", self.pan_x, ZOOM_EASE)
        pan_y = anim.to(f"{self.key}:pany", self.pan_y, ZOOM_EASE)
        cx = p0.x + size.x * 0.5 + pan_x
        cy = p0.y + size.y * 0.52 + pan_y
        base = 0.72 * min(size.x, size.y) / max(self.display.size, 1e-3)
        ppm = base * zoom
        dist = max(self.display.size * 2.6, 1e-3)
        R = _rot(yaw, pitch)

        key = (round(yaw, 4), round(pitch, 4), round(ppm, 4),
               round(cx, 1), round(cy, 1))
        disp = self.display
        disp.project(key, cx, cy, ppm, R, dist)

        dl.push_clip_rect(p0, p1, True)
        if self.show_bed:
            self._grid(dl, cx, cy, ppm, R, dist, t)

        ramp = self._ramp(t)
        col_todo = imgui.get_color_u32(
            theme_mod.with_alpha(t.text_mute, self.ghost))
        cur_z = disp.z_at_seg(shown)
        # Fade over a fraction of the model so recent layers stay legible
        # instead of the whole object glowing uniformly.
        fade = max(disp.size * self.fade, 1.0)
        line_px = max(0.9, min(48.0, self.line_mm * ppm))
        disp.draw(dl, shown, ramp, col_todo, line_px, cur_z, fade)

        h = self._nozzle(shown, cx, cy, ppm, R, dist)
        if h is not None:
            # The nozzle only pulses while printing. A time-based ease
            # rendered at the idle frame rate looks broken, so it must not
            # run when there is nothing to watch.
            if live:
                glow = 0.55 + 0.45 * anim.pulse(1.6)
                anim.mark_busy()
            else:
                glow = 1.0
            # Deliberately NOT the accent: the nozzle sits on top of
            # accent-coloured geometry, so a red dot on red lines is
            # invisible exactly where it matters.
            hot = self.head_color or ImVec4(1.0, 1.0, 1.0, 1.0)
            s = self.head_size
            dl.add_circle_filled(h, 12.0 * s * glow,
                                 imgui.get_color_u32(theme_mod.with_alpha(hot, 0.13)))
            dl.add_circle_filled(h, 6.5 * s * glow,
                                 imgui.get_color_u32(theme_mod.with_alpha(hot, 0.28)))
            # A dark rim keeps the core readable over bright geometry too.
            dl.add_circle_filled(h, 4.2 * s,
                                 imgui.get_color_u32(theme_mod.with_alpha(t.bg, 0.85)))
            dl.add_circle_filled(h, 2.9 * s, imgui.get_color_u32(hot))
        dl.pop_clip_rect()



    # ------------------------------------------------------------- GPU path

    FOV = math.radians(35.0)

    def _camera(self, size_px, zoom, yaw, pitch, pan=(0.0, 0.0)):
        """Eye/matrices for the current orbit. Same yaw/pitch convention as
        the CPU path, so dragging feels identical.

        `pan` arrives in panel pixels. It is converted against the world
        height one pixel covers at the target distance, so the model tracks
        the cursor exactly rather than sliding faster when zoomed out.
        """
        d = self.display
        centre = d.center.astype(np.float32)
        dist = max(d.size * 2.2 / max(zoom, 1e-3), 1e-3)
        eye = centre + np.array([
            math.cos(pitch) * math.cos(yaw) * dist,
            math.cos(pitch) * math.sin(yaw) * dist,
            -math.sin(pitch) * dist], np.float32)

        if pan[0] or pan[1]:
            up = np.array([0, 0, 1], np.float32)
            fwd = centre - eye
            fwd /= max(float(np.linalg.norm(fwd)), 1e-9)
            right = np.cross(fwd, up)
            right /= max(float(np.linalg.norm(right)), 1e-9)
            cam_up = np.cross(right, fwd)
            per_px = (2.0 * dist * math.tan(self.FOV * 0.5)
                      / max(size_px[1], 1.0))
            shift = (right * (-pan[0] * per_px)
                     + cam_up * (pan[1] * per_px)).astype(np.float32)
            centre = centre + shift
            eye = eye + shift

        view = glview.look_at(eye, centre, np.array([0, 0, 1], np.float32))
        aspect = max(size_px[0], 1.0) / max(size_px[1], 1.0)
        proj = glview.perspective(self.FOV, aspect, dist * 0.02, dist * 4.0)
        return eye, (proj @ view).astype(np.float32)

    def _clip_z(self):
        """World height above which nothing draws.

        A sentinel rather than the model's own top when the cutaway is off:
        the boxes stand a little proud of the last layer, so clipping at
        exactly hi.z would shave the top of the print for no reason.
        """
        if self.clip >= 0.999 or self.display is None:
            return 1.0e9
        d = self.display
        lo = float(d.center[2] - d.size * 0.5)
        hi = float(d.center[2] + d.size * 0.5)
        tp = self._tp
        if tp is not None:
            lo, hi = float(tp.lo[2]), float(tp.hi[2])
        return lo + (hi - lo) * float(self.clip)

    @staticmethod
    def _to_screen(mvp, pt, p0, size):
        """World point -> panel pixel, matching the flipped blit below."""
        v = mvp @ np.array([pt[0], pt[1], pt[2], 1.0], np.float32)
        if v[3] <= 1e-6:
            return None
        x = (v[0] / v[3]) * 0.5 + 0.5
        y = (v[1] / v[3]) * 0.5 + 0.5
        return ImVec2(float(p0.x + x * size.x),
                      float(p0.y + (1.0 - y) * size.y))

    def _draw_gl(self, dl, p0, p1, size, cur_seg, live, t, hovered):
        if self._gl is None:
            self._gl = glview.GLScene()
        # Extrusion width is baked into the vertices, so a change to it has
        # to rebuild them — it is a slider, not a uniform.
        if self._mesh is None or self._mesh_lw != self.line_mm:
            self._mesh_lw = self.line_mm
            self._mesh = glview.build_mesh(self._tp, self.display,
                                           self.display.layer_h, self.line_mm)
            self._gl.upload(*self._mesh)
            self._gl.upload_lines(*self._bed_lines())

        yaw = anim.to(f"{self.key}:yaw", self.yaw, 20.0)
        pitch = anim.to(f"{self.key}:pitch", self.pitch, 20.0)
        zoom = anim.to(f"{self.key}:zoom", self.zoom, ZOOM_EASE)
        shown = anim.to(f"{self.key}:prog", float(cur_seg), 3.5)

        pan = (anim.to(f"{self.key}:panx", self.pan_x, ZOOM_EASE),
               anim.to(f"{self.key}:pany", self.pan_y, ZOOM_EASE))
        eye, mvp = self._camera((size.x, size.y), zoom, yaw, pitch, pan)

        # Key light, offset from the view direction so the face being looked
        # at is bright but not flat.
        #
        # It has to point FROM the model back TOWARDS the camera. Built from
        # eye->centre instead, as it was, the light shines the same way you
        # are looking, so dot(n, L) is negative on every surface you can
        # actually see and the whole model shades as if backlit. That went
        # unnoticed for as long as every quad carried the same arbitrary
        # normal — half of those were wrong anyway, so half the model looked
        # lit by luck. Give the geometry correct normals and the sign error
        # becomes the picture: measured on the real model, flipping it took
        # mean luminance 50.4 -> 63.0 and neighbour-pixel noise 6.47 -> 2.45.
        to_eye = eye - self.display.center.astype(np.float32)
        n = float(np.linalg.norm(to_eye))
        to_eye = to_eye / n if n > 1e-9 else np.array([0, -1, 0], np.float32)
        ca, sa = math.cos(0.6), math.sin(0.6)
        light = np.array([to_eye[0] * ca - to_eye[1] * sa,
                          to_eye[0] * sa + to_eye[1] * ca, 0.35], np.float32)
        light /= max(float(np.linalg.norm(light)), 1e-9)

        base = self.accent if self.accent is not None else t.accent
        bright = theme_mod.lerp(base, ImVec4(1, 1, 1, 1), 0.35)
        deep = theme_mod.lerp(base, t.bg, 0.62)
        cur_z = self.display.z_at_seg(shown)

        tex = self._gl.render(
            int(size.x), int(size.y), mvp,
            {
                "u_light": light,
                "u_cur": float(shown),
                "u_zmin": float(self.display.center[2]),
                "u_fade": max(self.display.size * self.fade, 1.0),
                "u_curz": float(cur_z),
                "u_layer": float(self.display.layer_h),
                "u_base": (base.x, base.y, base.z),
                "u_bright": (bright.x, bright.y, bright.z),
                "u_deep": (deep.x, deep.y, deep.z),
                "u_ghost": (t.text_mute.x, t.text_mute.y, t.text_mute.z),
                # The slider was wired to everything except the shader,
                # which had the alpha as a literal 0.10 — so on the GPU
                # path (i.e. always, in practice) moving it did nothing.
                "u_ghosta": float(max(0.0, min(1.0, self.ghost))),
                "u_floor": float(min(max(self.floor_alpha * 0.5, 0.18), 0.9)),
                "u_clipz": self._clip_z(),
            },
            (t.bg.x, t.bg.y, t.bg.z),
            line_color=((t.border.x, t.border.y, t.border.z, 0.85)
                        if self.show_bed else None),
            # `seg` is non-decreasing, so this is where printed ends.
            split=int(np.searchsorted(self._mesh[2], shown, side="right")))

        # A GL framebuffer has its origin bottom-left, ImGui's is top-left.
        dl.add_image(imgui.ImTextureRef(int(tex)), p0, p1,
                     ImVec2(0, 1), ImVec2(1, 0))

        self._nozzle_gl(dl, mvp, p0, size, shown, live, t)

    def _bed_lines(self):
        """The bed plate at z=0, as GL_LINES pairs plus a per-vertex alpha.

        Built in world space and drawn in the same pass as the model, so
        the depth buffer hides the half that is behind the print. Drawn on
        the ImGui list afterwards — as it was — it is a wireframe rectangle
        painted straight across the front of the object.

        The grid inside the outline is deliberately near-invisible. It is
        there to say "this is a surface the print is standing on"; at the
        same weight as the outline it competes with the model instead, and
        a monitor is not a CAD viewport.
        """
        d = self.display
        lo = d.center - d.size * 0.5
        hi = d.center + d.size * 0.5
        x0, y0, x1, y1 = float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])
        segs = [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
                ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]
        alpha = [1.0] * 8
        n = 8
        for i in range(1, n):
            f = i / n
            x = x0 + (x1 - x0) * f
            y = y0 + (y1 - y0) * f
            segs.append(((x, y0), (x, y1)))
            segs.append(((x0, y), (x1, y)))
            alpha += [0.16] * 4
        out = np.zeros((len(segs) * 2, 3), np.float32)
        for i, (a_, b_) in enumerate(segs):
            out[i * 2] = (a_[0], a_[1], 0.0)
            out[i * 2 + 1] = (b_[0], b_[1], 0.0)
        return out, np.asarray(alpha, np.float32)

    def _nozzle_gl(self, dl, mvp, p0, size, shown, live, t):
        tp = self._tp
        if tp is None or len(tp.pts) < 2:
            return
        i = int(shown)
        f = float(shown) - i
        i = max(0, min(i, len(tp.pts) - 2))
        world = tp.pts[i] * (1.0 - f) + tp.pts[i + 1] * f
        h = self._to_screen(mvp, world, p0, size)
        if h is None:
            return
        if live:
            glow = 0.55 + 0.45 * anim.pulse(1.6)
            anim.mark_busy()
        else:
            glow = 1.0
        hot = self.head_color or ImVec4(1.0, 1.0, 1.0, 1.0)
        s = self.head_size
        dl.add_circle_filled(h, 12.0 * s * glow,
                             imgui.get_color_u32(theme_mod.with_alpha(hot, 0.13)))
        dl.add_circle_filled(h, 6.5 * s * glow,
                             imgui.get_color_u32(theme_mod.with_alpha(hot, 0.28)))
        dl.add_circle_filled(h, 4.2 * s,
                             imgui.get_color_u32(theme_mod.with_alpha(t.bg, 0.85)))
        dl.add_circle_filled(h, 2.9 * s, imgui.get_color_u32(hot))

    def _nozzle(self, shown, cx, cy, ppm, R, dist):
        """Nozzle position, taken from the FULL toolpath.

        Deriving it from the drawn geometry looked right until the printer
        entered a run that is decimated away or filtered out — sparse infill
        is hidden by default, and the run budget drops the shortest runs.
        With nothing to land on, the marker parked at the edge of the last
        visible run and sat there until perimeters resumed, which reads as
        the printer having stalled when it has not.

        One point, one projection, so it is free.
        """
        tp = self._tp
        if tp is None or len(tp.pts) < 2:
            return None
        i = int(shown)
        f = float(shown) - i
        i = max(0, min(i, len(tp.pts) - 2))
        p = tp.pts[i] * (1.0 - f) + tp.pts[i + 1] * f
        v = (p - tp.center) @ R.T
        depth = max(float(dist - v[2]), 1e-3)
        s = (ppm * dist) / depth
        return ImVec2(float(cx + v[0] * s), float(cy - v[1] * s))

    # ------------------------------------------------------------ chrome

    def _ramp(self, t):
        """Depth ramp from a sunk, desaturated base up to the live accent.

        Cached per theme+alpha so the per-run lookup in Display.draw is an
        array index rather than a colour computation.
        """
        base = self.accent if self.accent is not None else t.accent
        bright = (theme_mod.lerp(base, imgui.ImVec4(1, 1, 1, 1), 0.35)
                  if self.accent is not None else t.accent_bright)
        dim = (theme_mod.lerp(base, t.bg, 0.55)
               if self.accent is not None else t.accent_dim)
        key = (theme_mod.to_hex(base), theme_mod.to_hex(dim),
               round(self.floor_alpha, 3))
        if getattr(self, "_ramp_key", None) == key:
            return self._ramp_cache
        deep = theme_mod.lerp(dim, t.bg, 0.35)
        out = []
        for band in (0, 1):
            # A layer band is a small brightness offset, not a colour change:
            # enough to see the stack, not enough to look striped.
            band_f = 1.0 if band == 0 else 0.94
            rows = []
            for d in range(DEPTH_STEPS):
                # Far geometry sinks toward the background, which is what
                # gives a rotated model any sense of volume.
                near = 0.52 + 0.48 * (d / (DEPTH_STEPS - 1))
                col = []
                for i in range(HEIGHT_STEPS):
                    f = i / (HEIGHT_STEPS - 1)
                    c = theme_mod.lerp(deep, base, f)
                    if f > 0.88:
                        c = theme_mod.lerp(c, bright, (f - 0.88) / 0.12)
                    c = theme_mod.lerp(t.bg, c, near * band_f)
                    # Low alpha is what stopped hundreds of overlapping
                    # layers saturating; the leading edge stays opaque so the
                    # printing position still reads at a glance.
                    a = self.floor_alpha + (1.0 - self.floor_alpha) * (f ** 1.6)
                    col.append(imgui.get_color_u32(theme_mod.with_alpha(c, a)))
                rows.append(col)
            out.append(rows)
        self._ramp_key = key
        self._ramp_cache = out
        self._bright = bright
        return out

    def _grid(self, dl, cx, cy, ppm, R, dist, t):
        """Bed outline at z=0, so the model has a floor to sit on."""
        d = self.display
        lo, hi = d.center - d.size * 0.5, d.center + d.size * 0.5
        z0 = 0.0
        corners = np.array([
            [lo[0], lo[1], z0], [hi[0], lo[1], z0],
            [hi[0], hi[1], z0], [lo[0], hi[1], z0],
        ], np.float32)
        v = (corners - d.center) @ R.T
        depth = np.maximum(dist - v[:, 2], 1e-3)
        s = (ppm * dist) / depth
        pts = [ImVec2(float(cx + v[i, 0] * s[i]), float(cy - v[i, 1] * s[i]))
               for i in range(4)]
        col = imgui.get_color_u32(theme_mod.with_alpha(t.border, 0.85))
        dl.add_polyline(pts + [pts[0]], col, 1.0, 0)

    def _placeholder(self, dl, p0, p1, loading, t):
        cx = (p0.x + p1.x) * 0.5
        cy = (p0.y + p1.y) * 0.5
        if loading and loading[0] in ("downloading", "parsing"):
            label = f"{loading[0]}  {loading[2] * 100:.0f}%"
            w = 190.0
            x0 = cx - w * 0.5
            dl.add_rect_filled(ImVec2(x0, cy + 10), ImVec2(x0 + w, cy + 14),
                               imgui.get_color_u32(t.surface), 2.0)
            dl.add_rect_filled(ImVec2(x0, cy + 10),
                               ImVec2(x0 + w * max(0.02, loading[2]), cy + 14),
                               imgui.get_color_u32(t.accent), 2.0)
        elif loading and loading[0] == "error":
            label = loading[1]
        else:
            label = ("no active job   ·   drag to orbit, right-drag to "
                     "pan, double-click to reset")
        tw = imgui.calc_text_size(label).x
        dl.add_text(ImVec2(cx - tw * 0.5, cy - 14),
                    imgui.get_color_u32(t.text_mute), label)
