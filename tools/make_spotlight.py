"""Record the looping spotlight GIF that plays on the README.

    .venv\\Scripts\\python.exe tools/make_spotlight.py path/to/model.gcode

hello_imgui only exposes a *final* screenshot, so frames are read out of the
back buffer in the `before_swap` callback — after ImGui has rendered and
before the swap, which is the one moment the composed frame exists.

The loop is made exact rather than trimmed: the camera is driven to complete
one full turn over the frame count, and the backdrop clock is driven to a
whole number of its own scroll periods, so the last frame hands over to the
first with nothing to hide.
"""

from __future__ import annotations

import collections
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from imgui_bundle import hello_imgui, imgui, ImVec2          # noqa: E402
from OpenGL import GL                                        # noqa: E402
import gifwrite                                              # noqa: E402
from bedside import app as A                                # noqa: E402
from bedside.client import Snapshot                         # noqa: E402
from bedside.gcode import parse                             # noqa: E402

# Record close to the size it will be shown at. A 2x downscale turns 12px
# UI text into mush — the app's own name in the topbar stopped being
# readable, which rather defeats a spotlight.
WIN = (1100, 860)
OUT_W = 880
FRAMES = 36
DELAY_CS = 10                # 100 ms a frame, 10 fps
GRID_PERIOD = 1.0 / 0.45     # the grid scene scrolls at t * 0.45
BG_SECONDS = GRID_PERIOD * 2


class FakeClient:
    """No sockets: a recording must not depend on a printer being on."""

    def __init__(self, *a, **kw):
        self.host, self.api_key = "octopi.local", "x" * 8
        self.job_file, self.job_origin, self.job_generation = (
            "CE3E3V2_Back_Extender.gcode", "local", 0)
        self.completion = 68.4
        self.connected, self.error, self.state_text = True, "", "Printing"
        self.hist = {k: collections.deque(maxlen=120)
                     for k in ("t0", "t0t", "bed", "bedt")}
        for i in range(120):
            f = i / 119.0
            self.hist["t0"].append(24 + 186 * min(1.0, f * 3.2))
            self.hist["t0t"].append(210.0)
            self.hist["bed"].append(24 + 36 * min(1.0, f * 2.1))
            self.hist["bedt"].append(60.0)

    def start(self): pass
    def stop(self): pass
    def command(self, *a, **kw): pass
    def drain_logs(self): return []
    def temp_series(self): return {k: list(v) for k, v in self.hist.items()}

    def snapshot(self):
        return Snapshot(
            connected=True, error="", state_text="Printing",
            flags={"printing": True}, job_file=self.job_file,
            job_origin="local", job_size=17_300_000, completion=68.4,
            filepos=11_833_200, print_time=8412, print_left=3960,
            temps={"tool0": (208.6, 210.0), "bed": (60.1, 60.0)},
            z=21.6, fan=1.0)


