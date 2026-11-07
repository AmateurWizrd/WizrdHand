"""Scan-code keyboard input: SendInput fields, chords, typing and stuck-key cleanup.

Games that read DirectInput or raw input ignore virtual-key events, so
scancode=True sends KEYEVENTF_SCANCODE input instead. SendInput is mocked
everywhere: no test here sends real input.
"""

import asyncio
import ctypes
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from windows_mcp.desktop import control, scancode, service
from windows_mcp.desktop.service import Desktop
from windows_mcp.uia import KeyboardEventFlag

SCANCODE = KeyboardEventFlag.KeyScanCode
EXTENDED = KeyboardEventFlag.ExtendedKey
KEYUP = KeyboardEventFlag.KeyUp
US, FR = 0x0409, 0x040C

# MapVirtualKeyExW(vk, MAPVK_VK_TO_VSC_EX): 0xE0 in the high byte marks an extended key.
_US_SCANS = {0x10: 0x2A, 0x11: 0x1D, 0x12: 0x38, 0x0D: 0x1C, 0x25: 0xE04B, 0x24: 0xE047}
_US_SCANS |= {0x41: 0x1E, 0x51: 0x10, 0x31: 0x02, 0xA2: 0x1D, 0xA3: 0xE01D, 0x2E: 0xE053}
_US_SCANS |= {0x45: 0x12, 0x32: 0x03}
_US_SCANS |= {0xAD: 0xE020, 0x13: 0xE11D}  # Volume Mute, Pause (E1: no usable encoding).
_FR_SCANS = _US_SCANS | {0x41: 0x10, 0x51: 0x1E}  # AZERTY swaps A and Q.
_US_CHARS = {"a": 0x41, "A": 0x141, "q": 0x51, "1": 0x31, "!": 0x131, "\t": 0x09, "€": 0x645}
_FR_CHARS = _US_CHARS | {"1": 0x131, "!": -1, "é": 0x32}


class FakeUser32:
    def __init__(self) -> None:
        self.caps_lock = False
        self.map_threads: list[str] = []

    def MapVirtualKeyExW(self, vk: int, map_type: int, layout: int) -> int:
        assert map_type == 4
        self.map_threads.append(threading.current_thread().name)
        return (_FR_SCANS if layout == FR else _US_SCANS).get(vk, 0)

    def VkKeyScanExW(self, ch: str, layout: int) -> int:
        return (_FR_CHARS if layout == FR else _US_CHARS).get(ch, -1)

    def GetKeyState(self, vk: int) -> int:
        assert vk == 0x14
        return int(self.caps_lock)


class Recorder:
    """Stands in for uia.SendInput and keeps (scan, flags) per event."""

    def __init__(self) -> None:
        self.events: list[tuple[int, int]] = []
        self.raw: list[object] = []
        self.fail: set[int] = set()  # Event numbers (1-based) that SendInput rejects.

    def __call__(self, *inputs) -> int:
        item = inputs[0]
        if len(self.raw) + 1 in self.fail:
            self.raw.append(None)
            return 0
        self.raw.append(item)
        self.events.append((item.union.ki.wScan, item.union.ki.dwFlags))
        return len(inputs)


@pytest.fixture
def user32(monkeypatch) -> FakeUser32:
    fake = FakeUser32()
    monkeypatch.setattr(scancode, "_user32", fake)
    return fake


@pytest.fixture
def owner(monkeypatch) -> control.ControlCoordinator:
    """A real coordinator and ledger; no hook thread, so checkpoints are no-ops."""
    coordinator = control.ControlCoordinator()
    monkeypatch.setattr(scancode, "get_controller", lambda: coordinator)
    return coordinator


@pytest.fixture
def sent(monkeypatch, user32, owner) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(scancode.uia, "SendInput", recorder)
    monkeypatch.setattr(scancode.time, "sleep", lambda seconds: None)
    return recorder


def keys(*vks, layout=US):
    return [scancode.resolve(vk, layout) for vk in vks]


def test_chord_sends_scan_codes_with_extended_flag_and_reverse_release(sent, owner):
    scancode.press_chord(keys(0x11, 0x25))  # Ctrl+Left

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


def test_extended_flag_comes_from_the_layout_mapping(user32):
    assert scancode.resolve(0xA3, US) == scancode.Key(0xA3, 0x1D, True)  # Right Ctrl
    assert scancode.resolve(0xA2, US) == scancode.Key(0xA2, 0x1D, False)  # Left Ctrl
    assert scancode.resolve(0xAD, US) == scancode.Key(0xAD, 0x20, True)  # Mute, not D
    assert scancode.resolve(0x2E, US).extended  # Delete
    assert not scancode.resolve(0x41, US).extended


