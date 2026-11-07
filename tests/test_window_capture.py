"""Window capture through Windows Graphics Capture: crop math, session cache, fallback.

No real capture session is started: WGC frames are fake numpy arrays.
"""

from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from windows_mcp.desktop import service, window_capture
from windows_mcp.desktop.service import Desktop
from windows_mcp.tools._snapshot_helpers import build_snapshot_response
from windows_mcp.tree.views import BoundingBox
from windows_mcp.uia import Rect


def test_crop_box_cuts_the_client_area_out_of_the_frame():
    # Frame starts at the visible window edge; client sits under an 8px border and title bar.
    assert window_capture.crop_box((100, 50), (108, 81, 640, 480), (656, 539)) == (
        8,
        31,
        648,
        511,
    )


def test_crop_box_for_a_borderless_window_is_the_whole_frame():
    assert window_capture.crop_box((-1920, 0), (-1920, 0, 1920, 1080), (1920, 1080)) == (
        0,
        0,
        1920,
        1080,
    )


@pytest.mark.parametrize("client", [(90, 81, 640, 480), (108, 81, 649, 480), (108, 81, 640, 520)])
def test_crop_box_outside_the_frame_raises(client):
    with pytest.raises(RuntimeError, match="outside"):
        window_capture.crop_box((100, 50), client, (656, 539))


def test_crop_box_without_client_area_raises():
    with pytest.raises(RuntimeError, match="no client area"):
        window_capture.crop_box((0, 0), (0, 0, 0, 0), (160, 28))


def _fake_session(frame: np.ndarray) -> window_capture.WindowCapture:
    session = window_capture.WindowCapture.__new__(window_capture.WindowCapture)
    session.hwnd = 42
    session.closed = False
    session.last_used = 0.0
    session._frame = frame
    session._lock = MagicMock()
    return session


def test_grab_crops_and_converts_bgra_to_rgb(monkeypatch):
    frame = np.zeros((40, 60, 4), dtype=np.uint8)
    frame[:, :] = (0, 0, 0, 255)  # Border: black.
    frame[10:30, 5:55] = (30, 20, 10, 255)  # Client area, BGRA: RGB (10, 20, 30).
    monkeypatch.setattr(window_capture, "frame_origin", lambda hwnd: (200, 100))
    monkeypatch.setattr(window_capture, "client_rect", lambda hwnd: (205, 110, 50, 20))

    image, rect = _fake_session(frame).grab()

    assert rect == (205, 110, 50, 20)
    assert image.mode == "RGB"
    assert image.size == (50, 20)
    assert image.getpixel((0, 0)) == (10, 20, 30)
    assert image.getpixel((49, 19)) == (10, 20, 30)


def test_grab_on_closed_session_raises():
    session = _fake_session(np.zeros((4, 4, 4), dtype=np.uint8))
    session.closed = True
    with pytest.raises(RuntimeError, match="closed"):
        session.grab()


