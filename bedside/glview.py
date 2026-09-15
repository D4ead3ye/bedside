"""GPU toolpath renderer: real depth buffer, geometry uploaded once.

Why this exists
---------------
The previous renderer drew the toolpath as 2D polylines on ImGui's draw
list. That has no depth buffer, so occlusion had to be faked by sorting
pieces back-to-front — and painter's ordering cannot resolve surfaces that
interpenetrate or that share a mean depth. Every "geometry disappears at
some angles" report traced back to that limitation, and each fix only moved
which angles broke.

It was also slow for the wrong reason: every point was projected in Python
every frame, so a 100k-point model cost ~36ms just to rebuild vertex lists
before anything was drawn.

Here the geometry is uploaded to the GPU once. Each extrusion becomes a
box one layer high and one extrusion wide, so walls and top surfaces alike
are real surfaces rather than stacked lines, and the depth buffer resolves
them exactly. Rotating costs one uniform update.
"""

from __future__ import annotations

import ctypes

import numpy as np

try:
    from OpenGL import GL
    HAVE_GL = True
except Exception:                                    # pragma: no cover
    GL = None
    HAVE_GL = False


VERT = """#version 330 core
layout(location=0) in vec3  a_pos;      // world position
layout(location=1) in vec3  a_nrm;      // horizontal surface normal
layout(location=2) in float a_seg;      // segment index, for the progress split
layout(location=3) in float a_feat;     // feature class

uniform mat4  u_mvp;
uniform vec3  u_light;                  // world-space, follows the camera
uniform float u_cur;                    // segment index the nozzle is at
uniform float u_zmin;
uniform float u_fade;                   // mm below the nozzle to fade over
uniform float u_curz;                   // nozzle height
uniform float u_layer;                  // layer height
uniform vec3  u_base;                   // model colour
uniform vec3  u_bright;
uniform vec3  u_deep;
uniform vec3  u_ghost;
uniform float u_ghosta;                 // its opacity, from the settings
uniform float u_floor;                  // darkest shade, never zero

out vec4 v_col;
out float v_z;               // world height, for the cutaway

void main() {
    gl_Position = u_mvp * vec4(a_pos, 1.0);
    v_z = a_pos.z;

    if (a_seg > u_cur) {
        // not printed yet
        v_col = vec4(u_ghost, u_ghosta);
        return;
    }

    // Half-Lambert against a light that orbits with the camera, so the face
    // being looked at is never the dark one.
    float lit = clamp(0.5 + 0.5 * dot(normalize(a_nrm), u_light), 0.0, 1.0);

    // Height below the nozzle: recent layers stay bright.
    float f = clamp(1.0 - (u_curz - a_pos.z) / max(u_fade, 0.001), 0.0, 1.0);

    vec3 c = mix(u_deep, u_base, f);
    c = mix(c, u_bright, smoothstep(0.88, 1.0, f));

    // A faint band per layer so a stack still reads as layers.
    float band = mod(floor(a_pos.z / max(u_layer, 0.001)), 2.0);
    float shade = mix(u_floor, 1.0, lit) * (1.0 - 0.06 * band);

    v_col = vec4(c * shade, 1.0);
}
"""

FRAG = """#version 330 core
in vec4 v_col;
in float v_z;
uniform float u_clipz;     // nothing above this height is drawn
out vec4 frag;
void main() {
    // Ghost opacity of zero means "do not draw it", not "draw it
    // invisibly": an invisible fragment still writes depth, and would
    // punch holes in the printed model behind it.
    if (v_col.a < 0.004) discard;
    // Cutaway. Discarding in the fragment stage rather than moving the
    // vertex keeps the cut face flat at exactly this height instead of
    // slicing whole boxes off at their nearest corner.
    if (v_z > u_clipz) discard;
    frag = v_col;
}
"""


# The bed is drawn in the same pass as the model, not painted over the top
# of it afterwards, so the depth buffer decides which is in front.
LINE_VERT = """#version 330 core
layout(location=0) in vec3  a_pos;
layout(location=1) in float a_alpha;   // outline bright, grid faint
uniform mat4 u_mvp;
out float v_alpha;
void main() {
    gl_Position = u_mvp * vec4(a_pos, 1.0);
    v_alpha = a_alpha;
}
"""

LINE_FRAG = """#version 330 core
in float v_alpha;
uniform vec4 u_color;
out vec4 frag;
void main() { frag = vec4(u_color.rgb, u_color.a * v_alpha); }
"""


def _compile(src, kind):
    s = GL.glCreateShader(kind)
    GL.glShaderSource(s, src)
    GL.glCompileShader(s)
    if not GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS):
        raise RuntimeError(GL.glGetShaderInfoLog(s).decode())
    return s


