"""Themes Bedside adds to the ones VertexUI ships.

Registered into `theme_mod.BUILTIN` at import, the same way `picons` adds
its icons, so the picker and `list_presets()` find them without either
side knowing about the other.
"""

from __future__ import annotations

from dataclasses import replace

from vertexui import theme as theme_mod

rgb = theme_mod.rgb

# A deep twilight violet on the noir chassis. The accent is much darker
# than the other presets' — it works as a fill, where light text sits on
# top of it at 11.5:1, and not at all as text, where it is 1.45:1 on the
# window ground. That is the split the app already respects: `accent` for
# fills and shapes, `accent_bright` for anything accent-coloured that has
# to be read.
#
# `accent_bright` is set to a genuinely bright value at the same hue rather
# than a multiple of the accent, because scaling a V=43% colour upward by
# the ratio the other presets use lands somewhere still too dark to
# highlight anything.
NOIR_IRIS = replace(
    theme_mod.BUILTIN["noir-red"],
    name="noir-iris",
    accent=rgb("#37186E"),
    accent_bright=rgb("#8D4DFF"),
    accent_dim=rgb("#1E0D3D"),
)

EXTRA = {"noir-iris": NOIR_IRIS}
theme_mod.BUILTIN.update(EXTRA)
