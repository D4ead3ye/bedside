# Engineering notes

Why each part of [Bedside](../README.md) is the shape it is: what broke,
what the measurements said, and which fixes were wrong before they were
right. The user-facing feature list lives in [VISUALS.md](VISUALS.md).

---

## How it talks to the printer

| Concern | Mechanism |
| --- | --- |
| Live temps, progress, state, terminal | SockJS at `/sockjs/websocket` |
| Socket auth | `POST /api/login` with `{"passive": true}`, then `{"auth": "name:session"}` as the first frame |
| Commands | REST under `/api/…` with an `X-Api-Key` header |
| Toolpath geometry | `GET /downloads/files/local/<path>` |

That socket handshake is the one genuinely undocumented-feeling part: the
socket is anonymous until the auth frame arrives, and a passive login is
what turns an API key into the `name:session` pair it wants.

## The 3D view

Rendering is done on the GPU (`bedside/glview.py`): the toolpath is uploaded
once as triangles and drawn into an offscreen framebuffer **with a real depth
buffer**, then blitted into the ImGui frame.

This replaced a renderer that drew 2D polylines onto ImGui's draw list. That
approach has no depth buffer, so occlusion had to be approximated by sorting
pieces back-to-front — and painter's ordering cannot resolve surfaces that
interpenetrate or share a mean depth. Every "geometry disappears at some
angles" report traced back to that, and each fix only moved which angles
broke. It was also slow for the wrong reason: every point was projected in
Python every frame.

| | CPU polylines | GPU |
| --- | --- | --- |
| rotating a 575k-segment model | 71 ms | **5.1 ms** |
| still | 20 ms | **5.1 ms** |
| occlusion | sorted, approximate | depth buffer, exact |

Frame cost no longer depends on model size, because rotating is one uniform
update rather than re-projecting 100k points.

Each extrusion is emitted as a **vertical quad one layer high**, so a wall is
a real surface rather than stacked lines. Three things that needed care:

* A GL framebuffer's origin is bottom-left and ImGui's is top-left, so the
  blit uses flipped V coordinates.
* Zero-length segments make degenerate quads that rasterise to nothing and
  speckle a wall with pinholes; they are dropped.
* Real layer spacing wanders either side of the median, so quads sized
  exactly one layer high leave hairline gaps. They overlap by 35%.

Lighting is half-Lambert against a key light that **follows the camera**,
offset 0.6 rad. A light fixed in world space is wrong: orbiting rotates the
face you are looking at away from it, and the near surface — the one you
most want to see — goes dark.

The CPU renderer is still there as a fallback for a machine that cannot give
us a GL 3.3 context. One failure switches to it for the rest of the run
rather than throwing from a draw call every frame, and Settings → About
reports which renderer is live.

### Ribbons had no width, so flat surfaces rendered as static

The first GPU mesh emitted only the *vertical* face of each bead, on the
reasoning that a wall is what you actually look at. True of walls, false of
everything else. A bead modelled as a vertical ribbon has no width in plan,
so looking down at a solid top layer or a sheet of infill presents every
extrusion edge-on: each one rasterises to a sub-pixel sliver, and the
surface reads as a stipple of dots with a moire crawling over it as the
camera turns. Walls looked right; flat surfaces looked like TV static.

Each extrusion is now a **box** — one layer high, one extrusion wide — so
it has area from every direction and there is no angle at which the
geometry thins out. Three faces, not six: the bottom of a bead is never
visible (it sits on the layer below, or on the bed) and the ends are always
inside the neighbouring segment. Rendering the bottom face was measured to
change not one pixel at any angle, including from under the bed, and it is
a quarter of the geometry.

Each box is extended half a width past both ends, or every corner in a
perimeter has a notch bitten out of it where the two mitres fail to meet.

### ...which exposed a light that had been pointing the wrong way

`u_light` was built from `centre - eye` — the direction you are *looking*.
So `dot(n, L)` was negative on every surface facing the camera and the
whole model shaded as if backlit.

It survived that long because the ribbon mesh gave every quad the same
arbitrary normal (`[dir.y, -dir.x, 0]`, whichever way the path happened to
run). Half of those were wrong regardless, so half the model looked lit by
luck, and the sign error read as "the shading is a bit flat". Give the
geometry correct per-face normals and the error becomes the entire picture.
Both bugs had to be fixed together to see either one.

Measured on the real model at the reported angle:

| | mean luminance | neighbour-pixel noise |
| --- | --- | --- |
| ribbons | 50.4 | 7.66 |
| boxes, light unchanged | 50.4 | 6.47 |
| boxes + light corrected | 63.0 | **2.45** |

Three times the geometry (479k -> 1.44M vertices) cost nothing measurable:
5.1ms per frame before, 5.2ms after, across a full orbit at four pitches.
The renderer is fill-rate bound, not vertex bound.

### The bed was painted over the print

The bed outline was drawn on ImGui's draw list *after* the GL texture was
blitted, so it was not part of the scene at all — it was a wireframe
rectangle painted straight across the front of the model, at every angle
where the plate passed behind it.

It is now geometry in the same pass, with its own two-line shader, so the
depth buffer decides which is in front. A faint grid inside the outline
came with it: it is there to say "this is a surface the print stands on",
and it carries a per-vertex alpha (outline 1.0, grid 0.16) so it does that
without competing with the model. At the same weight it turns a printer
monitor into a CAD viewport.

### The old CPU path



`progress.filepos` is a byte offset into the G-code file. The parser records
the byte offset of every segment it emits, so a bisect turns that offset
into an exact segment index — the printed/unprinted boundary is the real
one. Percent-complete would have been an estimate, and a wrong one on any
file with a long non-printing preamble.

Three measurements shaped `view3d.py`:

| Measured | Result | Consequence |
| --- | --- | --- |
| `add_polyline` with a numpy array | rejected | must build `ImVec2` lists |
| …with a list of tuples | 14 ms/call | unusable |
| …with a list of `ImVec2` | 2.5 ms/call | the only viable form |
| Building 25k `ImVec2` | ~8 ms | cache it, keyed on the camera |
| 6000 short polylines vs 1 long one | about equal | run count is free; point count is not |

So the projection is cached and rebuilt only when the camera moves. That is
why auto-orbit defaults to off — a view that spins forever never lets the
app idle, and this is a window that stays open for a nine-hour print.

**Run count is the other budget, and the one that bites first.** Each run is
a Python loop iteration plus an `add_polyline` call, and a real print has
tens of thousands of short infill fragments, so capping *points* alone left
the frame rate bound by run count.

