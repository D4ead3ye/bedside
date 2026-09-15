"""Ambient effects drawn behind — and optionally over — the UI.

Deliberately cheap: a fixed particle budget, numpy for the motion, and one
draw call per particle. Anything fancier would be spending a printer
monitor's frame budget on decoration.

Every effect keeps the app at full frame rate while it runs, so "none" stays
the default — see the note in the settings panel.
"""

from __future__ import annotations

import numpy as np
from imgui_bundle import ImVec2, ImVec4, imgui

from vertexui import anim, theme as theme_mod

NAMES = ["none", "fireflies", "starfield", "constellation",
         "rain", "sakura", "sparkles", "bubbles",
         "embers", "orbits", "dust"]

COUNTS = {"fireflies": 70, "starfield": 130, "constellation": 46,
          "rain": 110, "sakura": 55, "sparkles": 80, "bubbles": 46,
          "embers": 80, "orbits": 64, "dust": 120}


class Effects:
    def __init__(self, kind: str = "none", intensity: float = 1.0):
        self.kind = kind if kind in NAMES else "none"
        self.intensity = float(intensity)
        self._kind_built = None
        self._rng = np.random.default_rng(7)
        self._p = None

    def set(self, kind: str):
        self.kind = kind if kind in NAMES else "none"

    # ------------------------------------------------------------ internals

    def _ensure(self, w, h):
        n = COUNTS.get(self.kind, 0)
        if self._kind_built == self.kind and self._p is not None \
                and len(self._p["x"]) == n:
            return
        r = self._rng
        self._p = {
            "x": r.random(n).astype(np.float32),
            "y": r.random(n).astype(np.float32),
            "vx": (r.random(n).astype(np.float32) - 0.5),
            "vy": (r.random(n).astype(np.float32) - 0.5),
            "r": (0.4 + 0.6 * r.random(n)).astype(np.float32),
            "ph": (r.random(n) * 6.283).astype(np.float32),
        }
        self._kind_built = self.kind

    # -------------------------------------------------------------- drawing

    def draw(self, dl, x0, y0, x1, y1, now, dt):
        """Advance and paint in one call — the background pass."""
        if self.step(now, dt):
            self.paint(dl, x0, y0, x1, y1, now, 1.0)

    def step(self, now, dt):
        """Advance the simulation. Returns False when there is nothing to
        draw, so callers can skip the paint.

        Motion is separate from painting because the effect is painted
        twice a frame: once behind the panels and once, fainter, over them.
        Stepping it twice would run everything at double speed.
        """
        if self.kind == "none":
            return False
        self._ensure(1.0, 1.0)
        p = self._p
        if p is None or not len(p["x"]):
            return False

        dt = min(0.05, max(0.001, dt))
        k = self.kind
        # Motion is animation, so the app must not idle while it runs.
        anim.mark_busy()

        if k == "fireflies":
            p["x"] += p["vx"] * dt * 0.016
            p["y"] += p["vy"] * dt * 0.016
            np.mod(p["x"], 1.0, out=p["x"])
            np.mod(p["y"], 1.0, out=p["y"])
        elif k == "constellation":
            p["x"] += p["vx"] * dt * 0.010
            p["y"] += p["vy"] * dt * 0.010
            np.mod(p["x"], 1.0, out=p["x"])
            np.mod(p["y"], 1.0, out=p["y"])
        elif k == "rain":
            p["y"] += (0.32 + 0.5 * p["r"]) * dt
            np.mod(p["y"], 1.0, out=p["y"])
        elif k == "sakura":
            p["y"] += (0.045 + 0.05 * p["r"]) * dt
            p["x"] += np.sin(now * 0.8 + p["ph"]) * 0.020 * dt
            np.mod(p["y"], 1.0, out=p["y"])
            np.mod(p["x"], 1.0, out=p["x"])
        elif k == "bubbles":
            p["y"] -= (0.030 + 0.045 * p["r"]) * dt
            p["y"] = np.where(p["y"] < 0.0, 1.0, p["y"])
            p["x"] += np.sin(now * 0.6 + p["ph"]) * 0.012 * dt
            np.mod(p["x"], 1.0, out=p["x"])
        elif k == "embers":
            # Rising and wandering, the way something hot actually drifts.
            p["y"] -= (0.020 + 0.055 * p["r"]) * dt
            p["x"] += np.sin(now * 1.1 + p["ph"] * 3.0) * 0.045 * dt
            reborn = p["y"] < -0.02
            p["y"] = np.where(reborn, 1.02, p["y"])
            np.mod(p["x"], 1.0, out=p["x"])
        elif k == "dust":
            # Three depths at different speeds. Parallax is the whole
            # trick: identical motes at one speed read as noise.
            p["x"] += p["vx"] * (0.004 + 0.016 * p["r"]) * dt
            p["y"] += p["vy"] * (0.004 + 0.016 * p["r"]) * dt
            np.mod(p["x"], 1.0, out=p["x"])
            np.mod(p["y"], 1.0, out=p["y"])
        # "orbits" needs no integration: position is a closed form of the
        # clock, so it can never drift out of shape however long it runs.
        return True

    def paint(self, dl, x0, y0, x1, y1, now, scale=1.0):
        """Draw the current state. `scale` multiplies every alpha, which is
        how the same particles read as ambience over a card and as a
        background behind it."""
        if self.kind == "none" or scale <= 0.004:
            return
        p = self._p
        if p is None or not len(p["x"]):
            return
        w, h = max(1.0, x1 - x0), max(1.0, y1 - y0)
        t = theme_mod.current()
        k = self.kind
        amp = self.intensity * scale

        if k == "fireflies":
            a = 0.30 + 0.30 * np.sin(now * 1.4 + p["ph"])
            self._dots(dl, x0, y0, w, h, p, a * amp, t.accent, 1.7)

        elif k == "starfield":
            a = 0.22 + 0.34 * np.sin(now * 2.1 + p["ph"]) ** 2
            self._dots(dl, x0, y0, w, h, p, a * amp, t.text, 1.15)

        elif k == "constellation":
            self._links(dl, x0, y0, w, h, p, t, scale)
            self._dots(dl, x0, y0, w, h, p,
                       np.full(len(p["x"]), 0.42) * amp, t.accent, 1.5)

        elif k == "rain":
            col = imgui.get_color_u32(
                theme_mod.with_alpha(t.info, 0.20 * amp))
            xs = x0 + p["x"] * w
            ys = y0 + p["y"] * h
            ln = 9.0 + 12.0 * p["r"]
            for i in range(len(xs)):
                dl.add_line(ImVec2(float(xs[i]), float(ys[i])),
                            ImVec2(float(xs[i]) + 1.2, float(ys[i] + ln[i])),
                            col, 1.0)

        elif k == "sakura":
            self._dots(dl, x0, y0, w, h, p,
                       np.full(len(p["x"]), 0.34) * amp,
                       theme_mod.lerp(t.accent, t.text, 0.35), 2.4)

        elif k == "sparkles":
            a = np.clip(np.sin(now * 2.6 + p["ph"] * 3.0), 0, 1) ** 6
            self._dots(dl, x0, y0, w, h, p, a * 0.75 * amp,
                       getattr(t, "accent_bright", t.accent), 1.9)

        elif k == "bubbles":
            xs = x0 + p["x"] * w
            ys = y0 + p["y"] * h
            rr = 3.0 + 9.0 * p["r"]
            col = imgui.get_color_u32(
                theme_mod.with_alpha(t.info, 0.16 * amp))
            for i in range(len(xs)):
                dl.add_circle(ImVec2(float(xs[i]), float(ys[i])),
                              float(rr[i]), col, 0, 1.0)

        elif k == "embers":
            a = (0.30 + 0.45 * np.abs(np.sin(now * 2.2 + p["ph"] * 5.0))) \
                * (0.35 + 0.65 * p["y"])          # dimmer as they rise
            xs = x0 + p["x"] * w
            ys = y0 + p["y"] * h
            rr = (0.5 + p["r"]) * 1.8
            warm = theme_mod.lerp(t.accent, getattr(t, "warn", t.accent), 0.45)
            for i in range(len(xs)):
                al = float(a[i]) * amp
                if al <= 0.01:
                    continue
                c = ImVec2(float(xs[i]), float(ys[i]))
                dl.add_circle_filled(c, float(rr[i]) * 2.6,
                                     imgui.get_color_u32(
                                         theme_mod.with_alpha(warm, al * 0.18)))
                dl.add_circle_filled(c, float(rr[i]),
                                     imgui.get_color_u32(
                                         theme_mod.with_alpha(warm, al)))

        elif k == "orbits":
            # Each particle rides its own circle; neighbours on the same
            # ring get joined, which is what makes it read as a system
            # rather than as scattered dots.
            ang = now * (0.25 + 0.5 * p["r"]) + p["ph"]
            rad = (0.04 + 0.16 * p["r"]) * min(w, h)
            cx = x0 + p["x"] * w
            cy = y0 + p["y"] * h
            xs = cx + np.cos(ang) * rad
            ys = cy + np.sin(ang) * rad * 0.6
            col = imgui.get_color_u32(
                theme_mod.with_alpha(t.accent, 0.10 * amp))
            for i in range(len(xs)):
                dl.add_circle(ImVec2(float(cx[i]), float(cy[i])),
                              float(rad[i]), col, 0, 1.0)
            self._dots(dl, 0.0, 0.0, 1.0, 1.0,
                       {"x": xs, "y": ys, "r": p["r"]},
                       np.full(len(xs), 0.55) * amp, t.accent_bright, 1.6)

        elif k == "dust":
            a = (0.10 + 0.30 * p["r"]) * amp
            self._dots(dl, x0, y0, w, h, p, np.full(len(p["x"]), 1.0) * a,
                       t.text, 1.0 + 1.6 * float(np.mean(p["r"])))

    # ----------------------------------------------------------- primitives

    def _dots(self, dl, x0, y0, w, h, p, alpha, colour, scale):
        xs = x0 + p["x"] * w
        ys = y0 + p["y"] * h
        rr = (0.6 + p["r"]) * scale
        a = np.clip(alpha, 0.0, 1.0)
        for i in range(len(xs)):
            if a[i] <= 0.01:
                continue
            dl.add_circle_filled(
                ImVec2(float(xs[i]), float(ys[i])), float(rr[i]),
                imgui.get_color_u32(theme_mod.with_alpha(colour, float(a[i]))))

    def _links(self, dl, x0, y0, w, h, p, t, scale=1.0):
        """Join near neighbours. O(n^2) but n is small by construction."""
        xs = x0 + p["x"] * w
        ys = y0 + p["y"] * h
        dx = xs[:, None] - xs[None, :]
        dy = ys[:, None] - ys[None, :]
        d = np.hypot(dx, dy)
        lim = min(w, h) * 0.16
        ii, jj = np.nonzero((d < lim) & (d > 0))
        for i, j in zip(ii, jj):
            if j <= i:
                continue
            a = 0.20 * (1.0 - d[i, j] / lim) * self.intensity * scale
            dl.add_line(ImVec2(float(xs[i]), float(ys[i])),
                        ImVec2(float(xs[j]), float(ys[j])),
                        imgui.get_color_u32(theme_mod.with_alpha(t.accent, a)),
                        1.0)
