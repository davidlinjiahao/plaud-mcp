"""Plaud API client using Plaud Desktop authentication."""

import base64
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class PlaudAPIError(Exception):
    """Custom exception for Plaud API errors."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Plaud API Error ({status_code}): {message}")


class PlaudClient:
    """
    Plaud API client using Plaud Desktop's authentication token.

    This client extracts the JWT token from Plaud Desktop's local storage
    and uses it to access the Plaud consumer API at api.plaud.ai.

    No developer API credentials required - uses the same auth as the desktop app.
    """

    API_BASE = "https://api.plaud.ai"
    TOKEN_PATH = Path.home() / "Library/Application Support/Plaud/Local Storage/leveldb"

    def __init__(self):
        self._access_token: str | None = None
        self._token_data: dict | None = None

    def _extract_token_from_leveldb(self) -> str | None:
        """
        Extract JWT token from Plaud Desktop's LevelDB storage.

        The token is stored in Local Storage but LevelDB may insert binary bytes
        between the base64 segments. We extract the key values and reconstruct
        the token.
        """
        if not self.TOKEN_PATH.exists():
            logger.warning(f"Plaud Desktop storage not found at {self.TOKEN_PATH}")
            return None

        try:
            # Read all LDB files and search for the bearer token
            for f in self.TOKEN_PATH.glob("*.ldb"):
                data = f.read_bytes()

                # Find bearer token location
                idx = data.find(b"bearer eyJ")
                if idx >= 0:
                    chunk = data[idx + 7 : idx + 600]  # Skip 'bearer '

                    # Extract header (up to first .)
                    header_end = chunk.find(b".")
                    if header_end < 0:
                        continue
                    header = chunk[:header_end].decode("ascii", errors="ignore")

                    # Extract all base64-like segments
                    segments = []
                    current = b""
                    base64_chars = set(
                        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                    )

                    for byte in chunk[header_end + 1 :]:
                        if byte in base64_chars:
                            current += bytes([byte])
                        else:
                            if len(current) > 5:
                                segments.append(current.decode("ascii"))
                            current = b""

                    if len(current) > 5:
                        segments.append(current.decode("ascii"))

                    # Filter to payload segments (> 20 chars, not data)
                    payload_parts = [
                        s for s in segments
                        if len(s) > 20 and not s.startswith("logged")
                    ]

                    if len(payload_parts) >= 4:
                        try:
                            # Decode first segment to get user ID
                            seg0_padded = payload_parts[0] + "=" * ((4 - len(payload_parts[0]) % 4) % 4)
                            seg0_decoded = base64.urlsafe_b64decode(seg0_padded).decode("utf-8", errors="ignore")

                            # Extract sub (user ID) from decoded segment
                            sub_match = re.search(r'"sub":"([a-f0-9]+)"', seg0_decoded)
                            if not sub_match:
                                continue
                            sub = sub_match.group(1)

                            # Decode second segment to get exp and iat
                            seg1_padded = payload_parts[1] + "=" * ((4 - len(payload_parts[1]) % 4) % 4)
                            seg1_decoded = base64.urlsafe_b64decode(seg1_padded).decode("utf-8", errors="ignore")

                            # Extract exp and iat values
                            exp_match = re.search(r'(\d{10})', seg1_decoded)
                            iat_match = re.search(r'"iat":(\d+)', seg1_decoded)

                            if not exp_match:
                                continue
                            exp = int(exp_match.group(1))
                            iat = int(iat_match.group(1)) if iat_match else exp - 25920000  # ~300 days

                            # Signature is usually segment 3 (after the garbage segment 2)
                            signature = payload_parts[3] if len(payload_parts) > 3 else payload_parts[-1]

                            # Reconstruct the payload with known structure
                            payload_data = {
                                "sub": sub,
                                "aud": "",
                                "exp": exp,
                                "iat": iat,
                                "client_id": "desktop",
                                "region": "aws:us-west-2"
                            }

                            payload_json = json.dumps(payload_data, separators=(",", ":"))
                            payload_b64 = (
                                base64.urlsafe_b64encode(payload_json.encode())
                                .decode()
                                .rstrip("=")
                            )

                            full_token = f"{header}.{payload_b64}.{signature}"
                            self._token_data = payload_data

                            logger.debug(
                                f"Extracted Plaud token for user: {sub}"
                            )
                            return full_token

                        except Exception as e:
                            logger.debug(f"Failed to decode token from {f.name}: {e}")
                            continue

            return None

        except Exception as e:
            logger.error(f"Failed to extract Plaud token: {e}")
            return None

    def _ensure_authenticated(self) -> None:
        """Ensure we have a valid token."""
        if self._access_token:
            # Check if token is expired
            if self._token_data and "exp" in self._token_data:
                if time.time() < self._token_data["exp"]:
                    return
                logger.debug("Token expired, re-extracting")

        token = self._extract_token_from_leveldb()
        if not token:
            raise PlaudAPIError(
                401,
                "No Plaud Desktop token found. Please sign in to Plaud Desktop app.",
            )
        self._access_token = token

    def is_available(self) -> bool:
        """Check if Plaud Desktop is installed and has valid auth."""
        try:
            self._ensure_authenticated()
            return True
        except PlaudAPIError:
            return False

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to Plaud API."""
        self._ensure_authenticated()

        url = f"{self.API_BASE}/{endpoint.lstrip('/')}"

        async with httpx.AsyncClient() as client:
            try:
                response = await client.request(
                    method=method,
                    url=url,
                    headers={
                        "Authorization": f"bearer {self._access_token}",
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    },
                    params=params,
                    timeout=30.0,
                )
                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as e:
                error_msg = e.response.text if e.response.content else str(e)
                raise PlaudAPIError(e.response.status_code, error_msg)
            except httpx.RequestError as e:
                raise PlaudAPIError(0, f"Request failed: {str(e)}")

    # ========================================================================
    # File Operations
    # ========================================================================

    async def get_files(
        self,
        skip: int = 0,
        limit: int = 100,
        is_trash: int = 2,  # 0=trashed, 1=untrashed, 2=all
        sort_by: str = "start_time",
        is_desc: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Get list of Plaud files.

        Args:
            skip: Number of files to skip (pagination)
            limit: Maximum number of results
            is_trash: Trash filter (0=trashed, 1=untrashed, 2=all)
            sort_by: Sort field
            is_desc: Sort descending

        Returns:
            List of file objects
        """
        params = {
            "skip": skip,
            "limit": limit,
            "is_trash": is_trash,
            "sort_by": sort_by,
            "is_desc": str(is_desc).lower(),
        }

        response = await self._request("GET", "file/simple/web", params=params)
        return response.get("data_file_list", [])

    async def get_file_count(self) -> int:
        """Get total number of files."""
        response = await self._request(
            "GET", "file/simple/web", params={"skip": 0, "limit": 1}
        )
        return response.get("data_file_total", 0)

    async def get_file(self, file_id: str) -> dict[str, Any]:
        """
        Get metadata for a specific file.

        Args:
            file_id: File ID

        Returns:
            File metadata object
        """
        # Get all files and find the one we want
        # (Consumer API doesn't have single-file endpoint)
        files = await self.get_files(limit=1000)
        for f in files:
            if f.get("id") == file_id:
                return f
        raise PlaudAPIError(404, f"File not found: {file_id}")

    async def get_file_detail(self, file_id: str) -> dict[str, Any]:
        """
        Get detailed file info including signed URLs for content.

        Args:
            file_id: File ID

        Returns:
            File detail object with content_list containing signed URLs
        """
        response = await self._request("GET", f"file/detail/{file_id}")
        return response.get("data", {})

    async def _fetch_content(self, url: str) -> dict[str, Any]:
        """Fetch content from signed URL, handling gzip if needed."""
        import gzip

        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()

            content = response.content
            # Check if gzipped (starts with gzip magic bytes)
            if content[:2] == b"\x1f\x8b":
                content = gzip.decompress(content)

            return json.loads(content)

    async def get_transcript(self, file_id: str) -> dict[str, Any]:
        """
        Get transcript for a file.

        Args:
            file_id: File ID

        Returns:
            Transcript data with segments
        """
        # Get file details to get signed URL
        detail = await self.get_file_detail(file_id)
        content_list = detail.get("content_list", [])

        # Find transcript URL
        transcript_url = None
        for content in content_list:
            if content.get("data_type") == "transaction":
                transcript_url = content.get("data_link")
                break

        if not transcript_url:
            return {"error": "No transcript available", "file_id": file_id}

        return await self._fetch_content(transcript_url)

    async def get_summary(self, file_id: str) -> dict[str, Any]:
        """
        Get AI summary for a file.

        Args:
            file_id: File ID

        Returns:
            Summary data
        """
        # Get file details to get signed URL
        detail = await self.get_file_detail(file_id)
        content_list = detail.get("content_list", [])

        # Find summary URL (auto_sum_note type)
        summary_url = None
        for content in content_list:
            if content.get("data_type") == "auto_sum_note":
                summary_url = content.get("data_link")
                break

        if not summary_url:
            return {"error": "No summary available", "file_id": file_id}

        return await self._fetch_content(summary_url)

    async def get_recent_files(self, days: int = 7) -> list[dict[str, Any]]:
        """
        Get files from the last N days.

        Args:
            days: Number of days to look back

        Returns:
            List of recent files
        """
        cutoff_ms = int((time.time() - days * 24 * 60 * 60) * 1000)
        files = await self.get_files(limit=100)
        return [f for f in files if f.get("start_time", 0) >= cutoff_ms]
