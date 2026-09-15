"""Bedside — layout and frame loop.

Settings, theming, sound, log scrolling and toasts all come from VertexUI;
this module is the printer-specific part on top of it.
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from dataclasses import replace

from imgui_bundle import ImVec2, ImVec4, hello_imgui, imgui

import vertexui as vui
from vertexui import anim, fonts, icons, logview, settings as vsettings
from vertexui import sound, theme as theme_mod, widgets

from . import (__version__, bg as bgmod, effects, files as filestore,
               gcode, pick, picons, sounds)
from .client import (OctoClient, classify_line, is_motion_command,
                     normalise_host)
from .gcode import Loader
from .view3d import DEFAULT_VISIBLE, ToolpathView, sample_toolpath

GAP = 10.0

# The side panel, in order. Each is switchable: a machine you watch all day
# does not need the same instruments as one you glance at.
PANEL_SECTIONS = ("temps", "graph", "controls", "job", "model")
PANEL_LABELS = {"temps": "temperature cards", "graph": "temperature graph",
                "controls": "temperature controls", "job": "job buttons",
                "model": "model card"}

# Face files are looked up in order, so a machine missing one falls through
# to the next rather than rendering in ImGui's bitmap default.
# (regular candidates, bold candidates). Segoe UI Variable is the modern
# Windows face and is noticeably cleaner than plain Segoe UI at small sizes;
# it is a variable font, so the bold role falls to Semibold, which pairs
# with it correctly.
UI_FACES = {
    "Segoe UI Variable": (("SegUIVar.ttf", "segoeui.ttf"),
                          ("seguisb.ttf", "segoeuib.ttf")),
    "Segoe UI": (("segoeui.ttf",), ("seguisb.ttf", "segoeuib.ttf")),
    "Bahnschrift": (("bahnschrift.ttf",), ("bahnschrift.ttf",)),
    "Calibri": (("calibri.ttf",), ("calibrib.ttf",)),
    "Tahoma": (("tahoma.ttf",), ("tahomabd.ttf",)),
    "Verdana": (("verdana.ttf",), ("verdanab.ttf",)),
}

# The readouts and micro-labels use their own face. One face doing every
# job is most of what makes an interface look like a default: a body face
# set large is just big body text, where a display face set large reads as
# an instrument. (candidates, size scale) — the scale exists because these
# faces disagree about how much of the em the x-height should take, so a
# matched em size is not a matched apparent size.
DISPLAY_FACES = {
    # DIN-derived, technical, and the reason the panel stops looking stock.
    "Bahnschrift": (("bahnschrift.ttf",), 0.88),
    "Cascadia Mono": (("CascadiaMono.ttf", "consola.ttf"), 0.92),
    "Match interface": (None, 1.0),
}

MONO_FACES = {
    "Cascadia Mono": ("CascadiaMono.ttf", "consola.ttf"),
    "Consolas": ("consola.ttf",),
    "Lucida Console": ("lucon.ttf", "consola.ttf"),
    "Courier New": ("cour.ttf",),
}


def build_faces(extras):
    """Font roles for vui.install, honouring the saved choices.

    A wider spread between the roles than the toolkit's defaults: readouts
    want to dominate, micro-labels want to recede, and the gap between them
    is most of what stops an interface looking generic.
    """
    ui, bold = UI_FACES.get(extras.get("face_ui", ""),
                            UI_FACES["Segoe UI Variable"])
    disp, scale = DISPLAY_FACES.get(extras.get("face_display", ""),
                                    DISPLAY_FACES["Bahnschrift"])
    if disp is None:                       # "Match interface"
        disp, scale = bold, 1.0
    mono = MONO_FACES.get(extras.get("face_mono", ""),
                          MONO_FACES["Cascadia Mono"])
    return {
        "ui": (ui, 17.5),
        "semi": (bold, 17.5),
        "title": (disp, 23.0 * scale),
        "mono": (mono, 14.0),
        "label": (disp, 13.5 * scale),     # the tracked uppercase labels
        "big": (disp, 27.0 * scale),
        "huge": (disp, 37.0 * scale),
    }


# Tuned defaults for the 3D view. Bumping VIEW_PRESET_VERSION re-applies
# them once on next launch, which is how an existing settings.json picks up
# a retune without the user hunting through sliders.
RECOMMENDED_VIEW = {
    "detail": 24000,        # perf headroom is large; smoother curves
    "line_mm": 0.52,        # a touch over nominal, so beads read as solid
    "fade": 0.42,           # more of the model stays bright
    "floor_alpha": 1.0,     # solid rather than translucent
    "ghost": 0.05,          # unprinted stays a hint, not a box
    "head_size": 1.2,
    "show_bed": True,
    "auto_orbit": False,
    # Whitelisted by id, so a feature added later would be invisible to an
    # existing settings file unless the migration re-writes this list.
    "features": sorted(DEFAULT_VISIBLE),
}
VIEW_PRESET_VERSION = 5

SETTINGS_TABS = ["Appearance", "3D View", "Terminal", "Alerts",
                 "Printer", "About"]
TAB_BLURB = {
    "Appearance": "Colours, shape and type for the whole interface.",
    "3D View": "What the toolpath shows, and how it is drawn.",
    "Terminal": "The G-code log — scrolling and what gets filtered out.",
    "Alerts": "Sounds and desktop notifications.",
    "Printer": "Connection, panel sizes and saved themes.",
    "About": "Live performance numbers and where files live.",
}

# Terminal noise filters. Each class of line the printer chatters is off by
# default and switchable; the key is what the setting is stored under.
LOG_KEYS = {"move": "log_show_moves", "ack": "log_show_acks",
            "temp": "log_show_temps", "sd": "log_show_sd",
            "busy": "log_show_busy"}
LOG_LABELS = (("moves", "move"), ("acks", "ack"), ("temps", "temp"),
              ("sd", "sd"), ("busy", "busy"))


def caps(dl, x, y, text, col, track=1.3):
    """A micro-label in tracked capitals, drawn a glyph at a time.

    ImGui has no letter-spacing. These labels are three to seven characters
    each and the tracking is most of what separates a laid-out panel from a
    stack of default text, so it is worth the loop.
    """
    for ch in text:
        dl.add_text(ImVec2(x, y), col, ch)
        x += imgui.calc_text_size(ch).x + track
    return x


def caps_width(text, track=1.3):
    return sum(imgui.calc_text_size(c).x + track for c in text) - track


# How the panel's cards are drawn. Four genuinely different grounds
# rather than four names for the same one: the choice changes whether the
# card has a fill at all, which is what decides how much of the background
# effect comes through the panel.
CARD_STYLES = ("raised", "plated", "outlined", "flat")
CARD_STYLE = "raised"
CARD_SHADOW = True


def set_card_style(name, shadow=True):
    global CARD_STYLE, CARD_SHADOW
    CARD_STYLE = name if name in CARD_STYLES else "raised"
    CARD_SHADOW = bool(shadow)


def grad_fill(dl, x, y, w, h, t, top, bottom, rounding=None):
    """A rounded rect with a vertical gradient.

    ImGui's own multi-colour rect is square-cornered, which is why this
    looked impossible at first. The way through is to draw the rounded fill
    normally and then re-colour the vertices it just emitted:
    ShadeVertsLinearColorGradientKeepAlpha lerps their RGB along an axis and
    leaves alpha alone, so the anti-aliased corner fringe keeps its
    coverage and picks up the gradient with everything else.
    """
    r = t.rounding if rounding is None else rounding
    i0 = dl.vtx_buffer.size()
    dl.add_rect_filled(ImVec2(x, y), ImVec2(x + w, y + h),
                       imgui.get_color_u32(top), r)
    i1 = dl.vtx_buffer.size()
    if i1 > i0:
        imgui.internal.shade_verts_linear_color_gradient_keep_alpha(
            dl, i0, i1, ImVec2(x, y), ImVec2(x, y + h),
            imgui.get_color_u32(top), imgui.get_color_u32(bottom))


def plate(dl, x, y, w, h, t, edge=None):
    """Card background, in whichever style is selected.

    raised    gradient fill, border, a hairline of light along the inside
              of the top edge, and a soft drop shadow
    plated    flat fill, border, accent rule along the top
    outlined  border only — the background effect shows straight through
    flat      fill only, no edges at all
    """
    r = t.rounding
    p0, p1 = ImVec2(x, y), ImVec2(x + w, y + h)

    if CARD_SHADOW and CARD_STYLE in ("raised", "plated"):
        # Three stacked rounded rects, each fainter and larger. Cheaper
        # than a blur and, at this size, indistinguishable from one.
        for i, a_ in ((3.0, 0.16), (6.0, 0.09), (10.0, 0.05)):
            dl.add_rect_filled(
                ImVec2(x + 1.0, y + i * 0.5), ImVec2(x + w - 1.0, y + h + i),
                imgui.get_color_u32(theme_mod.with_alpha(t.bg, a_)), r + i)

    if CARD_STYLE == "raised":
        grad_fill(dl, x, y, w, h, t,
                  theme_mod.lerp(t.surface, t.text, 0.055),
                  theme_mod.lerp(t.surface, t.bg, 0.35))
    elif CARD_STYLE != "outlined":
        dl.add_rect_filled(p0, p1, imgui.get_color_u32(t.surface), r)

    if CARD_STYLE in ("raised", "plated", "outlined"):
        dl.add_rect(p0, p1,
                    imgui.get_color_u32(theme_mod.with_alpha(t.border, 0.9)),
                    r, 1.0)

    if CARD_STYLE == "raised":
        dl.add_line(ImVec2(x + r, y + 1.0), ImVec2(x + w - r, y + 1.0),
                    imgui.get_color_u32(
                        theme_mod.with_alpha(t.text, 0.07)), 1.0)

    if edge is not None and CARD_STYLE in ("plated", "raised"):
        thick = 2.0 if CARD_STYLE == "plated" else 1.5
        dl.add_rect_filled(
            ImVec2(x + r + 1, y), ImVec2(x + w - r - 1, y + thick),
            imgui.get_color_u32(theme_mod.with_alpha(edge, 0.9)), 1.0)


HEAD_H = 27.0


def card_head(dl, x, y, w, t, icon, title, right=None, right_col=None,
              dot=None):
    """A card's own header: icon, tracked title, hairline, optional status.

    The column used to be organised by `widgets.section` — a tick, a label
    and a rule floating between the cards. That reads as captions *around*
    boxes; a header inside the card reads as an instrument with a name on
    it, which is what the temperature cards at the top of the column
    already looked like.
    """
    mute = imgui.get_color_u32(t.text_mute)
    cy = y + 13.0
    if icon:
        icons.draw(icon, dl, x + 14, cy, 6.5,
                   imgui.get_color_u32(theme_mod.with_alpha(t.accent, 0.9)))
    with fonts.use("label"):
        caps(dl, x + 27, y + 7, title, imgui.get_color_u32(t.text_dim), 1.5)
        if right:
            rw = caps_width(right, 1.3)
            rx = x + w - 14 - rw
            if dot is not None:
                dl.add_circle_filled(ImVec2(rx - 11, cy), 3.2,
                                     imgui.get_color_u32(dot))
            caps(dl, rx, y + 7,
                 right, imgui.get_color_u32(right_col or t.text_mute), 1.3)
    dl.add_line(ImVec2(x + 12, y + HEAD_H - 1), ImVec2(x + w - 12, y + HEAD_H - 1),
                imgui.get_color_u32(theme_mod.with_alpha(t.border, 0.75)), 1.0)
    return y + HEAD_H


def scrim(dl, x, y, w, h, t, amount, rounding=0.0):
    """Lay the theme background back over the backdrop at `amount`.

    The cards look after themselves — they have a fill. What does not is
    bare text sitting straight on the shader: the settings screen is almost
    entirely that, and the job strip and the terminal bar are on the
    dashboard. Worse, the backdrop's vignette *brightens* the edges of the
    window, which is exactly where left-aligned text lives, so the busiest
    part of the picture lands under the smallest, dimmest type.

    A wash of the background colour is enough. The scene still moves
    through it, so nothing is lost but contrast that was in the way.
    """
    if amount <= 0.004:
        return
    dl.add_rect_filled(
        ImVec2(x, y), ImVec2(x + w, y + h),
        imgui.get_color_u32(theme_mod.with_alpha(t.bg, min(1.0, amount))),
        rounding)


def meter(dl, x0, x1, y, frac, col, t, h=4.0):
    """Track plus fill. Used for heat-up progress and fan duty."""
    dl.add_rect_filled(ImVec2(x0, y), ImVec2(x1, y + h),
                       imgui.get_color_u32(theme_mod.with_alpha(t.bg, 0.9)),
                       h * 0.5)
    f = max(0.0, min(1.0, frac))
    if f > 0.001:
        dl.add_rect_filled(ImVec2(x0, y), ImVec2(x0 + (x1 - x0) * f, y + h),
                           imgui.get_color_u32(col), h * 0.5)


def fmt_secs(s):
    if s is None or s < 0:
        return "—"
    s = int(s)
    h, m = divmod(s // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s % 60:02d}s"


def fmt_clock(s):
    if s is None or s < 0:
        return "—"
    return time.strftime("%H:%M", time.localtime(time.time() + s))


class Pairing:
    """Application-Keys handshake on a worker thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"        # idle | waiting | done | error
        self.message = ""
        self.key = ""
        self._thread = None

    def start(self, host):
        if self._thread and self._thread.is_alive():
            return
        with self.lock:
            self.state = "waiting"
            self.message = "asking OctoPrint…"
            self.key = ""
        self._thread = threading.Thread(target=self._run, args=(host,),
                                        daemon=True, name="octo-pair")
        self._thread.start()

    def _set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def _run(self, host):
        try:
            token = OctoClient.appkey_request(host)
            self._set(message="click Allow in the OctoPrint web UI…")
            for _ in range(120):
                time.sleep(1.0)
                key = OctoClient.appkey_poll(host, token)
                if key:
                    self._set(state="done", key=key, message="paired")
                    return
            self._set(state="error", message="timed out waiting for approval")
        except Exception as exc:
            self._set(state="error", message=str(exc))

    def snapshot(self):
        with self.lock:
            return self.state, self.message, self.key


def _adopt_old_settings(old="octoview", new="bedside"):
    """Carry a previous install's settings across a rename.

    `config_dir` creates the folder it is asked for, so this has to run and
    finish before anything touches the new name — one call to it and the
    "has this user been here before" test is gone. Everything in there is
    the user's: the paired API key, saved themes, custom cue wavs and every
    tuned slider. Losing it silently to a rename would be a poor trade for
    a nicer word on a repo.
    """
    import os
    import shutil
    base = os.environ.get("APPDATA")
    if not base:
        return
    src, dst = os.path.join(base, old), os.path.join(base, new)
    if not os.path.isfile(os.path.join(src, "settings.json")):
        return
    # The test is "are there settings here already", NOT "does the folder
    # exist". `config_dir()` creates the folder as a side effect of being
    # asked where it is, so anything that merely *looks* — one import that
    # resolves a sounds path — would otherwise leave an empty directory
    # behind and convince this that the user had already been migrated.
    if os.path.isfile(os.path.join(dst, "settings.json")):
        return
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True)
    except Exception:
        pass            # a fresh start beats refusing to launch