class FakeCapture:
    created: list["FakeCapture"] = []

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.closed = False
        self.last_used = float(len(FakeCapture.created))
        FakeCapture.created.append(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_sessions(monkeypatch):
    FakeCapture.created = []
    monkeypatch.setattr(window_capture, "WindowCapture", FakeCapture)
    monkeypatch.setattr(window_capture, "_sessions", {})
    monkeypatch.setattr(window_capture, "_reaper", object())  # No reaper thread in tests.
    return FakeCapture.created


def test_session_is_cached_per_window(fake_sessions):
    first = window_capture.session(1)
    assert window_capture.session(1) is first
    assert window_capture.session(2) is not first
    assert len(fake_sessions) == 2


def test_closed_session_is_replaced(fake_sessions):
    first = window_capture.session(1)
    first.closed = True  # The window went away or the capture stopped.
    assert window_capture.session(1) is not first


def test_oldest_session_is_closed_past_the_limit(fake_sessions, monkeypatch):
    monkeypatch.setattr(window_capture.time, "monotonic", lambda: 0.0)
    for hwnd in range(window_capture.MAX_SESSIONS):
        window_capture.session(hwnd)
    fake_sessions[0].last_used = -1.0
    window_capture.session(99)

    assert fake_sessions[0].closed
    assert not any(s.closed for s in fake_sessions[1:])
    assert len(window_capture._sessions) == window_capture.MAX_SESSIONS


def test_close_all_closes_every_session(fake_sessions):
    window_capture.session(1)
    window_capture.session(2)
    window_capture.close_all()
    assert all(s.closed for s in fake_sessions)
    assert window_capture._sessions == {}


def test_resolve_window_by_exact_title(monkeypatch):
    titles = {10: "Game", 11: "game", 12: "Game"}
    monkeypatch.setattr(
        window_capture.win32gui,
        "EnumWindows",
        lambda callback, extra: [callback(hwnd, extra) for hwnd in titles],
    )
    monkeypatch.setattr(window_capture.win32gui, "GetWindowText", titles.get)
    monkeypatch.setattr(window_capture.win32gui, "IsWindowVisible", lambda hwnd: hwnd == 12)
    monkeypatch.setattr(window_capture.win32gui, "IsWindow", lambda hwnd: True)

    assert window_capture.resolve_window(title="Game") == 12  # Visible match first.
    assert window_capture.resolve_window(10, "Game") == 10
    with pytest.raises(ValueError, match="not titled"):
        window_capture.resolve_window(11, "Game")
    with pytest.raises(ValueError, match="No top-level window"):
        window_capture.resolve_window(title="Nope")


def test_resolve_window_rejects_bad_handles(monkeypatch):
    monkeypatch.setattr(window_capture.win32gui, "IsWindow", lambda hwnd: False)
    with pytest.raises(ValueError, match="not a window"):
        window_capture.resolve_window(1234)
    with pytest.raises(ValueError, match="window_handle or window_title"):
        window_capture.resolve_window()


def test_window_screenshot_uses_wgc(monkeypatch):
    desktop = Desktop.__new__(Desktop)
    image = Image.new("RGB", (50, 20))
    monkeypatch.setattr(window_capture, "capture", lambda hwnd: (image, (0, 0, 50, 20)))
    desktop.get_screenshot = MagicMock()

    assert desktop.get_window_screenshot(42, Rect(0, 0, 50, 20)) is image
    assert desktop._last_screenshot_backend == "wgc"
    desktop.get_screenshot.assert_not_called()


def test_window_screenshot_falls_back_to_a_screen_crop(monkeypatch):
    desktop = Desktop.__new__(Desktop)
    crop = Image.new("RGB", (50, 20))

    def failing(hwnd):
        raise RuntimeError("No frame within 3s (window minimized?)")

    def screen(capture_rect):
        desktop._last_screenshot_backend = "dxcam"
        return crop

    monkeypatch.setattr(window_capture, "capture", failing)
    desktop.get_screenshot = MagicMock(side_effect=screen)
    rect = Rect(5, 6, 55, 26)

    assert desktop.get_window_screenshot(42, rect) is crop
    desktop.get_screenshot.assert_called_once_with(capture_rect=rect)
    assert desktop._last_screenshot_backend.startswith("dxcam screen crop (WGC failed: No frame")


def test_get_state_captures_the_window_client_area(monkeypatch):
    desktop = Desktop.__new__(Desktop)
    desktop.desktop_state = None
    desktop.tree = MagicMock()
    desktop.get_displays = MagicMock(return_value=[])
    desktop.get_cursor_location = MagicMock(return_value=(0, 0))
    desktop.parse_region_selection = MagicMock()
    desktop.get_window_screenshot = MagicMock(return_value=Image.new("RGB", (640, 480)))
    monkeypatch.setattr(window_capture, "client_rect", lambda hwnd: (-1500, 40, 640, 480))
    monkeypatch.setattr(service.uia, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(service, "get_current_desktop", lambda: {"id": "1", "name": "D"})
    monkeypatch.setattr(service, "get_all_desktops", lambda: [{"id": "1", "name": "D"}])

    state = desktop.get_state(
        use_vision=True, use_annotation=False, use_ui_tree=False, region=[0, 0, 1, 1], window=7
    )

    desktop.parse_region_selection.assert_not_called()  # The window replaces region.
    desktop.get_window_screenshot.assert_called_once_with(7, Rect(-1500, 40, -860, 520))
    assert state.screenshot_region.xyxy_to_string() == "(-1500,40,-860,520)"


def test_get_state_rejects_a_minimized_window(monkeypatch):
    desktop = Desktop.__new__(Desktop)
    desktop.get_displays = MagicMock(return_value=[])
    monkeypatch.setattr(window_capture, "client_rect", lambda hwnd: (0, 0, 0, 0))
    monkeypatch.setattr(service.uia, "IsIconic", lambda hwnd: True)

    with pytest.raises(ValueError, match="minimized"):
        desktop.get_state(use_vision=True, use_ui_tree=False, window=7)


def test_response_reports_client_origin_and_size(sample_desktop_state):
    sample_desktop_state.screenshot_region = BoundingBox(
        left=-1500, top=40, right=-860, bottom=520, width=640, height=480
    )
    result = {
        "desktop_state": sample_desktop_state,
        "window": 7,
        "use_ui_tree": False,
        "screenshot_bytes": None,
        **{
            key: ""
            for key in (
                "interactive_elements",
                "scrollable_elements",
                "semantic_tree",
                "windows",
                "active_window",
                "active_desktop",
                "all_desktops",
            )
        },
    }

    text = build_snapshot_response(result, include_ui_details=False)[0]

    assert "Window Client Area: handle 7, origin (-1500,40), size 640x480." in text