Ranking runs by **length** to decide what survives that budget is exactly
backwards: sparse infill is made of long straight passes, while perimeters
get chopped into short runs by seams — so it kept the infill and threw the
shape away. Every major slicer annotates the file (`;TYPE:` for
PrusaSlicer/Cura/Orca, `;FEATURE:` for Bambu), so runs are classified into
perimeter / solid / infill / support / bridge and ranked by **what they
are**, then by length. Sparse infill and support are hidden by default,
which is most of why the view became readable.

On a 240-layer model with 7,473 runs, 6,032 of them infill:

| | median | p95 |
| --- | --- | --- |
| orbiting | 4.4 ms | 5.9 ms |
| static camera | 2.6 ms | 3.8 ms |

If a file carries no annotations (`has_features` is False) the filters are
disabled and selection falls back to length, because every segment reads as
`OTHER`.

Three things together are what make it read as an object rather than a
tangle of lines:

* **Extrusion width is in millimetres, not pixels.** A fixed pixel thickness
  meant zooming in gave you the same hairlines further apart. Screen
  thickness is `line_mm * pixels_per_mm`, so a 0.45mm bead looks like a
  0.45mm bead at any zoom.
* **Painter's algorithm.** Runs are sorted by camera depth and drawn far to
  near. Print order is bottom-to-top in Z, which is not the same thing once
  the view is rotated — a run at the back could be painted over one at the
  front.
* **Opacity — fully opaque.** The original low alpha existed to stop thin,
  unsorted lines saturating into a block. Once depth sorting, depth shading
  and layer banding carried that job, the leftover translucency only leaked:
  at 0.70 alpha every wall showed 30% of whatever was behind it, so the
  shell read as an x-ray wherever two surfaces overlapped. Printed geometry
  is opaque; `base opacity` is still a slider if you want translucency back.

**Pieces, not runs.** Painter's sorting can only order whole pieces against
each other, so a piece has to be short enough that its own depth is
coherent. A perimeter loop wraps right around the part, so as one unit its
mean depth is the object's centre — measured, a whole run spanned **83% of
the model's depth range**, which makes the sort meaningless: back surfaces
were painted over front ones and the shell looked like an x-ray from
ordinary angles. Runs are split into short overlapping pieces sized from
`PIECE_BUDGET`, which drops the spread to **8-13%** on a dense model.

That was the actual cause of the x-ray, not opacity. Opaque geometry drawn
in the wrong order shows the wrong surface, which looks identical to
transparency.

A closed loop turns 2*pi in total, so a turn tolerance above ~6.3 radians
lets no interior point trigger a keep and the whole loop collapses to a
chord from its first point to its last. On a concave outline that chord
leaves the material, which put stray lines *outside* the model. The budget
loop used to reach 17.8 rad in three passes, where measured deviation from
the true path was 9mm. `TOL_TURN_MAX` clamps it at 0.9 rad, where deviation
is 0.00mm; only `max_chord` grows without limit, because collapsing a
straight stretch to its two ends is free. When that still cannot reach the
budget, whole runs are dropped instead — a missing infill line reads as
less detail, a deformed perimeter reads as a defect.

Camera-depth shading is what gives the model volume. Runs sink toward the
background with distance, quantised into `DEPTH_STEPS` rows. Layer banding
handles the case depth shading cannot: a wall facing the camera squarely has
every layer at the same height *and* the same depth, so alternating layers
get a small brightness offset and the stack stays legible. Layer height is
measured from the file (median positive Z step), so it adapts to the slice.

The ramp is precomputed as `[band][depth][height]`, which keeps the per-run
work in the draw loop down to three array indexes.

Drag to orbit, wheel to zoom. The progress sweep is eased through
`anim.to`, so a once-a-second push from OctoPrint renders as continuous
motion rather than a jump. The nozzle marker interpolates *between* the two
points either side of the eased index — snapping to the nearest stored
vertex made it stutter, and after decimation those vertices can be far
apart. It is drawn white with a dark rim rather than in the accent, because
it sits on top of accent-coloured geometry and a red dot on red lines is
invisible exactly where it matters.

### Settings preview

The 3D View tab draws a small annotated cylinder with the current settings
applied, sweeping back and forth through its own progress so the
printed/unprinted boundary, the depth ramp and the nozzle marker are all
visible without waiting for the real print to reach an interesting point.

It is emitted as real G-code and run through the real parser, so it
exercises the same feature classification, decimation, depth sorting and
shading as a live job. A preview drawn by a separate code path would be a
picture of what the settings were *supposed* to do.

Two details that were not obvious:

* The sample is 84 layers at 9mm radius — **taller than wide on purpose**.
  The first attempt was 46 layers at 15mm, which is a disc, and a disc reads
  as a flat circle from every angle. The side wall is where extrusion width
  and opacity are actually judged.
* `ToolpathView` takes a `key`, because two instances now exist. Without it
  they share `anim` keys (`v3d:yaw`, `v3d:prog`, …) and the same
  `invisible_button` ID, so they would fight over one eased camera and one
  hit-test.

### "Show infill" could never change a pixel

It was not the toggle. The filter worked at every stage — `Display` kept
34,160 infill points, `build_mesh` emitted 17,080 infill segments, the
whitelist saved and reloaded correctly. The geometry was drawn. It was
simply invisible, because on this part the infill lives between the inner
and outer wall and the shell encloses it completely.

Measured, same camera, one visible-set against another:

| visible | + infill changes |
| --- | --- |
| outer wall | **0 px** |
| inner walls | 1,584 px (0.31%) |
| solid skin only | 48,551 px (9.6%) |

Zero. Not "subtle" — the outer wall occludes every last fragment of it, at
every angle, at any print progress. A filter that cannot alter the image
while the default view is on is a broken feature even though every line of
it behaves.

The fix is a **cutaway**: a height above which nothing draws, so you can
look inside. With it at 0.55, toggling infill changes 15.4% of the frame —
the wall cores go from hollow to solid.

This is not a quirk of one part or one infill pattern. Checked against a
synthetic cube with genuine gyroid — the case where the shape lives in the
*density* of the points, so any decimation could flatten the waves into
zigzags without dropping a single run. It survives: every run kept, 35,143
of ~35,000 infill points, median turn angle 12.5 degrees against the 0.0
of a line/grid pattern. And with the walls on it still changes **0 px**,
exactly like the straight-line case. The shell is what hides infill, not
the pattern. At a cutaway of 0.60 the same model changes 54.7% of the
frame and reads as a slicer preview.

