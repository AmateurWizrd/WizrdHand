"""Scan-code keyboard input: SendInput fields, chords, typing and stuck-key cleanup.

Games that read DirectInput or raw input ignore virtual-key events, so
scancode=True sends KEYEVENTF_SCANCODE input instead. SendInput is mocked
everywhere: no test here sends real input.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from windows_mcp.desktop import scancode
from windows_mcp.desktop import service
from windows_mcp.desktop.control_ledger import InputLedger
from windows_mcp.desktop.service import Desktop
from windows_mcp.uia import KeyboardEventFlag

SCANCODE = KeyboardEventFlag.KeyScanCode
EXTENDED = KeyboardEventFlag.ExtendedKey
KEYUP = KeyboardEventFlag.KeyUp

# A US layout: virtual key -> scan code, and character -> VkKeyScanW result.
_SCANS = {0x10: 0x2A, 0x11: 0x1D, 0x12: 0x38, 0x0D: 0x1C, 0x25: 0x4B, 0x24: 0x47, 0x41: 0x1E}
_SCANS |= {0x31: 0x02, 0xA3: 0x1D, 0x2E: 0x53}
_CHARS = {"a": 0x41, "A": 0x141, "1": 0x31, "!": 0x131, "\t": 0x09}


class FakeUser32:
    @staticmethod
    def MapVirtualKeyW(vk: int, map_type: int) -> int:
        assert map_type == 0
        return _SCANS.get(vk, 0)

    @staticmethod
    def VkKeyScanW(ch: str) -> int:
        return _CHARS.get(ch, -1)


class Recorder:
    """Stands in for uia.SendInput and keeps (scan, flags, vk, type) per event."""

    def __init__(self) -> None:
        self.events: list[tuple[int, int]] = []
        self.raw: list[object] = []

    def __call__(self, *inputs) -> int:
        for item in inputs:
            self.raw.append(item)
            self.events.append((item.union.ki.wScan, item.union.ki.dwFlags))
        return len(inputs)


@pytest.fixture
def owner():
    return SimpleNamespace(
        input_ledger=InputLedger(),
        checkpoint_current=MagicMock(),
        record_step_current=MagicMock(),
        physical_key_down=MagicMock(return_value=False),
    )


@pytest.fixture
def sent(monkeypatch, owner) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(scancode, "_user32", FakeUser32)
    monkeypatch.setattr(scancode.uia, "SendInput", recorder)
    monkeypatch.setattr(scancode, "get_controller", lambda: owner)
    monkeypatch.setattr(scancode.time, "sleep", lambda seconds: None)
    return recorder


def test_chord_sends_scan_codes_with_extended_flag_and_reverse_release(sent, owner):
    scancode.press_chord([0x11, 0x25])  # Ctrl+Left

    assert sent.events == [
        (0x1D, SCANCODE),
        (0x4B, SCANCODE | EXTENDED),
        (0x4B, SCANCODE | EXTENDED | KEYUP),
        (0x1D, SCANCODE | KEYUP),
    ]
    first = sent.raw[0]
    assert first.type == 1  # INPUT_KEYBOARD
    assert first.union.ki.wVk == 0  # The scan code alone identifies the key.
    assert owner.input_ledger.pending() == ()


@pytest.mark.parametrize("vk", [0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E])
def test_navigation_keys_are_extended(vk):
    assert vk in scancode.EXTENDED_VKS


@pytest.mark.parametrize("vk", [0xA3, 0xA5])
def test_right_ctrl_and_alt_are_extended(vk):
    assert vk in scancode.EXTENDED_VKS


def test_left_modifiers_and_letters_are_not_extended():
    assert not {0x10, 0x11, 0x12, 0xA0, 0xA2, 0xA4, 0x41} & scancode.EXTENDED_VKS


def test_typing_holds_shift_for_shifted_characters(sent):
    scancode.type_text("aA!")

    assert sent.events == [
        (0x1E, SCANCODE),
        (0x1E, SCANCODE | KEYUP),
        (0x2A, SCANCODE),
        (0x1E, SCANCODE),
        (0x1E, SCANCODE | KEYUP),
        (0x2A, SCANCODE | KEYUP),
        (0x2A, SCANCODE),
        (0x02, SCANCODE),
        (0x02, SCANCODE | KEYUP),
        (0x2A, SCANCODE | KEYUP),
    ]


def test_newline_types_enter_and_carriage_return_is_skipped(sent):
    scancode.type_text("\r\n")

    assert sent.events == [(0x1C, SCANCODE), (0x1C, SCANCODE | KEYUP)]


def test_untypable_text_sends_nothing(sent):
    with pytest.raises(ValueError, match="no key"):
        scancode.type_text("a\u00e9")

    assert sent.events == []


def test_every_down_is_held_in_the_ledger_until_its_up(sent, owner, monkeypatch):
    pending_during_sends: list[tuple[str, ...]] = []

    def spy(*inputs):
        pending_during_sends.append(owner.input_ledger.pending())
        return sent(*inputs)

    monkeypatch.setattr(scancode.uia, "SendInput", spy)
    scancode.press_chord([0x10, 0x41])

    # The ledger records each down before injecting it, and drops it after the up.
    assert pending_during_sends == [
        ("key:scan:0x10",),
        ("key:scan:0x10", "key:scan:0x41"),
        ("key:scan:0x10", "key:scan:0x41"),
        ("key:scan:0x10",),
    ]
    assert owner.input_ledger.pending() == ()


class Preempted(Exception):
    pass


def test_takeover_during_hold_releases_each_key_once(sent, owner, monkeypatch):
    clock = iter(range(100))
    monkeypatch.setattr(scancode.time, "monotonic", lambda: next(clock) * 0.1)
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 4:  # Two key downs, one hold tick, then the user takes over.
            owner.input_ledger.block_new()
            owner.input_ledger.release_all()  # What the coordinator's watchdog does.
            raise Preempted

    owner.checkpoint_current.side_effect = checkpoint
    with pytest.raises(Preempted):
        scancode.press_chord([0x11, 0x41], hold=5.0)

    ups = [event for event in sent.events if event[1] & KEYUP]
    assert sorted(ups) == [(0x1D, SCANCODE | KEYUP), (0x1E, SCANCODE | KEYUP)]
    assert owner.input_ledger.pending() == ()


def test_physically_held_key_is_not_released_by_the_ai(sent, owner):
    owner.physical_key_down.side_effect = lambda vk: vk == 0x41
    scancode.press_chord([0x41])

    assert sent.events == [(0x1E, SCANCODE)]  # The user's own up will clear it.
    assert owner.input_ledger.pending() == ()


def test_blocked_ledger_sends_nothing(sent, owner):
    owner.input_ledger.block_new()
    with pytest.raises(Exception, match="unavailable"):
        scancode.press_chord([0x41])
    assert sent.events == []


@pytest.mark.parametrize("hold", [-1, 10.5, float("nan"), True])
def test_hold_out_of_range_is_rejected(sent, hold):
    with pytest.raises(ValueError, match="hold"):
        scancode.press_chord([0x41], hold=hold)
    assert sent.events == []


def test_unmappable_key_in_chord_sends_nothing(sent):
    with pytest.raises(ValueError, match="no scan code"):
        scancode.press_chord([0x11, 0x99])
    assert sent.events == []


def test_blocked_send_input_raises(sent, monkeypatch):
    monkeypatch.setattr(scancode.uia, "SendInput", lambda *inputs: 0)
    with pytest.raises(OSError, match="blocked"):
        scancode.press_chord([0x41])


@pytest.mark.parametrize(
    ("name", "vk"), [("Left", 0x25), ("ENTER", 0x0D), ("ctrl", 0x11), ("A", 0x41), ("a", 0x41)]
)
def test_vk_for_name(sent, name, vk):
    assert scancode.vk_for_name(name) == vk


def test_vk_for_unknown_name(sent):
    with pytest.raises(ValueError, match="Unknown key"):
        scancode.vk_for_name("NotAKey")


# Desktop.shortcut / Desktop.type routing


def _desktop(monkeypatch, owner):
    monkeypatch.setattr(service, "get_controller", lambda: owner)
    send_keys = MagicMock()
    monkeypatch.setattr(service.uia, "SendKeys", send_keys)
    return Desktop.__new__(Desktop), send_keys


def test_shortcut_scancode_resolves_aliases_and_skips_send_keys(sent, owner, monkeypatch):
    desktop, send_keys = _desktop(monkeypatch, owner)
    desktop.shortcut("ctrl+Home", scancode=True)

    send_keys.assert_not_called()
    assert sent.events == [
        (0x1D, SCANCODE),
        (0x47, SCANCODE | EXTENDED),
        (0x47, SCANCODE | EXTENDED | KEYUP),
        (0x1D, SCANCODE | KEYUP),
    ]


def test_shortcut_default_path_unchanged(sent, owner, monkeypatch):
    desktop, send_keys = _desktop(monkeypatch, owner)
    desktop.shortcut("ctrl+c")

    send_keys.assert_called_once_with("{ctrl}c", interval=0.01)
    assert sent.events == []


def test_shortcut_hold_requires_scancode(sent, owner, monkeypatch):
    desktop, send_keys = _desktop(monkeypatch, owner)
    with pytest.raises(ValueError, match="scancode"):
        desktop.shortcut("right", hold=1.0)
    send_keys.assert_not_called()


def test_type_scancode_types_keys_and_presses_enter(sent, owner, monkeypatch):
    desktop, send_keys = _desktop(monkeypatch, owner)
    monkeypatch.setattr(desktop, "_validate_screen_point", lambda x, y: None, raising=False)
    monkeypatch.setattr(desktop, "_read_back", lambda *args: "", raising=False)
    click = MagicMock()
    monkeypatch.setattr(service.uia, "Click", click)

    desktop.type((10, 20), "a", press_enter=True, scancode=True)

    click.assert_called_once_with(10, 20)
    send_keys.assert_not_called()
    assert sent.events == [
        (0x1E, SCANCODE),
        (0x1E, SCANCODE | KEYUP),
        (0x1C, SCANCODE),
        (0x1C, SCANCODE | KEYUP),
    ]


def test_type_scancode_rejects_untypable_text_before_clicking(sent, owner, monkeypatch):
    desktop, _ = _desktop(monkeypatch, owner)
    click = MagicMock()
    monkeypatch.setattr(service.uia, "Click", click)

    with pytest.raises(ValueError):
        desktop.type((10, 20), "\u00e9", scancode=True)
    click.assert_not_called()


def test_shortcut_tool_passes_scancode_and_hold():
    import asyncio

    from windows_mcp.tools.input import register

    tools: dict = {}
    mcp = SimpleNamespace(tool=lambda **kw: lambda func: tools.setdefault(kw["name"], func))
    fake = MagicMock()
    register(mcp, get_desktop=lambda: fake, get_analytics=lambda: None)

    answer = asyncio.run(tools["Shortcut"](shortcut="right", scancode="true", hold=0.5))

    fake.shortcut.assert_called_once_with("right", scancode=True, hold=0.5)
    assert answer == "Pressed right as scan codes and held it for 0.5 seconds."
