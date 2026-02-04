"""Plaud API client via Chrome DevTools Protocol (CDP).

Connects to the running Plaud Desktop app's Node.js inspector and executes
API calls through the app's own authenticated $fetch function. This bypasses
all token/session issues since we piggyback on the app's live session.

Security note: SIGUSR1 opens the Node.js inspector on localhost:9229 for the
lifetime of the Plaud Desktop process. Only local processes can connect.

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
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Plaud API Error ({status_code}): {message}")


def _get_inspector_targets() -> list[dict[str, Any]]:
    """Fetch inspector targets from the debugging endpoint."""
    data = urllib.request.urlopen(
        f"http://{INSPECTOR_HOST}:{INSPECTOR_PORT}/json", timeout=2
    ).read()
    return json.loads(data)


def _find_plaud_pid() -> int | None:
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
    try:
        os.kill(pid, signal.SIGUSR1)
        time.sleep(0.5)
        try:
            targets = _get_inspector_targets()
            return len(targets) > 0
        except Exception:
            return False
    except Exception as e:
        logger.debug(f"Failed to enable inspector: {e}")
        return False


def _get_ws_url() -> str | None:
    try:
        targets = _get_inspector_targets()
        if targets:
            return targets[0].get("webSocketDebuggerUrl")
    except Exception as e:
        logger.debug(f"Inspector not available: {e}")
    return None


class PlaudClient:
    """Plaud API client using Chrome DevTools Protocol.

    Connects to the running Plaud Desktop app via its Node.js inspector
    and executes API calls through the app's authenticated $fetch function.
    No token extraction needed — uses the app's live session directly.
    """

    def __init__(self) -> None:
        self._ws_url: str | None = None
        self._msg_id = 0

    def _ensure_inspector(self) -> None:
        ws_url = _get_ws_url()
        if ws_url:
            self._ws_url = ws_url
            return

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

    async def _cdp_eval(self, js_expression: str, _retry: bool = True) -> Any:
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
                max_size=2**22,
                open_timeout=5,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps(msg))
                response = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        except Exception as e:
            self._ws_url = None
            if _retry:
                logger.debug(f"CDP connection failed, retrying: {e}")
                return await self._cdp_eval(js_expression, _retry=False)
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
            raise PlaudAPIError(result.get("status", 0), result["error"])
        return result

    async def _fetch_content_url(self, url: str) -> Any:
        """Fetch content from a signed S3 URL, handling gzip."""
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            content = response.content
            if content[:2] == b"\x1f\x8b":
                content = gzip.decompress(content)
            return json.loads(content)

    async def _get_content_by_type(self, file_id: str, data_type: str, label: str) -> Any:
        """Fetch file content (transcript, summary, etc.) by data_type."""
        detail = await self.get_file_detail(file_id)
        for content in detail.get("content_list", []):
            if content.get("data_type") == data_type:
                return await self._fetch_content_url(content["data_link"])
        raise PlaudAPIError(404, f"No {label} available for file {file_id}")

    async def get_files(
        self,
        skip: int = 0,
        limit: int = 100,
        is_trash: int = 2,
        sort_by: str = "start_time",
        is_desc: bool = True,
    ) -> list[dict[str, Any]]:
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
        response = await self._fetch(
            "/file/simple/web", params={"skip": 0, "limit": 1}
        )
        return response.get("data_file_total", 0)

    async def get_file(self, file_id: str) -> dict[str, Any]:
        """Get metadata for a specific file via detail endpoint."""
        return await self.get_file_detail(file_id)

    async def get_file_detail(self, file_id: str) -> dict[str, Any]:
        response = await self._fetch(f"/file/detail/{file_id}")
        return response.get("data", {})

    async def get_transcript(self, file_id: str) -> Any:
        return await self._get_content_by_type(file_id, "transaction", "transcript")

    async def get_summary(self, file_id: str) -> Any:
        return await self._get_content_by_type(file_id, "auto_sum_note", "summary")

    async def get_recent_files(self, days: int = 7) -> list[dict[str, Any]]:
        cutoff_ms = int((time.time() - days * 24 * 60 * 60) * 1000)
        files = await self.get_files(limit=100)
        return [f for f in files if f.get("start_time", 0) >= cutoff_ms]