It discards in the *fragment* stage rather than moving the vertex, so the
cut face is flat at exactly that height instead of slicing whole boxes off
at their nearest corner. The CPU fallback ignores it, and the settings row
says so when that path is active.

### Brim and skirt got their own class

Brim, skirt and raft used to classify as `SUPPORT`. They are a different
thing: first layer, around the part, thrown away afterwards — where support
stands up through the print and is what you watch to see whether it is
holding. You often want one without the other, so `SKIRT` is now its own
id and `support` keeps only support, tower and wipe.

### Feature ids are a whitelist, so adding one is a breaking change

`features` in settings.json is a list of feature ids, and the filter keeps
only what it names. When the outer wall was split out of `PERIMETER` into
its own id, every settings file already on disk became a list that could not
possibly contain it — so the entire outer shell silently vanished and the
model rendered as inner walls only, fading in and out as it turned.

Two guards now: the preset migration rewrites `features`, and
`_apply_view_prefs` adds `EXTERNAL` whenever it sees `PERIMETER` in a stored
list, because anyone who asked for perimeters wanted the outer wall too.
Worth remembering before splitting another class.

## Camera

| gesture | does |
| --- | --- |
| left drag | orbit |
| right / middle drag, or shift+left | pan |
| wheel | zoom, 12% a notch |
| ctrl+wheel | zoom, 4% a notch |
| double click | back to the opening framing |

The view opens at about 30 degrees above the bed. It used to be 60, which
looks down on the print from almost overhead — that flattens the walls into
outlines, and because auto-orbit only turns the yaw, it was the angle the
whole orbit ran at too.

Auto-orbit and reset also sit as buttons in the corner of the view itself,
rather than in Settings — they are the two controls you reach for while
looking at the model rather than while configuring it. The auto-orbit
button lights when it is on, and because a drag cancels auto-orbit, the
saved value follows the live one either way rather than only when the
button is pressed.

### The overlay buttons were dead on arrival

They did nothing, and the camera lurched instead. ImGui gives hover to the
FIRST item submitted that contains the cursor and then stops looking —
`ItemHoverable` bails as soon as `g.HoveredId` belongs to someone else — so
the view's full-panel `invisible_button`, submitted first, swallowed every
click aimed at the buttons painted over it.

Two fixes, either of which is sufficient, both applied because this control
has read as dead once already: the small hit boxes are now submitted
*before* the view's (their pixels are still painted afterwards — draw order
and hit order are independent), and the view's button carries
`SetNextItemAllowOverlap`, which is the supported way to say a later item
may take the hover.

Verified by driving the real window rather than by reading the docs. A
posted `WM_LBUTTONDOWN` works, but posted `WM_MOUSEMOVE` does not: the GLFW
backend re-reports the true cursor every frame and overwrites it. Moving
the *window* instead is equivalent — the cursor stays put, so in client
space it has moved — and that drives a genuine drag without touching the
user's mouse.

| | auto_orbit | yaw change |
| --- | --- | --- |
| click the button | False -> True -> False | — |
| 84px drag starting on the button | unchanged | 0.014 rad |
| the same drag on open canvas | unchanged | 1.094 rad |

Pan is stored in *panel pixels*, not world units, and converted against the
world height one pixel covers at the target distance. Store it in world
units and the model slides faster than the cursor when zoomed out, which
feels like the view is fighting you. It is clamped to one panel width in
each direction: unbounded, a stray drag pushes the model somewhere you
cannot see and cannot reason about, and the reset should be a convenience
rather than the only way home.

Releasing a drag coasts rather than stopping dead. The velocity is a
smoothed average of the last few frames — take only the final frame and one
stationary frame at the end of a fast drag throws the flick away. The decay
is `0.90 ** (dt * 60)`, i.e. framerate-independent, so a coast lasts the
same wall-clock time whether the app is at 9fps or 120.

`camera_input()` is split out from `draw()` because it cannot be tested
through ImGui's event queue: a live GLFW backend re-posts the real cursor
position every frame and overwrites anything injected. Driven directly with
a fake mouse, all of it is checkable.

### Ghost opacity did nothing, and hid the print when it did

Three bugs stacked on one slider.

The alpha was a literal in the fragment shader (`vec4(u_ghost, 0.10)`), so
the setting was wired to everything except the thing that draws. On the GPU
path — i.e. always, in practice — moving it did nothing at all.

Wiring it up exposed the second: at zero the geometry was still drawn, and
an invisible fragment still writes depth, so unprinted geometry punched
holes in the printed model behind it. The fragment shader now discards
below alpha 0.004 — "do not draw it" rather than "draw it invisibly".

The third was ordering. Printed and unprinted geometry were interleaved in
one pass, so an unprinted wall drawn early wrote depth and culled the
printed geometry behind it: raising the opacity made the print vanish
rather than veil it. The mesh is now **segment-major** — all faces of a
segment together — which makes `seg` non-decreasing, so everything printed
so far is a contiguous prefix and one `searchsorted` splits the draw into
an opaque pass and a translucent one, in that order.

Depth writes stay on for the translucent pass. Turning them off sounds
right and is wrong here: the unprinted region is hundreds of stacked layers
deep, so every fragment blends over the last and a 25% shell comes out
opaque. Writing depth keeps only the nearest ghost surface, which is what a
shell should be.

### Zoom follows the cursor

Zooming towards the middle of the bed means closing in on a corner walks it
off the edge of the panel, and you pan back after every notch.

A point on screen sits at `C + pan + w*scale`, and `scale` is proportional
to zoom, so holding the point under the cursor still across a zoom of `k`
needs

    pan' = (m - C)(1 - k) + k*pan

That correction is exact under perspective — it comes out independent of
the depth of the point — but two other things were quietly undoing it.

The pan clamp was one panel width. The pan needed to hold a point under the
cursor grows like the zoom ratio, so the bound was reached after a handful
of notches and every notch after that pulled the view back towards the
middle: exactly the drift the zoom-to-cursor was meant to remove. The bound
now scales with zoom.

The ease rates were the other. The correction is affine in the zoom ratio,
so easing pan and zoom together holds the invariant at every frame of the
animation — but pan was easing at 22 against zoom's 14, so the point slid
toward the centre for the whole transition and only landed correctly once
both settled. One `ZOOM_EASE` for both.

Measured against the real projection, not a model of it: 25 notches to
maximum zoom with the cursor well off-centre, sampling mid-ease as well as
settled, the point under the cursor drifts **0.00 px** at every step.

### The wheel used to scroll the page as well as zoom