@pytest.mark.parametrize("vk", [0x13, 0xFE])  # Pause (E1 prefix), unmapped
def test_key_without_usable_encoding_is_rejected(user32, vk):
    with pytest.raises(ValueError, match="no scan code"):
        scancode.resolve(vk, US)


def test_shortcut_with_unencodable_key_sends_nothing(sent, owner, monkeypatch):
    desktop, _ = _desktop(monkeypatch, owner)
    with pytest.raises(ValueError, match="no scan code"):
        desktop.shortcut("ctrl+Pause", scancode=True)
    assert sent.events == []


def test_typing_holds_shift_for_shifted_characters(sent):
    scancode.type_text(scancode.plan_text("aA!", US))

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


def test_caps_lock_inverts_shift_for_letters_only(sent, user32):
    user32.caps_lock = True
    scancode.type_text(scancode.plan_text("aA!", US))

    assert sent.events == [
        (0x2A, SCANCODE),  # "a" with Caps Lock on needs Shift.
        (0x1E, SCANCODE),
        (0x1E, SCANCODE | KEYUP),
        (0x2A, SCANCODE | KEYUP),
        (0x1E, SCANCODE),  # "A" needs no Shift.
        (0x1E, SCANCODE | KEYUP),
        (0x2A, SCANCODE),  # "!" is not a letter: Shift as usual.
        (0x02, SCANCODE),
        (0x02, SCANCODE | KEYUP),
        (0x2A, SCANCODE | KEYUP),
    ]


def test_altgr_characters_hold_ctrl_and_alt(user32):
    assert scancode.char_chord("€", US) == keys(0x11, 0x12, 0x45)


def test_typing_uses_the_target_layout(sent):
    scancode.type_text(scancode.plan_text("a", FR))
    assert sent.events == [(0x10, SCANCODE), (0x10, SCANCODE | KEYUP)]  # AZERTY "a" key


def test_text_is_checked_against_the_target_layout(user32):
    assert scancode.plan_text("é", FR)
    with pytest.raises(ValueError, match="no key"):
        scancode.plan_text("é", US)


def test_newline_types_enter_and_carriage_return_is_skipped(sent):
    scancode.type_text(scancode.plan_text("\r\n", US))

    assert sent.events == [(0x1C, SCANCODE), (0x1C, SCANCODE | KEYUP)]


def test_every_down_is_held_in_the_ledger_until_its_up(sent, owner, monkeypatch):
    pending_during_sends: list[tuple[str, ...]] = []

    def spy(*inputs):
        pending_during_sends.append(owner.input_ledger.pending())
        return sent(*inputs)

    monkeypatch.setattr(scancode.uia, "SendInput", spy)
    scancode.press_chord(keys(0x10, 0x41))

    # The ledger records each down before injecting it, and drops it after the up.
    assert pending_during_sends == [
        ("key:scan:2a",),
        ("key:scan:2a", "key:scan:1e"),
        ("key:scan:2a", "key:scan:1e"),
        ("key:scan:2a",),
    ]
    assert owner.input_ledger.pending() == ()


class Preempted(Exception):
    pass


def _takeover_during_hold(owner, monkeypatch, before_release=lambda: None, thread=False):
    """Make the first hold tick act like a takeover: block input, release all, preempt."""
    clock = iter(range(100))
    monkeypatch.setattr(scancode.time, "monotonic", lambda: next(clock) * 0.1)

    def checkpoint():
        if not owner.input_ledger.pending():  # Single-key tests: fire on the first hold tick.
            return
        owner.input_ledger.block_new()
        before_release()
        if thread:  # The watchdog releases from its own thread.
            worker = threading.Thread(target=owner.input_ledger.release_all, name="watchdog")
            worker.start()
            worker.join()
        else:
            owner.input_ledger.release_all()
        raise Preempted

    monkeypatch.setattr(owner, "checkpoint_current", checkpoint)


def test_takeover_releases_with_the_scan_code_used_for_the_down(sent, owner, user32, monkeypatch):
    """The watchdog thread may have another layout; the up must not be re-mapped there."""
    _takeover_during_hold(owner, monkeypatch, thread=True)
    with pytest.raises(Preempted):
        scancode.press_chord(keys(0x41, layout=FR), hold=5.0)

    assert sent.events == [(0x10, SCANCODE), (0x10, SCANCODE | KEYUP)]
    assert "watchdog" not in user32.map_threads
    assert owner.input_ledger.pending() == ()


