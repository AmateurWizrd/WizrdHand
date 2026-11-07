"""Pictures of one window's client area through Windows Graphics Capture (WGC).

WGC reads the window's own surface, so the picture is right when other windows
cover it or it sits on another monitor, and no screen overlay ends up in it. It
needs the optional ``windows-capture`` package (``windows-mcp[wgc]``). One
capture session is kept per window and closed after it sits unused.
"""

import atexit
import ctypes
import logging
import threading
import time
from ctypes import wintypes

import win32gui
from PIL import Image

import windows_mcp.uia  # noqa: F401  # Per-monitor DPI awareness: WGC frames are physical pixels.

logger = logging.getLogger(__name__)

_dwmapi = ctypes.WinDLL("dwmapi")
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
FIRST_FRAME_TIMEOUT = 3.0
IDLE_CLOSE_SECONDS = 30.0
MAX_SESSIONS = 4

Rect = tuple[int, int, int, int]  # left, top, width, height in screen pixels


def resolve_window(handle: int | None = None, title: str | None = None) -> tuple[int, list[int]]:
    """The top-level window given by handle and/or exact title, and other windows so titled.

    When several visible windows share the title (a dialog, a tool window), the
    largest one that is not minimized wins; the rest are returned so the caller can
    name them.
    """
    if handle is None and not title:
        raise ValueError("Give window_handle or window_title")
    others: list[int] = []
    if title:
        matches: list[int] = []

        def collect(hwnd: int, _: object) -> bool:
            if win32gui.GetWindowText(hwnd) == title:
                matches.append(hwnd)
            return True

        win32gui.EnumWindows(collect, None)
        if not matches:
            raise ValueError(f"No top-level window is titled exactly {title!r}")
        if handle is not None and handle not in matches:
            raise ValueError(f"Window {handle} is not titled {title!r}")
        visible = [hwnd for hwnd in matches if win32gui.IsWindowVisible(hwnd)]
        if handle is None:
            candidates = visible or matches
            handle = max(candidates, key=_pick_rank)
            others = [hwnd for hwnd in candidates if hwnd != handle]
    if isinstance(handle, bool) or not isinstance(handle, int) or not win32gui.IsWindow(handle):
        raise ValueError(f"{handle!r} is not a window handle")
    return handle, others


def _pick_rank(hwnd: int) -> tuple[bool, int]:
    """Not minimized first, then the larger client area."""
    try:
        _, _, width, height = win32gui.GetClientRect(hwnd)
    except Exception:
        width = height = 0
    return not win32gui.IsIconic(hwnd), width * height


def client_rect(hwnd: int) -> Rect:
    """The window's client area on screen."""
    _, _, width, height = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (0, 0))
    return left, top, width, height


def frame_origin(hwnd: int) -> tuple[int, int]:
    """Top-left of the visible window frame, which is where a WGC frame starts."""
    rect = wintypes.RECT()
    result = _dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        wintypes.DWORD(_DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if result:
        raise OSError(f"DwmGetWindowAttribute failed: {result:#x}")
    return rect.left, rect.top


def crop_box(
    origin: tuple[int, int], client: Rect, frame_size: tuple[int, int]
) -> tuple[int, int, int, int]:
    """Where the client area lies in a WGC frame, as a PIL (left, top, right, bottom) box."""
    left, top, width, height = client
    x, y = left - origin[0], top - origin[1]
    frame_width, frame_height = frame_size
    if width <= 0 or height <= 0:
        raise RuntimeError("The window has no client area (minimized?)")
    if x < 0 or y < 0 or x + width > frame_width or y + height > frame_height:
        raise RuntimeError(
            f"Client area {width}x{height} at ({x},{y}) is outside the "
            f"{frame_width}x{frame_height} frame"
        )
    return x, y, x + width, y + height


class WindowCapture:
    """A WGC session for one window; frames arrive on the library's thread."""

    def __init__(self, hwnd: int) -> None:
        from windows_capture import WindowsCapture  # Optional dependency.

        self.hwnd = hwnd
        self.closed = False
        self.last_used = time.monotonic()
        self._frame = None  # BGRA numpy array of the whole window.
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        capture = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd)

        @capture.event
        def on_frame_arrived(frame, control) -> None:
            pixels = frame.frame_buffer.copy()  # The library reuses its buffer.
            with self._lock:
                self._frame = pixels
            self._arrived.set()

        @capture.event
        def on_closed() -> None:
            self.closed = True
            self._arrived.set()

        self._control = capture.start_free_threaded()
        if not self._arrived.wait(FIRST_FRAME_TIMEOUT) or self._frame is None:
            self.close()
            raise RuntimeError(f"No frame within {FIRST_FRAME_TIMEOUT:g}s (window minimized?)")

    def grab(self) -> tuple[Image.Image, Rect]:
        """The newest frame cropped to the client area, and that area's screen rect."""
        self.last_used = time.monotonic()
        with self._lock:
            frame = self._frame
        if frame is None or self.closed:
            raise RuntimeError("The capture session is closed")
        client = client_rect(self.hwnd)
        height, width = frame.shape[:2]
        box = crop_box(frame_origin(self.hwnd), client, (width, height))
        rgb = Image.frombuffer("RGBA", (width, height), frame, "raw", "BGRA", 0, 1)
        return rgb.crop(box).convert("RGB"), client

    def close(self) -> None:
        self.closed = True
        try:
            self._control.stop()
        except Exception:
            logger.debug("Stopping the capture of window %s failed", self.hwnd, exc_info=True)


_sessions: dict[int, WindowCapture] = {}
_sessions_lock = threading.Lock()
_reaper: threading.Thread | None = None


def _reap_idle() -> None:
    while True:
        time.sleep(IDLE_CLOSE_SECONDS / 3)
        cutoff = time.monotonic() - IDLE_CLOSE_SECONDS
        with _sessions_lock:
            idle = [hwnd for hwnd, s in _sessions.items() if s.closed or s.last_used < cutoff]
            stale = [_sessions.pop(hwnd) for hwnd in idle]
        for session in stale:
            session.close()


def session(hwnd: int) -> WindowCapture:
    """The cached capture session for hwnd, started on first use."""
    global _reaper
    with _sessions_lock:
        current = _sessions.get(hwnd)
        if current is not None and not current.closed:
            current.last_used = time.monotonic()
            return current
        stale = _sessions.get(hwnd)  # Closed; the reaper drops it if the restart fails.
        # Start the replacement before evicting anything: if it fails, every
        # session stays reachable by the reaper and close_all.
        created = WindowCapture(hwnd)
        _sessions[hwnd] = created
        evicted = [stale] if stale is not None else []
        while len(_sessions) > MAX_SESSIONS:
            others = [key for key in _sessions if key != hwnd]
            oldest = min(others, key=lambda key: _sessions[key].last_used)
            evicted.append(_sessions.pop(oldest))
        if _reaper is None:
            _reaper = threading.Thread(target=_reap_idle, name="wgc-reaper", daemon=True)
            _reaper.start()
    for old in evicted:
        old.close()
    return created


def capture(hwnd: int) -> tuple[Image.Image, Rect]:
    """The client area of hwnd through WGC and its screen rect."""
    return session(hwnd).grab()


@atexit.register
def close_all() -> None:
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for open_session in sessions:
        open_session.close()
