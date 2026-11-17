"""Upload queue: one upload at a time, with progress for the tray icon and Plasma.

Files are uploaded one after the other; files added while an upload runs join the same
batch, and progress is shown for the whole batch (bytes of all its files).

Plasma's job view (org.kde.JobViewServer, what Dolphin uses for copies) shows the batch in
the notification area with a progress bar, speed and a Cancel button.  Plasma waits about a
second before showing a job, so quick screenshot uploads don't flash a popup.  Views are
"transient": our own notification (Open / Copy link) replaces Plasma's "finished" entry.
"""

import os
import time
from collections import deque

import dbus
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import settings, upload
from .app_util import log

JOB_SERVER = "org.kde.JobViewServer"
KILLED = 1    # KJob::KilledJobError


class Job:
    def __init__(self, path, uploader=None):
        self.path, self.uploader = path, uploader
        self.name = os.path.basename(path)
        try:
            self.size = os.path.getsize(path)
        except OSError:
            self.size = 0
        self.sent = 0
        self.target = uploader_label(uploader)
        self.result, self.error, self.exception = None, "", None

    @property
    def finished(self):
        return self.result is not None or bool(self.error)


class UploadQueue(QObject):
    changed = pyqtSignal()                 # progress or state changed
    active_changed = pyqtSignal(bool)
    job_done = pyqtSignal(object)          # Job (result or error set)
    batch_done = pyqtSignal(object, bool)  # [Job], cancelled

    def __init__(self, bus, parent=None):
        super().__init__(parent)
        self.bus = bus
        self.pending = deque()
        self.batch = []           # every Job of the current batch, in order
        self.current = None
        self.cancelled = False
        self._worker = None
        self._samples = deque()   # (time, bytes done) for the speed
        self.plasma = None

    # -- state

    @property
    def active(self):
        return bool(self.current or self.pending)

    def totals(self):
        """-> (bytes done, bytes total) for the batch."""
        total = sum(j.size for j in self.batch)
        done = sum(j.size if j is not self.current and j not in self.pending else j.sent for j in self.batch)
        return done, total

    def fraction(self):
        done, total = self.totals()
        return done / total if total else 0.0

    def speed(self):
        if len(self._samples) < 2:
            return 0
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0

    def describe(self):
        """One line for the tray tooltip."""
        if not self.current:
            return ""
        n = len(self.batch)
        index = self.batch.index(self.current) + 1
        text = f"Uploading {self.current.name}" + (f" ({index}/{n})" if n > 1 else "")
        text += f" — {int(self.fraction() * 100)}%"
        sp = self.speed()
        if sp > 0:
            text += f" · {human_size(sp)}/s"
        return text

    # -- control

    def add(self, paths, uploader=None):
        """Queue files; returns their Jobs."""
        was = self.active
        if not was:
            self.batch, self.cancelled = [], False
            self._samples.clear()
        jobs = [Job(p, uploader) for p in paths]
        self.pending.extend(jobs)
        self.batch.extend(jobs)
        if not was:
            self.active_changed.emit(True)
            self._start_plasma_job()
            self._next()
        else:
            self._update_plasma()
            self.changed.emit()
        return jobs

    def cancel(self):
        """Cancel the running upload and drop the waiting ones."""
        if not self.active:
            return
        self.cancelled = True
        for job in self.pending:
            job.error = "Upload cancelled"
            job.exception = upload.Cancelled()
        self.pending.clear()
        if self._worker:
            self._worker.cancel()
            if self.plasma:
                self.plasma.update(infoMessage="Cancelling…")
        else:
            self._finish_batch()

    def _next(self):
        if not self.pending:
            self._finish_batch()
            return
        job = self.current = self.pending.popleft()
        log(f"uploading {job.path}")
        up = self._worker = upload.Uploader()
        up.progress.connect(lambda sent, total: self._progress(job, sent, total))
        up.finished.connect(lambda res, err: self._done(job, up, res, err))
        self._update_plasma()
        self.changed.emit()
        up.start(job.path, job.uploader)

    def _progress(self, job, sent, total):
        if job is not self.current:
            return
        # the request body has a few hundred bytes of multipart framing on top of the file
        job.sent = min(job.size, sent) if job.size else sent
        now = time.monotonic()
        self._samples.append((now, self.totals()[0]))
        while len(self._samples) > 2 and now - self._samples[0][0] > 3:
            self._samples.popleft()
        self._update_plasma()
        self.changed.emit()

    def _done(self, job, up, res, err):
        self._worker = None
        self.current = None
        job.result, job.error, job.exception = res, err, getattr(up, "error", None)
        job.sent = job.size
        up.deleteLater()
        self.job_done.emit(job)
        if self.cancelled:
            self._finish_batch()
        else:
            QTimer.singleShot(0, self._next)

    def _finish_batch(self):
        jobs, cancelled = self.batch, self.cancelled
        self.current = None
        self.pending.clear()
        if self.plasma:
            self.plasma.terminate(KILLED if cancelled else 0)
            self.plasma = None
        self.active_changed.emit(False)
        self.changed.emit()
        self.batch_done.emit(jobs, cancelled)

    # -- Plasma job view

    def _start_plasma_job(self):
        if not settings.get("upload.plasma_progress"):
            return
        self.plasma = PlasmaJob(self.bus, on_cancel=self.cancel)

    def _update_plasma(self):
        if not self.plasma:
            return
        done, total = self.totals()
        n = len(self.batch)
        fields = {"title": "Uploading" if n == 1 else f"Uploading {n} files",
                  "percent": dbus.UInt32(int(self.fraction() * 100)),
                  "totalBytes": dbus.UInt64(total), "processedBytes": dbus.UInt64(done),
                  "speed": dbus.UInt64(int(self.speed()))}
        if n > 1:
            fields["totalFiles"] = dbus.UInt64(n)
            fields["processedFiles"] = dbus.UInt64(sum(1 for j in self.batch if j.finished))
        cur = self.current
        if cur:
            fields["descriptionLabel1"], fields["descriptionValue1"] = "File", cur.name
            fields["descriptionLabel2"], fields["descriptionValue2"] = "To", cur.target
            fields["infoMessage"] = "Waiting for the server…" if cur.size and cur.sent >= cur.size else ""
        self.plasma.update(**fields)


