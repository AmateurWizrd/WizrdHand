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
            "window_handle (an HWND) and/or window_title (exact title; if several visible "
            "windows share it, the largest one that is not minimized is recorded and the others "
            "are listed) and returns at once with the directory. Frames are full-size "
            "client-area JPEGs in <directory>/frames, named by seconds since the start. "
            "<directory>/events.jsonl logs key events (kind='key', t, source, vk, scan, down). "
            "The key log is GLOBAL: while a recording runs it logs keys typed into every "
            "window, not just the recorded one, marked source='user' for physical keys, "
            "'agent' for keys this server sent and 'injected' for keys other programs sent. "
            "Taking over the desktop does not stop a recording; use mode='stop'. fps defaults "
            f"to 15 (at most {recorder.MAX_FPS:g}), seconds to 10 (at most "
            f"{recorder.MAX_SECONDS:g}); a recording also stops when frames plus log reach "
            f"{recorder.MAX_BYTES // 1024**3} GB. directory must be an absolute path to a new "
            "or empty folder; omit it for a fresh temporary folder. mode='status' reports frame "
            "count, dropped frames, size and whether it is still running; mode='stop' ends it "
            "early. One recording runs at a time. Recording never sends input."
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
            hwnd, others = resolve_window(window_handle, window_title)
            result = recorder.start(hwnd, fps=fps, seconds=seconds, directory=directory).status()
            if others:
                result["other_windows_with_title"] = others
        elif mode == "status":
            result = recorder.status()
        elif mode == "stop":
            result = recorder.stop()
        else:
            raise ValueError("mode must be 'start', 'status' or 'stop'")
        return json.dumps(result) if result is not None else "No recording has been started."