Hovering the 3D view and scrolling zoomed the model *and* scrolled the
panel behind it — on the settings screen that threw the preview off-screen
mid-gesture.

There is nothing to "consume". ImGui scrolls the hovered window inside
`NewFrame`, before any application code for that frame runs, so by the time
we read `io.mouse_wheel` the scroll has already happened. Zeroing it is too
late.

The supported mechanism is key ownership:
`imgui.set_item_key_owner(imgui.Key.mouse_wheel_y)` while the canvas is
hovered claims the wheel, and `UpdateMouseWheel` skips a window whose wheel
is spoken for. Ownership takes effect from the next frame, which is fine —
the cursor is over the canvas for at least one frame before it can scroll.

Measured, ten notches over the view inside a scrollable child:

| | panel scroll | zoom |
| --- | --- | --- |
| before | 0 -> 608 px | 1.000 -> 0.893 |
| after | 0 -> 0 px | 1.000 -> 0.322 |

The zoom is wrong in the "before" row for the same reason: the panel
scrolled the canvas out from under the cursor, so most notches stopped
counting as hovering it.

### ...and adding one nearly took the renderer down

`_RUN_PRIORITY` decides what to sacrifice when the run budget is tight. It
was a plain dict lookup, so the first frame after `SKIRT` appeared raised
`KeyError: 7` out of `Display.__init__` and the 3D view stopped entirely —
a lot of damage for a table whose only job is ranking. It now has a default,
and unknown ids sort to "dropped first".

The migration is the other half. Bumping `VIEW_PRESET_VERSION` re-applies
the whole recommended block, which would have thrown away the ghost
opacity, detail, auto-orbit and feature choices already made. A one-line
repair does not deserve that, so the brim carries its own marker: if a
saved list has support but no `skirt_split` flag, it gains the brim once,
and the flag is written with it. Switching the brim off is itself a save,
so it cannot come back.

## The terminal

During a print the terminal is a firehose, and almost none of it is worth
reading. `client.classify_line` sorts each line into `move` / `ack` / `temp`
/ `sd` / `busy` / `other`, and everything but `other` is hidden by default —
dropped at drain, so it never reaches the log buffer at all. Measured on a
realistic stream: **246 lines in, 243 hidden, 3 kept**, those three being a
checksum error, an `M117` and a `G28`.

Three details in the classifier carry weight:

* `Recv: ok T:209.6 /210.0` is both an acknowledgement and a temperature
  report, and it is the temperature that makes it noise — so temp is tested
  first.
* `G0`-`G3` plus `G10`/`G11` (firmware retract) count as moves. `G28`/`G29`
  deliberately do not: homing and levelling happen a handful of times per
  print and are worth seeing.
* `busy` matches **only** `busy: processing` and a bare `wait`. Marlin sends
  `busy: paused for user` and `busy: paused for input` on the same prefix,
  and those mean the printer is waiting on you — filtering them away would
  be a real loss.

Errors, unknown commands and `// action:` lines are never filtered.

**Move traffic has no toggle.** It is the highest-volume class by a wide
margin, there is nothing readable in it, and switching it on mid-print just
buries everything else. `LOG_ALWAYS_HIDDEN` drops it regardless of what is
in the settings file, so a stale `log_show_moves: true` from an older
version cannot bring it back.

The bar above the log carries the follow toggle, the remaining noise
switches, a text filter and a hidden-line count.

## Fan speed

OctoPrint's push socket does not report fan speed anywhere — not in `temps`,
not in `state` — so it has to be inferred.

Reading it from live `M106` traffic **does not work on its own**, and the
failure is silent: `M106` is emitted only when the speed *changes*. Connect
to a print that set its fan at layer 2 and you will not see another one for
hours, so the reading sits at 0% looking like a broken sensor.

The real source is the G-code file, which is already downloaded and scanned
for the 3D view. The parser carries fan state forward line by line and
records it per segment, so `filepos` gives the duty at the exact point the
printer has reached — correct the instant a file finishes loading, however
long ago the fan was set.

Live `M106` parsing is kept as the fallback for SD-card prints, where there
is no file to read. It is tracked in `_absorb`, *before* the UI's noise
filter, so hiding move traffic does not stop it updating.

The fan card spins at a rate proportional to duty and holds the app at full
frame rate while it turns — a spinner rendered at the 9fps idle rate looks
broken rather than thrifty.

## Icons

`bedside/picons.py` adds hotend, bed, fan, cube, terminal, palette, sliders
and printer, following VertexUI's own contract — `(dl, cx, cy, r, col)`,
drawn from primitives, registered into `icons.ICONS` so `icons.draw()` and
`widgets.button(icon=...)` find them. They live here rather than in the
toolkit because a hotend and a heated bed are not general UI furniture.

`fan_at` takes an extra angle so it can spin.

## Settings

Two separate reasons the panel was unreachable. First, `imgui.same_line(0,
avail - 150)` passes that as *spacing after the previous item*, not an
absolute position — and it ran after an item that had already wrapped to a
new line, so the connection dot, host and **settings gear** were placed a
full window-width off the right edge. There was no way in at all. It now
uses `same_line(offset_from_start_x)`, measured from the start of the line.

Second, a full-screen ImGui window does not scroll its own overflow, so
everything past the bottom edge was truncated. The sections live in a child
window, which does scroll.

Thirteen identical collapsed bars is a wall, not a settings screen. The
sections are grouped into six tabs — Appearance, 3D View, Terminal, Alerts,
Printer, About — using VertexUI's own `widgets.tabs`, one visible at a time,
each with a one-line description of what it covers. Sections inside a tab
are always open: a collapsed header inside an already-hidden tab is a second
thing to click for nothing.

Sections: theme cards (drawn miniatures, click to switch), accent swatches
plus a custom picker, text size / density / corner radii / border width,
font choice (baked into the atlas, so it needs a restart), sound, terminal,
3D feature filters, 3D look, background effect, layout sizes, printer,
presets, diagnostics.

Sliders write to disk and re-decimate on mouse *release*, not per frame, so
dragging one does not stutter.

Two things from a typical "customise" panel that are **not** here: native
window blur (Windows acrylic) needs a transparent framebuffer that
hello_imgui does not expose, and wallpaper images need texture upload that
this app has no other reason to carry. `panel opacity` covers most of what
glassmorphism is actually for — it lets the background effect show through,
which opaque panels cover completely.

## Type

Two faces, not one. `UI_FACES` picks the body face; `DISPLAY_FACES` picks a
separate face for the readouts and the uppercase micro-labels, and
`build_faces()` hands them to VertexUI as roles.