class PlasmaJob:
    """One entry in Plasma's notification area. Calls are async: a missing or slow job server
    never blocks the GUI, and without one this does nothing."""

    def __init__(self, bus, on_cancel=None):
        self.bus, self.on_cancel = bus, on_cancel
        self.view = None
        self._match = None
        self._queued = {}
        self._ended = None
        try:
            if not bus.name_has_owner(JOB_SERVER):
                return
            server = bus.get_object(JOB_SERVER, "/JobViewServer")
            server.requestView("snappie", dbus.Int32(1),   # 1 = can be cancelled
                               dbus.Dictionary({"transient": True}, signature="sv"),
                               dbus_interface="org.kde.JobViewServerV2",
                               reply_handler=self._got_view, error_handler=self._failed)
        except dbus.DBusException as e:
            self._failed(e)

    def _got_view(self, path):
        self.view = self.bus.get_object(JOB_SERVER, path)
        self._match = self.bus.add_signal_receiver(self._cancel_requested, "cancelRequested",
                                                   "org.kde.JobViewV3", JOB_SERVER, path)
        if self._queued:
            self.update(**self._queued)
        if self._ended is not None:
            self.terminate(self._ended)

    def _failed(self, e):
        log("no Plasma job progress:", e)

    def _cancel_requested(self):
        if self.on_cancel:
            self.on_cancel()

    def update(self, **fields):
        if self.view is None:
            self._queued.update(fields)
            return
        try:
            self.view.update(dbus.Dictionary(fields, signature="sv"), dbus_interface="org.kde.JobViewV3",
                             ignore_reply=True)
        except dbus.DBusException as e:
            log("job progress update failed:", e)

    def terminate(self, code=0):
        if self.view is None:
            self._ended = code
            return
        if self._match:
            self._match.remove()
            self._match = None
        try:
            self.view.terminate(dbus.UInt32(code), "", dbus.Dictionary({"transient": True}, signature="sv"),
                                dbus_interface="org.kde.JobViewV3", ignore_reply=True)
        except dbus.DBusException as e:
            log("job progress end failed:", e)
        self.view = None
        self.on_cancel = None


def uploader_label(name):
    name = name or settings.get("upload.uploader") or ""
    for fname, display in upload.list_uploaders():
        if fname == name:
            return display
    return os.path.splitext(os.path.basename(name))[0] or "uploader"


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
