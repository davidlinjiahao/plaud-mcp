"""Plaud API client via Chrome DevTools Protocol (CDP).

Connects to the running Plaud Desktop app's Node.js inspector and executes
API calls through the app's own authenticated $fetch function. This bypasses
all token/session issues since we piggyback on the app's live session.

Requirements:
- Plaud Desktop must be running and logged in
- SIGUSR1 is sent to enable the Node.js inspector on port 9229
"""

import asyncio
import gzip
import json
import logging
import os
import signal
import subprocess
import time
import urllib.request
from typing import Any

import httpx
import websockets

logger = logging.getLogger(__name__)

INSPECTOR_PORT = 9229
INSPECTOR_HOST = "127.0.0.1"


class PlaudAPIError(Exception):
    """Custom exception for Plaud API errors."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Plaud API Error ({status_code}): {message}")


def _find_plaud_pid() -> int | None:
    """Find the main Plaud Desktop process PID."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "Plaud.app/Contents/MacOS/Plaud"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            pids = result.stdout.strip().split("\n")
            if pids and pids[0]:
                return int(pids[0])
    except Exception as e:
        logger.debug(f"Failed to find Plaud PID: {e}")
    return None


def _enable_inspector(pid: int) -> bool:
    """Send SIGUSR1 to enable Node.js inspector on the Plaud process."""
    try:
        os.kill(pid, signal.SIGUSR1)
        time.sleep(0.5)
        # Verify inspector is listening
        try:
            data = urllib.request.urlopen(
                f"http://{INSPECTOR_HOST}:{INSPECTOR_PORT}/json", timeout=2
            ).read()
            targets = json.loads(data)
            return len(targets) > 0
        except Exception:
            return False
    except Exception as e:
        logger.debug(f"Failed to enable inspector: {e}")
        return False


def _get_ws_url() -> str | None:
    """Get the WebSocket debugger URL from the inspector."""
    try:
        data = urllib.request.urlopen(
            f"http://{INSPECTOR_HOST}:{INSPECTOR_PORT}/json", timeout=2
        ).read()
        targets = json.loads(data)
        if targets:
            return targets[0].get("webSocketDebuggerUrl")
    except Exception:
        pass
    return None