One face doing every job is most of what makes an interface look like a
default — a body face set at 35px is just big body text. The readouts use
Bahnschrift, which is DIN-derived and reads as instrumentation rather than
as a dialog box. Body text stays on Segoe UI, which is better at paragraphs
and at the log.

`DISPLAY_FACES` carries a size scale per face, because these faces disagree
about how much of the em the x-height takes: Bahnschrift at a matched em
size renders noticeably larger, so it is scaled to 0.88 to keep the card
layout intact. Both faces are exposed in Settings -> Appearance -> font
("interface" and "readouts"); the atlas is baked at startup, so a change
needs a restart.

`caps()` draws the micro-labels a glyph at a time with tracking, because
ImGui has no letter-spacing. They are three to seven characters each and
the tracking is most of what separates a laid-out panel from a stack of
default text.

## The side panel

Three temperature cards, a temperature graph, the controls, and a model
card. `plate()` draws the shared card background — surface fill, hairline
border, and a short accent rule along the top edge, which is what makes a
row of cards read as one instrument cluster instead of three boxes.

The graph legend lives *inside* the plate. Floating above it, it read as a
separate thing and cost twenty pixels the column does not have.

The area fills under the traces are triangle pairs with anti-aliasing
switched off for the duration. ImGui feathers the edge of every filled
shape, so each pair gets a soft seam down its shared diagonal — at roughly
three pixels per sample that is a stripe every three pixels across the
whole area, which reads as a dither pattern rather than as a fill. The
jagged top edge it leaves is covered by the trace drawn over it.

The controls sit on plates of their own rather than loose in the column,
and their row heights come from `imgui.get_frame_height()` rather than a
constant — the density setting changes it, and a hard-coded plate leaves
the input boxes hanging out of the bottom on "roomy".

The model card fills what the controls leave. Everything on it comes from
the parsed file rather than from the printer, so it stays right when
OctoPrint's own estimates drift. It carries its own label instead of a
section header, and it drops rows as the space shrinks — the bottom row
below 84px, the layer ladder below 62px, the whole card below 46px — so a
short window loses detail instead of overflowing. The heights snap to
whole rows (88 / 66 / 52 px): interpolating instead just clips the last
line of text against the bottom of the column, which reads as a broken
card rather than as a compact one.

The ladder is one cell per *band*, not per layer. At 200 layers across 340
pixels a true per-layer tick would be sub-pixel and would alias into a grey
smear, which reads as a texture rather than as progress.

### The splitter

Ten pixels of gap between the 3D view and the instrument panel was a gap,
not a division — the cards read as floating next to the model rather than
as their own column. It is now a hairline that fades out at both ends, with
a grip at the middle, and dragging it resizes the panel. That width was
already a setting, so the drag and the slider in Settings write the same
value; the write happens on release, because saving every frame of a drag
would rewrite settings.json sixty times a second.

### Card styles

`plate()` draws in one of four styles, and the choice changes whether the
card has a fill at all — which is what decides how much of the background
effect comes through the panel.

| | |
| --- | --- |
| raised | gradient fill, border, a hairline of light inside the top edge, drop shadow |
| plated | flat fill, border, accent rule along the top |
| outlined | border only; the background effect shows straight through |
| flat | fill only, no edges |

### Rounded gradients are possible after all

An earlier version of this note said a gradient fill could not be rounded,
because `AddRectFilledMultiColor` is square-cornered and a gradient inset
far enough to clear the corners leaves a band where it starts. That was
wrong, and `grad_fill()` does it properly.

The way through is not to draw a gradient at all — it is to draw the
rounded fill normally and then re-colour the vertices it just emitted:

```python
i0 = dl.vtx_buffer.size()
dl.add_rect_filled(p0, p1, col, rounding)
imgui.internal.shade_verts_linear_color_gradient_keep_alpha(
    dl, i0, dl.vtx_buffer.size(), grad_p0, grad_p1, col_top, col_bot)
```

`KeepAlpha` is the part that matters: it lerps RGB along the axis and
leaves each vertex's alpha alone, so the anti-aliased fringe around the
corners keeps its coverage and picks up the gradient with everything else.

The shadow under a card is three stacked rounded rects, each fainter and
larger — cheaper than a blur and, at this size, indistinguishable from one.

Each of the five panel sections — temperature cards, graph, controls, job
buttons, model card — can be switched off in Settings -> Printer. A machine
you watch all day does not need the same instruments as one you glance at.

### The panel previews itself

Everything on the Printer tab was invisible from inside Settings: the card
style, which sections are on, and the three size sliders all only showed up
once you had closed the panel.

`_panel_preview()` draws the whole window in miniature at the top of the
group — topbar, 3D view with its overlay buttons, the panel column in the
chosen style with only the chosen sections, and the terminal strip. One
mock rather than five, because the sliders are proportions of *each other*
and previewing them separately would not show that. It takes its aspect
from `io.display_size`, so it is the user's window, not a generic one.

Two things it does not do to scale. Corners: the real 7px radius at a third
size turns every card into a pill, so `plate()` is handed a themed copy
with its own rounding — the style stays legible without the proportions
lying. And the print itself: a flat square reads as a colour swatch, so the
model is three faces of a box in three shades, which is the smallest thing
that reads as an object.

It is pure draw-list work — no widgets, nothing hoverable, nothing that can
steal a click from the real controls underneath. The `LAYOUT` group was
folded in beneath it for the obvious reason: a preview you have to scroll
away from to reach the control it previews is not a preview.

### Cards carry their own headers

The column used to be organised by `widgets.section`: a tick, a label and a
rule floating in the gaps between the cards. That reads as captions
*around* boxes. Each block is now one card with its own header — icon,
tracked title, hairline, and a right-aligned state — which is what the
temperature cards at the top of the column already looked like, so the
whole column finally reads as one stack of instruments.

The header's state also colours the card's accent edge, so the panel says
what the machine is doing from the corner of your eye: purple for locked,
green while running, amber when paused.

Removing the section rules paid for the extra header height almost exactly.
`imgui.dummy(ImVec2(0, 2))` between cards was not worth 12px each — a dummy
costs its own height *plus* a full `item_spacing` — and dropping both is
what let the model card keep its bottom row at the default window size.

## The window icon

hello_imgui reads the window and taskbar icon from `app_settings/icon.png`
*inside its assets folder*, and its default assets folder is the one
shipped with imgui_bundle — which is why the app wore the bundle's own icon
for so long. `_use_our_assets()` calls `set_assets_folder()` on ours and
`add_assets_search_path()` on the bundle's, so our icon wins while its
fonts stay reachable: the main folder is consulted first and the first
match wins.

