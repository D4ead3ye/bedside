"""OctoPrint transport: REST calls plus the push socket, on a worker thread.

The GUI never blocks on the network. This module owns a background thread
that keeps a socket open, and exposes a lock-guarded snapshot the frame
loop reads once per draw.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections import deque
from urllib.parse import quote

import requests
import websocket

TEMP_HISTORY = 600  # samples; OctoPrint pushes roughly one per second


def normalise_host(raw: str) -> str:
    """Accepts "192.168.1.50", "octopi.local" or a full URL."""
    t = (raw or "").strip().rstrip("/")
    if not t:
        return ""
    if t.startswith("http://") or t.startswith("https://"):
        return t
    return "http://" + t


class Snapshot:
    """Plain, immutable-enough copy of printer state for one frame."""

    __slots__ = ("connected", "error", "state_text", "flags", "job_file",
                 "job_origin", "job_size", "completion", "filepos",
                 "print_time", "print_left", "temps", "z", "fan")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def printing(self) -> bool:
        f = self.flags or {}
        return bool(f.get("printing") or f.get("paused"))

    @property
    def paused(self) -> bool:
        return bool((self.flags or {}).get("paused"))


class _SizedBody:
    """An iterable that knows its own length.

    `requests` decides between Content-Length and chunked encoding by
    calling `super_len()` on the body. A bare generator has no length, so
    it sets `Transfer-Encoding: chunked` — and it sets it *in addition* to
    any Content-Length already in the headers, which is a combination
    proxies are entitled to reject and which RFC 9112 resolves in favour of
    the chunked framing. Giving the body a `__len__` makes requests reach
    the other branch on its own, and only one framing header is sent.
    """

    __slots__ = ("_make", "_len")

    def __init__(self, make, length):
        self._make, self._len = make, length

    def __len__(self):
        return self._len

    def __iter__(self):
        return self._make()


class OctoClient:
    def __init__(self, host: str = "", api_key: str = ""):
        self.host = normalise_host(host)
        self.api_key = api_key or ""

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ws = None
        self._thread = None

        # -- state the GUI reads
        self.connected = False
        self.error = ""
        self.state_text = "offline"
        self.flags = {}
        self.job_file = None
        self.job_origin = "local"
        self.job_size = 0
        self.completion = 0.0
        self.filepos = 0
        self.print_time = None
        self.print_left = None
        self.temps = {"tool0": (None, None), "bed": (None, None)}
        self.z = None
        self.fan = 0.0

        # temperature curves, as parallel deques so the graph can slice them
        self.hist = {k: deque(maxlen=TEMP_HISTORY)
                     for k in ("t0", "t0t", "bed", "bedt")}

        # drained by the GUI into the LogView
        self._pending_logs: list[tuple[str, str]] = []
        # set whenever the active file changes, so the 3D view reloads
        self.job_generation = 0

    # ------------------------------------------------------------- REST

    def _headers(self):
        return {"X-Api-Key": self.api_key}

    def get(self, path, **kw):
        r = requests.get(self.host + path, headers=self._headers(),
                         timeout=kw.pop("timeout", 15), **kw)
        r.raise_for_status()
        return r

    def post(self, path, body):
        r = requests.post(self.host + path, headers=self._headers(),
                          json=body, timeout=15)
        r.raise_for_status()
        return r

    def command(self, path, body):
        """Fire-and-forget POST that logs failures rather than raising into
        a draw call."""
        try:
            self.post(path, body)
            return True
        except Exception as e:
            self.log(f"{path} failed: {e}", "error")
            return False

    def download_gcode(self, path: str, origin: str = "local") -> bytes:
        r = self.get(f"/downloads/files/{origin}/{path}", timeout=120)
        return r.content

    def delete(self, path):
        r = requests.delete(self.host + path, headers=self._headers(),
                            timeout=30)
        r.raise_for_status()
        return r

    # ------------------------------------------------------- file storage

    def list_files(self, origin: str = "local"):
        """Everything on the printer, plus how much room is left."""
        r = self.get(f"/api/files/{origin}?recursive=true", timeout=30)
        return r.json()

    def delete_file(self, path: str, origin: str = "local"):
        self.delete(f"/api/files/{origin}/{quote(path)}")

    def select_file(self, path: str, origin: str = "local",
                    start: bool = False):
        """Load a file into the printer, and optionally begin printing it."""
        self.post(f"/api/files/{origin}/{quote(path)}",
                  {"command": "select", "print": bool(start)})

    def upload_file(self, local_path: str, origin: str = "local",
                    folder: str = "", progress=None) -> str:
        """Streamed multipart upload. Returns the printer-side path.

        `requests` builds the whole multipart body in memory when handed
        `files=`, so a 120 MB sliced file costs 120 MB of body on top of
        the 120 MB already read, and reports no progress at all because the
        body is finished before the first byte leaves. Writing the envelope
        by hand costs about twenty lines and turns the upload into a
        generator: constant memory, and a fraction the UI can draw.
        """
        name = os.path.basename(local_path)
        size = os.path.getsize(local_path)
        boundary = "----bedside" + uuid.uuid4().hex

        def field(key, value):
            return (f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                    f"{value}\r\n").encode()

        head = b""
        if folder:
            head += field("path", folder)
        head += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="file"; '
                 f'filename="{name}"\r\n'
                 f"Content-Type: application/octet-stream\r\n\r\n").encode()
        tail = f"\r\n--{boundary}--\r\n".encode()

        def body():
            yield head
            sent = 0
            with open(local_path, "rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    sent += len(chunk)
                    if progress:
                        progress(sent / size if size else 1.0)
                    yield chunk
            yield tail

        headers = dict(self._headers())
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        total = len(head) + size + len(tail)
        r = requests.post(self.host + f"/api/files/{origin}",
                          data=_SizedBody(body, total),
                          headers=headers, timeout=(15, 600))
        r.raise_for_status()
        try:
            return (r.json().get("files", {}).get(origin, {})
                    .get("path") or name)
        except Exception:
            return name

    # --------------------------------------------------- app-key pairing

    @staticmethod
    def appkey_request(host: str) -> str:
        host = normalise_host(host)
        r = requests.post(f"{host}/plugin/appkeys/request",
                          json={"app": "Bedside"}, timeout=10)
        if r.status_code == 404:
            raise RuntimeError("No Application Keys plugin — paste a key manually.")
        r.raise_for_status()
        return r.json()["app_token"]

    @staticmethod
    def appkey_poll(host: str, token: str):
        """None while the user has not answered yet."""
        host = normalise_host(host)
        r = requests.get(f"{host}/plugin/appkeys/request/{token}", timeout=10)
        if r.status_code == 202:
            return None
        if r.status_code == 404:
            raise RuntimeError("Authorisation denied or timed out.")
        r.raise_for_status()
        return r.json()["api_key"]

    # -------------------------------------------------------- log buffer

    def log(self, text, kind=None, tag="octo"):
        with self._lock:
            self._pending_logs.append((text, kind, tag))

    def drain_logs(self):
        with self._lock:
            out, self._pending_logs = self._pending_logs, []
        return out

    # ------------------------------------------------------ push socket

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="octo-socket")
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except Exception as e:
                with self._lock:
                    self.connected = False
                    self.error = str(e)
                    self.state_text = "disconnected"
                self.log(f"socket: {e}", "error", "net")
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30.0)

    def _session(self):
        # Step 1: passive login trades the API key for a session token.
        r = requests.post(self.host + "/api/login", headers=self._headers(),
                          json={"passive": True}, timeout=10)
        r.raise_for_status()
        d = r.json()
        name, session = d.get("name", ""), d.get("session", "")
        if not session:
            raise RuntimeError("login returned no session token")

        url = (self.host.replace("https://", "wss://", 1)
                        .replace("http://", "ws://", 1)) + "/sockjs/websocket"

        ws = websocket.create_connection(url, timeout=20)
        self._ws = ws
        try:
            # Step 2: the socket is anonymous until this frame arrives.
            ws.send(json.dumps({"auth": f"{name}:{session}"}))
            with self._lock:
                self.connected = True
                self.error = ""
            self.log("connected", "ok", "net")

            while not self._stop.is_set():
                raw = ws.recv()
                if raw is None or raw == "":
                    break
                for payload in _sockjs_frames(raw):
                    self._absorb(payload)
        finally:
            self._ws = None
            with self._lock:
                self.connected = False
            try:
                ws.close()
            except Exception:
                pass

    # ------------------------------------------------------ state update

    def _absorb(self, msg: dict):
        data = msg.get("current") or msg.get("history")
        if not data:
            return
        is_history = "history" in msg

        with self._lock:
            st = data.get("state") or {}
            if st:
                self.state_text = st.get("text") or "—"
                self.flags = st.get("flags") or {}

            job = data.get("job") or {}
            jf = job.get("file") or {}
            newfile = jf.get("path") or jf.get("name")
            if newfile and newfile != self.job_file:
                self.job_file = newfile
                self.job_origin = jf.get("origin") or "local"
                self.job_size = jf.get("size") or 0
                self.job_generation += 1

            prog = data.get("progress") or {}
            if prog:
                self.completion = prog.get("completion") or 0.0
                # `or 0` would snap the marker back to the start of the file
                # any time OctoPrint sends a progress block without a
                # filepos — keep the last known position instead.
                fp = prog.get("filepos")
                if fp is not None:
                    self.filepos = int(fp)
                self.print_time = prog.get("printTime")
                self.print_left = prog.get("printTimeLeft")

            if data.get("currentZ") is not None:
                self.z = data["currentZ"]

            for t in (data.get("temps") or []):
                tool = t.get("tool0") or {}
                bed = t.get("bed") or {}
                self.temps = {
                    "tool0": (tool.get("actual"), tool.get("target")),
                    "bed": (bed.get("actual"), bed.get("target")),
                }
                self.hist["t0"].append(tool.get("actual"))
                self.hist["t0t"].append(tool.get("target"))
                self.hist["bed"].append(bed.get("actual"))
                self.hist["bedt"].append(bed.get("target"))

            logs = data.get("logs") or []

        # Outside the lock above; self.log takes it again per line.
        for line in logs:
            f = parse_fan(line)
            if f is not None:
                self.fan = f
            self.log(line, _log_kind(line), "gcode")
        if is_history and logs:
            self.log(f"({len(logs)} lines of backlog)", "info", "net")

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                connected=self.connected, error=self.error,
                state_text=self.state_text, flags=dict(self.flags),
                job_file=self.job_file, job_origin=self.job_origin,
                job_size=self.job_size, completion=self.completion,
                filepos=self.filepos, print_time=self.print_time,
                print_left=self.print_left, temps=dict(self.temps), z=self.z,
                fan=self.fan,
            )

    def temp_series(self):
        with self._lock:
            return {k: list(v) for k, v in self.hist.items()}


# ---------------------------------------------------------------- helpers

def _sockjs_frames(raw: str):
    """SockJS sends `o` (open), `h` (heartbeat), `a[...]` (payloads) and
    `c[...]` (close). The raw endpoint can also send bare JSON."""
    if not raw:
        return []
    tag = raw[0]
    if tag == "a":
        try:
            items = json.loads(raw[1:])
        except Exception:
            return []
        out = []
        for it in items:
            if isinstance(it, str):
                try:
                    out.append(json.loads(it))
                except Exception:
                    continue
            elif isinstance(it, dict):
                out.append(it)
        return out
    if tag in "ohc":
        return []
    try:
        v = json.loads(raw)
        return [v] if isinstance(v, dict) else []
    except Exception:
        return []


# Terminal traffic during a print is overwhelmingly four things, and none of
# them are worth reading: position moves, bare acknowledgements, temperature
# polling and SD status. Classify so the UI can drop them before they ever
# reach the log buffer.
_RE_TEMP = re.compile(r"^Send:\s*(?:N\d+\s+)?M105\b|^Recv:.*?\bT:\s*-?[\d.]+", re.I)
_RE_SD = re.compile(r"^Send:\s*(?:N\d+\s+)?M27\b|^Recv:\s*SD printing", re.I)
# G0-G3 are the bulk; G10/G11 are firmware retract, which is just as spammy.
# G28/G29 are deliberately NOT here — homing and bed levelling happen a
# handful of times per print and are worth seeing.
_RE_MOVE = re.compile(r"^Send:\s*(?:N\d+\s+)?G(?:10|11|0|1|2|3)\b", re.I)
_RE_ACK = re.compile(r"^Recv:\s*ok\b", re.I)
# Keepalive chatter while the firmware is chewing on its buffer. Matched
# narrowly on purpose: Marlin sends "busy: paused for user" and "busy:
# paused for input" on the same prefix, and those mean the printer is
# waiting on YOU — hiding them would be a real loss.
_RE_BUSY = re.compile(r"^Recv:\s*(?:echo:\s*)?busy:\s*processing\b"
                      r"|^Recv:\s*wait\s*$", re.I)


# OctoPrint's push socket does not report fan speed anywhere, but the fan
# commands go past in the terminal stream, so track them from there.
# M106 with no S means full speed; M107 is off.
_RE_FAN = re.compile(r"^Send:\s*(?:N\d+\s+)?M(106|107)\b(.*)$", re.I)
_RE_FAN_S = re.compile(r"\bS(\d+(?:\.\d+)?)", re.I)


def parse_fan(line: str):
    """Fan speed 0.0-1.0 from an M106/M107 line, or None if not one."""
    m = _RE_FAN.match(line)
    if not m:
        return None
    if m.group(1) == "107":
        return 0.0
    s = _RE_FAN_S.search(m.group(2) or "")
    if not s:
        return 1.0
    return max(0.0, min(1.0, float(s.group(1)) / 255.0))


# Commands that move the machine or move the coordinate system under it.
# Refused from the manual G-code box while a print is running.
#
# G92 and M84 are here for the same reason as the jog commands even though
# neither is a move: G92 redefines the origin, so every later coordinate in
# the running job lands somewhere else, and M84 drops the steppers, which
# lets the head sag mid-print. Both scrap the job as surely as a jog does.
# Longer codes come first in the alternation so G10 is not matched as G1.
_RE_MOTION = re.compile(
    r"^\s*(?:N\d+\s+)?(G10|G11|G28|G29|G92|G0|G1|G2|G3|M18|M84)\b", re.I)


def is_motion_command(cmd: str) -> bool:
    """True if this line would move the machine or shift its origin."""
    return bool(_RE_MOTION.match(cmd or ""))


def classify_line(line: str) -> str:
    """One of: temp, busy, sd, move, ack, other.

    Temperature is tested first because `Recv: ok T:205 /210` is both an
    acknowledgement and a temperature report, and it is the temperature that
    makes it noise.
    """
    if _RE_TEMP.search(line):
        return "temp"
    if _RE_BUSY.search(line):
        return "busy"
    if _RE_SD.search(line):
        return "sd"
    if _RE_MOVE.match(line):
        return "move"
    if _RE_ACK.match(line):
        return "ack"
    return "other"


def _log_kind(line: str):
    low = line.lower()
    if low.startswith("recv: error") or "error" in low[:24]:
        return "error"
    if low.startswith("send:"):
        return None
    if "warn" in low[:24]:
        return "warn"
    return None