def build_mesh(tp, disp, layer_h, line_mm=0.45):
    """A box per extrusion: one layer high, one extrusion wide.

    The first version emitted only the *vertical* face of each bead, on the
    reasoning that a wall is what you actually look at. That is true of
    walls and false of everything else. A bead modelled as a vertical
    ribbon has no width in plan, so looking down at a solid top layer or a
    sheet of infill presents every extrusion edge-on: each one rasterises
    to a sub-pixel sliver, and the surface reads as a stipple of dots with
    a moire crawling over it as the camera turns. Walls looked right and
    flat surfaces looked like static.

    A box has area from every direction, so there is no angle at which the
    geometry thins out. Three faces, not six: the bottom of a bead is never
    visible — it sits on the layer below, or on the bed — and the ends are
    always inside the neighbouring segment. Rendering the bottom face was
    measured to change not one pixel at any angle, and it is a quarter of
    the geometry. Culling is off, so winding does not matter here; the
    normals are only used for shading.

    Built from the Display's already-thinned points, so the feature filter,
    the deviation bound and the detail budget all still apply.
    """
    lo, hi = [], []
    for s0, s1 in disp.slices:
        run = disp.idx[s0:s1]
        if len(run) < 2:
            continue
        lo.append(run[:-1])
        hi.append(run[1:])
    empty = (np.zeros((0, 3), np.float32), np.zeros((0, 3), np.float32),
             np.zeros(0, np.float32), np.zeros(0, np.float32))
    if not lo:
        return empty
    i0 = np.concatenate(lo)
    i1 = np.concatenate(hi)

    # Drop zero-length segments. They make degenerate faces, which rasterise
    # to nothing and leave pinholes scattered over an otherwise solid wall.
    keep = np.linalg.norm(tp.pts[i1] - tp.pts[i0], axis=1) > 1e-6
    i0, i1 = i0[keep], i1[keep]
    if not len(i0):
        return empty
    idx = i0

    p0 = tp.pts[i0].astype(np.float32)
    p1 = tp.pts[i1].astype(np.float32)
    half = max(float(line_mm), 0.05) * 0.5

    d = p1 - p0
    ln = np.linalg.norm(d[:, :2], axis=1, keepdims=True)
    u = np.divide(d[:, :2], np.maximum(ln, 1e-9))          # along the path
    sv = np.stack([u[:, 1], -u[:, 0]], axis=1)             # across it

    # Extend each box half a width past both ends. Consecutive boxes are
    # independent, so without this every corner in a perimeter has a notch
    # bitten out of it where the two mitres fail to meet.
    ext = np.zeros_like(p0)
    ext[:, :2] = u * half
    off = np.zeros_like(p0)
    off[:, :2] = sv * half
    # Overlap into the layer above. Real layer spacing wanders a little
    # either side of the median, so boxes exactly one layer high leave
    # hairline gaps that speckle an otherwise solid wall.
    up = np.zeros_like(p0)
    up[:, 2] = layer_h * 1.35

    a_ = p0 - ext
    b_ = p1 + ext
    # bottom corners, then the same four raised
    c = [a_ - off, a_ + off, b_ + off, b_ - off]
    c += [x + up for x in c]
    a0, a1, b1, b0, a0u, a1u, b1u, b0u = c

    zero = np.zeros((len(p0), 1), np.float32)
    one = np.ones((len(p0), 1), np.float32)
    n_up = np.concatenate([zero, zero, one], axis=1)
    n_side = np.concatenate([sv, zero], axis=1).astype(np.float32)

    faces = (
        # (corners of the two triangles, face normal)
        ((a0u, b0u, b1u, a0u, b1u, a1u), n_up),        # top
        ((a0, b0, b0u, a0, b0u, a0u), -n_side),        # one side
        ((a1, b1u, b1, a1, a1u, b1u), n_side),         # the other
    )

    # Segment-major, not face-major: all faces of segment i sit together.
    # That makes `seg` non-decreasing, so "everything printed so far" is a
    # contiguous prefix of the buffer and the renderer can split the draw
    # into an opaque pass and a translucent one with one searchsorted.
    n_faces = len(faces)
    tri = np.stack([np.stack(v, axis=1) for v, _ in faces], axis=1)
    pos = tri.reshape(-1, 3)
    nrm = np.stack([np.repeat(n[:, None, :], 6, axis=1) for _, n in faces],
                   axis=1).reshape(-1, 3)
    seg = np.repeat(idx.astype(np.float32), n_faces * 6)
    feat = np.repeat(tp.feat[idx].astype(np.float32), n_faces * 6)
    return (pos.astype(np.float32), nrm.astype(np.float32), seg, feat)


