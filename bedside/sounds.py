"""A soft, airy cue set for Bedside.

VertexUI's own set is tonal — sine partials with a falling pitch, which is
what makes a cue read as a knock. That is right for a tool you are actively
clicking through, and wrong for something that sits open beside you for a
nine-hour print: every notification is a small tap on the shoulder.

This set is *noise*, not tone. A band of filtered noise with a slow
raised-cosine envelope reads as breath or wind, and carries no attack to
flinch at. Pitch identity — which cue was that? — comes from where the
band sits and which way it moves, plus a quiet low sine underneath, rather
than from a melody.

The spec shape is deliberately the same as the toolkit's, `(segments,
gain)` with a segment of `(from, to, milliseconds)`, so everything that
reads `specs` still works. The numbers mean something different: they are
the centre of the noise band in Hz, not an oscillator frequency.
"""

from __future__ import annotations

import io
import os
import wave
from pathlib import Path

import numpy as np

from vertexui import settings as vsettings
from vertexui import sound as vsound

CUE_NAMES = ("hover", "click", "ok", "warn", "error",
             "toast_in", "toast_out")

# The volume slider tops out here, and that is where a supplied file plays
# at the level it was recorded. Below it the file is scaled down. Baking
# the slider in rather than ignoring it keeps one control honest for both
# kinds of set.
VOL_FULL = 0.6

# Band centre in Hz, swept from -> to. Direction is most of the identity:
# rising opens, falling closes, and low-and-slow is the one that means
# something went wrong.
BREEZE_SPECS = {
    # a barely-there breath; hover fires constantly, so it has to be
    # something you notice only by its absence
    "hover": ([(2900.0, 2400.0, 90)], 0.22),
    # a soft puff of air, not a click
    "click": ([(2000.0, 1150.0, 150)], 0.55),
    # rising = opening = done
    "ok":    ([(1150.0, 2050.0, 380), (2050.0, 1500.0, 220)], 0.50),
    # falling, but still bright enough not to read as an error
    "warn":  ([(1600.0, 850.0, 300), (850.0, 700.0, 260)], 0.52),
    # low, dark and long enough to settle rather than startle
    "error": ([(900.0, 430.0, 340), (430.0, 330.0, 340)], 0.58),
    # A toast arriving and leaving. Quieter and shorter than the five
    # above, because they fire on their own rather than in reply to
    # something you did — and rising in, falling out, so the pair reads as
    # one object entering and leaving the room.
    "toast_in":  ([(1300.0, 2100.0, 240)], 0.30),
    "toast_out": ([(1900.0, 1150.0, 210)], 0.22),
}

# The toolkit's own sets predate the toast cues, so they are extended here
# rather than left silent for two of the seven.
TAPS_EXTRA = {"toast_in": ([(300.0, 380.0, 90)], 0.34),
              "toast_out": ([(380.0, 296.0, 90)], 0.26)}
CRISP_EXTRA = {"toast_in": ([(660.0, 880.0, 70)], 0.32),
               "toast_out": ([(880.0, 620.0, 70)], 0.26)}

RATE = 22050          # a noise band this soft has nothing above 11 kHz
BLOCK = 512           # overlap-add window
HOP = BLOCK // 2


class BreezeSet(vsound.SoundSet):
    """The toolkit's SoundSet with the oscillator replaced by moving air."""

    def __init__(self, specs=None, volume=0.16, enabled=True, **kw):
        kw.setdefault("rate", RATE)
        super().__init__(specs or BREEZE_SPECS, volume=volume,
                         enabled=enabled, **kw)
        # Fraction of the cue spent fading in and out. Long on both ends:
        # an onset is the thing that makes a sound feel like an alert, and
        # a long tail is what makes it feel like it belongs in the room.
        self.attack_frac = 0.28
        self.release_frac = 0.46
        self.body = 0.22          # the low sine under the noise
        self.breath_hz = 4.5      # slow amplitude shimmer, so it is not flat
        self.width = 0.62         # band width in octaves (gaussian sigma)

    # -- synthesis ------------------------------------------------------

    def _centres(self, segments, n):
        """Band centre for every sample, in Hz, across the whole cue."""
        out = np.empty(n, np.float64)
        total = sum(seg[2] for seg in segments) or 1.0
        at = 0
        for f0, f1, ms in segments:
            k = max(1, int(round(n * ms / total)))
            k = min(k, n - at)
            if k <= 0:
                break
            # Interpolate in log space: pitch is logarithmic, so a linear
            # ramp from 2000 to 400 spends most of its time up high and the
            # sweep sounds like it falls off a cliff at the end.
            out[at:at + k] = np.geomspace(f0, max(f1, 1.0), k)
            at += k
        if at < n:
            out[at:] = out[at - 1] if at else segments[0][0]
        return out

    def _noise_band(self, centres, n):
        """White noise steered through a moving band, by overlap-add.

        Filtering in blocks rather than per sample is what makes this cheap
        enough to render on demand: each 512-sample window gets one real
        FFT, a gaussian gain curve around that window's centre frequency,
        and one inverse.
        """
        rng = np.random.default_rng(20250907)
        pad = n + BLOCK * 2
        src = rng.standard_normal(pad)
        out = np.zeros(pad)
        win = np.hanning(BLOCK)
        freqs = np.fft.rfftfreq(BLOCK, 1.0 / self.rate)
        # log-frequency axis; guard the DC bin, which has no log
        lf = np.log2(np.maximum(freqs, 1.0))
        for start in range(0, n + BLOCK, HOP):
            block = src[start:start + BLOCK]
            if len(block) < BLOCK:
                break
            c = centres[min(start, n - 1)]
            gain = np.exp(-0.5 * ((lf - np.log2(c)) / self.width) ** 2)
            gain[0] = 0.0                      # no DC offset, ever
            spec = np.fft.rfft(block * win) * gain
            out[start:start + BLOCK] += np.fft.irfft(spec, BLOCK)
        band = out[:n]
        peak = float(np.max(np.abs(band))) or 1.0
        return band / peak

    def render(self, name: str) -> bytes:
        segments, gain = self.specs[name]
        total_ms = sum(seg[2] for seg in segments)
        n = max(1, int(self.rate * total_ms / 1000.0))
        t = np.arange(n) / self.rate

        centres = self._centres(segments, n)
        sig = self._noise_band(centres, n)

        # A quiet sine two octaves and a bit below the band gives the cue a
        # pitch you can name. Without it the five cues are all "shhh" and
        # only differ in length.
        if self.body > 0.0:
            phase = 2.0 * np.pi * np.cumsum(centres / 6.0) / self.rate
            sig = sig + self.body * np.sin(phase)
            sig /= (1.0 + self.body)

        # Raised cosine at both ends. A waveform that starts or stops at
        # non-zero amplitude clicks, and that click is most of "harsh".
        env = np.ones(n)
        na = max(1, int(n * self.attack_frac))
        nr = max(1, int(n * self.release_frac))
        env[:na] = 0.5 * (1.0 - np.cos(np.pi * np.arange(na) / na))
        env[n - nr:] = 0.5 * (1.0 + np.cos(np.pi * np.arange(nr) / nr))
        env *= 1.0 + 0.12 * np.sin(2.0 * np.pi * self.breath_hz * t)

        s = np.clip(sig * env * gain * self.volume, -1.0, 1.0)
        pcm = (s * 32767.0).astype("<i2")

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(pcm.tobytes())
        return buf.getvalue()


