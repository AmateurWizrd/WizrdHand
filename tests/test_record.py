"""Record: background window recording with a fake capture; no real WGC session or input."""

import asyncio
import ctypes
import json
import re
import time
from types import SimpleNamespace

import pytest
from PIL import Image

import windows_mcp.uia as uia
from windows_mcp.desktop import control_hooks, recorder
from windows_mcp.desktop.control import get_controller


class FakeGrab:
    def __init__(self, fail_on: int | None = None, delay: float = 0.0) -> None:
        self.calls = 0
        self.fail_on = fail_on
        self.delay = delay

    def __call__(self, hwnd: int):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("The capture session is closed")
        if self.calls > 1:
            time.sleep(self.delay)
        return Image.new("RGB", (64, 32), (self.calls % 255, 0, 0)), (100, 200, 64, 32)


@pytest.fixture(autouse=True)
def no_recording(monkeypatch):
    monkeypatch.setattr(recorder, "_current", None)
    observers = list(get_controller().key_observers)
    yield
    if recorder._current is not None:
        recorder._current.stop()
    assert get_controller().key_observers == observers  # No observer may leak.


def _wait_done(recording, timeout=5.0):
    deadline = time.monotonic() + timeout
    while recording.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not recording.running


def test_records_frames_named_by_seconds_until_the_duration(tmp_path):
    recording = recorder.start(
        7, fps=20, seconds=0.25, directory=str(tmp_path / "rec"), grab=FakeGrab()
    )
    _wait_done(recording)

    status = recorder.status()
    frames = sorted(p.name for p in (tmp_path / "rec" / "frames").glob("*.jpg"))
    assert status["stopped_by"] == "duration"
    assert status["frames"] == len(frames) >= 3
    assert status["frames"] + status["dropped_frames"] <= 5  # 0.25 s at 20 fps.
    assert all(re.fullmatch(r"\d{4}\.\d{3}\.jpg", name) for name in frames)
    assert float(frames[0][:-4]) < 0.1
    assert all(float(name[:-4]) < 0.25 for name in frames)
    assert status["frame_size"] == [64, 32]
    assert status["client_rect"] == [100, 200, 64, 32]
    assert status["frames_directory"] == str(tmp_path / "rec" / "frames")
    assert Image.open(tmp_path / "rec" / "frames" / frames[0]).size == (64, 32)
    assert (tmp_path / "rec" / "events.jsonl").exists()
    assert not list((tmp_path / "rec").glob("*.jpg"))  # Frames live only in frames/.


def test_slow_frames_never_start_a_capture_past_the_deadline(tmp_path):
    grab = FakeGrab(delay=0.15)  # Each capture takes longer than the frame interval.
    recording = recorder.start(7, fps=20, seconds=0.3, directory=str(tmp_path), grab=grab)
    _wait_done(recording)

    stamps = [float(p.stem) for p in (tmp_path / "frames").glob("*.jpg")]
    assert stamps and all(stamp < 0.3 + 0.15 + 0.1 for stamp in stamps)
    # Call 1 checks the window at start; no capture may begin at or after 0.3 s.
    assert grab.calls - 1 == len(stamps) <= 3
    assert recorder.status()["dropped_frames"] > 0


def test_stop_ends_a_recording_early(tmp_path):
    recording = recorder.start(7, fps=5, seconds=60, directory=str(tmp_path), grab=FakeGrab())

    status = recorder.stop()

    assert status["running"] is False
    assert status["stopped_by"] == "stop"
    assert status["elapsed_seconds"] < 5
    assert not recording.running


def test_only_one_recording_at_a_time(tmp_path):
    recorder.start(7, fps=5, seconds=60, directory=str(tmp_path / "a"), grab=FakeGrab())
    with pytest.raises(RuntimeError, match="already running"):
        recorder.start(7, directory=str(tmp_path / "b"), grab=FakeGrab())


