"""Fixed-size app windows (games, launchers) stay in the window list.

They have a minimize box but no maximize box. Requiring both hid them, so App
switch answered "not found" for a game that was on screen. All Win32 and UIA
calls are fakes.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.test_window_z_order import SERVICE, FakeWindow
from windows_mcp.desktop.service import Desktop

BOXES = {
    0x10: ("MapleStory", True, False),  # Fixed-size game: minimize box only.
    0x20: ("Notepad", True, True),
    0x30: ("Kiosk", False, True),
    0x40: ("Find", False, False),  # A dialog: neither box.
}


class BoxWindow(FakeWindow):
    def GetPattern(self, pattern_id):
        _, can_minimize, can_maximize = BOXES[self._fake_handle]
        return SimpleNamespace(CanMinimize=can_minimize, CanMaximize=can_maximize)


@pytest.fixture
def desktop():
    controls = {handle: BoxWindow(handle, name) for handle, (name, _, _) in BOXES.items()}
    with (
        patch.object(Desktop, "__init__", lambda self: None),
        patch(f"{SERVICE}._excluded_process_names", return_value=set()),
        patch(f"{SERVICE}.uia.ControlFromHandle", side_effect=controls.__getitem__),
        patch(f"{SERVICE}.uia.IsIconic", return_value=False),
        patch(f"{SERVICE}.uia.IsZoomed", return_value=False),
        patch(f"{SERVICE}.uia.IsWindowVisible", return_value=True),
        patch.object(Desktop, "is_window_browser", return_value=False),
    ):
        yield Desktop()


def test_windows_with_either_box_are_listed(desktop):
    windows, handles = desktop.get_windows(controls_handles=list(BOXES))

    assert [window.name for window in windows] == ["MapleStory", "Notepad", "Kiosk"]
    assert handles == {0x10, 0x20, 0x30}


def test_switch_finds_a_fixed_size_game(desktop):
    desktop.desktop_state = None
    windows, _ = desktop.get_windows(controls_handles=list(BOXES))
    with patch.object(
        Desktop,
        "get_state",
        lambda self, **kw: setattr(
            self, "desktop_state", SimpleNamespace(active_window=None, windows=windows)
        ),
    ):
        window, error = desktop._find_window_by_name("MapleStory")

    assert error == ""
    assert window.handle == 0x10
