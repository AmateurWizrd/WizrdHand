"""Background recording of one window: JPEG frames through WGC plus a key-event log.

Frames are full-size client-area JPEGs in ``frames/``, named by seconds since the
start (``frames/0012.345.jpg``). ``events.jsonl`` gets one line per key event
(``"kind": "key"``) from the coordinator's keyboard hook. The hook is global: it
logs keys typed into every window, not only the recorded one. ``source`` is
``"user"`` for physical keys, ``"agent"`` for keys this server injected (tagged
through dwExtraInfo) and ``"injected"`` for keys other programs injected. Taking
over the desktop does not stop a recording. Only one recording runs at a time.
"""

import atexit
import collections
import io
import json
import math
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from windows_mcp.desktop import window_capture
from windows_mcp.desktop.control import get_controller

MAX_FPS = 30.0
MAX_SECONDS = 600.0
MAX_BYTES = 2 * 1024**3
JPEG_QUALITY = 85

Grab = Callable[[int], tuple[Image.Image, tuple[int, int, int, int]]]


class Recording:
    def __init__(
        self,
        hwnd: int,
        fps: float,
        seconds: float,
        directory: Path,
        grab: Grab = window_capture.capture,
    ) -> None:
        self.hwnd = hwnd
        self.fps = fps
        self.seconds = seconds
        self.directory = directory
        self.frames = 0
        self.dropped = 0
        self.bytes = 0
        self.key_events = 0
        self.size: tuple[int, int] | None = None
        self.client_rect: tuple[int, int, int, int] | None = None
        self.stopped_by: str | None = None
        self.error: str | None = None
        self._grab = grab
        self._keys: collections.deque = collections.deque(maxlen=100_000)
        self._stop = threading.Event()
        self._started = time.monotonic()
        self._ended: float | None = None
        self._owner = get_controller()
        self._log_full = False
        (directory / "frames").mkdir(exist_ok=True)
        self._owner.key_observers.append(self._on_key)
        self._thread = threading.Thread(target=self._run, name="window-recorder", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._unregister()
            raise

    def _on_key(self, vk: int, scan: int, down: bool, source: str) -> None:
        self._keys.append((time.monotonic(), vk, scan, down, source))  # Hook thread: no I/O.

    def _unregister(self) -> None:
        try:
            self._owner.key_observers.remove(self._on_key)
        except ValueError:
            pass  # Already removed.

    def _write_keys(self, log) -> None:
        while self._keys and not self._log_full:
            stamp, vk, scan, down, source = self._keys.popleft()
            event = {"kind": "key", "t": round(stamp - self._started, 4), "source": source}
            line = json.dumps(event | {"vk": vk, "scan": scan, "down": down}) + "\n"
            size = len(line.encode("utf-8"))
            if self.bytes + size > MAX_BYTES:
                self._log_full = True
                return
            log.write(line)
            self.bytes += size
            self.key_events += 1

    def _run(self) -> None:
        try:
            log_path = self.directory / "events.jsonl"
            with open(log_path, "a", encoding="utf-8", newline="\n") as log:
                try:
                    self._capture(log)
                finally:
                    self._unregister()
                    self._write_keys(log)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.stopped_by = self.stopped_by or "error"
        finally:
            self._unregister()  # Also when the log could not even be opened.
            self._ended = time.monotonic()

    def _capture(self, log) -> None:
        deadline = self._started + self.seconds
        last_index = math.ceil(self.seconds * self.fps)
        index = 0
        while True:
            if self._stop.wait(max(0.0, self._started + index / self.fps - time.monotonic())):
                self.stopped_by = "stop"
                return
            if index >= last_index or time.monotonic() >= deadline:
                self.stopped_by = "duration"
                return
            image, self.client_rect = self._grab(self.hwnd)
            elapsed = time.monotonic() - self._started
            encoded = io.BytesIO()
            image.save(encoded, "JPEG", quality=JPEG_QUALITY)
            data = encoded.getvalue()
            if self.bytes + len(data) > MAX_BYTES:
                self.stopped_by = "size limit"
                return
            (self.directory / "frames" / f"{elapsed:08.3f}.jpg").write_bytes(data)
            self.size = image.size
            self.frames += 1
            self.bytes += len(data)
            self._write_keys(log)
            if self._log_full:
                self.stopped_by = "size limit"
                return
            # Schedule after all per-frame work: a slow frame skips the frames it
            # missed instead of bunching them up.
            done = time.monotonic() - self._started
            next_index = min(max(index + 1, math.floor(done * self.fps) + 1), last_index)
            self.dropped += next_index - index - 1
            index = next_index

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def status(self) -> dict:
        end = self._ended if self._ended is not None else time.monotonic()
        return {
            "directory": str(self.directory),
            "frames_directory": str(self.directory / "frames"),
            "window": self.hwnd,
            "running": self.running,
            "elapsed_seconds": round(end - self._started, 3),
            "fps": self.fps,
            "seconds": self.seconds,
            "frames": self.frames,
            "dropped_frames": self.dropped,
            "bytes": self.bytes,
            "frame_size": list(self.size) if self.size else None,
            "client_rect": list(self.client_rect) if self.client_rect else None,
            "key_events": self.key_events,
            # Key events come from the control coordinator's hook thread; off means none.
            "key_hooks_active": bool(self._owner._thread and self._owner._thread.is_alive()),
            "stopped_by": self.stopped_by,
            "error": self.error,
        }


_current: Recording | None = None
_lock = threading.Lock()


def _directory(path: str | None) -> Path:
    if not path:
        return Path(tempfile.mkdtemp(prefix="windows-mcp-record-"))
    directory = Path(path)
    if not directory.is_absolute():
        raise ValueError("directory must be an absolute path")
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError(f"directory {directory} is not empty")
    return directory


def start(
    hwnd: int,
    fps: float = 15.0,
    seconds: float = 10.0,
    directory: str | None = None,
    grab: Grab = window_capture.capture,
) -> Recording:
    """Start recording hwnd in the background; raises if a recording is running."""
    global _current
    for value, name, limit in ((fps, "fps", MAX_FPS), (seconds, "seconds", MAX_SECONDS)):
        if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= limit:
            raise ValueError(f"{name} must be above 0 and at most {limit:g}")
    with _lock:
        if _current is not None and _current.running:
            raise RuntimeError(f"A recording is already running into {_current.directory}")
        grab(hwnd)  # Fail now, not in the background, if the window can't be captured.
        _current = Recording(hwnd, fps, seconds, _directory(directory), grab)
        return _current


def status() -> dict | None:
    """The current or last recording's status, or None if nothing was recorded."""
    with _lock:
        return _current.status() if _current is not None else None


def stop() -> dict | None:
    """Stop the running recording early and return its final status."""
    with _lock:
        if _current is None:
            return None
        _current.stop()
        return _current.status()


atexit.register(stop)  # Flush the key log; registered after window_capture's close_all.
