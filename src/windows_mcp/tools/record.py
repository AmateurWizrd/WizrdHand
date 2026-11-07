"""Record tool — background frame and key-event recording of one window."""

import json
from typing import Literal

from fastmcp import Context
from mcp.types import ToolAnnotations

from windows_mcp.desktop import recorder
from windows_mcp.desktop.window_capture import resolve_window
from windows_mcp.infrastructure import with_analytics


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="Record",
        description=(
            "Records one window in the background through Windows Graphics Capture, so the "
            "frames are right even when the window is covered. mode='start' (default) needs "
            "window_handle (an HWND) and/or window_title (exact title) and returns at once with "
            "the directory; frames are full-size client-area JPEGs named by seconds since the "
            "start, and events.jsonl logs key events with their time, marked source='user' for "
            "physical keys, 'agent' for keys this server sent during a tool call and "
            "'injected' for keys other programs sent. fps defaults to 15 (at most "
            f"{recorder.MAX_FPS:g}), seconds to 10 (at most {recorder.MAX_SECONDS:g}); a "
            f"recording also stops at {recorder.MAX_BYTES // 1024**3} GB. directory must be an "
            "absolute path to a new or empty folder; omit it for a fresh temporary folder. "
            "mode='status' reports frame count, dropped frames, size and whether it is still "
            "running; mode='stop' ends it early. One recording runs at a time. Recording reads "
            "the screen only and never sends input."
        ),
        annotations=ToolAnnotations(
            title="Record",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Record-Tool")
    def record_tool(
        mode: Literal["start", "status", "stop"] = "start",
        window_handle: int | None = None,
        window_title: str | None = None,
        fps: float = 15.0,
        seconds: float = 10.0,
        directory: str | None = None,
        ctx: Context = None,
    ) -> str:
        if mode == "start":
            hwnd = resolve_window(window_handle, window_title)
            result = recorder.start(hwnd, fps=fps, seconds=seconds, directory=directory).status()
        elif mode == "status":
            result = recorder.status()
        elif mode == "stop":
            result = recorder.stop()
        else:
            raise ValueError("mode must be 'start', 'status' or 'stop'")
        return json.dumps(result) if result is not None else "No recording has been started."