def _physical_down(owner, monkeypatch, scan, extended, vk):
    monkeypatch.setattr(control._user32, "CallNextHookEx", lambda *args: 0)  # Delivered.
    event = control._KeyHookData()
    event.vkCode, event.scanCode, event.flags = vk, scan, int(extended)
    owner._physical_key(0, 0x100, ctypes.addressof(event))


def test_physical_left_ctrl_does_not_cancel_injected_right_ctrl_release(sent, owner, monkeypatch):
    _takeover_during_hold(
        owner,
        monkeypatch,
        before_release=lambda: _physical_down(owner, monkeypatch, 0x1D, False, 0xA2),
    )
    with pytest.raises(Preempted):
        scancode.press_chord(keys(0xA3), hold=5.0)

    assert sent.events == [(0x1D, SCANCODE | EXTENDED), (0x1D, SCANCODE | EXTENDED | KEYUP)]
    assert owner.input_ledger.pending() == ()


def test_physical_down_of_the_same_key_takes_over_its_release(sent, owner, monkeypatch):
    _takeover_during_hold(
        owner,
        monkeypatch,
        before_release=lambda: _physical_down(owner, monkeypatch, 0x1D, True, 0xA3),
    )
    with pytest.raises(Preempted):
        scancode.press_chord(keys(0xA3), hold=5.0)

    assert sent.events == [(0x1D, SCANCODE | EXTENDED)]  # The user's own up will clear it.
    assert owner.input_ledger.pending() == ()


def test_partial_chord_failure_releases_the_keys_already_down(sent, owner):
    sent.fail = {2}  # Ctrl goes down, then SendInput rejects A's down.
    with pytest.raises(OSError, match="blocked"):
        scancode.press_chord(keys(0x11, 0x41))

    ups = [event for event in sent.events if event[1] & KEYUP]
    assert (0x1D, SCANCODE | KEYUP) in ups
    assert owner.input_ledger.pending() == ()


def test_failed_up_stays_in_the_ledger_for_the_retry(sent, owner):
    sent.fail = {2}  # The up is rejected once.
    with pytest.raises(OSError, match="blocked"):
        scancode.press_chord(keys(0x41))
    assert owner.input_ledger.pending() == ("key:scan:1e",)

    owner.input_ledger.release_all()  # What takeover or shutdown does.

    assert sent.events == [(0x1E, SCANCODE), (0x1E, SCANCODE | KEYUP)]
    assert owner.input_ledger.pending() == ()


def test_blocked_ledger_sends_nothing(sent, owner):
    owner.input_ledger.block_new()
    with pytest.raises(Exception, match="unavailable"):
        scancode.press_chord(keys(0x41))
    assert sent.events == []


@pytest.mark.parametrize("hold", [-1, 10.5, float("nan"), True])
def test_hold_out_of_range_is_rejected(sent, hold):
    with pytest.raises(ValueError, match="hold"):
        scancode.press_chord(keys(0x41), hold=hold)
    assert sent.events == []


@pytest.mark.parametrize(
    ("name", "vk"), [("Left", 0x25), ("ENTER", 0x0D), ("ctrl", 0x11), ("A", 0x41), ("a", 0x41)]
)
def test_vk_for_name(user32, name, vk):
    assert scancode.vk_for_name(name, US) == vk


def test_vk_for_unknown_name(user32):
    with pytest.raises(ValueError, match="Unknown key"):
        scancode.vk_for_name("NotAKey", US)


# Desktop.shortcut / Desktop.type routing


def _desktop(monkeypatch, owner):
    monkeypatch.setattr(service, "get_controller", lambda: owner)
    monkeypatch.setattr(scancode, "foreground_layout", lambda: US)
    monkeypatch.setattr(scancode, "layout_at", lambda x, y: US)
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
    monkeypatch.setattr(desktop, "_validate_screen_point", lambda x, y: None, raising=False)
    click = MagicMock()
    monkeypatch.setattr(service.uia, "Click", click)

    with pytest.raises(ValueError):
        desktop.type((10, 20), "\u00e9", scancode=True)
    click.assert_not_called()


def test_shortcut_tool_passes_scancode_and_hold():
    from windows_mcp.tools.input import register

    tools: dict = {}
    mcp = SimpleNamespace(tool=lambda **kw: lambda func: tools.setdefault(kw["name"], func))
    fake = MagicMock()
    register(mcp, get_desktop=lambda: fake, get_analytics=lambda: None)

    answer = asyncio.run(tools["Shortcut"](shortcut="right", scancode="true", hold=0.5))

    fake.shortcut.assert_called_once_with("right", scancode=True, hold=0.5)
    assert answer == "Pressed right as scan codes and held it for 0.5 seconds."
