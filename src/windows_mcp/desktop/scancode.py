"""Keyboard input as hardware scan codes, for apps that read the keyboard below virtual keys.

DirectInput and raw-input games ignore virtual-key events; SendInput with
KEYEVENTF_SCANCODE reaches them as if the key were pressed on a keyboard. Every
key down goes through the control coordinator's input ledger, so a user takeover
or shutdown releases whatever is still held.

Keys are resolved to a scan code once, with the target window's keyboard layout,
before anything goes down: the matching up reuses those exact values even when a
takeover releases it from another thread with another layout.
"""

import ctypes
import math
import time
from ctypes import wintypes
from typing import NamedTuple

import windows_mcp.uia as uia
from windows_mcp.desktop.control import get_controller

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.VkKeyScanExW.argtypes = [ctypes.c_wchar, wintypes.HKL]
_user32.VkKeyScanExW.restype = ctypes.c_short
_user32.MapVirtualKeyExW.argtypes = [ctypes.c_uint, ctypes.c_uint, wintypes.HKL]
_user32.MapVirtualKeyExW.restype = ctypes.c_uint
_user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
_user32.GetKeyboardLayout.restype = wintypes.HKL
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetKeyState.argtypes = [ctypes.c_int]
_user32.GetKeyState.restype = ctypes.c_short

_MAPVK_VK_TO_VSC_EX = 4  # High byte 0xE0 marks an extended (E0-prefixed) key.
_GA_ROOT = 2
_VK_RETURN = 0x0D
_VK_CAPITAL = 0x14
# VkKeyScan shift-state bits and the modifier each one needs held.
_SHIFT_STATE_MODIFIERS = ((1, 0x10), (2, 0x11), (4, 0x12))
_KEY_GAP = 0.01  # Same pause SendKeys leaves between the keys of a chord.
_TYPE_INTERVAL = 0.04  # Same per-character pace Desktop.type uses.
MAX_HOLD_SECONDS = 10.0


class Key(NamedTuple):
    """One physical key: its scan code and whether it carries the E0 prefix."""

    vk: int
    scan: int
    extended: bool


def window_layout(hwnd: int | None) -> int:
    """The keyboard layout of the thread that owns hwnd (the caller's if hwnd is None)."""
    thread = _user32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0
    return _user32.GetKeyboardLayout(thread) or 0


def foreground_layout() -> int:
    """The keyboard layout of the window that receives keys now."""
    return window_layout(_user32.GetForegroundWindow())


def layout_at(x: int, y: int) -> int:
    """The keyboard layout of the top-level window under screen point (x, y)."""
    hwnd = _user32.WindowFromPoint(wintypes.POINT(x, y))
    root = _user32.GetAncestor(hwnd, _GA_ROOT) if hwnd else None
    return window_layout(root) if root else foreground_layout()


def resolve(vk: int, layout: int) -> Key:
    """The scan code layout gives vk; raises if it has none a keyboard could send."""
    code = _user32.MapVirtualKeyExW(vk, _MAPVK_VK_TO_VSC_EX, layout)
    scan, prefix = code & 0xFF, (code >> 8) & 0xFF
    if not scan or prefix not in (0, 0xE0):
        raise ValueError(f"Key {vk:#04x} has no scan code on this keyboard layout")
    return Key(vk, scan, prefix == 0xE0)


def vk_for_name(name: str, layout: int) -> int:
    """A virtual key for a SendKeys key name ('Enter', 'Left', 'F5') or one unshifted character."""
    if len(name) == 1:
        packed = _user32.VkKeyScanExW(name.lower(), layout)
        if packed == -1 or packed & 0xFF == 0xFF:
            raise ValueError(f"{name!r} has no key on the current keyboard layout")
        return packed & 0xFF
    vk = uia.SpecialKeyNames.get(name.upper())
    if vk is None:
        raise ValueError(f"Unknown key name {name!r}")
    return vk


def char_chord(ch: str, layout: int, caps_lock: bool = False) -> list[Key]:
    """The modifiers and key that type ch on layout, Caps Lock state accounted for."""
    if ch == "\n":
        return [resolve(_VK_RETURN, layout)]
    packed = _user32.VkKeyScanExW(ch, layout) if ord(ch) <= 0xFFFF else -1
    vk, state = packed & 0xFF, (packed >> 8) & 0xFF
    if packed == -1 or vk == 0xFF or state & ~0x7:
        raise ValueError(f"{ch!r} has no key on the current keyboard layout")
    if caps_lock and state & ~1 == 0 and ch.lower() != ch.upper():
        state ^= 1  # Caps Lock inverts Shift for letters; leave the toggle alone.
    modifiers = [modifier for bit, modifier in _SHIFT_STATE_MODIFIERS if state & bit]
    return [resolve(modifier, layout) for modifier in modifiers] + [resolve(vk, layout)]


def plan_text(text: str, layout: int) -> list[list[Key]]:
    """One chord per character, all resolved before anything is typed."""
    caps_lock = bool(_user32.GetKeyState(_VK_CAPITAL) & 1)
    return [char_chord(ch, layout, caps_lock) for ch in text if ch != "\r"]


def _send(key: Key, up: bool) -> None:
    flags = uia.KeyboardEventFlag.KeyScanCode
    if key.extended:
        flags |= uia.KeyboardEventFlag.ExtendedKey
    if up:
        flags |= uia.KeyboardEventFlag.KeyUp
    if not uia.SendInput(uia.KeyboardInput(0, key.scan, flags)):
        raise OSError(ctypes.get_last_error(), "SendInput was blocked")


def press_chord(keys: list[Key], hold: float = 0.0) -> None:
    """Press keys in order, hold them for hold seconds, then release in reverse order."""
    if isinstance(hold, bool) or not math.isfinite(hold) or not 0 <= hold <= MAX_HOLD_SECONDS:
        raise ValueError(f"hold must be between 0 and {MAX_HOLD_SECONDS:g} seconds")
    seen: set[tuple[int, bool]] = set()
    keys = [k for k in keys if not ((k.scan, k.extended) in seen or seen.add((k.scan, k.extended)))]
    owner = get_controller()
    held: list[str] = []
    try:
        for key in keys:
            owner.checkpoint_current()
            name = f"key:scan:{'e0' if key.extended else ''}{key.scan:02x}"
            owner.input_ledger.press(
                name,
                lambda key=key: _send(key, up=False),
                lambda key=key: _send(key, up=True),
                # Only a physical down of this same key (side and E0 included) makes
                # the injected up redundant; any other key still needs it.
                lambda key=key: owner.physical_scan_down(key.scan, key.extended),
            )
            held.append(name)
            owner.record_step_current()
            time.sleep(_KEY_GAP)
        deadline = time.monotonic() + hold
        while (left := deadline - time.monotonic()) > 0:
            owner.checkpoint_current()  # A takeover mid-hold ends the hold.
            time.sleep(min(0.05, left))
    finally:
        first_error: Exception | None = None
        for name in reversed(held):
            try:
                owner.input_ledger.release(name)  # No-op if a takeover already released it.
            except Exception as exc:  # The ledger keeps it for the takeover/shutdown retry.
                first_error = first_error or exc
        if first_error is not None:
            raise first_error


def type_text(plan: list[list[Key]]) -> None:
    """Type a plan from plan_text, one chord per character."""
    for chord in plan:
        press_chord(chord)
        time.sleep(_TYPE_INTERVAL)
