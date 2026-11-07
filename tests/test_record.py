"""Record: background window recording with a fake capture; no real WGC session or input."""

import asyncio
import json
import re
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from windows_mcp.desktop import control_hooks, recorder
from windows_mcp.desktop.control import get_controller


class FakeGrab:
    def __init__(self, fail_on: int | None = None) -> None:
        self.calls = 0
        self.fail_on = fail_on

    def __call__(self, hwnd: int):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("The capture session is closed")
        return Image.new("RGB", (64, 32), (self.calls % 255, 0, 0)), (100, 200, 64, 32)


@pytest.fixture(autouse=True)
def no_recording(monkeypatch):
    monkeypatch.setattr(recorder, "_current", None)
    yield
    if recorder._current is not None:
        recorder._current.stop()


def _wait_done(recording, timeout=5.0):
    deadline = time.monotonic() + timeout
    while recording.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not recording.running


def test_records_frames_named_by_seconds_until_the_duration(tmp_path):
    grab = FakeGrab()
    recording = recorder.start(7, fps=20, seconds=0.25, directory=str(tmp_path / "rec"), grab=grab)
    _wait_done(recording)

    status = recorder.status()
    frames = sorted(p.name for p in (tmp_path / "rec").glob("*.jpg"))
    assert status["stopped_by"] == "duration"
    assert status["frames"] == len(frames) >= 3
    assert status["frames"] + status["dropped_frames"] == 5  # 0.25 s at 20 fps.
    assert all(re.fullmatch(r"\d{4}\.\d{3}\.jpg", name) for name in frames)
    assert float(frames[0][:-4]) < 0.1
    assert status["frame_size"] == [64, 32]
    assert status["client_rect"] == [100, 200, 64, 32]
    assert Image.open(tmp_path / "rec" / frames[0]).size == (64, 32)
    assert (tmp_path / "rec" / "events.jsonl").exists()


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


def test_key_events_are_logged_with_their_source(tmp_path):
    recording = recorder.start(7, fps=5, seconds=60, directory=str(tmp_path), grab=FakeGrab())
    observers = get_controller().key_observers
    assert recording._on_key in observers
    for observer in list(observers):  # What the keyboard hook does.
        observer(0x41, 0x1E, True, "user")
        observer(0x41, 0x1E, False, "agent")

    status = recorder.stop()

    assert recording._on_key not in observers
    lines = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [(e["source"], e["vk"], e["scan"], e["down"]) for e in lines] == [
        ("user", 0x41, 0x1E, True),
        ("agent", 0x41, 0x1E, False),
    ]
    assert all(0 <= e["t"] < 5 for e in lines)
    assert status["key_events"] == 2


def test_size_limit_stops_the_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "MAX_BYTES", 1)
    recording = recorder.start(7, fps=30, seconds=10, directory=str(tmp_path), grab=FakeGrab())
    _wait_done(recording)

    assert recorder.status()["stopped_by"] == "size limit"
    assert recorder.status()["frames"] == 1


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
    ("injected", "active_calls", "source"),
    [(False, 0, "user"), (False, 1, "user"), (True, 1, "agent"), (True, 0, "injected")],
)
def test_hook_marks_the_key_source(injected, active_calls, source):
    seen = []

    def broken(*args):
        raise RuntimeError("an observer fault must not reach the hook")

    def observe(*event):
        seen.append(event)

    owner = SimpleNamespace(_active_calls=active_calls, key_observers=[broken, observe])
    data = SimpleNamespace(vkCode=0x26, scanCode=0x48)

    control_hooks._observe_key(owner, data, True, injected)

    assert seen == [(0x26, 0x48, True, source)]


def _record_tool():
    from windows_mcp.tools.record import register

    tools: dict = {}
    mcp = SimpleNamespace(tool=lambda **kw: lambda func: tools.setdefault(kw["name"], func))
    register(mcp, get_desktop=lambda: None, get_analytics=lambda: None)
    return tools["Record"]


def test_record_tool_status_before_any_recording():
    assert asyncio.run(_record_tool()(mode="status")) == "No recording has been started."


def test_record_tool_start_returns_at_once_with_the_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("windows_mcp.tools.record.resolve_window", lambda handle, title: 7)
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
    stopped = json.loads(asyncio.run(_record_tool()(mode="stop")))
    assert stopped["running"] is False
