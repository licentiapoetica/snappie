import pytest
from PyQt6.QtCore import QCoreApplication, QObject, QTimer, pyqtSignal

from snappie import transfers, upload


class FakeUploader(QObject):
    """Sends a file in 4 steps on the event loop; cancel() stops it like the real one."""
    finished = pyqtSignal(object, str)
    progress = pyqtSignal(object, object)
    started = []

    def __init__(self, parent=None):
        super().__init__(parent)
        self.error = None
        self._cancel = False

    def start(self, path, uploader=None):
        FakeUploader.started.append(path)
        self.path, self.size, self.step = path, 1000, 0
        QTimer.singleShot(0, self._tick)

    def cancel(self):
        self._cancel = True

    def _tick(self):
        if self._cancel:
            self.error = upload.Cancelled()
            self.finished.emit(None, str(self.error))
            return
        self.step += 1
        self.progress.emit(self.step * 250, self.size)
        if self.step == 4:
            self.finished.emit(upload.UploadResult("https://x/" + self.path), "")
        else:
            QTimer.singleShot(0, self._tick)


@pytest.fixture
def queue(monkeypatch, tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    FakeUploader.started = []
    monkeypatch.setattr(upload, "Uploader", FakeUploader)
    monkeypatch.setattr(transfers, "uploader_label", lambda name: "test")
    monkeypatch.setattr(transfers.settings, "get", lambda key: False)   # no Plasma job
    files = []
    for name in "abc":
        p = tmp_path / name
        p.write_bytes(b"x" * 1000)
        files.append(str(p))
    q = transfers.UploadQueue(bus=None)
    q.files = files
    q.batches = []
    q.batch_done.connect(lambda jobs, cancelled: q.batches.append((jobs, cancelled)))

    def run(until):
        for _ in range(1000):
            if until():
                return
            app.processEvents()
        raise AssertionError("timed out")
    q.run = run
    return q


def test_uploads_one_after_another_in_one_batch(queue):
    fractions = []
    queue.changed.connect(lambda: fractions.append(queue.fraction()))
    queue.add(queue.files[:2])
    assert queue.active
    queue.add(queue.files[2:])          # joins the running batch
    queue.run(lambda: queue.batches)
    jobs, cancelled = queue.batches[0]
    assert not cancelled and not queue.active
    assert [j.name for j in jobs] == ["a", "b", "c"]
    assert all(j.result.url.endswith(j.path) for j in jobs)
    assert FakeUploader.started == queue.files
    assert fractions == sorted(fractions) and fractions[-1] == 1.0


def test_cancel_drops_the_rest(queue):
    queue.add(queue.files)
    queue.run(lambda: queue.current and queue.current.sent > 0)
    assert "Uploading a (1/3)" in queue.describe()
    queue.cancel()
    queue.run(lambda: queue.batches)
    jobs, cancelled = queue.batches[0]
    assert cancelled
    assert all(isinstance(j.exception, upload.Cancelled) for j in jobs)
    assert FakeUploader.started == queue.files[:1]


def test_new_batch_after_the_last_one_ended(queue):
    queue.add(queue.files[:1])
    queue.run(lambda: queue.batches)
    queue.add(queue.files[1:2])
    queue.run(lambda: len(queue.batches) == 2)
    assert [j.name for j in queue.batches[1][0]] == ["b"]
