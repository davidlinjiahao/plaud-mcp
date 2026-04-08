"""Plaud API client via direct HTTP with token decryption.

Reads the auth token from Plaud Desktop's encryption.json, decrypts it
using the macOS Keychain "Plaud Safe Storage" key (Chromium v10 format),
and makes direct API calls to api.plaud.ai.

Requirements:
- Plaud Desktop must be installed and signed in (at least once)
- macOS Keychain access to "Plaud Safe Storage"
- cryptography package
"""

import base64
import gzip
import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

PLAUD_DATA_DIR = Path.home() / "Library" / "Application Support" / "Plaud"
API_BASE = "https://api.plaud.ai"


class PlaudAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Plaud API Error ({status_code}): {message}")


def _get_keychain_password() -> str:
    """Read the Plaud Safe Storage password from macOS Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "Plaud Safe Storage", "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PlaudAPIError(
            503, "Cannot read Plaud Safe Storage from Keychain. Is Plaud Desktop installed?"
        )
    return result.stdout.strip()


def _decrypt_v10_token(encrypted_b64: str, keychain_pass: str) -> str:
    """Decrypt a Chromium v10 Safe Storage encrypted value."""
    encrypted = base64.b64decode(encrypted_b64)
    if encrypted[:3] != b"v10":
        raise PlaudAPIError(500, "Unexpected encryption format (not v10)")
    encrypted = encrypted[3:]
    key = hashlib.pbkdf2_hmac("sha1", keychain_pass.encode(), b"saltysalt", 1003, dklen=16)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    decrypted = decryptor.update(encrypted) + decryptor.finalize()
    pad_len = decrypted[-1]
    if 1 <= pad_len <= 16:
        decrypted = decrypted[:-pad_len]
    return decrypted.decode("utf-8")


def _load_auth_token() -> str:
    """Load and decrypt the auth token from Plaud Desktop's local storage."""
    enc_path = PLAUD_DATA_DIR / "encryption.json"
    if not enc_path.exists():
        raise PlaudAPIError(503, "Plaud Desktop data not found. Is it installed and signed in?")
    data = json.loads(enc_path.read_text())
    encrypted_token = data.get("authToken")
    if not encrypted_token:
        raise PlaudAPIError(503, "No auth token in encryption.json. Sign into Plaud Desktop first.")
    keychain_pass = _get_keychain_password()
    decrypted = _decrypt_v10_token(encrypted_token, keychain_pass)
    # The decrypted value is "bearer <jwt>" — extract just the token
    if decrypted.lower().startswith("bearer "):
        return decrypted[7:]
    return decrypted


class PlaudClient:
    """Plaud API client using direct HTTP with decrypted auth token."""

    def __init__(self) -> None:
        self._token: str | None = None

    def _get_token(self) -> str:
        if self._token is None:
            self._token = _load_auth_token()
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def is_available(self) -> bool:
        try:
            self._get_token()
            return True
        except PlaudAPIError:
            return False

    async def _fetch(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make an authenticated API call to Plaud."""
        url = f"{API_BASE}{endpoint}"
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url, params=params, headers=self._headers(), timeout=15.0
            )
        if response.status_code == 401:
            # Token may have expired — clear cache and retry once
            self._token = None
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url, params=params, headers=self._headers(), timeout=15.0
                )
        if response.status_code != 200:
            raise PlaudAPIError(response.status_code, response.text[:300])
        return response.json()

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
