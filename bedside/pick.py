"""The Windows Open dialog, straight from comdlg32.

Bedside ships as a single folder with no Tk and no Qt, so the file picker
is the one the OS already has. `GetOpenFileNameW` is the whole dependency,
reached through ctypes the same way the window icon is.

It runs on a worker thread rather than the draw thread. The dialog is
modal and can sit open for as long as somebody takes to find a file, and
the rule the rest of this app is built on — the GUI never blocks — should
not stop applying just because the wait is a person instead of a socket.
Passing our own window as the owner still disables it for the duration, so
the app keeps animating but refuses input, which is exactly the behaviour
a modal dialog is supposed to produce.
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

OFN_READONLY = 0x00000001
OFN_HIDEREADONLY = 0x00000004
OFN_NOCHANGEDIR = 0x00000008          # do NOT move the process CWD
OFN_ALLOWMULTISELECT = 0x00000200
OFN_PATHMUSTEXIST = 0x00000800
OFN_FILEMUSTEXIST = 0x00001000
OFN_EXPLORER = 0x00080000

COINIT_APARTMENTTHREADED = 0x2


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", wintypes.LPARAM),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", wintypes.LPCWSTR),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]


# A filter is a run of NUL-separated pairs ending in a double NUL, which is
# why it cannot be built with a plain Python string literal.
_FILTER = "\0".join([
    "G-code", "*.gcode;*.gco;*.g;*.ufp",
    "All files", "*.*",
]) + "\0\0"

# 64K of wide chars. A multi-select returns every path in this one buffer,
# so it has to be big enough for a whole folder's worth of names.
_BUF = 1 << 16


def active_window():
    """Our own window, for the dialog to be modal to."""
    try:
        u = ctypes.windll.user32
        return u.GetActiveWindow() or u.GetForegroundWindow() or 0
    except Exception:
        return 0


def open_gcode(owner=0, multi=True, title="Upload G-code"):
    """Blocks until dismissed. Returns a list of paths; empty if cancelled."""
    buf = ctypes.create_unicode_buffer(_BUF)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = owner or 0
    ofn.lpstrFilter = _FILTER
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = _BUF
    ofn.lpstrTitle = title
    ofn.lpstrDefExt = "gcode"
    ofn.Flags = (OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST
                 | OFN_HIDEREADONLY | OFN_NOCHANGEDIR
                 | (OFN_ALLOWMULTISELECT if multi else 0))
    if not ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        # 0 with no extended error is a plain cancel, not a failure.
        err = ctypes.windll.comdlg32.CommDlgExtendedError()
        if err:
            raise OSError(f"file dialog failed (0x{err:04X})")
        return []

    # One file comes back as a bare path. Several come back as the folder,
    # then each name, NUL between and a second NUL at the end — so the
    # single-file case is just the one where there is nothing after it.
    raw = buf[:]
    parts = [p for p in raw.split("\0") if p]
    if len(parts) == 1:
        return [parts[0]]
    head, names = parts[0], parts[1:]
    return [os.path.join(head, n) for n in names]


class Picker:
    """Runs `open_gcode` on a thread and holds the result for the GUI."""

    def __init__(self):
        self.lock = threading.Lock()
        self.busy = False
        self.paths: list[str] = []
        self.error = ""
        self._thread = None

    def start(self, owner=0, multi=True, title="Upload G-code"):
        with self.lock:
            if self.busy:
                return False
            self.busy = True
            self.error = ""
        self._thread = threading.Thread(
            target=self._run, args=(owner, multi, title),
            daemon=True, name="file-dialog")
        self._thread.start()
        return True

    def _run(self, owner, multi, title):
        got, err = [], ""
        try:
            # Shell namespace extensions inside the dialog are COM objects,
            # and this thread has never initialised COM. S_FALSE (already
            # initialised) is fine; only a hard failure is worth caring
            # about, and even then the dialog usually still opens.
            try:
                ctypes.windll.ole32.CoInitializeEx(None,
                                                   COINIT_APARTMENTTHREADED)
            except Exception:
                pass
            got = open_gcode(owner, multi, title)
        except Exception as exc:
            err = str(exc)
        finally:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass
            with self.lock:
                self.paths = got
                self.error = err
                self.busy = False

    def take(self):
        """(busy, paths, error) — paths are handed over exactly once."""
        with self.lock:
            p, e = self.paths, self.error
            self.paths, self.error = [], ""
            return self.busy, p, e