Under PyInstaller the assets land in `_internal/`, which is `sys._MEIPASS`,
so the lookup is frozen-aware. `datas=[('assets', 'assets')]` in the spec
is what puts them there; the `--icon` in the spec is a separate thing, and
only covers what Explorer shows.

To check it actually took, ask the window manager rather than the screen:
`SendMessageW(hwnd, WM_GETICON, ...)` returns the HICON the window is
really wearing.

## Sound

The toolkit's own cues are tonal — sine partials with a falling pitch,
which is what makes a cue read as a knock. Right for a tool you are
clicking through; wrong for something open beside you for nine hours,
where every notification is a small tap on the shoulder.

`bedside/sounds.py` swaps the oscillator for moving air. A band of
filtered noise under a slow raised-cosine envelope reads as breath, and has
no onset to flinch at. Identity comes from where the band sits and which
way it sweeps — rising opens, falling closes, low-and-slow means something
went wrong — plus a quiet sine underneath so the five cues are not all
"shhh" at different lengths.

The band is steered by overlap-add: each 512-sample window gets one real
FFT, a gaussian gain curve in *log* frequency around that window's centre,
and one inverse. Filtering per sample would be too slow to render on
demand; sweeping the centre linearly rather than logarithmically makes a
2000 -> 400 Hz fall sound like it drops off a cliff at the end.

Rendered and measured, at volume 0.30:

| cue | length | peak | spectral centroid |
| --- | --- | --- | --- |
| hover | 90 ms | 0.07 | 3481 -> 3176 Hz |
| click | 150 ms | 0.18 | 2231 -> 1765 Hz |
| ok | 600 ms | 0.15 | 1752 -> 2342 Hz |
| warn | 560 ms | 0.13 | 1467 -> 973 Hz |
| error | 680 ms | 0.15 | 756 -> 480 Hz |

Every cue starts and ends at exactly 0.0 with no DC offset — a waveform
that starts or stops at non-zero amplitude clicks, and that click is most
of "harsh". The spec shape is the toolkit's, `(segments, gain)`, so
everything that reads `specs` still works; the numbers just mean band
centre rather than oscillator pitch. The old sets stay selectable in
Settings -> Alerts, because this is a preference, not a correction.

### Your own .wav files

`custom .wav` in the cue-set picker plays `hover.wav`, `click.wav`,
`ok.wav`, `warn.wav` and `error.wav` from `%APPDATA%\bedside\sounds`.

Per cue, not all-or-nothing: replacing just the click and leaving the rest
synthesised is the common case, and a set that stayed silent until all five
files existed would be useless for it. Any missing cue falls back to its
breeze equivalent.

A file that is not really a WAV — an mp3 renamed, or 24-bit out of an
editor — must not take the interface silent either, so the read is guarded
and the cue falls back with the reason recorded. The settings row shows all
three states: a green dot for present and readable, red for present and
unreadable, a grey ring for absent.

The volume slider still applies: a file is scaled linearly and plays at its
recorded level at the slider's maximum of 0.60. Only 16-bit PCM is
rescaled; anything else passes through untouched rather than mangled,
because winsound will happily play a great many things this code has no
business rewriting.

`toast_in` and `toast_out` are cues too, so a notification arriving and
leaving each have a sound — rising in, falling out, so the pair reads as
one object entering and leaving the room. VertexUI's `Toasts` had no hook
for it, so it gained `on_event("in"|"out", kind, text)`, fired from the
toast thread and guarded: a callback that raises must not take that thread
down, or the overlay stops repainting and the last frame stays on the
desktop. Bedside's handler only calls `sound.play`, which queues and
returns.

"write starter cues" renders the synthesised set into the folder as real
files. Hearing the shape you are replacing is most of knowing what to
record, and it saves guessing at the five names.

## The shader backdrop

`effects.py` draws on ImGui's draw list, one call per particle, which puts
a hard ceiling on what it can be: a few hundred dots and lines. Anything
*continuous* — a gradient that moves, cloud, a horizon, light that pools —
is per-pixel work, and per-pixel work on the CPU at 1300x880 is not a
background, it is the whole frame budget.

There is already a GL context here for the toolpath, so `bg.py` is a
fragment shader over a fullscreen triangle, rendered to its own target and
blitted underneath everything else. Six scenes — aurora, plasma, flow,
nebula, grid, warp — tinted from the same accent as the rest of the
interface, with brightness and speed sliders.

All six live in one program behind a branch on a uniform rather than one
program each: switching is then not a recompile, and the settings panel can
flick between them while you watch. It renders at half resolution on each
axis (a quarter of the pixels) because every scene is soft by construction
and nothing in them survives being looked at closely. Every scene is
vignetted — the panels sit in the middle and the backdrop has no business
competing with them for contrast there.

It is opaque: it composites the theme background itself rather than
blending over the window fill, which keeps one guess out of the colour
maths. And a backdrop is decoration, so one exception kills it for the rest
of the run rather than throwing from the frame loop forever.

### Measuring it took three attempts

The first pass timed one `render()` behind a `glFinish()` and reported
**5.2ms for every scene** — identical across shaders of wildly different
complexity, which is the tell that the number is a frame sync and not the
work. The second timed 1 against 9 and took the slope, and produced
*negative* milliseconds: the first render in a frame absorbs whatever the
driver still owes from the previous one, so subtracting it overshoots.

What works is flushing before starting the clock and amortising the one
remaining sync over sixteen renders:

| scene | per frame |
| --- | --- |
| aurora | 0.14 ms |
| plasma | 0.09 ms |
| flow | 0.10 ms |
| nebula | 0.09 ms |
| grid | 0.10 ms |
| warp | 0.13 ms |

Under 1% of a 60fps budget, and `flow` — three domain-warped five-octave
fbm lookups per pixel — is no dearer than `plasma`. At this resolution the
GPU is nowhere near its limit.

## The background effect

Painted twice a frame from one simulation: once behind the content, and
once over it at a fraction of the alpha (Settings -> "over panels").

Behind-only, the effect is visible in the gaps between panels and nowhere
else, which on a dashboard this dense is a thin border of weather around an
interface that does not participate in it. The second pass drifts across
the cards, the graph and the model, and that is what makes it read as one
atmosphere rather than as wallpaper. It goes on the window's own draw list
after the content, so it sits above everything drawn that frame but still
below popups and tooltips — a modal has to stay readable.

`step()` and `paint()` are separate for this reason: painting twice must
not advance the motion twice.