def record(gcode_path):
    A.OctoClient = FakeClient
    app = A.App()
    app.client = FakeClient()
    app.screen = "dash"
    app.toasts = None
    app.save = lambda: None                 # never touch real settings
    tp = parse(open(gcode_path, "rb").read())
    app._applied = tp
    app.view.set_toolpath(tp)
    app.loader.snapshot = lambda: ("ready", "cached", 1.0, tp)

    # Recording sizes, not the author's saved ones: the whole side panel
    # including the model card has to fit, or the showcase shows a clipped
    # card. These only live in this process.
    app.right_w, app.log_h, app.graph_h = 372.0, 150.0, 120.0
    app.panel_show = {k: True for k in A.PANEL_SECTIONS}

    app.bg.set("grid")
    app.bg.intensity = 1.25
    app.fx.set("none")
    app.view.auto_orbit = False             # driven by hand, for the loop
    app.view.pitch, app.view.zoom = -0.5, 1.15

    # The app asks the backdrop for `imgui.get_time()`; give it a clock that
    # divides evenly into the scene's own period instead.
    real_render = app.bg.render
    state = {"i": 0, "frames": [], "size": WIN}

    def looped(w, h, _now, *a, **kw):
        t = BG_SECONDS * (state["i"] % FRAMES) / FRAMES
        return real_render(w, h, t, *a, **kw)

    app.bg.render = looped

    # The camera is eased, so setting `view.yaw` sets a target the render
    # chases and never catches. Over a loop that costs the last slice of
    # the turn AND leaves the first frames rotating slower than the rest,
    # so the model visibly jumps back at the seam. For a recording the
    # yaw wants to be exactly what was asked for, so it is passed through.
    from vertexui import anim as _anim
    _real_to = _anim.to

    def _exact(key, target, speed=14.0):
        if key.endswith(":yaw"):
            return target
        return _real_to(key, target, speed)

    _anim.to = _exact

    params = hello_imgui.RunnerParams()
    params.app_window_params.window_title = "Bedside"
    params.app_window_params.window_geometry.size = WIN
    params.imgui_window_params.default_imgui_window_type = (
        hello_imgui.DefaultImGuiWindowType.provide_full_screen_window)
    params.imgui_window_params.show_menu_bar = False

    WARMUP = 14                             # let fonts, mesh and eases settle

    def gui():
        io = imgui.get_io()
        fb = getattr(io, "display_framebuffer_scale", None)
        sx = float(fb.x) if fb is not None else 1.0
        sy = float(fb.y) if fb is not None else 1.0
        state["size"] = (max(1, int(io.display_size.x * sx)),
                         max(1, int(io.display_size.y * sy)))
        i = state["i"] - WARMUP
        yaw0 = -0.9
        if i >= 0:
            app.view.yaw = yaw0 + 2.0 * np.pi * (i % FRAMES) / FRAMES
        app.frame()

    def grab():
        if state["i"] < WARMUP or len(state["frames"]) >= FRAMES:
            state["i"] += 1
            if len(state["frames"]) >= FRAMES:
                params.app_shall_exit = True
            return
        # NOT the GL viewport: the toolpath and the backdrop each render
        # into their own framebuffer and leave the viewport set to its
        # size, so reading it here captures a crop of the bottom-left
        # corner. ImGui knows the real window.
        w, h = state["size"]
        GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
        buf = GL.glReadPixels(0, 0, w, h, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
        img = np.frombuffer(buf, np.uint8).reshape(h, w, 3)[::-1]  # GL is bottom-up
        state["frames"].append(np.ascontiguousarray(img))
        state["i"] += 1

    params.callbacks.show_gui = gui
    params.callbacks.before_swap = grab

    import vertexui as vui
    A._use_our_assets()
    vui.install(params, theme_=app.st.build_theme(),
                faces=A.build_faces(app.st.extras))
    hello_imgui.run(params)
    return state["frames"]


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: make_spotlight.py <model.gcode> [out.gif]")
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(HERE), "docs", "spotlight.gif")

    frames = record(src)
    print(f"captured {len(frames)} frames at {frames[0].shape[1]}x"
          f"{frames[0].shape[0]}")

    import cv2
    scale = OUT_W / frames[0].shape[1]
    small = [cv2.resize(f, (OUT_W, int(round(f.shape[0] * scale))),
                        interpolation=cv2.INTER_AREA) for f in frames]

    # The still comes out of the same recording, so the two can never
    # drift apart — and it is full resolution, not an upscaled GIF frame.
    from makeicon import png_bytes
    still = frames[len(frames) // 4]
    rgba = np.dstack([still, np.full(still.shape[:2], 255, np.uint8)])
    shot = os.path.join(os.path.dirname(out), "screenshot.png")
    open(shot, "wb").write(png_bytes(np.ascontiguousarray(rgba, np.uint8)))
    print(f"wrote {shot}  {still.shape[1]}x{still.shape[0]}")

    pal = gifwrite.build_palette(small, 256)
    lut, bits, step = gifwrite._lut(pal, 6)
    idx = [gifwrite.quantise(f, lut, bits, step) for f in small]
    n = gifwrite.write_gif(out, small, pal, idx, delay_cs=DELAY_CS, loop=0)
    print(f"wrote {out}  {n / 1e6:.2f} MB, {len(idx)} frames, "
          f"{small[0].shape[1]}x{small[0].shape[0]}, "
          f"{len(idx) * DELAY_CS / 100:.1f}s loop")


if __name__ == "__main__":
    main()
