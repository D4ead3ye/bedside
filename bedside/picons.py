"""Printer-specific icons, drawn from primitives.

Same contract as VertexUI's own icon sheet — `(dl, cx, cy, r, col)` with
(cx, cy) the centre and r the half-size — and registered into `icons.ICONS`
so `icons.draw(...)` and `widgets.button(icon=...)` find them too.

These live here rather than in the toolkit because a hotend and a heated bed
are not general UI furniture.
"""

from __future__ import annotations

import math

from imgui_bundle import ImVec2, imgui

from vertexui import icons as vicons


def _p(cx, cy, dx, dy):
    return ImVec2(cx + dx, cy + dy)


def _polar(cx, cy, ang, dist):
    return ImVec2(cx + math.cos(ang) * dist, cy + math.sin(ang) * dist)


# ------------------------------------------------------------- printer parts

def hotend(dl, cx, cy, r, col):
    """Filament stem, heat block, tapering nozzle, tip."""
    w = max(1.2, r * 0.16)
    # filament coming in
    dl.add_line(_p(cx, cy, 0, -r), _p(cx, cy, 0, -r * 0.72), col, w)
    # heat block
    dl.add_rect_filled(_p(cx, cy, -r * 0.58, -r * 0.72),
                       _p(cx, cy, r * 0.58, -r * 0.06), col, r * 0.12)
    # nozzle cone
    dl.add_triangle_filled(_p(cx, cy, -r * 0.5, -r * 0.06),
                           _p(cx, cy, r * 0.5, -r * 0.06),
                           _p(cx, cy, 0, r * 0.66), col)
    # tip
    dl.add_rect_filled(_p(cx, cy, -r * 0.12, r * 0.52),
                       _p(cx, cy, r * 0.12, r * 0.86), col)


def bed(dl, cx, cy, r, col):
    """Heated plate on legs, with heat rising off it."""
    w = max(1.2, r * 0.15)
    # plate
    dl.add_rect_filled(_p(cx, cy, -r * 0.92, r * 0.30),
                       _p(cx, cy, r * 0.92, r * 0.56), col, r * 0.09)
    # legs
    dl.add_rect_filled(_p(cx, cy, -r * 0.66, r * 0.56),
                       _p(cx, cy, -r * 0.46, r * 0.9), col)
    dl.add_rect_filled(_p(cx, cy, r * 0.46, r * 0.56),
                       _p(cx, cy, r * 0.66, r * 0.9), col)
    # three rising heat curves
    for i, dx in enumerate((-0.52, 0.0, 0.52)):
        pts = []
        for k in range(9):
            f = k / 8.0
            ty = r * 0.16 - f * r * 1.02
            tx = dx * r + math.sin(f * 5.2 + i * 1.1) * r * 0.13
            pts.append(_p(cx, cy, tx, ty))
        dl.add_polyline(pts, col, w, 0)


def fan_at(dl, cx, cy, r, col, angle: float = 0.0, blades: int = 3):
    """Housing, hub and swept blades. `angle` in radians drives the spin."""
    dl.add_circle(ImVec2(cx, cy), r * 0.95, col, 0, max(1.2, r * 0.13))
    for i in range(blades):
        a = angle + i * (2.0 * math.pi / blades)
        dl.add_triangle_filled(
            ImVec2(cx, cy),
            _polar(cx, cy, a, r * 0.82),
            _polar(cx, cy, a + 0.86, r * 0.60),
            col)
    dl.add_circle_filled(ImVec2(cx, cy), r * 0.17, col)


def fan(dl, cx, cy, r, col):
    fan_at(dl, cx, cy, r, col, 0.0)


# ------------------------------------------------------------- settings tabs

def cube(dl, cx, cy, r, col):
    """Isometric box — the 3D view."""
    w = max(1.2, r * 0.15)
    top = [_p(cx, cy, 0, -r * 0.9), _p(cx, cy, r * 0.82, -r * 0.45),
           _p(cx, cy, 0, 0), _p(cx, cy, -r * 0.82, -r * 0.45)]
    dl.add_polyline(top + [top[0]], col, w, 0)
    dl.add_line(_p(cx, cy, -r * 0.82, -r * 0.45),
                _p(cx, cy, -r * 0.82, r * 0.42), col, w)
    dl.add_line(_p(cx, cy, r * 0.82, -r * 0.45),
                _p(cx, cy, r * 0.82, r * 0.42), col, w)
    dl.add_line(_p(cx, cy, 0, 0), _p(cx, cy, 0, r * 0.88), col, w)
    dl.add_line(_p(cx, cy, -r * 0.82, r * 0.42),
                _p(cx, cy, 0, r * 0.88), col, w)
    dl.add_line(_p(cx, cy, r * 0.82, r * 0.42),
                _p(cx, cy, 0, r * 0.88), col, w)


