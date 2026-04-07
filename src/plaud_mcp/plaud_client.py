"""Plaud API client via direct HTTP.

Reads the auth token from Plaud Desktop's LevelDB local storage and calls
the Plaud API directly. No CDP/inspector needed.

Requirements:
- Plaud Desktop must have been logged in at least once (token persisted)
"""

import gzip
import json
import logging
import os
import subprocess
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PLAUD_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/Plaud")
LEVELDB_DIR = os.path.join(PLAUD_SUPPORT_DIR, "Local Storage", "leveldb")
DEFAULT_API_BASE = "https://api.plaud.ai"


class PlaudAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Plaud API Error ({status_code}): {message}")


NODE_HELPER = os.path.join(os.path.dirname(__file__), "..", "..", "node_helper", "read-token.mjs")


def _read_token_from_leveldb() -> str | None:
    """Extract authToken from Plaud Desktop's LevelDB local storage.

    Uses a Node.js helper (classic-level) for reliable LevelDB parsing.
    LevelDB's binary encoding inserts framing bytes that corrupt raw regex
    extraction, so a proper parser is required.

    Falls back to direct binary regex search if Node.js is unavailable.
    """
    # --- Primary: Node.js LevelDB reader (accurate) ---
    if os.path.exists(NODE_HELPER):
        try:
            result = subprocess.run(
                ["node", NODE_HELPER],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.startswith("bearer "):
                return result.stdout.strip()
        except Exception as e:
            logger.debug(f"Node.js helper failed: {e}")

    # --- Fallback: direct binary scan with framing-byte cleanup ---
    # LevelDB may insert non-ASCII framing bytes within token values,
    # so a simple regex won't match. We find the "bearer eyJ" anchor and
    # then collect JWT-valid characters, skipping any framing bytes,
    # until we have a complete 3-part JWT token.
    try:
        import re as _re

        db_files = sorted(
            [
                os.path.join(LEVELDB_DIR, f)
                for f in os.listdir(LEVELDB_DIR)
                if f.endswith(".log") or f.endswith(".ldb")
            ],
            key=os.path.getmtime,
            reverse=True,
        )

        JWT_CHARS = set(
            b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-."
        )

        for db_file in db_files:
            with open(db_file, "rb") as f:
                data = f.read()

            # First try clean regex (works when no framing bytes)
            match = _re.search(
                rb"bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)",
                data,
            )
            if match:
                return "bearer " + match.group(1).decode("ascii")

            # Find all "bearer eyJ" anchors and try to reconstruct token
            for m in _re.finditer(rb"bearer eyJ", data):
                start = m.start() + len(b"bearer ")
                # Collect JWT-valid bytes, skipping framing bytes
                token_bytes = bytearray()
                i = start
                # Read enough bytes to cover a typical JWT (~1-2KB)
                # but stop at reasonable limit
                max_scan = min(len(data), start + 4096)
                consecutive_non_jwt = 0
                while i < max_scan:
                    b = data[i]
                    if b in JWT_CHARS:
                        token_bytes.append(b)
                        consecutive_non_jwt = 0
                    else:
                        consecutive_non_jwt += 1
                        # If too many consecutive non-JWT bytes, token has ended
                        if consecutive_non_jwt > 16:
                            break
                    i += 1

                token_str = token_bytes.decode("ascii", errors="ignore")
                # Strip trailing dots/garbage
                token_str = token_str.rstrip(".")
                # Validate 3-part JWT structure
                parts = token_str.split(".")
                if len(parts) == 3 and all(len(p) > 10 for p in parts):
                    logger.debug(
                        f"Recovered token from {os.path.basename(db_file)} "
                        f"via framing-byte cleanup"
                    )
                    return "bearer " + token_str

        return None
    except Exception as e:
        logger.debug(f"Failed to read token from LevelDB: {e}")
        return None


class PlaudClient:
    """Plaud API client using direct HTTP with token from local storage."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._api_base: str = DEFAULT_API_BASE

    def _ensure_token(self) -> None:
        if self._token:
            return
        self._token = _read_token_from_leveldb()
        if not self._token:
            raise PlaudAPIError(
                401,
                "Could not find Plaud auth token. "
                "Please ensure Plaud Desktop is logged in.",
            )

    def _resolve_api_base(self, response_data: dict) -> None:
        """Handle region mismatch by updating API base URL."""
        if isinstance(response_data, dict) and response_data.get("status") == -302:
            domains = response_data.get("data", {}).get("domains", {})
            api_url = domains.get("api")
            if api_url:
                logger.info(f"Redirected to regional API: {api_url}")
                self._api_base = api_url

    def is_available(self) -> bool:
        try:
            self._ensure_token()
            return True
        except PlaudAPIError:
            return False

    async def _fetch(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make an authenticated API call to Plaud."""
        self._ensure_token()
        assert self._token is not None

        url = f"{self._api_base}{endpoint}"
        headers = {"Authorization": self._token}

        async with httpx.AsyncClient() as client:
            response = await client.get(
                url, params=params, headers=headers, timeout=30.0
            )
            response.raise_for_status()
            data = response.json()

        # Handle region redirect
        if isinstance(data, dict) and data.get("status") == -302:
            self._resolve_api_base(data)
            url = f"{self._api_base}{endpoint}"
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url, params=params, headers=headers, timeout=30.0
                )
                response.raise_for_status()
                data = response.json()

        if isinstance(data, dict) and data.get("status", 0) != 0:
            raise PlaudAPIError(
                data.get("status", 0), data.get("msg", "Unknown error")
            )

        return data

    async def _fetch_content_url(self, url: str) -> Any:
        """Fetch content from a signed S3 URL, handling gzip."""
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            content = response.content
            if content[:2] == b"\x1f\x8b":
                content = gzip.decompress(content)
            return json.loads(content)

    async def _get_content_by_type(
        self, file_id: str, data_type: str, label: str
    ) -> Any:
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