def test_key_events_are_logged_with_kind_and_source(tmp_path):
    recording = recorder.start(7, fps=5, seconds=60, directory=str(tmp_path), grab=FakeGrab())
    observers = get_controller().key_observers
    assert recording._on_key in observers
    for observer in list(observers):  # What the keyboard hook does.
        observer(0x41, 0x1E, True, "user")
        observer(0x41, 0x1E, False, "agent")

    status = recorder.stop()

    assert recording._on_key not in observers
    lines = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [(e["kind"], e["source"], e["vk"], e["scan"], e["down"]) for e in lines] == [
        ("key", "user", 0x41, 0x1E, True),
        ("key", "agent", 0x41, 0x1E, False),
    ]
    assert all(0 <= e["t"] < 5 for e in lines)
    assert status["key_events"] == 2


def test_size_limit_counts_frames_and_log_and_never_overshoots(tmp_path, monkeypatch):
    frame_size = len(_jpeg())
    monkeypatch.setattr(recorder, "MAX_BYTES", frame_size * 2 + 10)
    grab = FakeGrab()

    def grab_with_keys(hwnd):
        if grab.calls == 1:  # During the first frame: 100 key events fill the budget.
            for observer in list(get_controller().key_observers):
                for _ in range(100):
                    observer(0x41, 0x1E, True, "user")
        return grab(hwnd)

    recording = recorder.start(7, fps=5, seconds=60, directory=str(tmp_path), grab=grab_with_keys)
    _wait_done(recording)

    status = recorder.status()
    written = sum(p.stat().st_size for p in tmp_path.rglob("*") if p.is_file())
    assert status["stopped_by"] == "size limit"
    assert status["bytes"] == written <= recorder.MAX_BYTES
    assert 0 < status["key_events"] < 100


def test_frame_that_would_exceed_the_limit_is_not_written(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "MAX_BYTES", 1)
    recording = recorder.start(7, fps=30, seconds=10, directory=str(tmp_path), grab=FakeGrab())
    _wait_done(recording)

    assert recorder.status()["stopped_by"] == "size limit"
    assert recorder.status()["frames"] == 0
    assert list((tmp_path / "frames").iterdir()) == []


def _jpeg() -> bytes:
    import io

    buffer = io.BytesIO()
    FakeGrab()(7)[0].save(buffer, "JPEG", quality=recorder.JPEG_QUALITY)
    return buffer.getvalue()


def test_capture_failure_mid_recording_is_reported(tmp_path):
    recording = recorder.start(
        7, fps=30, seconds=10, directory=str(tmp_path), grab=FakeGrab(fail_on=3)
    )
    _wait_done(recording)

    status = recorder.status()
    assert status["stopped_by"] == "error"
    assert "closed" in status["error"]
    assert status["frames"] == 1  # Call 1 checks the window at start, call 2 is frame one.
    assert recording._on_key not in get_controller().key_observers


def test_log_that_cannot_be_opened_unregisters_the_observer(tmp_path, monkeypatch):
    real_open = open

    def failing_open(path, *args, **kwargs):
        if str(path).endswith("events.jsonl"):
            raise PermissionError("read-only share")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_open)
    recording = recorder.start(7, fps=5, seconds=60, directory=str(tmp_path), grab=FakeGrab())
    _wait_done(recording)

    assert recorder.status()["stopped_by"] == "error"
    assert "read-only" in recorder.status()["error"]
    assert recording._on_key not in get_controller().key_observers


def test_thread_that_fails_to_start_unregisters_the_observer(tmp_path, monkeypatch):
    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(recorder.threading.Thread, "start", refuse)
    with pytest.raises(RuntimeError, match="new thread"):
        recorder.start(7, directory=str(tmp_path), grab=FakeGrab())
    # The autouse fixture checks that no observer was left behind.