def custom_dir() -> Path:
    """Where the user drops their own cues."""
    return Path(vsettings.config_dir("bedside")) / "sounds"


class FileSet(vsound.SoundSet):
    """Plays `<cue>.wav` from a folder, per cue, with a fallback.

    Per cue, not all-or-nothing: replacing just the click and leaving the
    rest synthesised is the common case, and a set that went silent until
    all five files existed would be useless for it.
    """

    def __init__(self, folder=None, volume=0.16, enabled=True, **kw):
        super().__init__({k: ([], 1.0) for k in CUE_NAMES},
                         volume=volume, enabled=enabled, **kw)
        self.folder = Path(folder) if folder else custom_dir()
        self.fallback = BreezeSet(volume=volume, enabled=enabled)
        self.bad = {}                 # cue -> why the file was unusable

    def path(self, name) -> Path:
        return self.folder / f"{name}.wav"

    def found(self):
        """{cue: True/False} — what the settings panel reports."""
        return {n: self.path(n).is_file() for n in CUE_NAMES}

    def _scaled(self, raw: bytes) -> bytes:
        """Apply the volume slider to a supplied file.

        Only 16-bit PCM is rescaled; anything else is passed through
        untouched rather than mangled, because winsound will play a great
        many things this code has no business rewriting.
        """
        with wave.open(io.BytesIO(raw)) as w:
            p = w.getparams()
            frames = w.readframes(w.getnframes())
        gain = max(0.0, min(1.0, self.volume / VOL_FULL))
        if p.sampwidth == 2 and gain < 0.999:
            a = np.frombuffer(frames, "<i2").astype(np.float32) * gain
            frames = np.clip(a, -32768, 32767).astype("<i2").tobytes()
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(p.nchannels)
            w.setsampwidth(p.sampwidth)
            w.setframerate(p.framerate)
            w.writeframes(frames)
        return out.getvalue()

    def render(self, name: str) -> bytes:
        p = self.path(name)
        if p.is_file():
            try:
                data = self._scaled(p.read_bytes())
                self.bad.pop(name, None)
                return data
            except Exception as exc:
                # A file that is not really a WAV (an mp3 renamed, or 24-bit
                # from an editor) must not take the interface silent.
                self.bad[name] = f"{type(exc).__name__}: {exc}"
        self.fallback.volume = self.volume
        return self.fallback.render(name)

    def export(self) -> int:
        """Write the synthesised cues into the folder as starter files.

        Hearing the shape you are replacing is most of knowing what to
        record, and it saves guessing at the five file names.
        """
        self.folder.mkdir(parents=True, exist_ok=True)
        n = 0
        ref = BreezeSet(volume=VOL_FULL)
        for name in CUE_NAMES:
            dst = self.path(name)
            if dst.exists():
                continue
            dst.write_bytes(ref.render(name))
            n += 1
        self._cache.clear()
        return n

    def reveal(self):
        """Open the folder in the file manager."""
        self.folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.folder))       # noqa: S606 - Windows only


# Named sets the settings panel can offer. The toolkit's own stays
# available: this is a preference, not a correction.
SETS = {
    "breeze": lambda **kw: BreezeSet(**kw),
    "soft taps": lambda **kw: vsound.SoundSet(
        dict(vsound.DEFAULT_SPECS, **TAPS_EXTRA), **kw),
    "crisp": lambda **kw: vsound.SoundSet(
        dict(vsound.CRISP_SPECS, **CRISP_EXTRA), **kw),
    "custom .wav": lambda **kw: FileSet(**kw),
}
CUSTOM_SET = "custom .wav"
DEFAULT_SET = "breeze"


def install(name: str, volume: float = 0.16, enabled: bool = True):
    """Make `name` the set every widget plays from."""
    make = SETS.get(name) or SETS[DEFAULT_SET]
    return vsound.use(make(volume=volume, enabled=enabled))