class PlaudClient:
    """
    Plaud API client using Chrome DevTools Protocol.

    Connects to the running Plaud Desktop app via its Node.js inspector
    and executes API calls through the app's authenticated $fetch function.

    No token extraction needed - uses the app's live session directly.
    """

    def __init__(self) -> None:
        self._ws_url: str | None = None
        self._msg_id = 0
        self._connected = False

    def _ensure_inspector(self) -> None:
        """Ensure the Plaud Desktop inspector is available."""
        # Check if inspector is already running
        ws_url = _get_ws_url()
        if ws_url:
            self._ws_url = ws_url
            return

        # Find Plaud process and enable inspector
        pid = _find_plaud_pid()
        if not pid:
            raise PlaudAPIError(
                503,
                "Plaud Desktop is not running. Please launch the Plaud Desktop app.",
            )

        if not _enable_inspector(pid):
            raise PlaudAPIError(
                503,
                "Could not enable Plaud Desktop inspector. "
                "Please ensure Plaud Desktop is running and try again.",
            )

        ws_url = _get_ws_url()
        if not ws_url:
            raise PlaudAPIError(
                503, "Inspector enabled but could not get WebSocket URL."
            )
        self._ws_url = ws_url

    async def _cdp_eval(self, js_expression: str) -> Any:
        """Execute JavaScript in the Plaud Desktop context via CDP."""
        self._ensure_inspector()
        assert self._ws_url is not None

        self._msg_id += 1
        msg = {
            "id": self._msg_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": js_expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
        }

        try:
            async with websockets.connect(
                self._ws_url,
                max_size=2**22,  # 4MB max message
                open_timeout=5,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps(msg))
                response = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        except Exception as e:
            # Inspector might have become stale, clear cached URL
            self._ws_url = None
            raise PlaudAPIError(503, f"CDP connection failed: {e}")

        result = response.get("result", {}).get("result", {})

        if result.get("subtype") == "error":
            desc = result.get("description", "Unknown JS error")
            raise PlaudAPIError(500, f"JavaScript error: {desc}")

        if result.get("type") == "string":
            return json.loads(result["value"])

        if result.get("type") == "undefined":
            return None

        return result.get("value")

    def is_available(self) -> bool:
        """Check if Plaud Desktop is running and inspector can connect."""
        try:
            self._ensure_inspector()
            return True
        except PlaudAPIError:
            return False

    async def _fetch(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make an API call through the Plaud Desktop's authenticated $fetch."""
        params_js = json.dumps(params) if params else "undefined"
        js = f"""
            (async () => {{
                try {{
                    const fetchFn = globalThis['$fetch'];
                    if (!fetchFn) {{
                        return JSON.stringify({{ error: '$fetch not available - user may not be logged in' }});
                    }}
                    const opts = {params_js} !== undefined ? {{ params: {params_js} }} : {{}};
                    const result = await fetchFn('{endpoint}', opts);
                    return JSON.stringify(result);
                }} catch(e) {{
                    return JSON.stringify({{ error: e.message, status: e.status || 0 }});
                }}
            }})()
        """
        result = await self._cdp_eval(js)
        if isinstance(result, dict) and "error" in result:
            status = result.get("status", 0)
            raise PlaudAPIError(status, result["error"])
        return result

    async def _fetch_content_url(self, url: str) -> Any:
        """Fetch content from a signed URL (e.g., S3), handling gzip."""
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            content = response.content
            if content[:2] == b"\x1f\x8b":
                content = gzip.decompress(content)
            return json.loads(content)

    # ========================================================================
    # File Operations
    # ========================================================================

    async def get_files(
        self,
        skip: int = 0,
        limit: int = 100,
        is_trash: int = 2,
        sort_by: str = "start_time",
        is_desc: bool = True,
    ) -> list[dict[str, Any]]:
        """Get list of Plaud files."""
        params = {
            "skip": skip,
            "limit": limit,
            "is_trash": is_trash,
            "sort_by": sort_by,
            "is_desc": str(is_desc).lower(),
        }
        response = await self._fetch("/file/simple/web", params=params)
        return response.get("data_file_list", [])

    async def get_file_count(self) -> int:
        """Get total number of files."""
        response = await self._fetch(
            "/file/simple/web", params={"skip": 0, "limit": 1}
        )
        return response.get("data_file_total", 0)

    async def get_file(self, file_id: str) -> dict[str, Any]:
        """Get metadata for a specific file."""
        files = await self.get_files(limit=1000)
        for f in files:
            if f.get("id") == file_id:
                return f
        raise PlaudAPIError(404, f"File not found: {file_id}")

    async def get_file_detail(self, file_id: str) -> dict[str, Any]:
        """Get detailed file info including signed URLs for content."""
        response = await self._fetch(f"/file/detail/{file_id}")
        return response.get("data", {})

    async def get_transcript(self, file_id: str) -> Any:
        """Get transcript for a file."""
        detail = await self.get_file_detail(file_id)
        content_list = detail.get("content_list", [])

        transcript_url = None
        for content in content_list:
            if content.get("data_type") == "transaction":
                transcript_url = content.get("data_link")
                break

        if not transcript_url:
            return {"error": "No transcript available", "file_id": file_id}

        return await self._fetch_content_url(transcript_url)

    async def get_summary(self, file_id: str) -> Any:
        """Get AI summary for a file."""
        detail = await self.get_file_detail(file_id)
        content_list = detail.get("content_list", [])

        summary_url = None
        for content in content_list:
            if content.get("data_type") == "auto_sum_note":
                summary_url = content.get("data_link")
                break

        if not summary_url:
            return {"error": "No summary available", "file_id": file_id}

        return await self._fetch_content_url(summary_url)

    async def get_recent_files(self, days: int = 7) -> list[dict[str, Any]]:
        """Get files from the last N days."""
        cutoff_ms = int((time.time() - days * 24 * 60 * 60) * 1000)
        files = await self.get_files(limit=100)
        return [f for f in files if f.get("start_time", 0) >= cutoff_ms]
