"""Record the Files screen still that the README shows.

    .venv\\Scripts\\python.exe tools/make_files_shot.py

Reuses `make_spotlight`'s stub client rather than repeating it, along with
the same "no sockets" rule and the same trick of reading the composed frame
out of the back buffer in `before_swap`, which is the one moment it exists.
The window is its own, because this shot has different subject matter.

The printer state is invented here and is deliberately *idle*, because
that is the state in which the screen offers everything it can do — with a
job running the print buttons are correctly inert, which makes for an
honest screenshot of a disabled feature and a poor one of the feature.
"""

from __future__ import annotations

import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from imgui_bundle import hello_imgui, imgui                    # noqa: E402
from OpenGL import GL                                          # noqa: E402
from make_spotlight import FakeClient                         # noqa: E402
from bedside import app as A                                   # noqa: E402
from bedside.client import Snapshot                            # noqa: E402

# Shorter than the spotlight's window on purpose. The list is the whole
# subject here, and at the spotlight's 860px a third of the frame is empty
# panel below the last row.
WIN = (1100, 648)

NOW = time.time()
DAY = 86400.0

# A plausible card: a couple of recent jobs, a folder of parts, and one
# old calibration print with no analysis, so the "—" column is shown too.
TREE = {
    "free": 11_400_000_000, "total": 15_000_000_000,
    "files": [
        {"type": "machinecode", "name": "CE3E3V2_Back_Extender.gcode",
         "path": "CE3E3V2_Back_Extender.gcode", "size": 17_300_000,
         "date": NOW - 420, "gcodeAnalysis": {"estimatedPrintTime": 8130.0}},
        {"type": "machinecode", "name": "benchy.gcode", "path": "benchy.gcode",
         "size": 4_100_000, "date": NOW - 3.1 * 3600,
         "gcodeAnalysis": {"estimatedPrintTime": 5400.0}},
        {"type": "folder", "name": "parts", "path": "parts", "children": [
            {"type": "machinecode", "name": "bracket_v3.gcode",
             "path": "parts/bracket_v3.gcode", "size": 912_000,
             "date": NOW - 3 * DAY,
             "gcodeAnalysis": {"estimatedPrintTime": 2750.0}},
            {"type": "machinecode", "name": "spool_holder.gcode",
             "path": "parts/spool_holder.gcode", "size": 6_400_000,
             "date": NOW - 9 * DAY,
             "gcodeAnalysis": {"estimatedPrintTime": 19800.0}},
            {"type": "machinecode", "name": "fan_duct_5015.gcode",
             "path": "parts/fan_duct_5015.gcode", "size": 2_180_000,
             "date": NOW - 17 * DAY,
             "gcodeAnalysis": {"estimatedPrintTime": 4260.0}},
        ]},
        {"type": "machinecode", "name": "calibration_cube.gcode",
         "path": "calibration_cube.gcode", "size": 308_000,
         "date": NOW - 34 * DAY, "gcodeAnalysis": {}},
    ],
}


class IdleClient(FakeClient):
    """Same stub, but a printer with nothing on it and files to show."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.job_file = None
        self.connected, self.state_text, self.completion = True, "Operational", 0.0

    def list_files(self, origin="local"):
        return TREE

    def upload_file(self, *a, **kw):
        return ""

    def delete_file(self, *a, **kw):
        pass

    def select_file(self, *a, **kw):
        pass

    def snapshot(self):
        return Snapshot(
            connected=True, error="", state_text="Operational",
            flags={"operational": True, "ready": True}, job_file=None,
            job_origin="local", job_size=0, completion=0.0, filepos=0,
            print_time=None, print_left=None,
            temps={"tool0": (24.4, 0.0), "bed": (23.9, 0.0)},
            z=None, fan=0.0)


def record():
    A.OctoClient = IdleClient
    app = A.App()
    app.client = IdleClient()
    app.toasts = None
    app.save = lambda: None

    # Pinned for the same reason the spotlight pins them: App() loads the
    # author's settings.json, so without this the README shows whatever
    # was configured on the machine that took the shot.
    app.st.theme_name = "noir-iris"
    app.st.accent = ""
    app.st.extras["bg_scrim"] = 0.62
    app.st.extras["panel_alpha"] = 0.55

    app.bg.set("grid")
    app.bg.intensity = 1.25
    app.fx.set("none")
    app.screen = "files"
    app.store.refresh(app.client)

    params = hello_imgui.RunnerParams()
    params.app_window_params.window_title = "Bedside"
    params.app_window_params.window_geometry.size = WIN
    params.imgui_window_params.default_imgui_window_type = (
        hello_imgui.DefaultImGuiWindowType.provide_full_screen_window)
    params.imgui_window_params.show_menu_bar = False

    state = {"i": 0, "img": None, "size": WIN}
    GRAB = 40                       # let the list land and the eases settle

    def gui():
        io = imgui.get_io()
        state["size"] = (max(1, int(io.display_size.x)),
                         max(1, int(io.display_size.y)))
        app.frame()

    def grab():
        state["i"] += 1
        if state["i"] == GRAB:
            w, h = state["size"]
            GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
            buf = GL.glReadPixels(0, 0, w, h, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
            state["img"] = np.frombuffer(buf, np.uint8).reshape(
                h, w, 3)[::-1].copy()
        if state["i"] >= GRAB:
            params.app_shall_exit = True

    params.callbacks.show_gui = gui
    params.callbacks.before_swap = grab

    import vertexui as vui
    A._use_our_assets()
    vui.install(params, theme_=A.readable(app.st.build_theme()),
                faces=A.build_faces(app.st.extras))
    params.callbacks.post_init = app._apply_theme
    hello_imgui.run(params)
    return state["img"], app


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(HERE), "docs", "files.png")
    img, app = record()
    if img is None:
        sys.exit("no frame captured")
    listed = len(app.store.snapshot()["entries"])
    if listed == 0:
        sys.exit("the file list came back empty — nothing worth shipping")

    from makeicon import png_bytes
    rgba = np.dstack([img, np.full(img.shape[:2], 255, np.uint8)])
    open(out, "wb").write(png_bytes(np.ascontiguousarray(rgba, np.uint8)))
    print(f"wrote {out}  {img.shape[1]}x{img.shape[0]}, {listed} files listed")


if __name__ == "__main__":
    main()