def test_window_that_cannot_be_captured_fails_at_start(tmp_path):
    with pytest.raises(RuntimeError, match="closed"):
        recorder.start(7, directory=str(tmp_path), grab=FakeGrab(fail_on=1))
    assert recorder.status() is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "kwargs",
    [{"fps": 0}, {"fps": 31}, {"seconds": 0}, {"seconds": 601}, {"fps": float("nan")}],
)
def test_limits_are_enforced(tmp_path, kwargs):
    with pytest.raises(ValueError):
        recorder.start(7, directory=str(tmp_path), grab=FakeGrab(), **kwargs)


def test_directory_must_be_absolute_and_empty(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        recorder.start(7, directory="relative/dir", grab=FakeGrab())
    (tmp_path / "old.jpg").write_bytes(b"")
    with pytest.raises(ValueError, match="not empty"):
        recorder.start(7, directory=str(tmp_path), grab=FakeGrab())


def test_default_directory_is_a_fresh_temp_folder():
    recording = recorder.start(7, fps=5, seconds=60, grab=FakeGrab())
    recorder.stop()
    assert recording.directory.name.startswith("windows-mcp-record-")
    assert (recording.directory / "events.jsonl").exists()


@pytest.mark.parametrize(
    ("injected", "extra_info", "source"),
    [
        (False, 0, "user"),
        (True, uia.INJECTED_EXTRA_INFO, "agent"),
        (True, 0, "injected"),
        (True, 12345, "injected"),
    ],
)
def test_hook_marks_the_key_source_by_injection_marker(injected, extra_info, source):
    seen = []

    def broken(*args):
        raise RuntimeError("an observer fault must not reach the hook")

    def observe(*event):
        seen.append(event)

    # _active_calls no longer matters: another program injecting during a tool call
    # is "injected", and the server's own cleanup ups after a call are "agent".
    owner = SimpleNamespace(_active_calls=1 - injected, key_observers=[broken, observe])
    data = SimpleNamespace(vkCode=0x26, scanCode=0x48, dwExtraInfo=extra_info)

    control_hooks._observe_key(owner, data, True, injected)

    assert seen == [(0x26, 0x48, True, source)]


def test_server_key_injections_carry_the_marker(monkeypatch):
    calls = []
    monkeypatch.setattr(
        uia.core.ctypes.windll.user32, "keybd_event", lambda *args: calls.append(args)
    )
    uia.core.keybd_event(0x41, 0x1E, 0, 0)
    assert calls == [(0x41, 0x1E, 0, uia.INJECTED_EXTRA_INFO)]

    ki = uia.KeyboardInput(0, 0x1E, uia.KeyboardEventFlag.KeyScanCode).union.ki
    address = ctypes.cast(ki.dwExtraInfo, ctypes.c_void_p).value
    assert address == uia.INJECTED_EXTRA_INFO


def _record_tool():
    from windows_mcp.tools.record import register

    tools: dict = {}
    mcp = SimpleNamespace(tool=lambda **kw: lambda func: tools.setdefault(kw["name"], func))
    register(mcp, get_desktop=lambda: None, get_analytics=lambda: None)
    return tools["Record"]


def test_record_tool_status_before_any_recording():
    assert asyncio.run(_record_tool()(mode="status")) == "No recording has been started."


def test_record_tool_start_returns_at_once_with_the_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("windows_mcp.tools.record.resolve_window", lambda handle, title: (7, [8]))
    real_start = recorder.start
    monkeypatch.setattr(
        recorder, "start", lambda hwnd, **kw: real_start(hwnd, grab=FakeGrab(), **kw)
    )

    started = time.monotonic()
    answer = json.loads(
        asyncio.run(
            _record_tool()(window_title="Game", seconds=60, directory=str(tmp_path / "rec"))
        )
    )

    assert time.monotonic() - started < 2
    assert answer["directory"] == str(tmp_path / "rec")
    assert answer["running"] is True
    assert answer["window"] == 7
    assert answer["other_windows_with_title"] == [8]
    stopped = json.loads(asyncio.run(_record_tool()(mode="stop")))
    assert stopped["running"] is False
