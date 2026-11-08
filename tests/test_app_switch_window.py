"""App mode=switch by window_handle/window_title: exact lookup, restore, verified foreground.

No real window is touched: focus calls and the foreground HWND are fakes.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from windows_mcp.desktop import service
from windows_mcp.desktop.service import Desktop
from windows_mcp.tools import app
from windows_mcp.tools.control_policy import requires_control

GAME = 786506


@pytest.fixture
def desktop(monkeypatch):
    desktop = Desktop.__new__(Desktop)
    desktop.bring_window_to_top = Mock()
    monkeypatch.setattr(service.win32gui, "GetWindowText", lambda hwnd: "MapleStory")
    monkeypatch.setattr(service.uia, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(service.win32gui, "GetForegroundWindow", lambda: GAME)
    clock = {"elapsed": 0.0}
    monkeypatch.setattr(service, "perf_counter", lambda: clock["elapsed"])
    monkeypatch.setattr(service, "sleep", lambda s: clock.update(elapsed=clock["elapsed"] + s))
    return desktop


@pytest.mark.parametrize("minimized", [False, True])
def test_switch_window_brings_it_forward_and_verifies(desktop, monkeypatch, minimized):
    monkeypatch.setattr(service.uia, "IsIconic", lambda hwnd: minimized)

    answer = desktop.switch_window(GAME)

    desktop.bring_window_to_top.assert_called_once_with(GAME)  # Restore + AttachThreadInput.
    assert answer == (
        "Restored Maplestory from minimized and switched to it."
        if minimized
        else "Switched to Maplestory window."
    )


def test_switch_window_fails_when_the_window_stays_behind(desktop, monkeypatch):
    monkeypatch.setattr(service.win32gui, "GetForegroundWindow", lambda: 999)
    with pytest.raises(ValueError, match="Failed to bring MapleStory to the foreground"):
        desktop.switch_window(GAME)


def _app_tool(fake_desktop):
    tools: dict = {}
    mcp = SimpleNamespace(tool=lambda **kw: lambda func: tools.setdefault(kw["name"], func))
    app.register(mcp, get_desktop=lambda: fake_desktop, get_analytics=lambda: None)
    return tools["App"]


def test_app_switch_by_title_uses_the_exact_lookup(monkeypatch):
    fake = MagicMock()
    fake.switch_window.return_value = "Switched to Maplestory window."
    lookup = Mock(return_value=(GAME, [4242]))
    monkeypatch.setattr(app, "resolve_window", lookup)

    answer = asyncio.run(_app_tool(fake)(mode="switch", window_title="MapleStory"))

    lookup.assert_called_once_with(None, "MapleStory")
    fake.switch_window.assert_called_once_with(GAME)
    fake.app.assert_not_called()  # The fuzzy-name path is not used.
    assert answer == (
        "Switched to Maplestory window. Other windows with the same title "
        "(pass window_handle): 4242."
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "resize", "window_handle": GAME},
        {"mode": "launch", "window_title": "MapleStory"},
        {"mode": "launch_executable", "executable": "x.exe", "window_handle": GAME},
        {"mode": "switch", "name": "Maple", "window_handle": GAME},
    ],
)
def test_window_arguments_are_rejected_outside_a_plain_switch(kwargs):
    fake = MagicMock()
    with pytest.raises(ValueError):
        asyncio.run(_app_tool(fake)(**kwargs))
    fake.switch_window.assert_not_called()
    fake.app.assert_not_called()


@pytest.mark.parametrize("policy", ["relaxed", "balanced", "strict"])
def test_switch_by_window_goes_through_the_control_gate(policy):
    arguments = {"mode": "switch", "window_handle": GAME, "window_title": "MapleStory"}
    assert requires_control(policy, "App", arguments)
