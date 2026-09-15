"""Themes Bedside adds to the ones VertexUI ships.

Registered into `theme_mod.BUILTIN` at import, the same way `picons` adds
its icons, so the picker and `list_presets()` find them without either
side knowing about the other.
"""

from __future__ import annotations

from dataclasses import replace

from vertexui import theme as theme_mod

rgb = theme_mod.rgb

# A violet on the noir chassis, at the same hue (262°) as the deeper
# #37186E it started as but taken up to V=93%, which puts it in the same
# brightness band as the other presets' accents rather than well below
# them. Light text on it lands at 4.75:1, so a primary button reads.
#
# `accent_bright` and `accent_dim` keep roughly the value relationship
# noir-red uses between the three — bright a little lighter and less
# saturated, dim at about 0.42 of the accent's value.
NOIR_IRIS = replace(
    theme_mod.BUILTIN["noir-red"],
    name="noir-iris",
    accent=rgb("#7C3AED"),
    accent_bright=rgb("#9E65FF"),
    accent_dim=rgb("#321564"),
)

EXTRA = {"noir-iris": NOIR_IRIS}
theme_mod.BUILTIN.update(EXTRA)
