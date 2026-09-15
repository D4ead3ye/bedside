# Visual & customisation features

Everything Bedside adds on top of [VertexUI](https://github.com/D4ead3ye/vertexui).

VertexUI supplies the primitives — the theme model, `widgets`, the easing in
`anim`, the log view, the toast overlay, the synthesised cue engine, the font
loader and settings persistence. What follows is what was built on those for
this app: a GPU backdrop, a card system, a typographic scale, a rebuilt 3D
renderer, and the knobs for all of it.

Screens: [`docs/spotlight.gif`](spotlight.gif) ·
[`docs/screenshot.png`](screenshot.png)

The spotlight loop is recorded by `tools/make_spotlight.py`, which drives the
real app: hello_imgui only exposes a *final* screenshot, so frames come out of
the back buffer in the `before_swap` callback — after ImGui has rendered and
before the swap, the one moment the composed frame exists. The loop is made
exact rather than trimmed, with the camera driven through one full turn over
the frame count and the backdrop clock driven to a whole number of its own
scroll periods.

GIF is the only format a GitHub README animates from a repo file with no
caveats, and Pillow, imageio and ffmpeg are all absent here, so
`tools/gifwrite.py` is a small encoder: a k-means palette, ordered dithering,
and GIF's LZW.

---

## 1. Shader backdrop — `bedside/bg.py`

A fragment shader over a fullscreen triangle, rendered to its own framebuffer
and blitted beneath the entire interface. This is the one thing the draw list
could not do: the particle system costs one draw call *per particle*, so it
tops out at a few hundred dots, while anything continuous — a gradient that
moves, cloud, a horizon, light that pools — is per-pixel work.

**Six scenes**, all tinted from your accent colour:

| scene | |
| --- | --- |
| `aurora` | four layered curtains, wave-modulated, fbm-broken |
| `plasma` | classic interfering sinusoids, radial term included |
| `flow` | domain-warped fbm — three five-octave lookups per pixel |
| `nebula` | layered fbm cloud with a sparse twinkling star field |
| `grid` | perspective grid receding to a glowing horizon |
| `warp` | radial noise streaks from the centre out |

Design notes worth keeping:

- **One program, not six.** Scenes branch on a uniform, so switching is not a
  recompile and the settings panel can flick between them live.
- **Half resolution on each axis** (a quarter of the pixels). Every scene is
  soft by construction; nothing in them survives close inspection anyway.
- **Vignetted, always.** The panels sit in the middle and the backdrop has no
  business competing with them for contrast there.
- **Opaque.** It composites the theme background itself rather than blending
  over the window fill, which keeps one guess out of the colour maths.
- **Fails once, then stops.** A backdrop is decoration; it does not get to
  throw from the frame loop every frame.

Controls: scene, brightness (0.1–2.0), speed (0.1–3.0), **readability**.

That last one exists because the cards look after themselves — they have a
fill — and bare text does not. The settings screen is almost entirely bare
text, and so are the job strip and the terminal bar. Worse, the vignette
*brightens* the edges of the window, which is exactly where left-aligned text
lives, so the busiest part of the scene lands under the smallest, dimmest
type. "Readability" washes the background colour back in behind those regions
only; the scene still moves through it, and the open areas keep the backdrop
at full strength. On the `grid` scene it takes the background variation under
the settings copy from 16.3 to 5.7 while leaving the median untouched — it
flattens the bright streaks rather than dimming everything.

Measured cost per frame at 1300×880: **0.09–0.14 ms**, under 1% of a 60fps
budget. `flow` is no dearer than `plasma`.

---

## 2. Particle effects — `bedside/effects.py`

**Ten effects:** `fireflies`, `starfield`, `constellation`, `rain`, `sakura`,
`sparkles`, `bubbles`, `embers`, `orbits`, `dust`.

- `embers` rise, wander and flicker, dimming as they climb.
- `dust` runs three depths at different speeds — parallax is the whole trick,
  and identical motes at one speed read as noise.
- `orbits` has no integration at all: position is a closed form of the clock,
  so it cannot drift out of shape however long it runs. Rings are drawn faint
  behind the points, which is what makes it read as a system.

**Painted twice per frame from one simulation** — once behind the content and
once over it at a fraction of the alpha ("over panels" slider). Behind only,
the effect is visible in the gaps between panels and nowhere else, which on a
dense screen is a thin border of weather around an interface that does not
participate in it. `step()` and `paint()` are separate precisely so painting
twice does not advance the motion twice.

The over-pass goes on the window's own draw list after the content, so it sits
above everything drawn that frame but still below popups — a modal has to stay
readable.

---

## 3. Card system

`plate()` draws every card in the interface, in one of **four styles**:

| style | |
| --- | --- |
| `raised` | gradient fill, border, a hairline of light inside the top edge, drop shadow |
| `plated` | flat fill, border, accent rule along the top |
| `outlined` | border only — the background effect shows straight through |
| `flat` | fill only, no edges |

**Rounded gradients.** `AddRectFilledMultiColor` is square-cornered, so the
first attempt at this concluded it was impossible. It is not: draw the rounded
fill normally, then re-colour the vertices it just emitted with
`ShadeVertsLinearColorGradientKeepAlpha`. `KeepAlpha` is the part that matters
— it lerps RGB along an axis and leaves each vertex's alpha alone, so the
anti-aliased corner fringe keeps its coverage and picks up the gradient with
everything else.

**Shadows** are three stacked rounded rects, each fainter and larger — cheaper
than a blur and, at this size, indistinguishable from one. Separately
toggleable.

**Cards carry their own headers.** Icon, tracked title, hairline, right-aligned
state. The column used to be organised by `widgets.section` — a tick, a label
and a rule floating in the *gaps between* cards, which reads as captions around
boxes rather than as instruments with names on them.

**State colours the accent edge**, so the panel says what the machine is doing
from the corner of your eye: purple locked, green running, amber paused.

---

## 4. Typography

Two faces, not one. One face doing every job is most of what makes an interface
look like a default — a body face set at 35px is just big body text.

| role | face |
| --- | --- |
| body, log | Segoe UI Variable *(pick from 6)* |
| readouts, micro-labels | Bahnschrift — DIN-derived, reads as instrumentation *(pick from 3)* |
| terminal | Cascadia Mono *(pick from 4)* |

Seven roles are wired: `ui`, `semi`, `title`, `mono`, `label`, `big`, `huge`.

`DISPLAY_FACES` carries a **per-face size scale**, because faces disagree about
how much of the em the x-height takes — Bahnschrift at a matched em size renders
noticeably larger, so it is scaled to 0.88 to keep card layouts intact.

`caps()` draws every micro-label a glyph at a time with letter tracking, because
ImGui has no letter-spacing. They are three to seven characters each, and the
tracking is most of what separates a laid-out panel from a stack of default text.

### Contrast is guaranteed, not configured

Every text role is checked against the lightest ground the theme defines and
pushed away from it until it clears WCAG — 7:1 for anything carrying a
sentence, 4.5:1 for labels and metadata — at theme-build time, by binary
search on a blend toward whichever pole the ground is not. A light theme
gets darker text rather than brighter, and a role that already passes is
left alone.

This is not a slider. All three shipped themes were failing before it
(`text_mute` between 1.77:1 and 2.11:1, against a 4.5:1 floor), and the
readability wash could not reach the problem because the problem was the
type, not the ground. Measured on rendered frames afterwards: 0 of 13 text
bands below the floor on every backdrop at the default wash, median 8.63:1.

---

## 5. The 3D toolpath view

Rewritten from a CPU polyline renderer to a GPU one with a real depth buffer.
The visual consequences:

- **Boxes, not ribbons.** Each extrusion is a box one layer high and one
  extrusion wide. The previous mesh emitted only the *vertical* face of each
  bead, which has no width in plan — so looking down at a solid layer presented
  every extrusion edge-on, each rasterising to a sub-pixel sliver, and the
  surface read as a stipple of static with moiré crawling over it.
- **The key light was pointing the wrong way**, built from the direction you
  are *looking* rather than back at the camera, so every visible surface shaded
  as if backlit. It survived because the old arbitrary normals made half the
  model look lit by luck. Fixing both together took mean luminance 50.4 → 63.0
  and neighbour-pixel noise 7.66 → **2.45**.
- **The bed is in the scene**, not painted over it. It used to be a wireframe
  rectangle drawn on the ImGui list *after* the blit — straight across the front
  of the print. It now shares the depth buffer, with a per-vertex alpha so the
  outline reads at 1.0 and the grid inside at 0.16.
- **Cutaway slider.** Everything the outer wall encloses — infill above all — is
  hidden by it at *every* angle: measured, enabling infill changed **0 pixels**
  with the shell on, for straight-line and gyroid infill alike. The cutaway
  discards in the fragment stage, so the cut face is flat at exactly that height
  instead of slicing whole boxes off at their nearest corner.
- **Ghost opacity** actually reaches the shader now, and printed/unprinted are
  drawn as two passes in that order so a translucent shell veils the print
  rather than culling it.
- **Feature filters:** outer wall, inner walls, solid, infill, support,
  brim/skirt, bridges — brim and skirt split out of support into their own class.

**Camera:** orbit, pan (right/middle/shift-drag), zoom-to-cursor (0.00 px drift
over 25 notches to maximum zoom), flick inertia with framerate-independent
decay, double-click reset, and lit overlay buttons for auto-orbit and reset in
the corner of the view.

---

## 6. Side panel

Five sections, **each independently switchable**: temperature cards,
temperature graph, controls, job buttons, model card — plus the
movement pad, which appears only when it is both enabled and safe.

- **Temperature cards** — tracked caps label, big readout with the unit set
  small and dim beside it, feature icon, heat-up meter, and a **sparkline
  scaled to that card's own range**. The graph below compares both curves on a
  shared axis, which is the right tool for comparing them and the wrong one for
  "is this one climbing".
- **Temperature graph** — legend inside the plate (floating above it read as a
  separate thing and cost 20px), live values, MAX tick, gradient area fills and
  a head dot so the eye finds "now" without hunting.
- **Model card** — layer *n* of *m*, a 34-cell layer ladder with a pulsing glow
  on the leading cell, footprint and current feature. Heights snap to whole
  rows (104/78/60px) rather than interpolating, because interpolating clips the
  last line of text and reads as a broken card.
- **Movement pad** — an optional sixth section, off by default. Locked, it is
  a single bar that fills as you hold it; armed, it grows a step-size row, a
  cross pad and a Z column set apart from it, and the header counts down the
  20 seconds until it re-locks. The centre of the cross is an inert readout
  rather than a button, because that square is where a slipped cursor lands.
  Two layouts — the cross above 176px of panel height, a paired single row
  below it — so the card is never the thing that clips.
- **Splitter** — a hairline that fades out at both ends with a grip at the
  middle, draggable to resize the panel. Ten pixels of gap was a gap, not a
  division.

---

## 7. The file browser

`Ctrl+O`. A full screen rather than a panel section, because here the list
is the content rather than an instrument beside it.

- **Rows as cards**, not a table: the name on the semibold face, a muted
  metadata line under it (folder, size, estimated time, age), and a feature
  icon that swaps to the printer glyph with a green accent edge on the file
  currently being printed.
- **Actions live on the row**, right-aligned. Where one cannot act it is
  **not a disabled button but an inert dim glyph** — no id taken, no press
  state, and it still explains itself on hover. A button that looks live and
  then declines is at its worst on exactly the row that matters.
- **Worker strip** above the list: a gradient plate carrying what the
  background thread is doing, a percentage, and a hairline fill along its
  bottom edge. It turns `t.danger` and holds the message when something
  fails.
- **Storage** right-aligned on the title row, in `t.warn` past 92% full.
- **One ground either way** — the empty state draws inside the same child as
  the list, so the screen does not swap its whole background depending on how
  many files the printer happens to have.
- **Confirm dialogs** centred on the viewport and titled through `###`, so
  the popup id stops leaking into the title bar. Each names the file and what
  it will cost.
- **`PREVIEW` badge** over the top-left of the 3D view whenever it is showing
  a file that is not the running job, with its own close box. It stands down
  by itself the moment the printer starts a job.

---

## 8. Motion and feedback

- **Print progress hairline** along the very top edge of the window, full width,
  with a bloom on the leading edge. The one number worth seeing from across the
  room, and up there it costs no layout at all.
- **Screen transitions** — a wipe across a dash↔settings change, which without a
  beat in between reads as a glitch rather than as navigation.
- **Eased camera** — pan and zoom share one rate, because the zoom-to-cursor
  correction is affine in the zoom ratio and different rates make the point
  slide centre-ward for the whole animation.
- Fan icon spins at the real duty cycle; nozzle marker pulses only while
  printing; activity rule in the topbar; status pills and badges.

---

## 9. Sound — `bedside/sounds.py`

Visual's sibling, and the same idea: the toolkit's cues are tonal, which is what
makes a cue read as a knock. Right for a tool you click through, wrong for
something open beside you for nine hours.

- **`breeze`** — filtered noise, not tone. A band steered by overlap-add FFT
  under a long raised-cosine envelope, so there is no onset to flinch at.
  Identity comes from where the band sits and which way it sweeps.
- **`soft taps`**, **`crisp`** — the toolkit's own sets, extended.
- **`custom .wav`** — drop your own files in `%APPDATA%\bedside\sounds`,
  per cue rather than all-or-nothing, with a "write starter cues" button that
  renders the synthesised set out as real files to replace.

**Seven cues:** `hover`, `click`, `ok`, `warn`, `error`, `toast_in`,
`toast_out`.

---

## 10. Every knob

| group | controls |
| --- | --- |
| Theme | presets, accent colour + custom, text size, density, corner radius, panel radius, border width |
| Type | interface face, readouts face, terminal face |
| Backdrop | scene, brightness, speed, readability |
| Contrast | none — the text ramp is guaranteed against the theme automatically, not dialled |
| Effects | effect, intensity, over-panels, panel opacity |
| 3D view | feature filters ×7, cutaway, detail, extrusion width, depth fade, base opacity, ghost opacity, nozzle size, model colour, bed outline |
| Panel | card style, shadow, 5 section toggles, side-panel width, terminal height, graph height |
| Terminal | follow, 5 noise filters, text filter, scroll speed, mono, timestamps, tags, row spacing |
| Alerts | sounds on, volume, cue set, custom cue folder, desktop toasts |
| Printer | movement controls on/off (the only setting that adds a control rather than restyling one) |

Settings live across six tabs — Appearance, 3D View, Terminal, Alerts, Printer,
About — and the Printer tab opens with a **live miniature of your whole window**
that previews the card style, which sections are on, and all three size sliders
at once.

---

## 11. Changes that live in VertexUI

Two, both in `vertexui/toasts.py`, and Bedside depends on both:

1. **`ShowWindow(hwnd, SW_HIDE)` when the item list empties.** Clearing a
   layered window by transparent `UpdateLayeredWindow` blit is not reliable, and
   the failure mode is a toast painted on the desktop forever.
2. **`on_event("in"|"out", kind, text)` callback**, fired from the toast thread
   when a toast appears and when it starts sliding away — this is what the
   `toast_in` / `toast_out` cues hang off. Guarded, because a callback that
   raises must not take that thread down with it.

Both shipped in VertexUI **1.2.0**, which is what `requirements.txt` pins.
