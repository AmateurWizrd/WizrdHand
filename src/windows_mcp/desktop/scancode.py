"""Keyboard input as hardware scan codes, for apps that read the keyboard below virtual keys.

DirectInput and raw-input games ignore virtual-key events; SendInput with
KEYEVENTF_SCANCODE reaches them as if the key were pressed on a keyboard. Every
key down goes through the control coordinator's input ledger, so a user takeover
or shutdown releases whatever is still held.
"""

import ctypes
import math
import time

import windows_mcp.uia as uia
from windows_mcp.desktop.control import get_controller

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
_user32.VkKeyScanW.restype = ctypes.c_short
_user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
_user32.MapVirtualKeyW.restype = ctypes.c_uint

_MAPVK_VK_TO_VSC = 0
# Keys whose scan code has the E0 prefix: arrows, Ins/Del/Home/End/PgUp/PgDn,
# PrintScreen, Win/Apps, numpad divide, NumLock, right Ctrl and right Alt.
EXTENDED_VKS = frozenset(
    {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E}
    | {0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5}
)
# VkKeyScanW shift-state bits and the modifier each one needs held.
_SHIFT_STATE_MODIFIERS = ((1, 0x10), (2, 0x11), (4, 0x12))
_VK_RETURN = 0x0D
_KEY_GAP = 0.01  # Same pause SendKeys leaves between the keys of a chord.
_TYPE_INTERVAL = 0.04  # Same per-character pace Desktop.type uses.
MAX_HOLD_SECONDS = 10.0


def scan_code(vk: int) -> int:
    """The scan code the active keyboard layout gives a virtual key."""
    scan = _user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC) & 0xFF
    if not scan:
        raise ValueError(f"Key {vk:#04x} has no scan code on this keyboard layout")
    return scan


def vk_for_name(name: str) -> int:
    """A virtual key for a SendKeys key name ('Enter', 'Left', 'F5') or one unshifted character."""
    if len(name) == 1:
        vk, _ = char_keys(name.lower())
        return vk
    vk = uia.SpecialKeyNames.get(name.upper())
    if vk is None:
        raise ValueError(f"Unknown key name {name!r}")
    return vk


def char_keys(ch: str) -> tuple[int, list[int]]:
    """The key that types ch on the active layout and the modifiers it needs held."""
    if ch == "\n":
        return _VK_RETURN, []
    packed = _user32.VkKeyScanW(ch) if ord(ch) <= 0xFFFF else -1
    state = (packed >> 8) & 0xFF
    if packed == -1 or (packed & 0xFF) == 0xFF or state & ~0x7:
        raise ValueError(f"{ch!r} has no key on the current keyboard layout")
    return packed & 0xFF, [vk for bit, vk in _SHIFT_STATE_MODIFIERS if state & bit]


def _send(vk: int, up: bool) -> None:
    flags = uia.KeyboardEventFlag.KeyScanCode
    if vk in EXTENDED_VKS:
        flags |= uia.KeyboardEventFlag.ExtendedKey
    if up:
        flags |= uia.KeyboardEventFlag.KeyUp
    if not uia.SendInput(uia.KeyboardInput(0, scan_code(vk), flags)):
        raise OSError(ctypes.get_last_error(), "SendInput was blocked")


def press_chord(vks: list[int], hold: float = 0.0) -> None:
    """Press keys in order, hold them for hold seconds, then release in reverse order."""
    if isinstance(hold, bool) or not math.isfinite(hold) or not 0 <= hold <= MAX_HOLD_SECONDS:
        raise ValueError(f"hold must be between 0 and {MAX_HOLD_SECONDS:g} seconds")
    vks = list(dict.fromkeys(vks))
    for vk in vks:
        scan_code(vk)  # Fail on an unmappable key before anything goes down.
    owner = get_controller()
    held: list[str] = []
    try:
        for vk in vks:
            owner.checkpoint_current()
            name = f"key:scan:{vk:#04x}"
            owner.input_ledger.press(
                name,
                lambda vk=vk: _send(vk, up=False),
                lambda vk=vk: _send(vk, up=True),
                lambda vk=vk: owner.physical_key_down(vk),
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


def type_text(text: str) -> None:
    """Type text key by key as scan codes, holding Shift (or AltGr) where the layout needs it."""
    plan = [char_keys(ch) for ch in text if ch != "\r"]  # Validate everything before typing.
    for vk, modifiers in plan:
        press_chord([*modifiers, vk])
        time.sleep(_TYPE_INTERVAL)
