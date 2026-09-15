"""The printer's own storage: listing, uploading, deleting, starting.

Same shape as `client.py` — one worker thread, a lock-guarded snapshot the
frame loop reads once per draw, and nothing that can block a draw call.

Every job runs through one queue rather than a thread each. Uploads to a
Pi over wifi are slow enough that a second one started by accident would
halve the first, and "delete while uploading" is not a state worth having
a correct answer for.
"""

from __future__ import annotations

import os
import queue
import threading
import time


class Entry:
    """One printable file on the printer."""

    __slots__ = ("path", "name", "display", "size", "date", "est", "origin")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def folder(self) -> str:
        head, _, _ = self.path.rpartition("/")
        return head

    def size_text(self) -> str:
        n = self.size or 0
        if n >= 1 << 20:
            return f"{n / (1 << 20):.1f} MB"
        if n >= 1 << 10:
            return f"{n / (1 << 10):.0f} KB"
        return f"{n} B"

    def est_text(self) -> str:
        if not self.est:
            return "—"
        s = int(self.est)
        if s >= 3600:
            return f"{s // 3600}h {(s % 3600) // 60:02d}m"
        if s >= 60:
            return f"{s // 60}m"
        return f"{s}s"

    def age_text(self) -> str:
        if not self.date:
            return "—"
        d = max(0.0, time.time() - self.date)
        if d < 3600:
            return f"{int(d // 60)} min ago"
        if d < 86400:
            return f"{int(d // 3600)}h ago"
        if d < 86400 * 14:
            return f"{int(d // 86400)}d ago"
        return time.strftime("%d %b %Y", time.localtime(self.date))


def _flatten(nodes, out):
    """OctoPrint nests folders; the list is easier to use flat."""
    for n in nodes or ():
        if n.get("type") == "folder":
            _flatten(n.get("children") or (), out)
            continue
        if n.get("type") != "machinecode":
            continue
        an = n.get("gcodeAnalysis") or {}
        out.append(Entry(
            path=n.get("path") or n.get("name") or "",
            name=n.get("name") or "",
            display=n.get("display") or n.get("name") or "",
            size=int(n.get("size") or 0),
            date=float(n.get("date") or 0.0),
            est=float(an.get("estimatedPrintTime") or 0.0),
            origin=n.get("origin") or "local",
        ))
    return out


class FileStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries: list[Entry] = []
        self.free = 0
        self.total = 0
        self.busy = False
        self.op = ""            # what the worker is doing, for the UI
        self.message = ""
        self.fraction = 0.0
        self.error = ""
        self.generation = 0     # bumped whenever `entries` changes
        self.events: list[tuple[str, str]] = []   # (text, kind) for the log

        self._q: queue.Queue = queue.Queue()
        self._thread = None

    # ----------------------------------------------------------- plumbing

    def _set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def _say(self, text, kind="info"):
        with self.lock:
            self.events.append((text, kind))

    def drain_events(self):
        with self.lock:
            out, self.events = self.events, []
            return out

    def snapshot(self):
        with self.lock:
            return {
                "entries": self.entries, "free": self.free,
                "total": self.total, "busy": self.busy, "op": self.op,
                "message": self.message, "fraction": self.fraction,
                "error": self.error, "generation": self.generation,
            }

    def _ensure(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="files")
        self._thread.start()

    def _submit(self, kind, client, **kw):
        self._q.put((kind, client, kw))
        self._set(busy=True, error="")
        self._ensure()

    # ------------------------------------------------------------- public

    def refresh(self, client):
        self._submit("list", client)

    def upload(self, client, paths, folder=""):
        for p in paths:
            self._submit("upload", client, path=p, folder=folder)
        self._submit("list", client)

    def remove(self, client, entry):
        self._submit("delete", client, path=entry.path, origin=entry.origin,
                     name=entry.display)
        self._submit("list", client)

    def start_print(self, client, entry):
        self._submit("print", client, path=entry.path, origin=entry.origin,
                     name=entry.display)

    # ------------------------------------------------------------- worker

    def _run(self):
        while True:
            try:
                kind, client, kw = self._q.get(timeout=2.0)
            except queue.Empty:
                self._set(busy=False, op="", fraction=0.0, message="")
                return
            try:
                self._do(kind, client, kw)
            except Exception as exc:
                self._set(error=str(exc), fraction=0.0)
                self._say(f"{kind} failed: {exc}", "error")
            finally:
                self._q.task_done()
                if self._q.empty():
                    self._set(busy=False, op="", fraction=0.0, message="")

    def _do(self, kind, client, kw):
        if kind == "list":
            self._set(op="listing", message="reading the printer's files",
                      fraction=0.0)
            data = client.list_files()
            entries = _flatten(data.get("files"), [])
            # Newest first: the thing you just uploaded is the thing you
            # are about to print.
            entries.sort(key=lambda e: e.date or 0.0, reverse=True)
            with self.lock:
                self.entries = entries
                self.free = int(data.get("free") or 0)
                self.total = int(data.get("total") or 0)
                self.generation += 1

        elif kind == "upload":
            local = kw["path"]
            name = os.path.basename(local)
            self._set(op="uploading", message=name, fraction=0.0)
            client.upload_file(local, folder=kw.get("folder", ""),
                               progress=lambda f: self._set(fraction=f))
            self._say(f"uploaded {name}", "ok")

        elif kind == "delete":
            self._set(op="deleting", message=kw["name"], fraction=0.0)
            client.delete_file(kw["path"], kw.get("origin", "local"))
            self._say(f"deleted {kw['name']}", "info")

        elif kind == "print":
            self._set(op="starting", message=kw["name"], fraction=0.0)
            client.select_file(kw["path"], kw.get("origin", "local"),
                               start=True)
            self._say(f"started {kw['name']}", "ok")