class GLScene:
    """Owns the GL objects. All calls must happen on the render thread."""

    def __init__(self):
        self.ok = False
        self.prog = None
        self.vao = self.vbo = None
        self.fbo = self.tex = self.rbo = None
        self.fb_w = self.fb_h = 0
        self.count = 0
        self.uni = {}
        # the bed, in its own tiny program: flat colour, no shading
        self.line_prog = None
        self.line_uni = {}
        self.line_vao = self.line_vbo = None
        self.line_count = 0

    # -- lifecycle --------------------------------------------------------

    def _ensure_program(self):
        if self.prog is not None:
            return
        prog = GL.glCreateProgram()
        vs = _compile(VERT, GL.GL_VERTEX_SHADER)
        fs = _compile(FRAG, GL.GL_FRAGMENT_SHADER)
        GL.glAttachShader(prog, vs)
        GL.glAttachShader(prog, fs)
        GL.glLinkProgram(prog)
        if not GL.glGetProgramiv(prog, GL.GL_LINK_STATUS):
            raise RuntimeError(GL.glGetProgramInfoLog(prog).decode())
        self.prog = prog
        for n in ("u_mvp", "u_light", "u_cur", "u_zmin", "u_fade", "u_curz",
                  "u_layer", "u_base", "u_bright", "u_deep", "u_ghost",
                  "u_ghosta", "u_floor", "u_clipz"):
            self.uni[n] = GL.glGetUniformLocation(prog, n)

    def _ensure_line_program(self):
        if self.line_prog is not None:
            return
        prog = GL.glCreateProgram()
        GL.glAttachShader(prog, _compile(LINE_VERT, GL.GL_VERTEX_SHADER))
        GL.glAttachShader(prog, _compile(LINE_FRAG, GL.GL_FRAGMENT_SHADER))
        GL.glLinkProgram(prog)
        if not GL.glGetProgramiv(prog, GL.GL_LINK_STATUS):
            raise RuntimeError(GL.glGetProgramInfoLog(prog).decode())
        self.line_prog = prog
        for n in ("u_mvp", "u_color"):
            self.line_uni[n] = GL.glGetUniformLocation(prog, n)

    def upload_lines(self, pts, alpha):
        """`pts` is (N, 3) float32, consecutive pairs forming GL_LINES;
        `alpha` is (N,) per-vertex opacity, so the plate outline and the
        grid inside it can differ without a second draw call."""
        self._ensure_line_program()
        if self.line_vao is None:
            self.line_vao = GL.glGenVertexArrays(1)
            self.line_vbo = GL.glGenBuffers(1)
        GL.glBindVertexArray(self.line_vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.line_vbo)
        pts = np.ascontiguousarray(pts, np.float32)
        data = np.concatenate(
            [pts, np.asarray(alpha, np.float32).reshape(-1, 1)],
            axis=1).astype(np.float32)
        data = np.ascontiguousarray(data)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, data.nbytes, data,
                        GL.GL_DYNAMIC_DRAW)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, False, 16,
                                 ctypes.c_void_p(0))
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 1, GL.GL_FLOAT, False, 16,
                                 ctypes.c_void_p(12))
        GL.glBindVertexArray(0)
        self.line_count = len(data)

    def _ensure_target(self, w, h):
        w, h = max(16, int(w)), max(16, int(h))
        if self.fbo is not None and (w, h) == (self.fb_w, self.fb_h):
            return
        self.release_target()
        self.fbo = GL.glGenFramebuffers(1)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo)
        self.tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0,
                        GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                  GL.GL_TEXTURE_2D, self.tex, 0)
        self.rbo = GL.glGenRenderbuffers(1)
        GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, self.rbo)
        GL.glRenderbufferStorage(GL.GL_RENDERBUFFER, GL.GL_DEPTH_COMPONENT24, w, h)
        GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_DEPTH_ATTACHMENT,
                                     GL.GL_RENDERBUFFER, self.rbo)
        good = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER) == GL.GL_FRAMEBUFFER_COMPLETE
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        self.fb_w, self.fb_h = w, h
        if not good:
            self.release_target()
            raise RuntimeError("framebuffer incomplete")

    def release_lines(self):
        if self.line_vbo is not None:
            GL.glDeleteBuffers(1, [self.line_vbo])
            self.line_vbo = None
        if self.line_vao is not None:
            GL.glDeleteVertexArrays(1, [self.line_vao])
            self.line_vao = None
        self.line_count = 0

    def release_target(self):
        if self.tex is not None:
            GL.glDeleteTextures(int(self.tex)); self.tex = None
        if self.rbo is not None:
            GL.glDeleteRenderbuffers(1, [self.rbo]); self.rbo = None
        if self.fbo is not None:
            GL.glDeleteFramebuffers(1, [self.fbo]); self.fbo = None
        self.fb_w = self.fb_h = 0

    # -- geometry ---------------------------------------------------------

    def upload(self, pos, nrm, seg, feat):
        self._ensure_program()
        if self.vao is None:
            self.vao = GL.glGenVertexArrays(1)
            self.vbo = GL.glGenBuffers(1)
        inter = np.empty((len(pos), 8), np.float32)
        inter[:, 0:3] = pos
        inter[:, 3:6] = nrm
        inter[:, 6] = seg
        inter[:, 7] = feat
        GL.glBindVertexArray(self.vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, inter.nbytes, inter, GL.GL_STATIC_DRAW)
        stride = 32
        for loc, size, off in ((0, 3, 0), (1, 3, 12), (2, 1, 24), (3, 1, 28)):
            GL.glEnableVertexAttribArray(loc)
            GL.glVertexAttribPointer(loc, size, GL.GL_FLOAT, False, stride,
                                     ctypes.c_void_p(off))
        GL.glBindVertexArray(0)
        self.count = len(pos)
        self.ok = True

    # -- drawing ----------------------------------------------------------

    def render(self, w, h, mvp, uniforms, bg, line_color=None, split=None):
        self._ensure_target(w, h)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo)
        GL.glViewport(0, 0, self.fb_w, self.fb_h)
        GL.glClearColor(bg[0], bg[1], bg[2], 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        if self.count:
            GL.glEnable(GL.GL_DEPTH_TEST)
            GL.glDepthFunc(GL.GL_LESS)
            GL.glEnable(GL.GL_BLEND)
            GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
            GL.glUseProgram(self.prog)
            GL.glUniformMatrix4fv(self.uni["u_mvp"], 1, GL.GL_TRUE,
                                  mvp.astype(np.float32))
            for k, v in uniforms.items():
                loc = self.uni.get(k)
                if loc is None or loc < 0:
                    continue
                if isinstance(v, (tuple, list, np.ndarray)):
                    GL.glUniform3f(loc, float(v[0]), float(v[1]), float(v[2]))
                else:
                    GL.glUniform1f(loc, float(v))
            GL.glBindVertexArray(self.vao)
            # Two passes, in that order, because the ghost is translucent
            # and the printed part is not. Vertices are ordered by segment,
            # so everything printed so far is a contiguous prefix.
            #
            # Order is the whole fix. One interleaved pass let an unprinted
            # wall drawn early write depth and cull the printed geometry
            # behind it, which is why raising the opacity used to make the
            # print vanish rather than veil it.
            #
            # Depth writes stay ON for the ghost too. Turning them off
            # sounds right for transparency and is wrong here: the
            # unprinted region is hundreds of stacked layers deep, so every
            # fragment blends over the last and a 25% shell comes out
            # opaque. Writing depth keeps only the nearest ghost surface,
            # which is what a shell should be.
            cut = self.count if split is None else max(0, min(split, self.count))
            if cut:
                GL.glDrawArrays(GL.GL_TRIANGLES, 0, cut)
            if cut < self.count:
                GL.glDrawArrays(GL.GL_TRIANGLES, cut, self.count - cut)
            GL.glBindVertexArray(0)

            if self.line_count and line_color is not None:
                # Same depth buffer, so the part of the bed behind the model
                # is simply not drawn. Painting it on the ImGui draw list
                # afterwards, as this used to, put a wireframe rectangle
                # straight across the front of the print.
                GL.glUseProgram(self.line_prog)
                GL.glUniformMatrix4fv(self.line_uni["u_mvp"], 1, GL.GL_TRUE,
                                      mvp.astype(np.float32))
                GL.glUniform4f(self.line_uni["u_color"], *line_color)
                GL.glBindVertexArray(self.line_vao)
                GL.glDrawArrays(GL.GL_LINES, 0, self.line_count)
                GL.glBindVertexArray(0)

            GL.glDisable(GL.GL_DEPTH_TEST)
            GL.glDisable(GL.GL_BLEND)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        # NOTE: an OpenGL framebuffer has its origin bottom-left and ImGui's
        # is top-left, so this must be drawn with flipped V coordinates:
        #   imgui.image(ref, size, ImVec2(0, 1), ImVec2(1, 0))
        return self.tex


def look_at(eye, target, up):
    f = target - eye
    f /= max(np.linalg.norm(f), 1e-9)
    s = np.cross(f, up)
    s /= max(np.linalg.norm(s), 1e-9)
    u = np.cross(s, f)
    m = np.eye(4, dtype=np.float32)
    m[0, :3], m[1, :3], m[2, :3] = s, u, -f
    m[0, 3] = -np.dot(s, eye)
    m[1, 3] = -np.dot(u, eye)
    m[2, 3] = np.dot(f, eye)
    return m


def perspective(fovy, aspect, near, far):
    t = 1.0 / np.tan(fovy * 0.5)
    m = np.zeros((4, 4), np.float32)
    m[0, 0] = t / max(aspect, 1e-6)
    m[1, 1] = t
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m
