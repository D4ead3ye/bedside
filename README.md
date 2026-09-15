# Bedside

A desktop client for OctoPrint, built on
[VertexUI](https://github.com/D4ead3ye/vertexui) and Dear ImGui. Live
temperatures, job progress, a G-code terminal, and a GPU-rendered 3D
toolpath that fills in as the print actually happens.

![Bedside](docs/spotlight.gif)

<sub>Recorded with `tools/make_spotlight.py` — a real 3.8s loop of the app,
not a mock-up. [Still screenshot](docs/screenshot.png).</sub>

- **Real progress, not an estimate.** The 3D view is driven by the byte
  offset OctoPrint reports, bisected against the offsets recorded while
  parsing, so the boundary between printed and unprinted is the actual
  segment the nozzle is on.
- **A real renderer.** Every extrusion is a box with a depth buffer behind
  it — 1.4M vertices of the reference model at ~5ms a frame, with a
  cutaway for looking inside.
- **Send work to it.** Upload sliced `.gcode`, look at it in the 3D view
  before committing to it, and start the print — each behind a dialog
  that names the file.
- **Safe by construction.** Movement controls are off by default, absent
  unless the printer is idle, and have to be held to unlock; motion G-code
  is refused on the send path, not just hidden in the UI.
- **Themed throughout.** Four themes, six shader backdrops, ten particle
  effects, four card styles, your own fonts and cue sounds, and every panel
  section switchable — with text contrast guaranteed against whatever theme
  you land on, rather than left to a slider.
- Click-through toasts that report a finished print *over* a fullscreen
  game.

Everything visual, and every knob, is catalogued in
[docs/VISUALS.md](docs/VISUALS.md).

## Download

### [⬇ Bedside for Windows](https://github.com/D4ead3ye/bedside/releases/latest)

Unzip anywhere and run `Bedside.exe`. No Python, no installer, nothing
written outside the folder you unzipped it to and `%APPDATA%\bedside`.

Needs Windows 10/11 and a GPU with OpenGL 3.3. There is a CPU fallback for
the 3D view if that is missing, though the cutaway needs the GPU path.

### First launch

It asks for your printer's address — `192.168.1.50`, or `octopi.local`.
**Pair with OctoPrint** then starts the Application Keys handshake:
OctoPrint shows an authorisation prompt in its own web UI, you click Allow,
and the key arrives here. You never type or paste it anywhere. There is a
manual key box for older OctoPrint builds without the plugin.

Settings, saved themes and your own cue sounds live in `%APPDATA%\bedside`.

## Run from source

```bash
git clone https://github.com/D4ead3ye/bedside.git
cd bedside
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe run.py
```

Python 3.11+. `requirements.txt` pulls VertexUI straight from its
repository — it is not on PyPI, and Bedside needs the `Toasts` `on_event`
hook from 1.2.0.

To build the exe yourself:

```bash
.venv\Scripts\python.exe -m PyInstaller --noconfirm Bedside.spec
```

The result is `dist/Bedside/Bedside.exe`. Run **that** one — the
`Bedside.exe` left in `build/` is an intermediate stub without its
`_internal` folder and fails with "Failed to load Python DLL".

### Keyboard

| | |
| --- | --- |
| `Esc` | back out of settings or files |
| `Ctrl` `O` | the printer's files |
| `Ctrl` `,` | toggle settings |
| `F5` | reconnect |
| `F1` | about |

Nothing destructive is bound. Pause, cancel, start and every jog are
deliberately not on a key — one you can hit by accident must not be able
to ruin a nine-hour print. `Ctrl+O` only opens a list.

## Files

`Ctrl+O`, or the folder in the title bar, opens the printer's own storage:
everything on the card with its size, estimated time and age, newest first,
with a filter box for when there are a lot of them.

- **Upload** picks one or more sliced `.gcode` files through the ordinary
  Windows dialog and streams them up with a progress bar. The request body
  is a generator, so a 120 MB file does not also become 120 MB of request
  sitting in memory.
- **Preview** downloads a file and draws it in the 3D view *without*
  sending it anywhere. The view wears a `PREVIEW` badge for as long as it
  is showing something other than the running job, and stands down on its
  own the moment the printer starts one — a monitor that shows the wrong
  model with a straight face is worse than one that shows nothing.
- **Print** asks first, naming the file, its size and its estimated time,
  and re-checks that the printer is still idle when you confirm: the
  dialog can sit open while something else starts.
- **Delete** asks too, and is simply not offered on the file being
  printed.

The dialog is the only way to start a print from here, and nothing on the
screen is bound to a key beyond `Ctrl+O` to open it.

## Movement

There are jog and home buttons. They are not on by default, and they are
not always there.

1. **Off until you turn them on**, in Settings → Printer.
2. **Absent unless the printer is idle** — absent, not greyed out. A
   control that exists while a job runs is one bad frame, one stale flag
   or one mis-click away from ruining nine hours of work, and there is no
   arrangement of confirmations that makes it worth having there.
3. **Hold to unlock**, 0.7s on a bar that fills as you hold. A click is
   exactly the thing being guarded against, so the gesture has to be one
   you cannot make by accident with a stray cursor.
4. **Re-locks itself** 20 seconds after the last move, counting down in
   the card header.
5. **The middle of the pad is not a button.** That square is where a
   slipped cursor lands, so it holds the step size instead of an action.
6. **Z down is refused** unless the height is known *and* the move keeps
   the nozzle at or above the bed. With no reported Z there is no way to
   tell a safe move from a crash, so it does not guess.
7. **Re-checked at send time**, not only where the button was drawn —
   the `printing` flag comes from the last socket push and can lag the
   printer by a frame.

The printer's own LCD and the OctoPrint web UI still have their own
controls for everything this does not cover.

The manual G-code box is closed off separately, in two layers:

1. It is **hidden entirely** while a job runs. A text box that accepts
   `G1 X0 Y0` is a jog control with extra steps.
2. `client.is_motion_command` refuses motion on the **send path** as well,
   so the guard does not depend on the box being invisible.

Refused: `G0`-`G3`, `G10`/`G11`, `G28`, `G29`, `G92`, `M18`, `M84`. The last
three are not moves but belong on the list anyway — `G92` redefines the
origin, so every later coordinate in the running job lands somewhere else,
and `M18`/`M84` drop the steppers and let the head sag. Both scrap the print
as surely as a jog does.

Everything else still goes through while printing: `M117`, fan and
temperature commands, queries. When the printer is idle nothing is refused.

---

## More

- [docs/VISUALS.md](docs/VISUALS.md) — every visual feature and every knob
- [docs/ENGINEERING.md](docs/ENGINEERING.md) — the engineering log
- [VertexUI](https://github.com/D4ead3ye/vertexui) — the toolkit this is
  built on

MIT. See [LICENSE](LICENSE).