class App:
    def __init__(self):
        _adopt_old_settings()
        self.st = vsettings.Settings.load(app="bedside")
        # Before st.apply(): that configures whatever set is active, so the
        # swap has to happen while there is still nothing to configure.
        sounds.install(self.st.extras.get("sound_set", sounds.DEFAULT_SET),
                       volume=self.st.volume, enabled=self.st.sounds)
        self.log = logview.LogView(speed=self.st.log_speed)
        self.st.apply(self.log)

        ex = self.st.extras
        self.client = OctoClient(ex.get("host", ""), ex.get("api_key", ""))
        self.loader = Loader()
        self.view = ToolpathView()
        self.preview = ToolpathView(key='v3dp')
        self.pair = Pairing()
        self.fx = effects.Effects()
        self.bg = bgmod.Backdrop()
        self._fade = 0.0            # screen-change wipe, 1 -> 0
        self._jog_hold = 0.0        # seconds the unlock button has been held
        self._jog_until = 0.0       # armed until this imgui clock time
        self._jog_step = 1.0
        self.store = filestore.FileStore()
        self.picker = pick.Picker()
        self._files_filter = ""
        self._files_seen = False    # has this session listed once
        self._preview_path = None   # previewing a file that is not the job
        self._ask_print = None      # Entry awaiting confirmation
        self._ask_delete = None
        self._screen_was = ""
        self.preset_name = ""
        self._log_hidden = 0
        self.log_filter = ""
        self._fan_angle = 0.0
        self.tab = 0
        self._dirty = False
        self._needs_rebuild = False
        migrated = int(ex.get("view_preset_version", 0)) < VIEW_PRESET_VERSION
        if migrated:
            ex.update(RECOMMENDED_VIEW)
            ex["view_preset_version"] = VIEW_PRESET_VERSION
        self._apply_view_prefs()

        self.screen = "dash" if (self.client.host and self.client.api_key) else "setup"
        self.host_field = (self.client.host or "").replace("http://", "")
        self.key_field = ""

        self._gen = 0
        self._applied = None
        self._was_printing = False
        self._unlock = False
        self.set_t0 = 200
        self.set_bed = 60
        self.gcode_field = ""

        self.toasts = None
        if ex.get("toasts", True):
            try:
                from vertexui import toasts as vtoasts
                # No anchor_titles on purpose. Anchoring to our OWN window
                # title makes _find() enumerate windows in this process and
                # GetWindowText sends WM_GETTEXT back to our render thread —
                # the deadlock the toolkit's own docs warn about. When that
                # stalls the toast thread mid-display the last frame stays
                # painted, which is a toast stuck on screen forever.
                # Unanchored puts it in the screen corner instead.
                self.toasts = vtoasts.Toasts(on_status=self._toast_status,
                                             on_event=self._toast_event)
                self.toasts.start()
            except Exception as exc:
                self.log.add(f"toasts unavailable: {exc}", "warn", "ui")

        if migrated:
            self.save()
            self.log.add("3D view set to recommended defaults", "info", "ui")

        if self.screen == "dash":
            self.client.start()

    # ------------------------------------------------------------- plumbing

    def _apply_view_prefs(self):
        """Push saved preferences into the view and the layout."""
        ex, v = self.st.extras, self.view
        v.auto_orbit = bool(ex.get("auto_orbit", False))
        v.budget = int(ex.get("detail", 22000))
        v.line_mm = float(ex.get("line_mm", 0.45))
        v.fade = float(ex.get("fade", 0.30))
        v.clip = float(ex.get("clip", 1.0))
        v.ghost = float(ex.get("ghost", 0.055))
        v.floor_alpha = float(ex.get("floor_alpha", 1.0))
        v.show_bed = bool(ex.get("show_bed", True))
        hexcol = ex.get("accent3d", "")
        v.accent = theme_mod.rgb(hexcol) if hexcol else None
        v.head_color = theme_mod.rgb(ex.get("head_color", "#ffffff"))
        v.head_size = float(ex.get("head_size", 1.0))
        self.fx.set(ex.get("effect", "none"))
        self.fx.intensity = float(ex.get("effect_intensity", 1.0))
        self.fx_over = float(ex.get("effect_over", 0.42))
        self.bg.set(ex.get("bg_scene", "none"))
        self.bg.intensity = float(ex.get("bg_intensity", 1.0))
        self.bg.speed = float(ex.get("bg_speed", 1.0))
        self.scrim = float(ex.get("bg_scrim", 0.72))
        self.jog_enabled = bool(ex.get("jog_enabled", False))
        set_card_style(ex.get("card_style", "raised"),
                       ex.get("card_shadow", True))
        self.panel_show = {k: bool(ex.get("panel_" + k, True))
                           for k in PANEL_SECTIONS}
        feats = ex.get("features")
        if feats:
            vis = set(int(f) for f in feats) | {gcode.OTHER}
            # Anyone who wanted perimeters wanted the outer wall too; it only
            # became a separate id later, so an older list cannot name it.
            if gcode.PERIMETER in vis:
                vis.add(gcode.EXTERNAL)
            # Same story for the brim: it used to be classed as support, so
            # a list written before the split cannot name it, and turning
            # support on has to keep meaning what it used to mean.
            #
            # Its own marker rather than the preset version, because bumping
            # that re-applies the whole recommended block — it would take
            # the user's ghost, detail, auto-orbit and feature choices with
            # it, which is a lot of collateral for one repair. The marker is
            # written here and persisted by the next save; switching the
            # brim off is itself a save, so it cannot come back.
            if not ex.get("skirt_split") and gcode.SUPPORT in vis:
                vis.add(gcode.SKIRT)
            ex["skirt_split"] = True
            v.visible = vis
        else:
            v.visible = set(DEFAULT_VISIBLE)
        self.right_w = float(ex.get("right_w", 372))
        self.log_h = float(ex.get("log_h", 178))
        self.graph_h = float(ex.get("graph_h", 104))

    def _toast_status(self, msg):
        self.log.add(f"toasts: {msg}", "info", "ui")

    def _toast_event(self, phase, kind, text):
        """A toast appeared or started leaving.

        Called from the toast thread. `sound.play` only queues, so this
        returns immediately — doing real work here would stall the overlay
        and leave a half-drawn toast on the desktop.
        """
        sound.play("toast_in" if phase == "in" else "toast_out")

    def notify(self, kind, text):
        if self.toasts:
            try:
                self.toasts.notify(kind, text)
            except Exception:
                pass

    def save(self):
        self.st.extras.update({
            "host": self.client.host,
            "api_key": self.client.api_key,
            "auto_orbit": self.view.auto_orbit,
            "toasts": bool(self.toasts),
        })
        try:
            self.st.save()
        except Exception as exc:
            self.log.add(f"could not save settings: {exc}", "warn", "ui")

    def _log_allowed(self, text):
        """Anything not recognised as routine chatter always gets through —
        errors and firmware messages must never be filtered away."""
        cls = classify_line(text)
        if cls == "other":
            return True
        return bool(self.st.extras.get(LOG_KEYS[cls], False))

    def _pump(self):
        """Move background-thread output into the UI, once per frame."""
        for text, kind, tag in self.client.drain_logs():
            if tag == "gcode" and not self._log_allowed(text):
                self._log_hidden += 1
                continue
            self.log.add(text, kind, tag)

        # A new active file means new geometry.
        gen = self.client.job_generation
        if gen != self._gen and self.client.job_file:
            self._gen = gen
            # The printer moving on to a real job outranks whatever was
            # being looked at, so the preview stands down rather than
            # sitting there claiming to be the thing on the bed.
            self._preview_path = None
            self._applied = None
            self.view.set_toolpath(None)
            self.loader.start(self.client, self.client.job_file,
                              self.client.job_origin)
            self.log.add(f"loading {self.client.job_file}", "info", "gcode")

        if self.view.gl_error and not getattr(self, "_gl_warned", False):
            self._gl_warned = True
            self.log.add("3D falling back to CPU rendering: "
                         + self.view.gl_error, "warn", "ui")

        state, msg, frac, tp = self.loader.snapshot()
        if state == "ready" and tp is not None and tp is not self._applied:
            self._applied = tp
            self.view.set_toolpath(tp)
            d = self.view.display
            arcs = ("" if not tp.arcs
                    else f", {tp.arcs:,} arcs flattened")
            self.log.add(f"toolpath ready — {msg}, drawing {len(d.pts):,} pts "
                         f"in {d.runs_kept:,} of {d.runs_visible:,} shown "
                         f"/ {d.runs_total:,} total runs{arcs}",
                         "ok", "gcode")
        elif state == "error" and self._applied is None and msg:
            if getattr(self, "_last_err", "") != msg:
                self._last_err = msg
                self.log.add(f"toolpath: {msg}", "error", "gcode")
        return state, msg, frac

    def _watch_transitions(self, snap):
        printing = snap.printing
        if printing != self._was_printing:
            if printing:
                self.log.add("print started", "ok", "job")
                self.notify("info", "Print started")
                sound.play("ok")
            else:
                done = (snap.completion or 0) >= 99.0
                if done:
                    self.log.add("print finished", "ok", "job")
                    self.notify("ok", "Print finished")
                    sound.play("ok")
                else:
                    self.log.add("print stopped", "warn", "job")
                    self.notify("warn", "Print stopped")
                    sound.play("error")
                self._unlock = False
            self._was_printing = printing

    # ---------------------------------------------------------------- frame

    def _shortcuts(self):
        """Keys that work anywhere. Nothing destructive is bound.

        A monitor gets left open and glanced at, so the bindings are the
        ones you reach for without looking: Escape backs out, Ctrl+, opens
        preferences the way every other desktop app does, F5 retries a dead
        connection. Pause and cancel are deliberately NOT bound — a key you
        can hit by accident must not be able to ruin a nine-hour print.
        """
        io = imgui.get_io()
        if io.want_capture_keyboard:
            return                      # a text field has the keyboard
        if imgui.is_key_pressed(imgui.Key.escape):
            if self.screen in ("settings", "files"):
                self.screen = "dash"
                sound.play("click")
        elif io.key_ctrl and imgui.is_key_pressed(imgui.Key.o):
            # Open, the way every other desktop app spells it. Nothing
            # destructive hangs off it — it only shows the list.
            if self.screen == "files":
                self.screen = "dash"
            else:
                self._open_files()
            sound.play("click")
        elif io.key_ctrl and imgui.is_key_pressed(imgui.Key.comma):
            self.screen = "dash" if self.screen == "settings" else "settings"
            sound.play("click")
        elif imgui.is_key_pressed(imgui.Key.f5):
            self._reconnect()
        elif imgui.is_key_pressed(imgui.Key.f1):
            self.screen = "settings"
            self.tab = len(SETTINGS_TABS) - 1        # About

    def _reconnect(self):
        """Drop the socket and dial again."""
        if not (self.client.host and self.client.api_key):
            return
        try:
            self.client.stop()
        except Exception:
            pass
        self.client.start()
        self.log.add("reconnecting to " + self.client.host, "info", "ui")
        sound.play("click")

    def frame(self):
        # Drawn first, onto *this window's* list — the background draw list
        # sits behind the full-screen window's own opaque fill, so anything
        # put there is simply painted over. Called before any content, so
        # it still lands underneath everything in the frame.
        io = imgui.get_io()
        now = imgui.get_time()
        w, h = io.display_size.x, io.display_size.y

        # The shader backdrop goes down before anything else, covering the
        # window's own fill. It is opaque by construction — it composites
        # the theme background itself rather than blending over it, which
        # keeps one guess out of the colour maths.
        if self.bg.active:
            t = theme_mod.current()
            hot = self.view.accent if self.view.accent is not None else t.accent
            two = t.accent_bright
            tex = self.bg.render(int(w), int(h), now,
                                 (hot.x, hot.y, hot.z),
                                 (two.x, two.y, two.z),
                                 (t.bg.x, t.bg.y, t.bg.z))
            if tex is not None:
                imgui.get_window_draw_list().add_image(
                    imgui.ImTextureRef(int(tex)), ImVec2(0, 0), ImVec2(w, h),
                    ImVec2(0, 1), ImVec2(1, 0))
                anim.mark_busy()

        # A hairline of print progress along the very top edge, outside the
        # padding and across the full width. It is the one number worth
        # seeing from the other side of the room, and up there it costs no
        # layout at all.
        if self.client.host:
            pct = float(self.client.completion or 0.0) / 100.0
            if pct > 0.0005:
                t = theme_mod.current()
                dlw = imgui.get_window_draw_list()
                eased = anim.to("top:pct", pct, 5.0)
                x1 = w * max(0.0, min(1.0, eased))
                dlw.add_rect_filled(ImVec2(0, 0), ImVec2(w, 3.0),
                                    imgui.get_color_u32(
                                        theme_mod.with_alpha(t.border, 0.55)))
                dlw.add_rect_filled(ImVec2(0, 0), ImVec2(x1, 3.0),
                                    imgui.get_color_u32(t.accent))
                # the leading edge gets a bloom, so the eye finds it moving
                dlw.add_circle_filled(
                    ImVec2(x1, 1.5), 7.0,
                    imgui.get_color_u32(
                        theme_mod.with_alpha(t.accent_bright, 0.30)))

            # Settings and setup are almost nothing but bare text, so the
            # whole window gets the wash rather than a rect per label.
            if self.screen in ("settings", "setup", "files"):
                scrim(imgui.get_window_draw_list(), 0.0, 0.0, w, h,
                      theme_mod.current(), self.scrim)

        moving = self.fx.kind != "none" and self.fx.step(now, io.delta_time)
        if moving:
            self.fx.paint(imgui.get_window_draw_list(), 0.0, 0.0, w, h,
                          now, 1.0)

        self._pump_files()
        self._shortcuts()
        if self.screen == "setup":
            self._setup()
        elif self.screen == "settings":
            self._settings()
        elif self.screen == "files":
            self._files_screen()
        else:
            self._dash()

        # Second pass, same particles, over the top. Behind-only, the effect
        # is visible in the gaps between panels and nowhere else — on a
        # dense screen that is a thin border of weather around an interface
        # that does not participate in it. Painted again here it drifts
        # across the cards, the graph and the model, which is what makes it
        # read as one atmosphere rather than as a wallpaper. It goes on the
        # window's own list at the end of the content, so it sits above
        # everything drawn this frame but still below popups and tooltips —
        # a modal has to stay readable.
        if moving and self.fx_over > 0.004:
            self.fx.paint(imgui.get_window_draw_list(), 0.0, 0.0, w, h,
                          now, self.fx_over)

        # A wipe across a screen change. Switching between the dashboard
        # and settings is a whole-window swap, and without a beat in
        # between it reads as a glitch rather than as navigation.
        if self.screen != self._screen_was:
            self._screen_was = self.screen
            self._fade = 1.0
        if self._fade > 0.004:
            self._fade = max(0.0, self._fade - io.delta_time * 4.5)
            t = theme_mod.current()
            imgui.get_window_draw_list().add_rect_filled(
                ImVec2(0, 0), ImVec2(w, h),
                imgui.get_color_u32(
                    theme_mod.with_alpha(t.bg, self._fade * 0.85)))
            anim.mark_busy()
        vui.end_frame()

    # ---------------------------------------------------------------- setup

    def _setup(self):
        t = theme_mod.current()
        avail = imgui.get_content_region_avail()
        imgui.dummy(ImVec2(0, max(20.0, avail.y * 0.16)))
        pad = max(20.0, (avail.x - 460) * 0.5)
        imgui.indent(pad)

        with fonts.use("title"):
            imgui.text_colored(t.text, "Bedside")
        imgui.text_colored(t.text_dim, "Point this at your OctoPrint box.")
        imgui.dummy(ImVec2(0, 14))

        widgets.section("printer", 460)
        imgui.set_next_item_width(460)
        changed, self.host_field = imgui.input_text_with_hint(
            "##host", "192.168.1.50   or   octopi.local", self.host_field)

        state, message, key = self.pair.snapshot()
        if state == "done" and key:
            self.client.host = normalise_host(self.host_field)
            self.client.api_key = key
            self.save()
            self.pair.state = "idle"
            self.screen = "dash"
            self.client.start()
            sound.play("ok")

        imgui.dummy(ImVec2(0, 8))
        busy = state == "waiting"
        if widgets.button("Pair with OctoPrint", 220, primary=True,
                          enabled=not busy and bool(self.host_field.strip())):
            self.pair.start(self.host_field)
        imgui.same_line()
        if busy:
            widgets.badge(message, "info")
        elif state == "error":
            widgets.badge(message, "error")

        imgui.dummy(ImVec2(0, 6))
        imgui.text_colored(t.text_mute,
                           "OctoPrint shows an authorisation prompt in its own")
        imgui.text_colored(t.text_mute,
                           "web UI. Click Allow and the key arrives here.")

        imgui.dummy(ImVec2(0, 18))
        widgets.section("or paste a key", 460)
        imgui.set_next_item_width(460)
        _, self.key_field = imgui.input_text(
            "##key", self.key_field, imgui.InputTextFlags_.password)
        imgui.dummy(ImVec2(0, 6))
        if widgets.button("Save and connect", 190,
                          enabled=bool(self.host_field.strip() and self.key_field.strip())):
            self.client.host = normalise_host(self.host_field)
            self.client.api_key = self.key_field.strip()
            self.key_field = ""
            self.save()
            self.screen = "dash"
            self.client.start()
        imgui.unindent(pad)

    # ---------------------------------------------------------------- files

    def _open_files(self):
        """Show the browser, and list once on the way in."""
        self.screen = "files"
        if not self._files_seen and self.client.host and self.client.api_key:
            self._files_seen = True
            self.store.refresh(self.client)

    def _can_start(self, snap):
        """Whether it is safe to hand the printer a new job right now."""
        return bool(snap.connected and not snap.printing
                    and (snap.flags or {}).get("operational"))

    def _pump_files(self):
        """Background file work into the log, and the picker's result in."""
        for text, kind in self.store.drain_events():
            self.log.add(text, kind, "ui")
            if self.toasts and kind in ("ok", "error"):
                try:
                    self.toasts.notify(kind, text)
                except Exception:
                    pass
        busy, paths, err = self.picker.take()
        if err:
            self.log.add("file dialog: " + err, "error", "ui")
        if paths:
            self.log.add(f"uploading {len(paths)} file(s)", "info", "ui")
            self.store.upload(self.client, paths)
        if busy:
            anim.mark_busy()

    def _preview(self, entry):
        """Draw a file that is not the running job, without printing it."""
        self._preview_path = entry.path
        self._applied = None
        self.view.set_toolpath(None)
        self.loader.start(self.client, entry.path, entry.origin)
        self.log.add(f"previewing {entry.display}", "info", "gcode")
        self.screen = "dash"
        sound.play("click")

    def _preview_banner(self, vp, size):
        """Say, on the view itself, that this is not what is being printed.

        Without it the 3D panel is indistinguishable from the live job, and
        a monitor that shows the wrong model with a straight face is worse
        than one that shows nothing.
        """
        if not self._preview_path:
            return
        t = theme_mod.current()
        dl = imgui.get_window_draw_list()
        name = self._preview_path.rpartition("/")[2]
        with fonts.use("semi"):
            tw = imgui.calc_text_size(name).x
        w = 96.0 + tw + 34.0
        x, y = vp.x + 10.0, vp.y + 10.0
        h = 30.0
        dl.add_rect_filled(ImVec2(x, y), ImVec2(x + w, y + h),
                           imgui.get_color_u32(
                               theme_mod.with_alpha(t.surface, 0.92)),
                           t.rounding)
        dl.add_rect(ImVec2(x, y), ImVec2(x + w, y + h),
                    imgui.get_color_u32(theme_mod.with_alpha(t.accent, 0.9)),
                    t.rounding, 1.0)
        with fonts.use("label"):
            caps(dl, x + 12, y + 11, "PREVIEW",
                 imgui.get_color_u32(t.accent), 1.4)
        with fonts.use("semi"):
            dl.add_text(ImVec2(x + 84, y + 7),
                        imgui.get_color_u32(t.text), name)
        # A close box, so getting back to the job does not need the browser.
        bx = x + w - 26.0
        keep = imgui.get_cursor_screen_pos()
        imgui.set_cursor_screen_pos(ImVec2(bx, y + 5))
        imgui.invisible_button("##unpreview", ImVec2(20.0, 20.0))
        hot = imgui.is_item_hovered()
        if imgui.is_item_clicked():
            self._preview_path = None
            self._applied = None
            self.view.set_toolpath(None)
            self._gen = -1          # make _pump reload the real job
            sound.play("click")
        if hot:
            imgui.set_tooltip("back to the running job")
        icons.draw("cross", dl, bx + 10, y + 15, 6.0,
                   imgui.get_color_u32(t.text if hot else t.text_mute))
        imgui.set_cursor_screen_pos(keep)

    def _row_action(self, key, icon, x, y, tip, enabled):
        """An icon button, or an inert glyph where it would refuse.

        `widgets.icon_button` has no disabled state, and a button that
        looks live and then declines is at its worst on exactly the row
        this matters for — the one being printed. So when the action is
        unavailable the button is not submitted at all: the glyph is
        painted dim, it takes no id, and it still explains itself on
        hover.
        """
        t = theme_mod.current()
        if enabled:
            imgui.set_cursor_screen_pos(ImVec2(x, y))
            return widgets.icon_button(icon, tooltip=tip, key=key)
        icons.draw(icon, imgui.get_window_draw_list(), x + 15.0, y + 15.0,
                   7.0, imgui.get_color_u32(
                       theme_mod.with_alpha(t.text_mute, 0.3)))
        if tip and imgui.is_mouse_hovering_rect(ImVec2(x, y),
                                                ImVec2(x + 30.0, y + 30.0)):
            imgui.set_tooltip(tip)
        return False

    def _files_screen(self):
        t = theme_mod.current()
        snap = self.client.snapshot()
        st = self.store.snapshot()

        if widgets.icon_button("chevron", tooltip="back to the printer"):
            self.screen = "dash"
        imgui.same_line()
        with fonts.use("title"):
            imgui.text_colored(t.text, "Files")
        imgui.same_line()
        imgui.text_colored(t.text_mute, "  on the printer")

        # Storage, right-aligned on the title row.
        if st["total"]:
            free, total = st["free"], st["total"]
            txt = f"{free / 1e9:.1f} GB free of {total / 1e9:.0f} GB"
            imgui.same_line()
            avail = imgui.get_content_region_avail().x
            imgui.same_line(0, max(8.0, avail - imgui.calc_text_size(txt).x
                                   - 8.0))
            used = 1.0 - (free / total if total else 0.0)
            imgui.text_colored(t.warn if used > 0.92 else t.text_mute, txt)

        imgui.dummy(ImVec2(0, 10))

        # ---- toolbar
        paired = bool(self.client.host and self.client.api_key)
        picking = self.picker.busy
        if widgets.button("Upload…", 130, primary=True, icon="upload",
                          enabled=paired and not picking,
                          tooltip="pick one or more .gcode files"):
            self.picker.start(pick.active_window(), multi=True)
        imgui.same_line(0, 8)
        if widgets.button("Refresh", 110, icon="refresh", enabled=paired,
                          key="frefresh"):
            self.store.refresh(self.client)
        imgui.same_line(0, 8)
        imgui.set_next_item_width(240)
        _, self._files_filter = imgui.input_text_with_hint(
            "##ffilter", "filter…", self._files_filter)
        if self._files_filter:
            imgui.same_line(0, 6)
            if widgets.icon_button("cross", tooltip="clear", key="fclear"):
                self._files_filter = ""

        imgui.dummy(ImVec2(0, 8))

        # ---- what the worker is doing
        if st["busy"] or st["error"]:
            w = imgui.get_content_region_avail().x
            p = imgui.get_cursor_screen_pos()
            dl = imgui.get_window_draw_list()
            bad = bool(st["error"])
            col = t.danger if bad else t.accent
            grad_fill(dl, p.x, p.y, w, 34.0, t,
                      theme_mod.with_alpha(col, 0.16),
                      theme_mod.with_alpha(col, 0.06))
            msg = (st["error"] if bad
                   else f"{st['op']} — {st['message']}")
            with fonts.use("semi"):
                dl.add_text(ImVec2(p.x + 12, p.y + 8),
                            imgui.get_color_u32(t.text), msg[:90])
            frac = st["fraction"] or 0.0
            if not bad and frac > 0.001:
                pct = f"{frac * 100:.0f}%"
                dl.add_text(ImVec2(p.x + w - 12 - imgui.calc_text_size(pct).x,
                                   p.y + 8), imgui.get_color_u32(t.accent),
                            pct)
                dl.add_rect_filled(
                    ImVec2(p.x, p.y + 31), ImVec2(p.x + w * frac, p.y + 34),
                    imgui.get_color_u32(col), 1.5)
            imgui.dummy(ImVec2(w, 42))
            anim.mark_busy()

        if not paired:
            imgui.text_colored(t.text_mute,
                               "Not paired with a printer yet.")
            return

        # ---- the list
        entries = st["entries"]
        needle = self._files_filter.strip().lower()
        if needle:
            entries = [e for e in entries if needle in e.display.lower()]

        can_start = self._can_start(snap)
        imgui.begin_child("##filelist",
                          imgui.get_content_region_avail(), False)
        dl = imgui.get_window_draw_list()
        w = imgui.get_content_region_avail().x
        ROW = 52.0

        if not entries:
            imgui.dummy(ImVec2(0, 24))
            imgui.indent(16)
            with fonts.use("semi"):
                imgui.text_colored(
                    t.text_dim,
                    "Nothing here yet." if not needle
                    else f"No file matches \u201c{self._files_filter}\u201d.")
            imgui.dummy(ImVec2(0, 2))
            imgui.text_colored(
                t.text_mute,
                "Upload a sliced .gcode to get started."
                if not needle else "Clear the filter to see the rest.")
            imgui.unindent(16)

        for i, e in enumerate(entries):
            p = imgui.get_cursor_screen_pos()
            live = (snap.job_file == e.path and snap.printing)
            hot = imgui.is_mouse_hovering_rect(
                ImVec2(p.x, p.y), ImVec2(p.x + w, p.y + ROW))
            edge = t.ok if live else None
            plate(dl, p.x, p.y, w, ROW, t, edge=edge)
            if hot:
                dl.add_rect_filled(
                    ImVec2(p.x, p.y), ImVec2(p.x + w, p.y + ROW),
                    imgui.get_color_u32(
                        theme_mod.with_alpha(t.surface_hover, 0.5)),
                    t.rounding)

            icons.draw("printer" if live else "cube", dl,
                       p.x + 24, p.y + ROW * 0.5, 8.0,
                       imgui.get_color_u32(t.ok if live else t.text_mute))
            shown = (e.display if len(e.display) <= 58
                     else e.display[:57] + "\u2026")
            with fonts.use("semi"):
                dl.add_text(ImVec2(p.x + 44, p.y + 9),
                            imgui.get_color_u32(t.text), shown)
            sub_ = f"{e.size_text()}   ·   {e.est_text()}   ·   {e.age_text()}"
            if e.folder:
                sub_ = e.folder + "/   ·   " + sub_
            dl.add_text(ImVec2(p.x + 44, p.y + 28),
                        imgui.get_color_u32(t.text_mute), sub_)
            if live:
                with fonts.use("label"):
                    caps(dl, p.x + w - 210, p.y + 21, "PRINTING",
                         imgui.get_color_u32(t.ok), 1.4)

            # Actions, right-aligned. Print is the one with consequences,
            # so it is the only one that is ever disabled and the only one
            # that asks first.
            bx = p.x + w - 122.0
            by = p.y + 11.0
            if self._row_action(f"fview{i}", "cube", bx, by,
                                "show it in the 3D view", True):
                self._preview(e)
            if self._row_action(
                    f"fprint{i}", "play", bx + 36.0, by,
                    ("this is the running job" if live else
                     "print this file" if can_start else
                     f"the printer is {(snap.state_text or 'busy').lower()}"),
                    can_start and not live):
                self._ask_print = e
            if self._row_action(
                    f"fdel{i}", "trash", bx + 72.0, by,
                    ("cannot delete the file it is printing" if live
                     else "delete from the printer"),
                    not live):
                self._ask_delete = e

            imgui.set_cursor_screen_pos(ImVec2(p.x, p.y + ROW + 6))
            imgui.dummy(ImVec2(w, 0))

        imgui.end_child()
        self._confirm_print(snap)
        self._confirm_delete()

    def _dialog_subject(self, name):
        """The file a dialog is about: an accent bar, then the name.

        Drawn rather than `text_colored` so the bar and the name share a
        baseline. The name is `text`, never `accent` — see the note on
        contrast in docs/ENGINEERING.md.
        """
        t = theme_mod.current()
        dl = imgui.get_window_draw_list()
        shown = name if len(name) <= 52 else name[:51] + "\u2026"
        p = imgui.get_cursor_screen_pos()
        with fonts.use("semi"):
            h = imgui.get_text_line_height()
            dl.add_rect_filled(ImVec2(p.x, p.y + 1),
                               ImVec2(p.x + 3.0, p.y + h - 1),
                               imgui.get_color_u32(t.accent), 1.5)
            dl.add_text(ImVec2(p.x + 11.0, p.y),
                        imgui.get_color_u32(t.text), shown)
            imgui.dummy(ImVec2(11.0 + imgui.calc_text_size(shown).x, h))

    def _confirm_print(self, snap):
        """Starting a print is the most expensive click in the app."""
        t = theme_mod.current()
        if self._ask_print is not None and not imgui.is_popup_open(
                "Start print###askprint"):
            imgui.open_popup("Start print###askprint")
        vp = imgui.get_main_viewport()
        imgui.set_next_window_pos(
            ImVec2(vp.pos.x + vp.size.x * 0.5, vp.pos.y + vp.size.y * 0.5),
            imgui.Cond_.appearing, ImVec2(0.5, 0.5))
        if imgui.begin_popup_modal(
                "Start print###askprint", None,
                imgui.WindowFlags_.always_auto_resize)[0]:
            e = self._ask_print
            if e is None:
                imgui.close_current_popup()
                imgui.end_popup()
                return
            with fonts.use("semi"):
                imgui.text_colored(t.text, "Send this to the printer?")
            imgui.dummy(ImVec2(0, 6))
            self._dialog_subject(e.display)
            imgui.text_colored(
                t.text_dim,
                f"{e.size_text()}   ·   about {e.est_text()}")
            imgui.dummy(ImVec2(0, 4))
            # Re-checked here, not just where the button was drawn: the
            # dialog can sit open while the printer starts something else.
            ready = self._can_start(snap)
            if not ready:
                imgui.text_colored(t.danger,
                                   "The printer is no longer idle.")
            else:
                imgui.text_colored(t.text_dim,
                                   "Make sure the bed is clear.")
            imgui.dummy(ImVec2(0, 10))
            if widgets.button("Start print", 150, height=30.0, primary=True,
                              enabled=ready, icon="play", key="dostart"):
                self.store.start_print(self.client, e)
                self._ask_print = None
                imgui.close_current_popup()
            imgui.same_line(0, 8)
            if widgets.button("Keep it", 110, height=30.0, key="nostart"):
                self._ask_print = None
                imgui.close_current_popup()
            imgui.end_popup()
        elif self._ask_print is not None and not imgui.is_popup_open(
                "Start print###askprint"):
            self._ask_print = None      # dismissed with Escape

    def _confirm_delete(self):
        t = theme_mod.current()
        if self._ask_delete is not None and not imgui.is_popup_open(
                "Delete file###askdelete"):
            imgui.open_popup("Delete file###askdelete")
        vp = imgui.get_main_viewport()
        imgui.set_next_window_pos(
            ImVec2(vp.pos.x + vp.size.x * 0.5, vp.pos.y + vp.size.y * 0.5),
            imgui.Cond_.appearing, ImVec2(0.5, 0.5))
        if imgui.begin_popup_modal(
                "Delete file###askdelete", None,
                imgui.WindowFlags_.always_auto_resize)[0]:
            e = self._ask_delete
            if e is None:
                imgui.close_current_popup()
                imgui.end_popup()
                return
            with fonts.use("semi"):
                imgui.text_colored(t.text, "Delete from the printer?")
            imgui.dummy(ImVec2(0, 6))
            self._dialog_subject(e.display)
            imgui.text_colored(t.text_dim,
                               "This removes it from the printer's storage. "
                               "The copy on this PC is untouched.")
            imgui.dummy(ImVec2(0, 10))
            if widgets.button("Delete", 130, height=30.0, primary=True,
                              key="dodel"):
                self.store.remove(self.client, e)
                self._ask_delete = None
                imgui.close_current_popup()
            imgui.same_line(0, 8)
            if widgets.button("Keep it", 110, height=30.0, key="nodel"):
                self._ask_delete = None
                imgui.close_current_popup()
            imgui.end_popup()
        elif self._ask_delete is not None and not imgui.is_popup_open(
                "Delete file###askdelete"):
            self._ask_delete = None     # dismissed with Escape

    # ------------------------------------------------------------- settings

    def _settings(self):
        t = theme_mod.current()
        if widgets.icon_button("chevron", tooltip="back to the printer"):
            self.screen = "dash"
        imgui.same_line()
        with fonts.use("title"):
            imgui.text_colored(t.text, "Settings")
        imgui.same_line()
        imgui.text_colored(t.text_mute, "  changes save as you go")
        imgui.dummy(ImVec2(0, 8))

        self.tab = widgets.tabs("settings", SETTINGS_TABS, self.tab)
        name = SETTINGS_TABS[self.tab]
        imgui.dummy(ImVec2(0, 4))
        imgui.text_colored(t.text_mute, TAB_BLURB.get(name, ""))
        imgui.dummy(ImVec2(0, 6))

        # A full-screen ImGui window does not scroll its own overflow, so
        # anything past the bottom edge would be unreachable. A child does.
        imgui.begin_child("##settingscroll", ImVec2(0, 0))
        if name == "Appearance":
            self._sec_theme()
            self._sec_accent()
            self._sec_shape()
            self._sec_font()
            self._sec_backdrop()
            self._sec_effects()
        elif name == "3D View":
            self._sec_view_preview()
            self._sec_view_show()
            self._sec_view_look()
        elif name == "Terminal":
            self._sec_log()
        elif name == "Alerts":
            self._sec_sound()
            self._sec_alerts()
        elif name == "Printer":
            self._sec_printer()
            self._sec_panel()
            self._sec_presets()
        else:
            self._sec_diagnostics()
            self._sec_about()
        imgui.dummy(ImVec2(0, 24))
        imgui.end_child()

        # Re-decimating or writing to disk mid-drag stutters; do both once
        # the mouse comes back up.
        if not imgui.is_mouse_down(0):
            if self._needs_rebuild:
                self._needs_rebuild = False
                self.view.rebuild()
                self.preview.rebuild()
            if self._dirty:
                self._dirty = False
                self._apply_theme()
                self.save()

    # -- settings plumbing -------------------------------------------------

    def _group(self, label, default_open=False):
        """Sections are always open now — the tab bar does the hiding, so a
        collapsed header inside one is a second thing to click for nothing."""
        widgets.section(label.lower())
        return True

    def _pref(self, key, default):
        return self.st.extras.get(key, default)

    def _slider_pref(self, label, key, default, lo, hi, fmt="%.2f", width=190.0,
                     rebuild=False, apply=None):
        cur = float(self._pref(key, default))
        new = widgets.slider(label, cur, lo, hi, width, fmt, key=key)
        if abs(new - cur) > 1e-6:
            self.st.extras[key] = new
            if apply:
                apply(new)
            self._dirty = True
            self._needs_rebuild |= rebuild
        return new

    def _check_pref(self, label, key, default, apply=None):
        cur = bool(self._pref(key, default))
        new = widgets.checkbox(label, cur, key=key)
        if new != cur:
            self.st.extras[key] = new
            if apply:
                apply(new)
            self._dirty = True
        return new

    def _apply_theme(self):
        """Settings.apply() first, then our own shape overrides on top."""
        base = self.st.apply(self.log)
        ex = self.st.extras
        t = replace(
            base,
            rounding=float(ex.get("rounding", base.rounding)),
            rounding_panel=float(ex.get("rounding_panel", base.rounding_panel)),
            border_width=float(ex.get("border_width", base.border_width)),
        )
        # Translucent panels are what make a background effect worth having:
        # opaque ones cover it completely on a dashboard this dense. Cheap
        # too — it is alpha on two tokens, not a blur.
        pa = float(ex.get("panel_alpha", 1.0))
        if pa < 0.999:
            t = replace(t,
                        panel=theme_mod.with_alpha(t.panel, pa),
                        surface=theme_mod.with_alpha(t.surface, pa),
                        surface_hover=theme_mod.with_alpha(t.surface_hover, pa))
        theme_mod.use(t)
        try:
            theme_mod.apply_style(t)
        except Exception:
            pass
        self.view._ramp_key = None

    # -- sections ----------------------------------------------------------

    def _sec_theme(self):
        if not self._group("SELECT THEME", True):
            return
        t = theme_mod.current()
        names = theme_mod.list_presets(self.st.app)
        avail = imgui.get_content_region_avail().x
        cw, ch = 208.0, 92.0
        per_row = max(1, int(avail // (cw + 8)))
        dl = imgui.get_window_draw_list()

        for i, name in enumerate(names):
            th = theme_mod.load_preset(name, self.st.app) or theme_mod.NOIR_RED
            p = imgui.get_cursor_screen_pos()
            imgui.invisible_button("##th" + name, ImVec2(cw, ch))
            hovered = imgui.is_item_hovered()
            if imgui.is_item_clicked():
                self.st.theme_name, self.st.accent = name, ""
                self._dirty = True
                sound.play("click")
            sel = name == self.st.theme_name
            lift = anim.to("th:" + name, 1.0 if (hovered or sel) else 0.0, 16.0)

            # a miniature of the app: ground, panel, a bar, an accent bar
            dl.add_rect_filled(p, ImVec2(p.x + cw, p.y + ch),
                               imgui.get_color_u32(th.bg), th.rounding_panel)
            dl.add_rect_filled(ImVec2(p.x + 10, p.y + 10),
                               ImVec2(p.x + cw - 10, p.y + 52),
                               imgui.get_color_u32(th.panel), th.rounding)
            dl.add_rect_filled(ImVec2(p.x + 18, p.y + 19),
                               ImVec2(p.x + 96, p.y + 27),
                               imgui.get_color_u32(th.surface), 3.0)
            dl.add_rect_filled(ImVec2(p.x + 18, p.y + 33),
                               ImVec2(p.x + 70, p.y + 41),
                               imgui.get_color_u32(th.accent_dim), 3.0)
            dl.add_rect_filled(ImVec2(p.x + 106, p.y + 19),
                               ImVec2(p.x + cw - 18, p.y + 41),
                               imgui.get_color_u32(th.accent), 3.0)
            dl.add_text(ImVec2(p.x + 12, p.y + 62),
                        imgui.get_color_u32(th.text), name)
            for j, c in enumerate((th.accent, th.ok, th.warn)):
                dl.add_circle_filled(
                    ImVec2(p.x + cw - 22 - j * 18, p.y + 70), 5.0,
                    imgui.get_color_u32(c))
            edge = anim.lerp_col(t.border, t.accent,
                                 1.0 if sel else lift * 0.6)
            dl.add_rect(p, ImVec2(p.x + cw, p.y + ch),
                        imgui.get_color_u32(edge), th.rounding_panel,
                        2.0 if sel else 1.0, 0)

            if (i + 1) % per_row and i < len(names) - 1:
                imgui.same_line(0, 8)
        imgui.dummy(ImVec2(0, 8))

    _SWATCHES = ("#da2538", "#ff6b35", "#e5aa45", "#53c971", "#2bb3a3",
                 "#4aa8ff", "#7c3aed", "#d946a8", "#e6e6ef")

    def _sec_accent(self):
        if not self._group("ACCENT COLOUR"):
            return
        t = theme_mod.current()
        base = theme_mod.load_preset(self.st.theme_name, self.st.app) \
            or theme_mod.NOIR_RED
        cur = theme_mod.rgb(self.st.accent) if self.st.accent else base.accent
        cur_hex = theme_mod.to_hex(cur).lower()
        dl = imgui.get_window_draw_list()

        for i, hx in enumerate(self._SWATCHES):
            p = imgui.get_cursor_screen_pos()
            imgui.invisible_button("##sw%d" % i, ImVec2(30, 30))
            if imgui.is_item_clicked():
                self.st.accent = hx
                self._dirty = True
                sound.play("click")
            hot = anim.to("sw:%d" % i,
                          1.0 if imgui.is_item_hovered() else 0.0, 16.0)
            c = ImVec2(p.x + 15, p.y + 15)
            if hx.lower() == cur_hex:
                dl.add_circle(c, 14.0, imgui.get_color_u32(t.text), 0, 2.0)
            dl.add_circle_filled(c, 10.0 + 2.0 * hot,
                                 imgui.get_color_u32(theme_mod.rgb(hx)))
            imgui.same_line(0, 6)

        picked = widgets.color_button("custom", cur, key="accentpick")
        if theme_mod.to_hex(picked).lower() != cur_hex:
            self.st.accent = theme_mod.to_hex(picked)
            self._dirty = True
        if self.st.accent:
            imgui.same_line()
            if widgets.button("follow theme", 140, height=26.0,
                              key="accent_follow"):
                self.st.accent = ""
                self._dirty = True
        imgui.dummy(ImVec2(0, 6))

    def _sec_shape(self):
        if not self._group("TEXT & CORNERS"):
            return
        st = self.st
        ns = widgets.slider("text size", st.text_scale, 0.8, 1.6, 190.0,
                            "%.2fx", key="textscale")
        if abs(ns - st.text_scale) > 0.001:
            st.text_scale = round(ns, 2)
            self._dirty = True
        order = vsettings.ROW_ORDER
        ridx = order.index(st.row_spacing) if st.row_spacing in order else 1
        nr = widgets.combo("density", order, ridx, 150.0, key="rowspace")
        if nr != ridx:
            st.row_spacing = order[nr]
            self._dirty = True
        self._slider_pref("corner radius", "rounding", 7.0, 0.0, 18.0, "%.0f px")
        self._slider_pref("panel radius", "rounding_panel", 9.0, 0.0, 22.0,
                          "%.0f px")
        self._slider_pref("border width", "border_width", 1.0, 0.0, 3.0,
                          "%.1f px")
        imgui.dummy(ImVec2(0, 6))

    def _sec_font(self):
        if not self._group("FONT"):
            return
        t = theme_mod.current()
        ui_names, mono_names = list(UI_FACES), list(MONO_FACES)
        cur_ui = self._pref("face_ui", ui_names[0])
        cur_mono = self._pref("face_mono", mono_names[0])
        i = ui_names.index(cur_ui) if cur_ui in ui_names else 0
        j = mono_names.index(cur_mono) if cur_mono in mono_names else 0
        ni = widgets.combo("interface", ui_names, i, 190.0, key="faceui")
        if ni != i:
            self.st.extras["face_ui"] = ui_names[ni]
            self._dirty = True
        disp_names = list(DISPLAY_FACES)
        cur_disp = self._pref("face_display", disp_names[0])
        k = disp_names.index(cur_disp) if cur_disp in disp_names else 0
        nk = widgets.combo("readouts", disp_names, k, 190.0, key="facedisp")
        if nk != k:
            self.st.extras["face_display"] = disp_names[nk]
            self._dirty = True
        nj = widgets.combo("terminal", mono_names, j, 190.0, key="facemono")
        if nj != j:
            self.st.extras["face_mono"] = mono_names[nj]
            self._dirty = True
        imgui.text_colored(t.text_mute,
                           "fonts are baked into the atlas at startup — "
                           "restart to apply")
        imgui.dummy(ImVec2(0, 6))

    def _sec_sound(self):
        if not self._group("SOUND"):
            return
        t = theme_mod.current()
        st = self.st
        on = widgets.checkbox("interface sounds", st.sounds, key="snd")
        if on != st.sounds:
            st.sounds = on
            self._dirty = True
        imgui.same_line()
        nv = widgets.slider("volume", st.volume, 0.0, 0.6, 160.0, "%.2f",
                            key="vol")
        if abs(nv - st.volume) > 0.001:
            st.volume = round(nv, 3)
            self._dirty = True
        imgui.same_line()
        if widgets.button("test", 80, height=26.0):
            self._apply_theme()
            sound.play("ok")

        names = list(sounds.SETS)
        cur = self._pref("sound_set", sounds.DEFAULT_SET)
        i = names.index(cur) if cur in names else 0
        ni = widgets.combo("cue set", names, i, 190.0, key="sndset")
        if ni != i:
            self.st.extras["sound_set"] = names[ni]
            sounds.install(names[ni], volume=st.volume, enabled=st.sounds)
            self._dirty = True
            sound.play("ok")
        if names[ni] == sounds.CUSTOM_SET:
            self._sec_sound_files()
        else:
            imgui.text_colored(t.text_mute,
                               "breeze is filtered noise rather than tones — "
                               "no onset to flinch at when a nine-hour print "
                               "is running beside you")
        imgui.dummy(ImVec2(0, 6))

    def _sec_sound_files(self):
        """Which of the five cues the user has supplied, and how to add them."""
        t = theme_mod.current()
        dl = imgui.get_window_draw_list()
        active = sound.current()
        folder = getattr(active, "folder", sounds.custom_dir())
        found = active.found() if isinstance(active, sounds.FileSet) else {}
        bad = getattr(active, "bad", {})

        imgui.text_colored(t.text_mute, str(folder))
        p = imgui.get_cursor_screen_pos()
        x = p.x
        for name in sounds.CUE_NAMES:
            ok = found.get(name, False)
            col = t.danger if name in bad else (t.ok if ok else t.text_mute)
            dl.add_circle_filled(ImVec2(x + 4, p.y + 10), 3.4,
                                 imgui.get_color_u32(col))
            if not ok:
                # A ring, not a disc: "no file here" should not look like a
                # state you chose.
                dl.add_circle_filled(ImVec2(x + 4, p.y + 10), 1.8,
                                     imgui.get_color_u32(t.bg))
            with fonts.use("label"):
                x = caps(dl, x + 13, p.y + 4, name.upper(),
                         imgui.get_color_u32(col), 1.3) + 18
        imgui.dummy(ImVec2(x - p.x, 20))

        if widgets.button("open folder", 140, height=28.0):
            try:
                if isinstance(active, sounds.FileSet):
                    active.reveal()
            except Exception as exc:
                self.log.add(f"could not open {folder}: {exc}", "warn", "ui")
        imgui.same_line(0, 8)
        if widgets.button("write starter cues", 180, height=28.0):
            try:
                n = active.export() if isinstance(active, sounds.FileSet) else 0
                self.log.add(
                    f"wrote {n} cue file(s) to {folder}" if n else
                    "all five cue files already exist — nothing overwritten",
                    "ok" if n else "info", "ui")
                sound.play("ok")
            except Exception as exc:
                self.log.add(f"could not write cues: {exc}", "error", "ui")
        if bad:
            imgui.text_colored(
                t.danger,
                "unreadable: " + ", ".join(f"{k}.wav" for k in sorted(bad)))
        imgui.text_colored(t.text_mute,
                           "drop 16-bit .wav files named hover / click / ok / "
                           "warn / error; any missing one keeps its breeze "
                           "cue, and the volume slider scales yours (full at "
                           "0.60)")

    def _sec_log(self):
        if not self._group("TERMINAL"):
            return
        st = self.st
        order = logview.SPEED_ORDER
        si = order.index(st.log_speed) if st.log_speed in order else 0
        ns = widgets.combo("scroll", order, si, 150.0, key="logspeed")
        if ns != si:
            st.log_speed = order[ns]
            self._dirty = True
        for label, attr in (("monospaced", "monospaced_log"),
                            ("timestamps", "timestamps"),
                            ("tag column", "tag_column")):
            v = widgets.checkbox(label, getattr(st, attr), key="lg" + attr)
            if v != getattr(st, attr):
                setattr(st, attr, v)
                self._dirty = True
            imgui.same_line()
        imgui.new_line()

        t = theme_mod.current()
        fol = widgets.checkbox("auto-scroll (follow)", self.log.follow,
                               key="setfollow")
        if fol != self.log.follow:
            self.log.follow = fol
        imgui.text_colored(t.text_mute,
                           "show routine traffic — all hidden by default, "
                           "because during a print it is ~99% of the lines:")
        for label, cls in LOG_LABELS:
            self._check_pref(label, LOG_KEYS[cls], False)
            imgui.same_line()
        imgui.new_line()
        imgui.text_colored(t.text_mute,
                           "errors, unknown commands and \"busy: paused for "
                           "user\" are never filtered")
        imgui.dummy(ImVec2(0, 6))

    _PREVIEW_SYNC = ("line_mm", "fade", "ghost", "floor_alpha", "show_bed",
                     "accent", "head_color", "head_size", "budget", "visible",
                     "clip")

    def _sec_view_preview(self):
        self._group("preview")
        t = theme_mod.current()
        p, v = self.preview, self.view
        if p._tp is None:
            p.set_toolpath(sample_toolpath())
            p.auto_orbit = True
            # A third of the dashboard's rate. The preview model is small
            # and close, so the same angular speed sweeps far more of it
            # per second and reads as a spin rather than a slow turn —
            # which is no use for judging extrusion width or shading.
            p.orbit_speed = 0.055
            # Lower than the dashboard default on purpose: the side wall
            # is where extrusion width and opacity actually read.
            p.pitch, p.zoom = -0.62, 0.92

        # Mirror every tunable from the live view, so the preview cannot
        # drift from what the dashboard will actually do.
        for attr in self._PREVIEW_SYNC:
            setattr(p, attr, getattr(v, attr))

        # Sweep back and forth so the printed/unprinted boundary, the depth
        # ramp and the nozzle marker are all visible without waiting.
        n = max(len(p._tp), 1)
        frac = 0.28 + 0.64 * (0.5 - 0.5 * math.cos(imgui.get_time() * 0.32))

        w = min(imgui.get_content_region_avail().x, 520.0)
        p.draw(ImVec2(w, 250.0), frac * n, live=True)
        imgui.text_colored(t.text_mute,
                           "a sample model with your current settings — "
                           "drag to orbit, right-drag to pan, wheel to "
                           "zoom, double-click to reset")
        imgui.dummy(ImVec2(0, 8))

    def _sec_view_show(self):
        if not self._group("3D VIEW — SHOW", True):
            return
        t = theme_mod.current()
        d, v = self.view.display, self.view
        if d is not None and not d.has_features:
            imgui.text_colored(t.warn,
                               "this file has no ;TYPE: annotations — "
                               "feature filters unavailable")
        else:
            vis = set(v.visible)
            groups = (("outer wall", (gcode.EXTERNAL,)),
                      ("inner walls", (gcode.PERIMETER,)),
                      ("solid", (gcode.SOLID,)),
                      ("infill", (gcode.INFILL,)),
                      ("support", (gcode.SUPPORT,)),
                      ("brim / skirt", (gcode.SKIRT,)),
                      ("bridges", (gcode.BRIDGE,)))
            for label, fids in groups:
                cur = all(f in vis for f in fids)
                on = widgets.checkbox(label, cur, key="feat%d" % fids[0])
                if on != cur:
                    for f in fids:
                        vis.add(f) if on else vis.discard(f)
                    v.visible = vis | {gcode.OTHER}
                    self.st.extras["features"] = sorted(v.visible)
                    self._dirty = True
                    self._needs_rebuild = True
                imgui.same_line()
            imgui.new_line()

            imgui.dummy(ImVec2(0, 4))
            self._slider_pref("cutaway", "clip", 1.0, 0.05, 1.0, "%.2f",
                              apply=lambda x: setattr(v, "clip", x))
            imgui.text_colored(
                t.text_mute,
                "everything the outer wall encloses — infill most of all — "
                "is hidden by it at any angle, so drop the cutaway to look "
                "inside")
            if not v.use_gl:
                imgui.text_colored(t.warn,
                                   "cutaway needs the GPU renderer; this "
                                   "session fell back to the CPU path")
        imgui.dummy(ImVec2(0, 6))

    def _sec_view_look(self):
        if not self._group("3D VIEW — LOOK"):
            return
        v = self.view
        if widgets.button("Reset to recommended", 220, height=28.0):
            self.st.extras.update(RECOMMENDED_VIEW)
            self._apply_view_prefs()
            self._needs_rebuild = True
            self._dirty = True
            sound.play("ok")
        imgui.dummy(ImVec2(0, 6))
        self._slider_pref("detail", "detail", 22000, 6000, 60000, "%.0f pts",
                          rebuild=True,
                          apply=lambda x: setattr(v, "budget", int(x)))
        self._slider_pref("extrusion width", "line_mm", 0.45, 0.1, 1.6,
                          "%.2f mm",
                          apply=lambda x: setattr(v, "line_mm", x))
        self._slider_pref("depth fade", "fade", 0.30, 0.05, 1.0, "%.2f",
                          apply=lambda x: setattr(v, "fade", x))
        self._slider_pref("base opacity", "floor_alpha", 1.0, 0.02, 1.0,
                          "%.2f", apply=lambda x: setattr(v, "floor_alpha", x))
        self._slider_pref("ghost opacity", "ghost", 0.055, 0.0, 0.5, "%.3f",
                          apply=lambda x: setattr(v, "ghost", x))
        self._slider_pref("nozzle size", "head_size", 1.0, 0.5, 3.0, "%.2fx",
                          apply=lambda x: setattr(v, "head_size", x))

        # auto-orbit is not here any more: it is a button on the view
        # itself, because it is the one 3D setting you reach for while
        # looking at the model rather than while configuring it.
        self._check_pref("bed outline", "show_bed", True,
                         apply=lambda x: setattr(v, "show_bed", x))

        cur_hex = self._pref("accent3d", "")
        cur = theme_mod.rgb(cur_hex) if cur_hex else theme_mod.current().accent
        picked = widgets.color_button("model colour", cur, key="accent3d")
        if theme_mod.to_hex(picked) != theme_mod.to_hex(cur):
            self.st.extras["accent3d"] = theme_mod.to_hex(picked)
            v.accent = picked
            v._ramp_key = None
            self._dirty = True
        if cur_hex:
            imgui.same_line()
            if widgets.button("follow theme", 130, height=26.0,
                              key="model_follow"):
                self.st.extras["accent3d"] = ""
                v.accent = None
                v._ramp_key = None
                self._dirty = True
            # Unprinted geometry is drawn in a neutral grey. A model colour
            # with no chroma of its own is nearly the same thing, so printed
            # and not-yet-printed merge into haze and the view reads as
            # broken rather than as a colour choice.
            mx = max(cur.x, cur.y, cur.z)
            mn = min(cur.x, cur.y, cur.z)
            if mx - mn < 0.12:
                t2 = theme_mod.current()
                imgui.text_colored(
                    t2.warn,
                    "this colour has almost no hue — it looks like the "
                    "unprinted ghost, so the model reads as see-through")

        hh = self._pref("head_color", "#ffffff")
        hpick = widgets.color_button("nozzle colour", theme_mod.rgb(hh),
                                     key="headcol")
        if theme_mod.to_hex(hpick).lower() != hh.lower():
            self.st.extras["head_color"] = theme_mod.to_hex(hpick)
            v.head_color = hpick
            self._dirty = True
        imgui.dummy(ImVec2(0, 6))

    def _sec_backdrop(self):
        if not self._group("BACKDROP"):
            return
        t = theme_mod.current()
        if not bgmod.HAVE_GL:
            imgui.text_colored(t.warn, "no OpenGL — backdrop unavailable")
            imgui.dummy(ImVec2(0, 6))
            return
        cur = self._pref("bg_scene", "none")
        for i, name in enumerate(bgmod.SCENES):
            # Namespaced, because the effects picker below has a "none" too
            # and a widget's ImGui id defaults to its label — two items with
            # one id is a hard error, not a cosmetic clash.
            if widgets.button(name, 118, height=30.0, primary=name == cur,
                              key="bgscene:" + name):
                self.st.extras["bg_scene"] = name
                self.bg.set(name)
                self._dirty = True
            if (i + 1) % 4:
                imgui.same_line(0, 6)
            else:
                imgui.new_line()
        imgui.new_line()
        self._slider_pref("brightness", "bg_intensity", 1.0, 0.1, 2.0, "%.2f",
                          apply=lambda x: setattr(self.bg, "intensity", x))
        self._slider_pref("speed", "bg_speed", 1.0, 0.1, 3.0, "%.2fx",
                          apply=lambda x: setattr(self.bg, "speed", x))
        self._slider_pref("readability", "bg_scrim", 0.72, 0.0, 1.0, "%.2f",
                          apply=lambda x: setattr(self, "scrim", x))
        if self.bg.error:
            imgui.text_colored(t.danger, self.bg.error[:160])
        imgui.text_colored(t.text_mute,
                           "a fragment shader over the whole window, drawn "
                           "at half resolution and tinted from your accent "
                           "— it holds the app at full frame rate, same as "
                           "the particles")
        imgui.text_colored(t.text_mute,
                           "readability washes the background back in behind "
                           "bare text — this screen, the job strip and the "
                           "terminal bar. Cards have their own fill and are "
                           "not affected")
        imgui.dummy(ImVec2(0, 6))

    def _sec_effects(self):
        if not self._group("BACKGROUND EFFECT"):
            return
        t = theme_mod.current()
        cur = self._pref("effect", "none")
        per_row = 4
        for i, name in enumerate(effects.NAMES):
            sel = name == cur
            if widgets.button(name, 132, height=30.0, primary=sel,
                              key="fxkind:" + name):
                self.st.extras["effect"] = name
                self.fx.set(name)
                self._dirty = True
            if (i + 1) % per_row:
                imgui.same_line(0, 6)
            else:
                imgui.new_line()
        imgui.new_line()
        self._slider_pref("intensity", "effect_intensity", 1.0, 0.2, 2.0,
                          "%.2f",
                          apply=lambda x: setattr(self.fx, "intensity", x))
        self._slider_pref("over panels", "effect_over", 0.42, 0.0, 1.0,
                          "%.2f",
                          apply=lambda x: setattr(self, "fx_over", x))
        self._slider_pref("panel opacity", "panel_alpha", 1.0, 0.35, 1.0,
                          "%.2f")
        imgui.text_colored(t.text_mute,
                           "\"over panels\" repaints the same particles on "
                           "top of the UI; panel opacity lets them show "
                           "through it as well")
        if cur != "none":
            imgui.text_colored(t.text_mute,
                               "an animated background holds the app at full "
                               "frame rate — it will not idle")
        imgui.dummy(ImVec2(0, 6))

    def _panel_preview(self, avail_w):
        """A scaled mock of the whole dashboard.

        Everything this tab controls is invisible from inside Settings: the
        card style, which sections are on, and the three layout sliders all
        only show up once the panel is closed. Drawing the window in
        miniature puts all of them on screen at once — and it is one mock
        rather than five, because the sliders are proportions of each other
        and previewing them separately would not show that.

        Pure draw-list work: no widgets, nothing hoverable, nothing that can
        steal a click from the real controls underneath.
        """
        t = theme_mod.current()
        dl = imgui.get_window_draw_list()
        io = imgui.get_io()
        # The real window, so the mock has the aspect the user actually sees.
        W = max(float(io.display_size.x), 900.0)
        H = max(float(io.display_size.y), 620.0)
        scale = min(min(avail_w, 520.0) / W, 224.0 / H)
        w, h = W * scale, H * scale
        p = imgui.get_cursor_screen_pos()

        def S(v):
            return v * scale

        def bar(x, y, bw, bh, col, alpha=1.0, r=1.0):
            dl.add_rect_filled(
                ImVec2(x, y), ImVec2(x + bw, y + bh),
                imgui.get_color_u32(theme_mod.with_alpha(col, alpha)), r)

        # Corners do not scale well: at a third size the real 7px radius
        # turns every card into a pill. A themed copy with its own rounding
        # keeps the *style* legible without lying about the proportions.
        tm = replace(t, rounding=max(2.0, t.rounding * scale * 1.7),
                     rounding_panel=max(2.0, t.rounding_panel * scale * 1.7))

        dl.add_rect_filled(p, ImVec2(p.x + w, p.y + h),
                           imgui.get_color_u32(t.bg), 6.0)
        dl.add_rect(p, ImVec2(p.x + w, p.y + h),
                    imgui.get_color_u32(t.border), 6.0, 1.0)

        pad, topbar, gap = S(16.0), S(46.0), S(GAP)
        x0, y0 = p.x + pad, p.y + pad
        inner_w = w - pad * 2
        log_h = S(self.log_h)
        top_h = max(S(60.0), h - pad * 2 - topbar - log_h - gap)
        right_w = min(S(self.right_w), inner_w * 0.72)
        left_w = max(S(40.0), inner_w - right_w - gap)
        mute = t.text_mute

        # ---- topbar: bolt, status pill, host dot, then the accent rule
        dl.add_circle_filled(ImVec2(x0 + S(9), y0 + S(11)), max(1.2, S(3.5)),
                             imgui.get_color_u32(t.accent))
        bar(x0 + S(20), y0 + S(5), S(42), S(13), t.text, 0.5, S(3))
        bar(x0 + S(70), y0 + S(5), S(52), S(13), t.ok, 0.30, S(7))
        dl.add_circle_filled(ImVec2(x0 + inner_w - S(46), y0 + S(11)),
                             max(1.0, S(3)), imgui.get_color_u32(t.ok))
        bar(x0 + inner_w - S(38), y0 + S(7), S(30), S(8), mute, 0.5, 1.0)
        bar(x0, y0 + topbar - S(6), inner_w, max(1.0, S(1.5)), t.accent, 1.0)
        y0 += topbar

        # ---- the 3D view: a box on the bed, and the two overlay buttons
        vy = y0
        dl.add_rect_filled(ImVec2(x0, vy), ImVec2(x0 + left_w, vy + top_h),
                           imgui.get_color_u32(
                               theme_mod.lerp(t.bg, t.panel, 0.5)),
                           tm.rounding_panel)
        dl.add_rect(ImVec2(x0, vy), ImVec2(x0 + left_w, vy + top_h),
                    imgui.get_color_u32(t.border), tm.rounding_panel, 1.0)
        cx, cy = x0 + left_w * 0.5, vy + top_h * 0.52
        rr = min(left_w, top_h) * 0.30
        dl.add_polyline([ImVec2(cx - rr, cy), ImVec2(cx, cy - rr * 0.5),
                         ImVec2(cx + rr, cy), ImVec2(cx, cy + rr * 0.5),
                         ImVec2(cx - rr, cy)],
                        imgui.get_color_u32(
                            theme_mod.with_alpha(t.border, 0.9)), 1.0, 0)
        # A flat square reads as a swatch, not a print. Three faces of a box
        # in three shades is the smallest thing that reads as an object.
        bw, bh, bz = rr * 0.42, rr * 0.21, rr * 0.46
        top = [ImVec2(cx, cy - bz - bh), ImVec2(cx + bw, cy - bz),
               ImVec2(cx, cy - bz + bh), ImVec2(cx - bw, cy - bz)]
        dl.add_convex_poly_filled(top, imgui.get_color_u32(
            theme_mod.lerp(t.accent, ImVec4(1, 1, 1, 1), 0.30)))
        dl.add_convex_poly_filled(
            [top[3], top[2], ImVec2(cx, cy + bh), ImVec2(cx - bw, cy)],
            imgui.get_color_u32(t.accent))
        dl.add_convex_poly_filled(
            [top[2], top[1], ImVec2(cx + bw, cy), ImVec2(cx, cy + bh)],
            imgui.get_color_u32(theme_mod.lerp(t.accent, t.bg, 0.42)))
        for k in (0, 1):
            bx = x0 + left_w - S(76) + k * S(36)
            bar(bx, vy + S(10), S(30), S(30),
                t.accent if (k and self.view.auto_orbit) else t.surface,
                0.95, max(1.0, S(6)))

        # ---- the side panel, in the chosen style, with the chosen sections
        px = x0 + left_w + gap
        py = vy
        show = self.panel_show

        def head(x, y, cw):
            """A card header: a stub of a title, then its hairline."""
            bar(x + S(12), y + S(8), S(38), S(7), mute, 0.55, 1.0)
            dl.add_line(ImVec2(x + S(11), y + S(26)),
                        ImVec2(x + cw - S(11), y + S(26)),
                        imgui.get_color_u32(
                            theme_mod.with_alpha(t.border, 0.85)), 1.0)

        def card(height, edge=None, body=None):
            nonlocal py
            hh = S(height)
            if py + hh > vy + top_h:
                return False
            plate(dl, px, py, right_w, hh, tm, edge=edge)
            if body:
                body(px, py, right_w, hh)
            py += hh + S(6)
            return True

        if show["temps"]:
            hh = S(88)
            if py + hh <= vy + top_h:
                cw = (right_w - S(16)) / 3.0
                for i, col in enumerate((t.accent, t.info, t.info)):
                    bx = px + (cw + S(8)) * i
                    plate(dl, bx, py, cw, hh, tm, edge=col)
                    bar(bx + S(11), py + S(9), cw * 0.42, S(7), mute, 0.55)
                    bar(bx + S(11), py + S(24), cw * 0.46, S(22), col, 0.9,
                        max(1.0, S(2)))
                    bar(bx + S(11), py + hh - S(14), cw - S(22), S(4),
                        col, 0.45, max(1.0, S(2)))
                py += hh + S(6)

        if show["graph"]:
            def graph_body(x, y, cw, hh):
                bar(x + S(11), y + S(7), cw * 0.5, S(7), mute, 0.5)
                for k in (1, 2):
                    yy = y + S(22) + (hh - S(30)) * k / 3.0
                    dl.add_line(ImVec2(x + S(10), yy),
                                ImVec2(x + cw - S(10), yy),
                                imgui.get_color_u32(
                                    theme_mod.with_alpha(t.border, 0.6)), 1.0)
                pts, n = [], 24
                for i in range(n + 1):
                    f = i / n
                    yy = (y + hh * 0.60 - hh * 0.26 * min(1.0, f * 5.0)
                          - hh * 0.05 * math.sin(f * 9.0))
                    pts.append(ImVec2(x + S(10) + (cw - S(20)) * f, yy))
                dl.add_polyline(pts, imgui.get_color_u32(t.accent),
                                max(1.0, S(2)), 0)
                dl.add_line(ImVec2(x + S(10), y + hh - S(14)),
                            ImVec2(x + cw - S(10), y + hh - S(14)),
                            imgui.get_color_u32(t.info), max(1.0, S(2)))
            card(self.graph_h, body=graph_body)

        if show["controls"]:
            def controls_body(x, y, cw, hh):
                head(x, y, cw)
                half = (cw - S(24)) * 0.5
                for i in range(2):
                    bx = x + S(12) + half * i
                    bar(bx, y + S(34), half * 0.40, S(6), mute, 0.45)
                    bar(bx, y + S(48), half * 0.42, S(22),
                        t.bg, 0.85, max(1.0, S(3)))
                    bar(bx + half * 0.52, y + S(50), half * 0.34, S(18),
                        t.text_mute, 0.22, max(1.0, S(4)))
            card(94, edge=t.accent, body=controls_body)

        if show["job"]:
            def job_body(x, y, cw, hh):
                head(x, y, cw)
                bwid = (cw - S(32)) * 0.5
                for i in range(2):
                    bar(x + S(13) + (bwid + S(8)) * i, y + S(36), bwid, S(26),
                        t.text_mute, 0.26, max(1.0, S(5)))
                bar(x + S(13), y + hh - S(48), cw - S(26), S(34),
                    t.bg, 0.8, max(1.0, S(5)))
            card(128, edge=t.ok, body=job_body)

        if show["model"]:
            def model_body(x, y, cw, hh):
                head(x, y, cw)
                bar(x + S(13), y + S(34), S(30), S(20), t.accent, 1.0,
                    max(1.0, S(2)))
                bar(x + S(48), y + S(40), S(46), S(7), mute, 0.5)
                bar(x + cw - S(70), y + S(40), S(57), S(7), mute, 0.5)
                x1, bx = x + cw - S(13), x + S(13)
                for i in range(14):
                    cwid = (x1 - bx) / 14.0
                    col, al = (t.accent, 1.0) if i < 8 else (t.border, 0.85)
                    bar(bx + cwid * i + S(1), y + S(62),
                        max(1.0, cwid - S(2)), S(7), col, al, max(1.0, S(1.5)))
                dl.add_line(ImVec2(x + S(11), y + S(78)),
                            ImVec2(x + cw - S(11), y + S(78)),
                            imgui.get_color_u32(
                                theme_mod.with_alpha(t.border, 0.6)), 1.0)
                bar(x + S(13), y + S(86), cw * 0.34, S(7), mute, 0.5)
                bar(x + cw - S(13) - cw * 0.22, y + S(86), cw * 0.22, S(7),
                    t.accent, 0.75)
            card(104, edge=t.accent, body=model_body)

        # ---- the terminal strip: a few lines, not a hatch pattern
        ly = p.y + h - pad - log_h
        plate(dl, x0, ly, inner_w, log_h, tm)
        widths = (0.30, 0.46, 0.22, 0.38, 0.52, 0.27)
        step = max(S(15), 4.0)
        for i in range(len(widths)):
            yy = ly + S(12) + i * step
            if yy + S(6) > ly + log_h - S(6):
                break
            bar(x0 + S(12), yy, S(26), S(6), mute, 0.30)     # timestamp
            bar(x0 + S(44), yy, inner_w * widths[i], S(6), mute, 0.45)

        imgui.dummy(ImVec2(w, h))
        imgui.text_colored(t.text_mute,
                           "your window, to scale — card style, which "
                           "sections are on, and the three sliders below")
        imgui.dummy(ImVec2(0, 6))

    def _sec_panel(self):
        if not self._group("PANEL & LAYOUT"):
            return
        t = theme_mod.current()
        self._panel_preview(imgui.get_content_region_avail().x)
        names = list(CARD_STYLES)
        cur = self._pref("card_style", "raised")
        i = names.index(cur) if cur in names else 0
        ni = widgets.combo("card style", names, i, 170.0, key="cardstyle")
        if ni != i:
            self.st.extras["card_style"] = names[ni]
            set_card_style(names[ni], self.st.extras.get("card_shadow", True))
            self._dirty = True
        imgui.same_line(0, 14)
        sh = bool(self.st.extras.get("card_shadow", True))
        nsh = widgets.checkbox("shadow", sh, key="cardshadow")
        if nsh != sh:
            self.st.extras["card_shadow"] = nsh
            set_card_style(self.st.extras.get("card_style", "raised"), nsh)
            self._dirty = True
        imgui.text_colored(t.text_mute,
                           "outlined drops the fill, so the background "
                           "effect shows through the panel")

        imgui.dummy(ImVec2(0, 8))
        widgets.label_underlined("show")
        for k in PANEL_SECTIONS:
            cur_on = self.panel_show.get(k, True)
            new = widgets.checkbox(PANEL_LABELS[k], cur_on, key="pan" + k)
            if new != cur_on:
                self.panel_show[k] = new
                self.st.extras["panel_" + k] = new
                self._dirty = True

        # The three sliders live here rather than in a group of their own:
        # they are proportions of each other, the mock above is what makes
        # that visible, and a preview you have to scroll away from to reach
        # the control it previews is not a preview.
        imgui.dummy(ImVec2(0, 8))
        widgets.label_underlined("sizes")
        self._slider_pref("side panel", "right_w", 372, 300, 560, "%.0f px",
                          apply=lambda x: setattr(self, "right_w", x))
        self._slider_pref("terminal height", "log_h", 178, 90, 460, "%.0f px",
                          apply=lambda x: setattr(self, "log_h", x))
        self._slider_pref("graph height", "graph_h", 104, 60, 240, "%.0f px",
                          apply=lambda x: setattr(self, "graph_h", x))
        imgui.dummy(ImVec2(0, 6))

    def _sec_printer(self):
        if not self._group("PRINTER"):
            return
        t = theme_mod.current()
        self._check_pref("movement controls", "jog_enabled", False,
                         apply=lambda x: setattr(self, "jog_enabled", x))
        # Two short lines, not one long one: `text_colored` does not wrap,
        # and the single-line version ran off the right edge of the panel.
        imgui.text_colored(
            t.text_mute,
            "a jog pad in the side panel, shown only while the printer is "
            "idle — never during a print or a pause")
        imgui.text_colored(
            t.text_mute,
            "hold it to unlock, and it re-locks itself 20 seconds after the "
            "last move")
        imgui.dummy(ImVec2(0, 6))
        imgui.text_colored(t.text_dim, self.client.host or "—")
        imgui.same_line()
        widgets.badge("connected" if self.client.connected else "offline",
                      "ok" if self.client.connected else "error")
        imgui.same_line()
        if widgets.button("Forget", 100, height=26.0):
            self.client.stop()
            self.client.host = ""
            self.client.api_key = ""
            self.save()
            self.screen = "setup"
        imgui.dummy(ImVec2(0, 6))

    def _sec_presets(self):
        if not self._group("PRESETS"):
            return
        t = theme_mod.current()
        imgui.text_colored(t.text_mute,
                           "saves the current colours to "
                           "%APPDATA%/bedside/themes, and they appear as a "
                           "card above")
        imgui.set_next_item_width(240)
        _, self.preset_name = imgui.input_text_with_hint(
            "##presetname", "preset name", self.preset_name)
        imgui.same_line()
        if widgets.button("Save preset", 150, height=26.0,
                          enabled=bool(self.preset_name.strip())):
            try:
                theme_mod.save_preset(theme_mod.current(),
                                      self.preset_name.strip(), self.st.app)
                self.log.add("preset saved: " + self.preset_name.strip(),
                             "ok", "ui")
                self.preset_name = ""
            except Exception as exc:
                self.log.add("preset save failed: %s" % exc, "error", "ui")
        imgui.dummy(ImVec2(0, 6))

    def _sec_diagnostics(self):
        if not self._group("DIAGNOSTICS"):
            return
        t = theme_mod.current()
        d = self.view.display
        io = imgui.get_io()
        v = self.view
        gpu = bool(v.use_gl and getattr(v, "_gl", None) is not None)
        imgui.text_colored(t.ok if gpu else t.warn,
                           "renderer: GPU (depth buffer)" if gpu
                           else "renderer: CPU fallback")
        if v.gl_error:
            imgui.text_colored(t.text_mute, v.gl_error[:120])
        imgui.text_colored(t.text_mute, "%.0f fps" % io.framerate)
        if d is not None:
            imgui.same_line()
            imgui.text_colored(
                t.text_mute,
                "·  {:,} pts  ·  {:,} of {:,} shown / {:,} total runs"
                "  ·  stride {}".format(len(d.pts), d.runs_kept,
                                        d.runs_visible, d.runs_total,
                                        d.stride))
        imgui.dummy(ImVec2(0, 4))
        if widgets.button("Copy report", 150, height=28.0):
            imgui.set_clipboard_text(self._diag_report())
            self.log.add("diagnostics copied to the clipboard", "ok", "ui")
            sound.play("ok")
        imgui.same_line(0, 10)
        imgui.text_colored(t.text_mute,
                           "everything a bug report needs, and no host or "
                           "API key in it")
        imgui.dummy(ImVec2(0, 6))

    def _diag_report(self) -> str:
        """A paste-ready dump for an issue.

        Deliberately no host and no API key: the natural thing to do with
        this is paste it into a public tracker, and a monitor that leaks
        the credentials to its own printer in a bug report would be a poor
        trade for saving someone one line of typing.
        """
        import platform
        io = imgui.get_io()
        v, d = self.view, self.view.display
        tp = self._applied
        ex = self.st.extras
        rows = [
            f"Bedside {__version__}",
            f"python   {platform.python_version()}  {platform.platform()}",
            f"renderer {'GPU' if v.use_gl and v._gl is not None else 'CPU'}"
            f"  {v.gl_error or ''}".rstrip(),
            f"fps      {io.framerate:.0f}   window {io.display_size.x:.0f}"
            f"x{io.display_size.y:.0f}",
            f"printer  {self.client.state_text}"
            f"  (connected={self.client.connected})",
        ]
        if tp is not None:
            rows.append(f"model    {len(tp):,} segments, {tp.arcs:,} arcs, "
                        f"layer {tp.layer_h:.3f} mm")
        if d is not None:
            rows.append(f"display  {len(d.pts):,} pts, {d.runs_kept:,}/"
                        f"{d.runs_visible:,} of {d.runs_total:,} runs")
        rows.append("features " + ", ".join(
            sorted(gcode.FEATURE_NAMES.get(f, str(f)) for f in v.visible)))
        keys = ("detail", "line_mm", "fade", "ghost", "clip", "floor_alpha",
                "card_style", "effect", "effect_intensity", "effect_over",
                "sound_set", "face_ui", "face_display")
        rows.append("settings " + "  ".join(
            f"{k}={ex.get(k)!r}" for k in keys if k in ex))
        return "\n".join(rows)

    def _sec_alerts(self):
        self._group("notifications")
        t = theme_mod.current()
        self._check_pref("desktop toasts", "toasts", True,
                         apply=self._set_toasts)
        imgui.text_colored(t.text_mute,
                           "click-through, drawn over other windows — you "
                           "get one when a print finishes or stops")
        imgui.dummy(ImVec2(0, 6))

    def _sec_about(self):
        self._group("files")
        t = theme_mod.current()
        import os
        with fonts.use("semi"):
            imgui.text_colored(t.text, f"Bedside {__version__}")
        imgui.same_line(0, 10)
        imgui.text_colored(t.text_mute,
                           "Esc back · Ctrl+, settings · F5 reconnect · "
                           "F1 about")
        imgui.dummy(ImVec2(0, 4))
        base = os.path.join(os.environ.get("APPDATA", ""), "bedside")
        for label, path in (("settings", os.path.join(base, "settings.json")),
                            ("themes", os.path.join(base, "themes"))):
            imgui.text_colored(t.text_mute, "%-9s %s" % (label, path))
        imgui.dummy(ImVec2(0, 6))

    def _set_toasts(self, on):
        if not on and self.toasts:
            try:
                self.toasts.stop()
            except Exception:
                pass
            self.toasts = None
            self.log.add("toasts off", "info", "ui")
        elif on and not self.toasts:
            self.log.add("toasts start on next launch", "info", "ui")


    # ------------------------------------------------------------ dashboard

    def _dash(self):
        state, lmsg, lfrac = self._pump()
        snap = self.client.snapshot()
        self._watch_transitions(snap)

        cur_seg = 0
        # Fan duty comes from the G-code file when we have it: M106 is only
        # emitted when the speed changes, so a print that set its fan hours
        # ago never re-sends it and the live value would sit at zero for
        # ever. Live M106 traffic is the fallback for SD prints, where there
        # is no file to read.
        fan = snap.fan
        if self._applied is not None and self._preview_path is None:
            cur_seg = self._applied.index_at(snap.filepos)
            fan = self._applied.fan_at(cur_seg)

        # A monitor that idles to 9fps while the printer is moving reads as
        # a broken frame rate, not as thrift. Hold full rate while printing.
        if snap.printing:
            anim.mark_busy()

        self._topbar(snap)

        avail = imgui.get_content_region_avail()
        top_h = max(220.0, avail.y - self.log_h - GAP)
        left_w = max(320.0, avail.x - self.right_w - GAP)

        imgui.begin_child("##left", ImVec2(left_w, top_h))
        self._left(snap, cur_seg, (state, lmsg, lfrac))
        imgui.end_child()

        imgui.same_line(0.0, 0.0)
        self._splitter(top_h)
        imgui.same_line(0.0, 0.0)
        imgui.begin_child("##right", ImVec2(self.right_w, top_h))
        show = self.panel_show
        if show["temps"]:
            self._temps(snap, fan)
        if show["graph"]:
            self._graph(imgui.get_content_region_avail().x, self.graph_h)
        if show["temps"] or show["graph"]:
            imgui.dummy(ImVec2(0, 2))
        if show["controls"]:
            self._controls(snap)
        self._jog(snap)
        if show["job"]:
            self._job(snap)
        if show["model"]:
            self._model(cur_seg)
        imgui.end_child()

        # One wash for everything below the columns — the seam, the filter
        # row and the ground behind the terminal. Scrimming each piece to
        # its own rect left strips of raw backdrop between them, and a
        # bright scene line landing in one cuts across the controls.
        if self.bg.active:
            sp = imgui.get_cursor_screen_pos()
            scrim(imgui.get_window_draw_list(), sp.x - 6, sp.y - 6,
                  imgui.get_content_region_avail().x + 12,
                  imgui.get_content_region_avail().y + 12,
                  theme_mod.current(), self.scrim, theme_mod.current().rounding)

        imgui.dummy(ImVec2(0, 2))
        self._log_bar()
        self.log.draw(ImVec2(0, max(60.0, self.log_h - 34)))

    def _splitter(self, h):
        """The rule between the 3D view and the instrument panel.

        A ten pixel gap was a gap, not a division — the eye read the cards
        as floating next to the model rather than as a separate column. A
        hairline that fades out at both ends divides without drawing a box,
        and since the boundary is already a setting, it may as well be
        draggable: the slider in Settings still works, this is the same
        value.
        """
        t = theme_mod.current()
        dl = imgui.get_window_draw_list()
        p = imgui.get_cursor_screen_pos()
        imgui.invisible_button("##split", ImVec2(GAP, h))
        hot = imgui.is_item_hovered() or imgui.is_item_active()
        if hot:
            imgui.set_mouse_cursor(imgui.MouseCursor_.resize_ew)
        if imgui.is_item_active():
            d = imgui.get_io().mouse_delta.x
            if abs(d) > 0.01:
                self.right_w = max(300.0, min(560.0, self.right_w - d))
                anim.mark_busy()
        if imgui.is_item_deactivated():
            # Written once, on release. Saving every frame of a drag would
            # rewrite settings.json sixty times a second.
            self.st.extras["right_w"] = round(self.right_w)
            self.save()

        x = p.x + GAP * 0.5
        col = t.accent if hot else t.border
        solid = imgui.get_color_u32(theme_mod.with_alpha(col, 0.9 if hot else 0.7))
        clear = imgui.get_color_u32(theme_mod.with_alpha(col, 0.0))
        fade = min(80.0, h * 0.22)
        dl.add_rect_filled_multi_color(ImVec2(x, p.y), ImVec2(x + 1, p.y + fade),
                                       clear, clear, solid, solid)
        dl.add_rect_filled(ImVec2(x, p.y + fade), ImVec2(x + 1, p.y + h - fade),
                           solid)
        dl.add_rect_filled_multi_color(ImVec2(x, p.y + h - fade),
                                       ImVec2(x + 1, p.y + h),
                                       solid, solid, clear, clear)
        # A grip at the middle, so it looks like something you can take hold
        # of rather than a border that happens to be draggable.
        gy = p.y + h * 0.5
        grip = imgui.get_color_u32(
            theme_mod.with_alpha(t.accent if hot else t.text_mute,
                                 1.0 if hot else 0.75))
        for k in (-7.0, 0.0, 7.0):
            dl.add_circle_filled(ImVec2(x + 0.5, gy + k), 1.7 if hot else 1.3,
                                 grip)

    def _log_bar(self):
        """Follow toggle, noise filters and a text filter, above the log."""
        t = theme_mod.current()
        follow = widgets.checkbox("follow", self.log.follow, key="logfollow")
        if follow != self.log.follow:
            self.log.follow = follow
        imgui.same_line(0, 16)

        imgui.text_colored(t.text_mute, "show:")
        imgui.same_line(0, 8)
        for label, cls in LOG_LABELS:
            key = LOG_KEYS[cls]
            cur = bool(self.st.extras.get(key, False))
            new = widgets.checkbox(label, cur, key="lb" + key)
            if new != cur:
                self.st.extras[key] = new
                self.save()
            imgui.same_line(0, 10)

        imgui.set_next_item_width(190)
        _, self.log_filter = imgui.input_text_with_hint(
            "##logfilter", "filter text…", self.log_filter)
        self.log.filter = self.log_filter
        imgui.same_line(0, 8)
        if widgets.button("clear", 68, height=24.0):
            self.log.clear()
            self._log_hidden = 0
        if self._log_hidden:
            imgui.same_line(0, 12)
            imgui.text_colored(t.text_mute,
                               "{:,} lines hidden".format(self._log_hidden))

    def _topbar(self, snap):
        t = theme_mod.current()
        dl = imgui.get_window_draw_list()
        p = imgui.get_cursor_screen_pos()
        # Measured before anything is placed, so it is the full line width.
        total = imgui.get_content_region_avail().x

        icons.draw("bolt", dl, p.x + 10, p.y + 11, 9.0,
                   imgui.get_color_u32(t.accent))
        imgui.dummy(ImVec2(26, 22))
        imgui.same_line(0, 0)
        with fonts.use("semi"):
            imgui.text_colored(t.text, "Bedside")
        imgui.same_line(0, 14)
        widgets.status_pill("IDLE", "PRINTING", on=snap.printing, key="run")
        imgui.same_line(0, 12)
        imgui.text_colored(t.text_dim, snap.state_text or "—")

        # same_line(offset_from_start_x) positions from the START of the line.
        # The previous form — same_line(0, avail - 150) — added that as
        # *spacing* after an item that had already wrapped to a new line, so
        # the dot, the host and the settings gear were all placed a full
        # window-width off the right edge and were simply never on screen.
        host = (self.client.host or "").replace("http://", "")
        block = 22 + imgui.calc_text_size(host).x + 8 + 34 + 36
        imgui.same_line(max(240.0, total - block))
        col = t.ok if snap.connected else t.danger
        pp = imgui.get_cursor_screen_pos()
        a = 0.55 + 0.45 * anim.pulse(2.2) if snap.connected else 1.0
        dl.add_circle_filled(ImVec2(pp.x + 5, pp.y + 11), 4.0,
                             imgui.get_color_u32(theme_mod.with_alpha(col, a)))
        imgui.dummy(ImVec2(14, 22))
        imgui.same_line(0, 4)
        imgui.text_colored(t.text_mute, host)
        imgui.same_line(0, 8)
        if not snap.connected and self.client.host:
            # Only when it is actually needed: a button that does nothing
            # most of the time is furniture, and this one is the single
            # thing you want within reach when the socket has dropped.
            if widgets.icon_button("refresh", tooltip="reconnect  (F5)"):
                self._reconnect()
            imgui.same_line(0, 6)
        if widgets.icon_button("folder", tooltip="files on the printer"
                               "  (Ctrl+O)"):
            self._open_files()
        imgui.same_line(0, 6)
        if widgets.icon_button("gear", tooltip="settings  (Ctrl+,)"):
            self.screen = "settings"
        widgets.activity_rule(imgui.get_content_region_avail().x,
                              snap.printing, key="top")

    # ---- left column: 3D view and job progress

    # Where the overlay buttons sit, relative to the view's top-right.
    VIEW_BTNS = (("orbit", "refresh", "auto-orbit", 40.0),
                 ("home", "cube", "reset the view (or double-click it)", 76.0))

    def _view_overlay_input(self, vp, size):
        """Submit the overlay's hit boxes *before* the view's own.

        ImGui gives hover to the FIRST item submitted that contains the
        cursor, not the last: `ItemHoverable` bails out as soon as
        `g.HoveredId` belongs to someone else. The view's invisible_button
        covers the whole panel, so submitting it first meant it swallowed
        every click aimed at these buttons — the toggle appeared dead while
        the camera lurched instead. Submitting the small boxes first is the
        fix; their *pixels* are still painted afterwards, on top of the
        rendered frame, because draw order and hit order are independent.
        """
        keep = imgui.get_cursor_screen_pos()
        out = {}
        for key, _icon, tip, dx in self.VIEW_BTNS:
            x, y = vp.x + size.x - dx, vp.y + 10.0
            imgui.set_cursor_screen_pos(ImVec2(x, y))
            imgui.invisible_button(f"##vb{key}", ImVec2(30.0, 30.0))
            out[key] = (x, y, imgui.is_item_hovered(), imgui.is_item_clicked())
            if out[key][2] and tip:
                imgui.set_tooltip(tip)
        imgui.set_cursor_screen_pos(keep)

        if out["orbit"][3]:
            self.view.auto_orbit = not self.view.auto_orbit
        if out["home"][3]:
            self.view.home()
        # A drag cancels auto-orbit, so the saved value follows the live one
        # either way rather than only when the button is pressed.
        if bool(self.st.extras.get("auto_orbit", False)) != self.view.auto_orbit:
            self.st.extras["auto_orbit"] = self.view.auto_orbit
            self.save()
        return out

    def _view_overlay_paint(self, hits):
        """Paint the buttons over the rendered frame."""
        t = theme_mod.current()
        dl = imgui.get_window_draw_list()
        state = {"orbit": self.view.auto_orbit, "home": False}
        for key, icon, _tip, _dx in self.VIEW_BTNS:
            x, y, hot, _ = hits[key]
            on = state[key]
            s = 30.0
            ground = t.accent if on else (t.surface_hover if hot else t.surface)
            alpha = 0.95 if on else (0.9 if hot else 0.62)
            dl.add_rect_filled(ImVec2(x, y), ImVec2(x + s, y + s),
                               imgui.get_color_u32(
                                   theme_mod.with_alpha(ground, alpha)),
                               t.rounding)
            dl.add_rect(ImVec2(x, y), ImVec2(x + s, y + s),
                        imgui.get_color_u32(theme_mod.with_alpha(
                            t.accent if on else t.border, 0.9)),
                        t.rounding, 1.0)
            icons.draw(icon, dl, x + s * 0.5, y + s * 0.5, 7.0,
                       imgui.get_color_u32(t.bg if on else t.text_dim))

    def _left(self, snap, cur_seg, loading):
        t = theme_mod.current()
        avail = imgui.get_content_region_avail()
        # Reserve enough for the filename row, the bar and the two-line
        # metric row underneath, or the values clip off the bottom.
        view_h = max(160.0, avail.y - 138.0)
        vp = imgui.get_cursor_screen_pos()
        size = ImVec2(avail.x, view_h)
        hits = self._view_overlay_input(vp, size)
        self.view.draw(size, cur_seg, loading,
                       live=snap.printing and self._preview_path is None)
        self._view_overlay_paint(hits)
        self._preview_banner(vp, size)

        imgui.dummy(ImVec2(0, 8))
        dl = imgui.get_window_draw_list()

        # The job strip is text on the window ground, not a card.
        if self.bg.active:
            sp = imgui.get_cursor_screen_pos()
            sa = imgui.get_content_region_avail()
            scrim(dl, sp.x - 6, sp.y - 6, sa.x + 12, sa.y + 12,
                  t, self.scrim, t.rounding)

        name = snap.job_file or "—"
        with fonts.use("semi"):
            imgui.text_colored(t.text, name[-58:])
        pct = snap.completion or 0.0
        imgui.same_line()
        rightpad = imgui.get_content_region_avail().x
        imgui.same_line(0, max(4.0, rightpad - 96))
        with fonts.use("big"):
            imgui.text_colored(t.accent, f"{pct:.1f}%")

        p = imgui.get_cursor_screen_pos()
        w = imgui.get_content_region_avail().x
        eased = anim.to("job:pct", pct, 6.0)
        dl.add_rect_filled(ImVec2(p.x, p.y), ImVec2(p.x + w, p.y + 6),
                           imgui.get_color_u32(t.surface), 3.0)
        if eased > 0.05:
            dl.add_rect_filled(ImVec2(p.x, p.y),
                               ImVec2(p.x + w * eased / 100.0, p.y + 6),
                               imgui.get_color_u32(t.accent), 3.0)
        imgui.dummy(ImVec2(w, 14))

        cells = (("ELAPSED", fmt_secs(snap.print_time), t.text),
                 ("REMAINING", fmt_secs(snap.print_left), t.text),
                 ("FINISHES", fmt_clock(snap.print_left), t.text),
                 ("Z", "—" if snap.z is None else f"{snap.z:.2f} mm",
                  t.accent))
        cw = w / len(cells)
        base = imgui.get_cursor_screen_pos()
        mute = imgui.get_color_u32(t.text_mute)
        for i, (label, value, col) in enumerate(cells):
            x = base.x + cw * i
            if i:
                # A hairline between the metrics: four numbers in a row need
                # something to say where one ends and the next begins.
                dl.add_line(ImVec2(x - 12, base.y + 1),
                            ImVec2(x - 12, base.y + 31),
                            imgui.get_color_u32(
                                theme_mod.with_alpha(t.border, 0.8)), 1.0)
            with fonts.use("label"):
                caps(dl, x, base.y + 1, label, mute, 1.4)
            with fonts.use("semi"):
                dl.add_text(ImVec2(x, base.y + 15),
                            imgui.get_color_u32(col), value)
        imgui.dummy(ImVec2(w, 34))

    # ---- right column: temperatures

    def _temps(self, snap, fan):
        t = theme_mod.current()
        dl = imgui.get_window_draw_list()
        w = imgui.get_content_region_avail().x
        cw = (w - 16) / 3.0
        p = imgui.get_cursor_screen_pos()
        h = 88.0
        mute = imgui.get_color_u32(t.text_mute)
        series = self.client.temp_series()

        def spark(x, key, col):
            """The card's own history, behind its number.

            The graph below already plots both curves against a shared
            axis, which is the right tool for comparing them and the wrong
            one for "is this one climbing". Per card, scaled to its own
            range, that question answers itself at a glance.
            """
            vals = [v for v in series.get(key, ()) if v is not None][-60:]
            if len(vals) < 3:
                return
            lo, hi = min(vals), max(vals)
            span = max(hi - lo, 1.0)
            n = len(vals) - 1
            x0, x1 = x + 8, x + cw - 8
            # Stops above the TARGET row rather than running through
            # it: a trace crossing a label makes both harder to read.
            top, bot = p.y + 28.0, p.y + h - 31.0
            pts = [ImVec2(x0 + (x1 - x0) * (i / n),
                          bot - (bot - top) * ((v - lo) / span))
                   for i, v in enumerate(vals)]
            fill = imgui.get_color_u32(theme_mod.with_alpha(col, 0.13))
            aa = dl.flags
            dl.flags = aa & ~imgui.ImDrawListFlags_.anti_aliased_fill.value
            for pa, pb in zip(pts, pts[1:]):
                dl.add_triangle_filled(pa, pb, ImVec2(pb.x, bot), fill)
                dl.add_triangle_filled(pa, ImVec2(pb.x, bot),
                                       ImVec2(pa.x, bot), fill)
            dl.flags = aa
            dl.add_polyline(
                pts, imgui.get_color_u32(theme_mod.with_alpha(col, 0.40)),
                1.3, 0)

        def head(x, label, col, icon, angle=None):
            """Card chrome shared by all three: plate, tracked label, icon."""
            plate(dl, x, p.y, cw, h, t, edge=col)
            with fonts.use("label"):
                caps(dl, x + 13, p.y + 11, label, mute, 1.6)
            ic = imgui.get_color_u32(theme_mod.with_alpha(col, 0.85))
            if angle is None:
                picons.EXTRA[icon](dl, x + cw - 21, p.y + 21, 10.0, ic)
            else:
                picons.fan_at(dl, x + cw - 21, p.y + 21, 11.0, ic, angle)

        def value(x, text, col, unit):
            """Big readout with the unit set small and dim beside it, so the
            number keeps the weight and the panel does not shout twice."""
            with fonts.use("huge"):
                dl.add_text(ImVec2(x + 12, p.y + 24),
                            imgui.get_color_u32(col), text)
                vw = imgui.calc_text_size(text).x
            with fonts.use("semi"):
                dl.add_text(ImVec2(x + 15 + vw, p.y + 38),
                            imgui.get_color_u32(theme_mod.with_alpha(col, 0.55)),
                            unit)

        for i, (key, label) in enumerate((("tool0", "HOTEND"), ("bed", "BED"))):
            actual, target = snap.temps.get(key, (None, None))
            x = p.x + (cw + 8) * i
            # heating = warn, at temperature = its own colour
            heating = target and actual and actual < target - 2
            col = t.warn if heating else (t.accent if key == "tool0" else t.info)
            head(x, label, col, "hotend" if key == "tool0" else "bed")
            spark(x, "t0" if key == "tool0" else "bed", col)
            value(x, "—" if actual is None else f"{actual:.0f}", col, "°C")

            tgt = "—" if not target else f"{target:.0f}°"
            with fonts.use("label"):
                caps(dl, x + 12, p.y + h - 26, "TARGET", mute, 1.2)
            dl.add_text(ImVec2(x + cw - 12 - imgui.calc_text_size(tgt).x,
                               p.y + h - 28),
                        imgui.get_color_u32(t.text_dim), tgt)
            # Heat-up progress. Reads as full once the target is reached,
            # which is exactly when it stops being interesting.
            frac = (actual / target) if (actual and target) else 0.0
            meter(dl, x + 12, x + cw - 12, p.y + h - 12, frac, col, t)

        # ---- fan
        x = p.x + (cw + 8) * 2
        sp = float(fan or 0.0)
        if sp > 0.001:
            # Spin rate follows the duty cycle. Idling at 9fps would render
            # this as a stutter, so hold full rate while it turns.
            self._fan_angle += imgui.get_io().delta_time * (1.5 + 11.0 * sp)
            anim.mark_busy()
        fcol = t.info if sp > 0.001 else t.text_mute
        head(x, "FAN", fcol, "fan", angle=self._fan_angle)
        value(x, f"{sp * 100:.0f}", fcol, "%")
        with fonts.use("label"):
            caps(dl, x + 12, p.y + h - 26,
                 "SPINNING" if sp > 0.001 else "IDLE", mute, 1.2)
        meter(dl, x + 12, x + cw - 12, p.y + h - 12, sp, fcol, t)

        imgui.dummy(ImVec2(w, h + 4))

    def _graph(self, w, h):
        """Temperature history, with the legend inside the plate.

        A legend floating above the card read as a separate thing and cost
        twenty pixels the column does not have; inside, it is a header.
        """
        t = theme_mod.current()
        dl = imgui.get_window_draw_list()
        series = self.client.temp_series()
        p = imgui.get_cursor_screen_pos()
        mute = imgui.get_color_u32(t.text_mute)
        plate(dl, p.x, p.y, w, h, t)

        vals = [v for k in ("t0", "bed") for v in series[k] if v is not None]
        top = max(60.0, max(vals) if vals else 60.0) * 1.18

        # ---- header strip: which colour is which, and the live value
        x = p.x + 10
        for key, colour, name in (("t0", t.accent, "HOTEND"),
                                  ("bed", t.info, "BED")):
            live = [v for v in series[key] if v is not None]
            dl.add_circle_filled(ImVec2(x + 3, p.y + 13), 3.0,
                                 imgui.get_color_u32(colour))
            with fonts.use("label"):
                x = caps(dl, x + 11, p.y + 7, name, mute, 1.2) + 6
            txt = "—" if not live else f"{live[-1]:.1f}°"
            dl.add_text(ImVec2(x, p.y + 5),
                        imgui.get_color_u32(theme_mod.with_alpha(colour, 0.95)),
                        txt)
            x += imgui.calc_text_size(txt).x + 14
        with fonts.use("label"):
            scale = f"MAX {top:.0f}°"
            caps(dl, p.x + w - 10 - caps_width(scale, 1.2), p.y + 7,
                 scale, mute, 1.2)

        # ---- plot area, below the header
        y0 = p.y + 24.0
        ph = h - 30.0
        if ph < 24.0:
            imgui.dummy(ImVec2(w, h + 4))
            return
        for i in range(1, 4):
            y = y0 + ph * i / 4.0
            dl.add_line(ImVec2(p.x + 8, y), ImVec2(p.x + w - 8, y),
                        imgui.get_color_u32(theme_mod.with_alpha(t.border, 0.7)),
                        1.0)
        base_y = y0 + ph

        def points(vs):
            got = [(i, v) for i, v in enumerate(vs) if v is not None]
            if len(got) < 2:
                return []
            n = max(len(vs) - 1, 1)
            return [ImVec2(p.x + 8 + (w - 16) * (i / n),
                           base_y - ph * min(v / top, 1.0))
                    for i, v in got]

        def curve(vs, colour, dashed=False, fill=False):
            out = points(vs)
            if not out:
                return
            if fill:
                # Area under the trace, as triangle pairs. add_convex_poly is
                # no use here: a temperature trace is not a convex outline.
                #
                # Anti-aliasing has to come off for this. ImGui feathers the
                # edge of every filled shape, so each pair of triangles gets
                # a soft seam down its shared diagonal — at ~3px per sample
                # that is a stripe every three pixels across the whole area,
                # which reads as a dither pattern rather than as a fill. The
                # jagged top edge it leaves is covered by the trace drawn
                # over it.
                shade = imgui.get_color_u32(theme_mod.with_alpha(colour, 0.16))
                aa = dl.flags
                dl.flags = aa & ~imgui.ImDrawListFlags_.anti_aliased_fill.value
                for pa, pb in zip(out, out[1:]):
                    dl.add_triangle_filled(pa, pb, ImVec2(pb.x, base_y), shade)
                    dl.add_triangle_filled(pa, ImVec2(pb.x, base_y),
                                           ImVec2(pa.x, base_y), shade)
                dl.flags = aa
            dl.add_polyline(out, imgui.get_color_u32(colour),
                            1.0 if dashed else 1.9, 0)
            if not dashed:
                # A head dot, so the eye finds "now" without hunting.
                dl.add_circle_filled(
                    out[-1], 4.5,
                    imgui.get_color_u32(theme_mod.with_alpha(colour, 0.22)))
                dl.add_circle_filled(out[-1], 2.2, imgui.get_color_u32(colour))

        # Fills first, then a wash that sinks their bottoms into the card, so
        # they read as gradients instead of two flat slabs; traces on top.
        curve(series["bed"], t.info, fill=True)
        curve(series["t0"], t.accent, fill=True)
        clear = imgui.get_color_u32(theme_mod.with_alpha(t.surface, 0.0))
        solid = imgui.get_color_u32(theme_mod.with_alpha(t.surface, 0.6))
        dl.add_rect_filled_multi_color(
            ImVec2(p.x + 1, y0 + ph * 0.5), ImVec2(p.x + w - 1, base_y + 4),
            clear, clear, solid, solid)
        curve(series["t0t"], theme_mod.with_alpha(t.accent, 0.40), True)
        curve(series["bedt"], theme_mod.with_alpha(t.info, 0.40), True)
        curve(series["bed"], t.info)
        curve(series["t0"], t.accent)
        imgui.dummy(ImVec2(w, h + 4))

    # ---- right column: controls

    def _controls(self, snap):
        """One card: a header carrying the lock state, and the setters."""
        t = theme_mod.current()
        dl = imgui.get_window_draw_list()
        locked = snap.printing and not self._unlock
        en = not locked
        mute = imgui.get_color_u32(t.text_mute)

        if snap.printing:
            col = t.accent if locked else t.warn
            msg = "LOCKED" if locked else "UNLOCKED"
        else:
            col, msg = t.ok, "IDLE"

        p = imgui.get_cursor_screen_pos()
        w = imgui.get_content_region_avail().x
        fh = imgui.get_frame_height()
        h = HEAD_H + 6.0 + 14.0 + fh + 12.0
        plate(dl, p.x, p.y, w, h, t, edge=col)
        by = card_head(dl, p.x, p.y, w, t, "sliders", "CONTROLS",
                       right=msg, right_col=col, dot=col)

        half = (w - 24.0) * 0.5
        for i, (label, attr, key, path, body) in enumerate((
                ("HOTEND", "set_t0", "settemp_tool", "/api/printer/tool",
                 lambda v: {"command": "target", "targets": {"tool0": v}}),
                ("BED", "set_bed", "settemp_bed", "/api/printer/bed",
                 lambda v: {"command": "target", "target": v}))):
            x0 = p.x + 13 + half * i
            with fonts.use("label"):
                caps(dl, x0, by + 7, label, mute, 1.4)
            row = by + 22.0
            imgui.set_cursor_screen_pos(ImVec2(x0, row))
            imgui.set_next_item_width(58)
            changed, val = imgui.input_int(f"##{key}", getattr(self, attr), 0, 0)
            if changed:
                setattr(self, attr, val)
            dl.add_text(ImVec2(x0 + 63, row + (fh - 18) * 0.5), mute,
                        "\u00b0C")
            imgui.set_cursor_screen_pos(
                ImVec2(x0 + 88, row + (fh - 28) * 0.5))
            if widgets.button("set", 54, height=28.0, enabled=en,
                              key=f"set{key}"):
                self.client.command(path, body(int(getattr(self, attr))))

        if snap.printing:
            # Right-aligned on the label row, so it sits with the state it
            # governs rather than in a row of its own.
            imgui.set_cursor_screen_pos(ImVec2(p.x + w - 96, by + 2))
            u = widgets.checkbox("unlock", self._unlock, key="unlock")
            if u != self._unlock:
                self._unlock = u

        imgui.set_cursor_screen_pos(p)
        imgui.dummy(ImVec2(w, h))

        # No jog controls, deliberately. Moving the head or the bed during a
        # print ruins it, and "disabled behind an unlock checkbox" is still
        # one mis-click from doing exactly that. There is no version of this
        # panel where a jog button is worth the risk — the printer's own LCD
        # and the OctoPrint web UI both still have them for when the machine
        # is idle.

    def _job(self, snap):
        """One card: pause / cancel, and the G-code box when it is safe."""
        t = theme_mod.current()
        dl = imgui.get_window_draw_list()
        locked = snap.printing and not self._unlock
        en = not locked
        run = snap.printing and self._unlock
        mute = imgui.get_color_u32(t.text_mute)

        # Declines rather than clipping, the way the model card does. On an
        # idle machine every button in here is disabled anyway, so it is
        # the right thing to give up the space to the movement pad.
        if imgui.get_content_region_avail().y < 96.0:
            return

        p = imgui.get_cursor_screen_pos()
        w = imgui.get_content_region_avail().x
        fh = imgui.get_frame_height()
        row2 = fh + 10.0
        h = HEAD_H + 6.0 + 30.0 + 8.0 + row2 + 12.0
        state = ("PAUSED" if snap.paused else "RUNNING") if snap.printing \
            else "IDLE"
        scol = t.warn if snap.paused else (t.ok if snap.printing
                                           else t.text_mute)
        plate(dl, p.x, p.y, w, h, t, edge=scol)
        by = card_head(dl, p.x, p.y, w, t, "printer", "JOB",
                       right=state, right_col=scol)

        bw = (w - 34.0) * 0.5
        imgui.set_cursor_screen_pos(ImVec2(p.x + 13, by + 6))
        if widgets.button("Pause" if not snap.paused else "Resume", bw,
                          height=30.0, primary=run and not snap.paused,
                          enabled=run,
                          icon="pause" if not snap.paused else "play"):
            self.client.command("/api/job", {"command": "pause",
                                             "action": "toggle"})
        imgui.set_cursor_screen_pos(ImVec2(p.x + 21 + bw, by + 6))
        if widgets.button("Cancel", bw, height=30.0, enabled=run,
                          icon="stop"):
            imgui.open_popup("Cancel print###askcancel")
        self._confirm_cancel()

        ry = by + 44.0
        if snap.printing:
            # Hidden outright while a job runs — a text box that accepts
            # "G1 X0 Y0" is a jog control with extra steps.
            grad_fill(dl, p.x + 12, ry, w - 24, row2, t,
                      theme_mod.with_alpha(t.bg, 0.55),
                      theme_mod.with_alpha(t.bg, 0.9))
            icons.draw("warning", dl, p.x + 28, ry + row2 * 0.5, 6.0, mute)
            dl.add_text(ImVec2(p.x + 40, ry + (row2 - 18) * 0.5), mute,
                        "manual G-code hidden while printing")
        else:
            imgui.set_cursor_screen_pos(
                ImVec2(p.x + 13, ry + (row2 - fh) * 0.5))
            imgui.set_next_item_width(w - 94)
            entered, self.gcode_field = imgui.input_text_with_hint(
                "##gcode", "G-code\u2026", self.gcode_field,
                imgui.InputTextFlags_.enter_returns_true)
            imgui.set_cursor_screen_pos(
                ImVec2(p.x + w - 71, ry + (row2 - 28) * 0.5))
            if widgets.button("Send", 58, height=28.0, enabled=en) or entered:
                self._send_gcode(snap)

        imgui.set_cursor_screen_pos(p)
        imgui.dummy(ImVec2(w, h))

    # How long the unlock must be held, and how long it stays unlocked with
    # nothing pressed. Both deliberately awkward.
    JOG_HOLD = 0.7
    JOG_ARMED = 20.0
    JOG_STEPS = (0.1, 1.0, 10.0)

    def _jog_idle(self, snap):
        """Movement is offered only on a machine that is doing nothing.

        Not "disabled while printing" — *absent*. A control that exists
        while a job runs is one bad frame, one stale flag or one mis-click
        away from ruining nine hours of work, and there is no arrangement
        of confirmations that makes it worth having there. The printer's
        own LCD is still right next to the printer.
        """
        return bool(self.jog_enabled and snap.connected and not snap.printing
                    and (snap.flags or {}).get("operational"))

    def _jog_send(self, snap, axis=None, delta=0.0, home=False):
        """Re-checks the state it was drawn under, immediately before send.

        The printing flag comes from the last socket push and can lag the
        printer by a frame, so a guard that lives only in the draw code can
        be raced by one. OctoPrint refuses these endpoints while printing
        too — this is the belt to that braces.
        """
        if not self._jog_idle(snap):
            self.log.add("movement refused: printer is not idle", "warn", "ui")
            return
        if home:
            self.client.command("/api/printer/printhead",
                                {"command": "home", "axes": ["x", "y", "z"]})
            self.log.add("homing all axes", "info", "ui")
        else:
            self.client.command(
                "/api/printer/printhead",
                {"command": "jog", axis: delta, "absolute": False,
                 "speed": 1500 if axis == "z" else 3000})
        self._jog_until = imgui.get_time() + self.JOG_ARMED
        sound.play("click")

    def _jog(self, snap):
        """Arm-by-hold, auto-disarming movement pad."""
        if not self._jog_idle(snap):
            self._jog_until = 0.0
            self._jog_hold = 0.0
            return
        t = theme_mod.current()
        dl = imgui.get_window_draw_list()
        mute = imgui.get_color_u32(t.text_mute)
        now = imgui.get_time()
        armed = now < self._jog_until
        avail = imgui.get_content_region_avail()
        # Two armed layouts. The cross pad is the one you want — the
        # spatial mapping is what stops a slip landing on the wrong axis —
        # but it needs height the panel does not always have. Shrinking its
        # buttons to fit would trade one mis-click risk for a worse one, so
        # below the threshold it becomes grouped rows at full button size
        # instead.
        cross = avail.y >= 176.0
        h = (170.0 if cross else 118.0) if armed else 74.0
        if avail.y < h + 4:
            return                      # no room at all: leave it out
        w = avail.x
        p = imgui.get_cursor_screen_pos()
        edge = t.warn if armed else t.text_mute
        plate(dl, p.x, p.y, w, h, t, edge=edge)
        left = max(0.0, self._jog_until - now)
        by = card_head(dl, p.x, p.y, w, t, "sliders", "MOVE",
                       right=(f"{left:.0f}S LEFT" if armed else "LOCKED"),
                       right_col=edge, dot=edge if armed else None)

        if not armed:
            # Hold, not click. A click is exactly the thing being guarded
            # against, so the gesture has to be one you cannot make by
            # accident with a stray cursor.
            bw = w - 26.0
            imgui.set_cursor_screen_pos(ImVec2(p.x + 13, by + 8))
            imgui.invisible_button("##jogarm", ImVec2(bw, 30.0))
            held = imgui.is_item_active()
            hot = imgui.is_item_hovered()
            io = imgui.get_io()
            if held:
                self._jog_hold += io.delta_time
                anim.mark_busy()
                if self._jog_hold >= self.JOG_HOLD:
                    self._jog_until = now + self.JOG_ARMED
                    self._jog_hold = 0.0
                    sound.play("ok")
            else:
                self._jog_hold = max(0.0, self._jog_hold - io.delta_time * 2.0)
            frac = min(1.0, self._jog_hold / self.JOG_HOLD)
            dl.add_rect_filled(
                ImVec2(p.x + 13, by + 8), ImVec2(p.x + 13 + bw, by + 38),
                imgui.get_color_u32(theme_mod.with_alpha(
                    t.surface_hover if hot else t.bg, 0.9)), t.rounding)
            if frac > 0.001:
                dl.add_rect_filled(
                    ImVec2(p.x + 13, by + 8),
                    ImVec2(p.x + 13 + bw * frac, by + 38),
                    imgui.get_color_u32(theme_mod.with_alpha(t.warn, 0.55)),
                    t.rounding)
            label = "hold to unlock movement"
            with fonts.use("semi"):
                tw = imgui.calc_text_size(label).x
                dl.add_text(ImVec2(p.x + 13 + (bw - tw) * 0.5, by + 14),
                            imgui.get_color_u32(t.text_dim), label)
            imgui.set_cursor_screen_pos(p)
            imgui.dummy(ImVec2(w, h))
            return

        # ---- step size, and home, on one row so the pad keeps its height
        sx = p.x + 13
        for st_ in self.JOG_STEPS:
            on = abs(self._jog_step - st_) < 1e-6
            imgui.set_cursor_screen_pos(ImVec2(sx, by + 5))
            if widgets.button(f"{st_:g}", 44, height=26.0, primary=on,
                              key=f"jogstep{st_:g}"):
                self._jog_step = st_
            sx += 48
        with fonts.use("label"):
            caps(dl, sx + 2, by + 12, "MM", mute, 1.2)
        imgui.set_cursor_screen_pos(ImVec2(p.x + w - 99, by + 5))
        if widgets.button("Home all", 86, height=26.0, key="joghome"):
            self._jog_send(snap, home=True)

        # ---- the pad. Z sits apart from X/Y on purpose: it is the axis
        # that can drive the nozzle into the bed, and it has no business
        # sharing a cluster with the harmless ones.
        step = self._jog_step
        bs, gap = 30.0, 4.0
        span = bs + gap
        oy = by + 37
        z = snap.z
        # Z down needs a known height AND a move that keeps the nozzle at
        # or above the bed. With no reported Z there is no way to tell a
        # safe move from a crash, so it does not guess.
        down_ok = z is not None and (z - step) >= -1e-6
        down_tip = (None if down_ok else
                    ("Z is unknown \u2014 home first" if z is None
                     else f"would go below the bed (Z {z:.2f})"))

        def pad(cx, cy, label, axis, delta, enabled=True, tip=None):
            imgui.set_cursor_screen_pos(ImVec2(cx, cy))
            if widgets.button(label, bs, height=bs, enabled=enabled,
                              key=f"jog{label}{axis}", tooltip=tip):
                self._jog_send(snap, axis, delta)

        if cross:
            ox = p.x + 16
            pad(ox + span, oy, "Y+", "y", step)
            pad(ox, oy + span, "X-", "x", -step)
            pad(ox + 2 * span, oy + span, "X+", "x", step)
            pad(ox + span, oy + 2 * span, "Y-", "y", -step)
            # The middle of the pad is not a button. It is the square a
            # slipped cursor lands on, so it holds the step size instead
            # of an action.
            cx0, cy0 = ox + span, oy + span
            dl.add_rect_filled(ImVec2(cx0, cy0), ImVec2(cx0 + bs, cy0 + bs),
                               imgui.get_color_u32(
                                   theme_mod.with_alpha(t.bg, 0.55)),
                               t.rounding)
            with fonts.use("label"):
                lab = f"{step:g}"
                caps(dl, cx0 + (bs - caps_width(lab, 1.2)) * 0.5, cy0 + 10,
                     lab, mute, 1.2)
            zx = ox + 3 * span + 16
            pad(zx, oy, "Z+", "z", step)
            with fonts.use("label"):
                caps(dl, zx + 11, oy + span + 10, "Z", mute, 1.2)
            pad(zx, oy + 2 * span, "Z-", "z", -step, enabled=down_ok,
                tip=down_tip)
        else:
            # Grouped in pairs with a wide gutter between axes, so the
            # neighbour of a button is always its own opposite — the one
            # press whose worst case is undoing the last one.
            ox = p.x + 14
            group = 2 * span + 18
            pad(ox, oy, "X-", "x", -step)
            pad(ox + span, oy, "X+", "x", step)
            pad(ox + group, oy, "Y-", "y", -step)
            pad(ox + group + span, oy, "Y+", "y", step)
            pad(ox + 2 * group, oy, "Z-", "z", -step, enabled=down_ok,
                tip=down_tip)
            pad(ox + 2 * group + span, oy, "Z+", "z", step)

        imgui.set_cursor_screen_pos(p)
        imgui.dummy(ImVec2(w, h))

    def _model(self, cur_seg):
        """What is actually being built, in the space the controls leave.

        Everything here comes from the parsed file rather than from the
        printer, so it stays right even when OctoPrint's own estimates
        drift, and it costs nothing per frame. It drops rows as the space
        shrinks, and the heights snap to whole rows: interpolating instead
        clips the last line of text against the bottom of the column, which
        reads as a broken card rather than as a compact one.
        """
        t = theme_mod.current()
        avail = imgui.get_content_region_avail()
        h = next((x for x in (104.0, 78.0, 60.0) if avail.y - 2.0 >= x), None)
        if h is None:
            return                      # a short window: leave it out
        w = avail.x
        dl = imgui.get_window_draw_list()
        p = imgui.get_cursor_screen_pos()
        mute = imgui.get_color_u32(t.text_mute)

        tp = self._applied
        if tp is None or not len(tp.pts):
            h = min(h, 56.0)
            plate(dl, p.x, p.y, w, h, t, edge=theme_mod.with_alpha(t.accent, 0.5))
            card_head(dl, p.x, p.y, w, t, "cube", "MODEL")
            dl.add_text(ImVec2(p.x + 14, p.y + HEAD_H + 7), mute,
                        "no file loaded")
            imgui.dummy(ImVec2(w, h))
            return

        # tp.hi is the highest *extruding* point; the raw path climbs past
        # it on the end-of-print lift, so an unclamped reading shows a Z
        # above the model's own height ("42.60 / 32.40 mm").
        span = float(tp.hi[2] - tp.lo[2])
        total = max(1, int(round(span / tp.layer_h)) + 1)
        z = min(tp.z_at(cur_seg), float(tp.hi[2]))
        cur = max(1, min(total,
                         int(round((z - float(tp.lo[2])) / tp.layer_h)) + 1))
        pct = (cur - 1) / max(total - 1, 1)
        dims = tp.hi - tp.lo

        plate(dl, p.x, p.y, w, h, t, edge=t.accent)
        by = card_head(dl, p.x, p.y, w, t, "cube", "MODEL",
                       right=f"{pct * 100:.0f}% OF HEIGHT")

        # ---- the layer count is the number that changes, so it leads
        with fonts.use("big"):
            num = str(cur)
            dl.add_text(ImVec2(p.x + 14, by + 6),
                        imgui.get_color_u32(t.accent), num)
            nw = imgui.calc_text_size(num).x
        dl.add_text(ImVec2(p.x + 20 + nw, by + 13),
                    imgui.get_color_u32(t.text_dim), f"/ {total} layers")
        dz = f"{z:.2f} / {float(tp.hi[2]):.2f} mm"
        with fonts.use("semi"):
            dl.add_text(ImVec2(p.x + w - 14 - imgui.calc_text_size(dz).x,
                               by + 12),
                        imgui.get_color_u32(t.text), dz)

        # ---- layer ladder: one cell per band, not per layer. At 200 layers
        # across 340 pixels a true per-layer tick would be sub-pixel and
        # would alias into a grey smear — a texture, not a progress bar.
        if h >= 78.0:
            cells = 34
            x0, x1 = p.x + 14, p.x + w - 14
            cw = (x1 - x0) / cells
            y = by + 38
            done = pct * cells
            for i in range(cells):
                fill = max(0.0, min(1.0, done - i))
                cx0, cx1 = x0 + cw * i + 0.7, x0 + cw * (i + 1) - 0.7
                if 0.001 < fill < 0.999:
                    # The cell being printed pulses, and throws a little
                    # light either side of itself: on a 34-cell bar the
                    # leading edge is the only part worth finding quickly.
                    glow = 0.55 + 0.45 * anim.pulse(2.0)
                    dl.add_rect_filled(
                        ImVec2(cx0 - 5, y - 1), ImVec2(cx1 + 5, y + 8),
                        imgui.get_color_u32(
                            theme_mod.with_alpha(t.accent_bright,
                                                 0.18 * glow)), 3.0)
                    col = theme_mod.with_alpha(t.accent_bright, glow)
                    anim.mark_busy()
                else:
                    col = theme_mod.lerp(theme_mod.with_alpha(t.border, 0.85),
                                         t.accent, fill)
                dl.add_rect_filled(ImVec2(cx0, y), ImVec2(cx1, y + 7),
                                   imgui.get_color_u32(col), 1.5)

        # ---- footprint, and what the nozzle is laying down right now
        if h >= 104.0:
            ly = by + 52
            dl.add_line(ImVec2(p.x + 12, ly), ImVec2(p.x + w - 12, ly),
                        imgui.get_color_u32(
                            theme_mod.with_alpha(t.border, 0.6)), 1.0)
            with fonts.use("label"):
                caps(dl, p.x + 14, ly + 9, "SIZE", mute, 1.2)
            dl.add_text(
                ImVec2(p.x + 56, ly + 6), imgui.get_color_u32(t.text),
                f"{dims[0]:.0f} \u00d7 {dims[1]:.0f} \u00d7 {dims[2]:.0f} mm")
            if tp.has_features and len(tp.feat):
                i = max(0, min(cur_seg, len(tp.feat) - 1))
                name = gcode.FEATURE_NAMES.get(int(tp.feat[i]), "other")
                nw = imgui.calc_text_size(name).x
                dl.add_text(ImVec2(p.x + w - 14 - nw, ly + 6),
                            imgui.get_color_u32(t.accent), name)
                with fonts.use("label"):
                    caps(dl, p.x + w - 20 - nw - caps_width("NOW", 1.2),
                         ly + 9, "NOW", mute, 1.2)
        imgui.dummy(ImVec2(w, h))

    def _send_gcode(self, snap):
        cmd = self.gcode_field.strip()
        if not cmd:
            return
        # Second line of defence. The box is hidden while printing, but the
        # printing flag comes from the last socket push and can lag the
        # printer by a frame — so the refusal lives on the send path too,
        # where it cannot be raced.
        if snap.printing and is_motion_command(cmd):
            self.log.add(f"refused while printing: {cmd}", "warn", "ui")
            sound.play("error")
            return
        self.client.command("/api/printer/command", {"command": cmd})
        self.gcode_field = ""

    def _confirm_cancel(self):
        t = theme_mod.current()
        vp = imgui.get_main_viewport()
        imgui.set_next_window_pos(
            ImVec2(vp.pos.x + vp.size.x * 0.5, vp.pos.y + vp.size.y * 0.5),
            imgui.Cond_.appearing, ImVec2(0.5, 0.5))
        if imgui.begin_popup_modal("Cancel print###askcancel", None,
                                   imgui.WindowFlags_.always_auto_resize)[0]:
            imgui.text_colored(t.text, "Cancel the running print?")
            imgui.text_colored(t.text_mute, "This cannot be undone.")
            imgui.dummy(ImVec2(0, 8))
            if widgets.button("Cancel print", 130, height=30.0, primary=True):
                self.client.command("/api/job", {"command": "cancel"})
                imgui.close_current_popup()
            imgui.same_line()
            if widgets.button("Keep printing", 130, height=30.0):
                imgui.close_current_popup()
            imgui.end_popup()


def _use_our_assets():
    """Make `assets/` ours, so the window and taskbar show the Bedside mark.

    hello_imgui takes its window icon from `app_settings/icon.png` *inside
    the assets folder*, and its default assets folder is the one shipped
    with imgui_bundle — which is why the app was wearing the bundle's own
    icon. Setting ours as the main folder and keeping the bundle's as a
    search path gives us the icon without losing its fonts: the docs say
    the main folder is consulted first and the first match wins.
    """
    if getattr(sys, "frozen", False):
        # onedir PyInstaller puts datas in _internal/, which is _MEIPASS.
        root = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ours = os.path.join(root, "assets")
    if not os.path.isdir(ours):
        return
    try:
        import imgui_bundle
        stock = os.path.join(os.path.dirname(imgui_bundle.__file__), "assets")
        hello_imgui.set_assets_folder(ours)
        if os.path.isdir(stock):
            hello_imgui.add_assets_search_path(stock)
    except Exception:
        pass            # a missing icon is not worth failing to start over


def run():
    app = App()
    params = hello_imgui.RunnerParams()
    params.app_window_params.window_title = "Bedside"
    params.app_window_params.window_geometry.size = (1300, 880)
    params.imgui_window_params.default_imgui_window_type = (
        hello_imgui.DefaultImGuiWindowType.provide_full_screen_window)
    params.imgui_window_params.show_menu_bar = False
    params.callbacks.show_gui = app.frame
    _use_our_assets()

    vui.install(params, theme_=app.st.build_theme(),
                faces=build_faces(app.st.extras))
    # Corner/border overrides live in extras, so they land after install.
    params.callbacks.post_init = app._apply_theme
    try:
        hello_imgui.run(params)
    finally:
        app.client.stop()
        # Without this the layered overlay can outlive the app and leave a
        # toast painted on the desktop.
        if app.toasts:
            try:
                app.toasts.stop()
            except Exception:
                pass
        app.save()