def terminal(dl, cx, cy, r, col):
    w = max(1.2, r * 0.15)
    dl.add_rect(_p(cx, cy, -r * 0.92, -r * 0.72), _p(cx, cy, r * 0.92, r * 0.72),
                col, r * 0.16, w, 0)
    dl.add_line(_p(cx, cy, -r * 0.5, -r * 0.28), _p(cx, cy, -r * 0.16, 0),
                col, w)
    dl.add_line(_p(cx, cy, -r * 0.16, 0), _p(cx, cy, -r * 0.5, r * 0.28),
                col, w)
    dl.add_line(_p(cx, cy, r * 0.02, r * 0.3), _p(cx, cy, r * 0.55, r * 0.3),
                col, w)


def palette(dl, cx, cy, r, col):
    dl.add_circle(ImVec2(cx, cy), r * 0.88, col, 0, max(1.2, r * 0.14))
    for ang in (-2.3, -1.2, -0.1):
        dl.add_circle_filled(_polar(cx, cy, ang, r * 0.48), r * 0.17, col)
    dl.add_circle_filled(_polar(cx, cy, 1.1, r * 0.45), r * 0.24, col)


def sliders(dl, cx, cy, r, col):
    w = max(1.2, r * 0.14)
    for i, (y, knob) in enumerate(((-0.5, -0.3), (0.0, 0.35), (0.5, 0.0))):
        dl.add_line(_p(cx, cy, -r * 0.85, r * y), _p(cx, cy, r * 0.85, r * y),
                    col, w)
        dl.add_circle_filled(_p(cx, cy, r * knob, r * y), r * 0.21, col)


def printer(dl, cx, cy, r, col):
    w = max(1.2, r * 0.15)
    dl.add_rect(_p(cx, cy, -r * 0.9, -r * 0.85), _p(cx, cy, r * 0.9, r * 0.85),
                col, r * 0.14, w, 0)
    dl.add_line(_p(cx, cy, -r * 0.9, -r * 0.25), _p(cx, cy, r * 0.9, -r * 0.25),
                col, w)
    dl.add_rect_filled(_p(cx, cy, -r * 0.22, -r * 0.66),
                       _p(cx, cy, r * 0.22, -r * 0.36), col, r * 0.06)
    dl.add_rect_filled(_p(cx, cy, -r * 0.55, r * 0.42),
                       _p(cx, cy, r * 0.55, r * 0.56), col, r * 0.05)


def upload(dl, cx, cy, r, col):
    """An arrow rising out of a tray."""
    w = r * 0.72
    # tray
    dl.add_line(_p(cx, cy, -w, r * 0.62), _p(cx, cy, -w, r * 0.95), col, 1.6)
    dl.add_line(_p(cx, cy, -w, r * 0.95), _p(cx, cy, w, r * 0.95), col, 1.6)
    dl.add_line(_p(cx, cy, w, r * 0.62), _p(cx, cy, w, r * 0.95), col, 1.6)
    # shaft and head
    dl.add_line(_p(cx, cy, 0, r * 0.55), _p(cx, cy, 0, -r * 0.9), col, 1.7)
    dl.add_triangle_filled(_p(cx, cy, 0, -r * 1.02),
                           _p(cx, cy, -r * 0.46, -r * 0.42),
                           _p(cx, cy, r * 0.46, -r * 0.42), col)


def trash(dl, cx, cy, r, col):
    """Lid, can and two staves."""
    w = r * 0.62
    dl.add_line(_p(cx, cy, -r * 0.88, -r * 0.55),
                _p(cx, cy, r * 0.88, -r * 0.55), col, 1.7)
    # the handle on the lid
    dl.add_line(_p(cx, cy, -r * 0.3, -r * 0.55),
                _p(cx, cy, -r * 0.24, -r * 0.88), col, 1.5)
    dl.add_line(_p(cx, cy, -r * 0.24, -r * 0.88),
                _p(cx, cy, r * 0.24, -r * 0.88), col, 1.5)
    dl.add_line(_p(cx, cy, r * 0.24, -r * 0.88),
                _p(cx, cy, r * 0.3, -r * 0.55), col, 1.5)
    # the can, tapering
    dl.add_line(_p(cx, cy, -w, -r * 0.4), _p(cx, cy, -w * 0.76, r * 0.95),
                col, 1.6)
    dl.add_line(_p(cx, cy, w, -r * 0.4), _p(cx, cy, w * 0.76, r * 0.95),
                col, 1.6)
    dl.add_line(_p(cx, cy, -w * 0.76, r * 0.95),
                _p(cx, cy, w * 0.76, r * 0.95), col, 1.6)
    for dx in (-r * 0.2, r * 0.2):
        dl.add_line(_p(cx, cy, dx, -r * 0.18), _p(cx, cy, dx, r * 0.66),
                    col, 1.2)


# Registered so icons.draw() and widgets.button(icon=...) can find them.
EXTRA = {
    "hotend": hotend, "bed": bed, "fan": fan, "cube": cube,
    "terminal": terminal, "palette": palette, "sliders": sliders,
    "printer": printer, "upload": upload, "trash": trash,
}
vicons.ICONS.update(EXTRA)