Ten effects now. `embers` rise and wander and flicker; `dust` runs three
depths at different speeds, because parallax is the whole trick and
identical motes at one speed read as noise; `orbits` has no integration at
all — position is a closed form of the clock, so it cannot drift out of
shape however long it runs.

"panel opacity" is the other half, and works the opposite way round — it
fades `surface` so the *background* pass shows through the cards.

## What VertexUI provides

| Module | Used for |
| --- | --- |
| `theme` / `settings` | the whole palette, the settings panel, persistence |
| `anim` | camera easing, the progress sweep, the connection pulse |
| `logview` | the G-code terminal — glides instead of yanking |
| `toasts` | print finished / stopped, over a fullscreen app |
| | *unanchored on purpose — see below* |
| `sound` | connect, finished, error cues |
| `widgets` / `icons` | buttons, pills, sliders, sections, badges |

## Toasts must not be anchored to our own window

`Toasts(anchor_titles=["Bedside"])` looks right and is a trap. `_find()`
runs `EnumWindows` and calls `GetWindowText` on every visible window —
including our own, in our own process — which sends `WM_GETTEXT` back to the
render thread. That is the deadlock VertexUI's own docs warn about, and when
it stalls the toast thread mid-display the last blit stays painted: a toast
frozen on the desktop that never clears.

Constructed with no `anchor_titles`, `_anchor()` returns a fixed screen
corner and never enumerates anything. `toasts.stop()` also runs on shutdown,
or the layered overlay can outlive the app.

That was not the whole story, though. With the anchor removed the toast
still stranded itself in the corner, and instrumenting `_items` under load
showed the *logic* was fine — the entry expired on schedule and the blank
frame was issued. The clear itself was what failed: `UpdateLayeredWindow`
with an all-zero bitmap does not reliably erase on every compositor, and
overlay injectors (NVIDIA, Overwolf) make it worse. **`vertexui/toasts.py`
now hides the window instead of trusting a transparent blit** — that change
lives in the toolkit, not here, and is currently uncommitted in
`C:\claude\vertexui-repo`.

## Layout

```
bedside/client.py   REST + push socket on a worker thread
bedside/gcode.py    G-code -> segments, extrude flags, byte offsets
bedside/view3d.py   orbit camera and the cached projection
bedside/app.py      layout and frame loop
bedside/glview.py   GPU mesh build, shaders and the offscreen target
bedside/sounds.py   the airy cue set, and the picker for the others
bedside/effects.py  ambient particles, behind and over the UI
tools/makeicon.py    regenerates assets/icon.png, .ico and app_settings/
```

## Sending files

### requests sends two framing headers at once

Uploading with `files=` builds the entire multipart body in memory before
anything leaves, so a 120 MB sliced file costs 120 MB of body on top of the
120 MB already read, and there is no progress to report because the body is
finished before the first byte goes out. So the envelope is written by
hand and the body is a generator.

That swapped one problem for a quieter one. `requests` picks its framing by
calling `super_len()` on the body; a bare generator has no length, so it
adds `Transfer-Encoding: chunked` — **in addition to** the `Content-Length`
already set in the headers, rather than instead of it. Both framings on one
request is a combination RFC 9112 resolves in favour of chunked and that
proxies are entitled to reject outright, which would have shown up as
uploads that work against a bare OctoPrint and fail behind nginx.

A local server that re-parses the request with the stdlib's own multipart
parser caught it: `Transfer-Encoding` present, `Content-Length` present,
payload byte-identical anyway because the stdlib honoured the length. The
fix is not to strip the header afterwards but to give the body a `__len__`,
so requests takes the other branch on its own:

```python
class _SizedBody:
    def __len__(self):  return self._len
    def __iter__(self): return self._make()
```

### The file dialog does not belong on the draw thread

`GetOpenFileNameW` is modal and stays open as long as somebody takes to
find a file. On the draw thread that is a frozen window with a stalled
shader backdrop behind it. It runs on a worker instead, with our own window
passed as `hwndOwner`: the dialog still disables the owner for its
duration, so the app keeps animating but correctly refuses input. The
worker calls `CoInitializeEx` first, because the shell namespace extensions
inside the dialog are COM objects and that thread has never initialised it.

Drag-and-drop was considered and dropped. hello_imgui exposes no drop
callback, the `glfw` Python package is not installed, and the Win32 route
means subclassing the window proc that GLFW owns — a crash risk in a
released app for a convenience the Open dialog already covers.

### Popups opened inside a child window

`open_popup` and `begin_popup_modal` have to meet in the same ID stack.
Calling both inside the list's `begin_child` lines up and *looks* fine, but
the modal is then parented to a scrolling region: it clips at the child's
edge and scrolls with the content. The row loop now only records which
entry was asked about, and the dialog is raised after `end_child`.

The popup name is also its window title, so `"start print?"` was showing up
in the title bar. Everything after `###` is id-only, which lets the visible
half be written for a person: `"Start print###askprint"`. The pre-existing
cancel dialog had the same slip and was fixed with it.

### The accent is a setting, so it cannot carry meaning alone

The confirm dialogs first drew the filename — the one fact the dialog
exists to convey — in `t.accent`. Measured against the popup ground:

| colour | on `surface` `#16161A` | |
| --- | --- | --- |
| `accent` (shipped `noir-red`) | 3.70:1 | fails AA |
| `accent` (a dark user pick) | 1.35:1 | invisible |
| `text_mute` | 2.38:1 | fails AA |
| `text_dim` | 4.76:1 | AA |
| `text` | 15.05:1 | AAA |

So it failed the 4.5:1 floor *before* anyone touched the theme, and the
accent is a user setting: any sufficiently dark pick takes the filename to
the point of vanishing, which is what prompted the report. Read off the
rendered frame rather than the palette, the filename was at **1.45:1**.

The fix is not a brighter accent — `accent_bright` is only 5.32:1 on the
shipped theme and is equally free to be dark. It is that **critical text
does not get its colour from a setting at all**. The filename is now `text`
(measured **16.07:1** on the same frame), with the accent kept as a 3px bar
beside it: a bar is not text, the non-text threshold is 3:1, and 3.70:1
clears it. Supporting lines moved `text_mute` → `text_dim`.

The rule this leaves behind: `accent` is for shapes, fills, and text you
could delete without losing information. Anything the user has to *read* to
make a decision is `text` or `text_dim`.

### …and the rest of the palette was failing too

Fixing the dialog prompted the obvious question, and the answer was worse
than the dialog. Rendering the settings screen and reading the pixels back:

| | bands below 4.5:1 | median | worst |
| --- | --- | --- | --- |
| backdrop **off**, wash off | 5 of 13 | 5.19:1 | 2.59:1 |
| grid backdrop, wash at the 0.72 default | 5 of 13 | 5.19:1 | 2.59:1 |
| aurora backdrop, wash off | 3 of 8 | 4.64:1 | 2.35:1 |

The first two rows are the finding. **Turning the backdrop off entirely
changed nothing**, and turning the readability wash up to its default
changed nothing either — the ground was already dark, measured at
L=0.0034, and flattening a dark ground flatter does not help type that is
too dim to begin with. The wash had been asked to fix a palette problem it
could not reach, which is exactly how it felt to use.

The palette itself, against the lightest ground text lands on
(`surface_hover` `#212127`):

| role | noir-red | noir-blue | slate-lime |
| --- | --- | --- | --- |
| `text_mute` | 2.11:1 | 2.11:1 | 1.77:1 |
| `text_dim` | 4.23:1 | 4.23:1 | 3.54:1 |

Every shipped theme, failing. `text_mute` carries 57 uses in `app.py`
alone — every micro-label, every metadata line, every settings blurb.

#### The fix is derived, not dialled

Hard-coding replacement greys would be right for exactly these three
themes and wrong the moment anyone saved a custom one. So `readable(t)`
takes a theme and pushes each text role away from the ground until it
clears a target — 7:1 for anything carrying a sentence, 4.5:1 for labels
and metadata — by binary search on a blend toward whichever pole the
ground is not. A light theme therefore gets *darker* text, not brighter,
and a role that already passes is returned untouched so a
well-designed theme is never repainted.

It runs in `_apply_theme` and at `vui.install`, so it covers the startup
theme, every settings change, and any preset loaded later.

| | before | after |
| --- | --- | --- |
| `text_mute` | `#53535F` 2.11:1 | `#87878F` 4.50:1 |
| `text_dim` | `#828290` 4.23:1 | `#ABABB4` 7.00:1 |
| `danger` | `#E54355` 4.01:1 | `#E75565` 4.50:1 |

Re-measured on rendered frames, counting only bands whose brightest pixel
is actually one of the theme's text colours — scoring the backdrop or the
panel miniature as failed text makes the number meaningless in both
directions:

| scene | before | after |
| --- | --- | --- |
| none | 5 fail, median 5.19 | **0 fail**, median 8.63 |
| grid @ 0.72 | 5 fail | **0 fail**, median 8.63 |
| nebula @ 0.72 | 4 fail | **0 fail**, median 8.62 |
| plasma | 4 fail | **0 fail**, median 8.44 |
| aurora, wash off | 3 fail | 1 fail, median 6.61 |

The one residual is a bright scene with the wash turned off, which is the
narrow job the wash should have had all along — and now does, instead of
standing in for a palette that did not clear the floor.

#### Two ways the measurement lied first

Worth recording, because both flattered and then damned the result:

1. **The harness built its own theme.** It called `vui.install` with a bare
   `build_theme()` and never set `post_init`, so it was rendering a theme
   the app never shows — the fix landed and the numbers did not move at
   all. The harness now mirrors `main()` exactly.
2. **The 97th percentile is not a glyph.** A short label is mostly empty
   space, so the 97th percentile of its band lands on an anti-aliased edge
   and reports roughly half the real contrast. Six bands looked like
   failures until the estimator was changed to the glyph core; probing
   their brightest pixels returned `#ABABB4` and `#87878F`, the correctly
   lifted colours.

### Panel translucency was compositing the shader into the type

`panel_alpha` was written so the *particle* effects could show through a
card — sparse dots, harmless behind text. It also lets the *shader
backdrop* through, which is dense and structured, and that composites
straight into the surface text is read on. At the author's setting of 0.48
the grid ran visibly through the JOB and MODEL cards.

The first attempt to measure it reported almost nothing, because it asked
for the card's "ground" as the darker 60% of its pixels — precisely the
set that excludes the grid lines doing the damage. Rendering the card at
the chosen alpha and again opaque, then subtracting, is unambiguous:
whatever differs inside the card *is* the backdrop coming through it.

The metric that matches the complaint turned out to be the brightest
non-text pixel inside the card, against the card's own surface — how much
the backdrop stands out from the thing it is behind:

| panel_alpha | damp | peak / surface |
| --- | --- | --- |
| 0.48 | none | 4.80x |
| 0.48 | 0.50 | 2.53x |
| 0.48 | 0.85 | **1.51x** |
| 1.00 (opaque) | — | 1.25x ← the target |

The fix is a dark underlay beneath the card. Compositing
backdrop → underlay(alpha *d*) → card(alpha *a*) leaves the backdrop
contributing (1−*a*)(1−*d*) where it contributed (1−*a*), so *d* is simply
the fraction of the leak removed, and an opaque card pays nothing because
an opaque fill covers the underlay.

The first version scaled *d* by (1−*a*) as well, reasoning that a barely
transparent card should barely be damped. That factor is already in the
compositing, and applying it twice caps the underlay at (1−*a*) — which is
why it could not clear the grid even at *d*=1, topping out at 2.48x. With
the double-count removed, *d*=0.85 lands 0.48 at 1.51x against an opaque
baseline of 1.25x.

`outlined` cards are excluded: showing the background straight through is
the entire point of that style, and picking it is an unambiguous request
for exactly the thing being damped everywhere else.

### A disabled icon button, where there isn't one

`widgets.icon_button` has no disabled state, and the first version simply
ignored the click while still drawing a live-looking button — worst of all
on the row being printed, where the two buttons you must not press looked
exactly like the ones you may. Where an action is unavailable the button is
now not submitted at all: the glyph is painted at 30% `text_mute`, takes no
id, and still explains itself on hover through `is_mouse_hovering_rect`.

---

## Known gaps

- The CPU fallback still sorts whole pieces rather than pixels, so on a
  machine that cannot give us a GL 3.3 context two pieces that genuinely
  interleave in depth resolve as a unit. The GPU path has a real depth
  buffer and does not have this problem.
- No webcam pane yet — it needs an MJPEG decode into a GL texture, which is
  a different job from everything else here.
- Single printer.
- No folder creation or rename in the file browser, and uploads all land at
  the storage root. Both are `/api/files` calls away; neither has come up.
- The file dialog is `comdlg32`, so the browser's upload button is
  Windows-only even though everything it does afterwards is not.
- `toasts` and `sound` are Windows-only; the rest is cross-platform.
