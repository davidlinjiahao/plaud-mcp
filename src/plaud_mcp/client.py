"""
PlaudClient — authenticated async HTTP client for the Plaud cloud API.

Satisfies:
  AUTH-02: All six required headers sent on every request.
  AUTH-03: -302 domain redirect updates base_url and retries once.
  AUTH-04: -10000 auth failure raises PlaudAuthError.

Security:
  T-01-02: Redirect domain validated against *.plaud.ai before base_url mutation.
  T-01-03: _redirect_attempted flag prevents infinite redirect loops.
  T-01-05: 30-second timeout on all requests prevents hung connections.
  T-01-01: Token value never logged (only last 4 chars safe to log).
"""
from __future__ import annotations

import httpx

from .config import settings
from .errors import PlaudAPIError, PlaudAuthError


class PlaudClient:
    """Async HTTP client for the Plaud cloud API with full AUTH-02 header set."""

    def __init__(self) -> None:
        self._redirect_attempted = False
        self._client = httpx.AsyncClient(
            base_url=settings.plaud_base_url,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0),
            headers={
                "Authorization": f"bearer {settings.plaud_token}",
                "X-Device-Id": settings.plaud_device_id,
                "edit-from": "desktop",
                "app-platform": "desktop",
                "app-versionNumber": settings.plaud_app_version,
                "app-language": "en",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
                    " AppleWebKit/537.36 (KHTML, like Gecko)"
                    " Chrome/120.0.0.0 Electron/29.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://web.plaud.ai",
                "Referer": "https://web.plaud.ai/",
            },
        )

    async def __aenter__(self) -> PlaudClient:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """
        Core request method. Handles Plaud application-level response codes:
          0       -> return parsed response dict
          -302    -> update base_url to redirected domain and retry once
          -10000  -> raise PlaudAuthError (token invalid or expired)
          other   -> raise PlaudAPIError
        """
        response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        body = response.json()

        status = body.get("status")

        if status == 0:
            return body

        if status == -302:
            if self._redirect_attempted:
                raise PlaudAPIError(
                    "Redirect loop: received -302 on retry request"
                )

            new_domain = (
                body.get("data", {}).get("domains", {}).get("api")
            )
            if new_domain is None:
                raise PlaudAPIError(
                    "Received -302 redirect but no domain in response body"
                )

            # T-01-02: Reject redirects to non-Plaud domains
            if not new_domain.endswith("plaud.ai"):
                raise PlaudAPIError(
                    f"Reject redirect to non-Plaud domain: {new_domain}"
                )

            self._redirect_attempted = True
            self._client.base_url = httpx.URL(f"https://{new_domain}")

            try:
                result = await self._request(method, path, **kwargs)
            finally:
                self._redirect_attempted = False

            return result

        if status == -10000:
            msg = body.get("msg", "")
            raise PlaudAuthError(
                f"Plaud token is invalid or expired: {msg}"
            )

        msg = body.get("msg", "")
        raise PlaudAPIError(
            f"Plaud API returned non-zero status {status}: {msg}"
        )

    async def get(self, path: str, **kwargs) -> dict:
        """Perform a GET request against the Plaud API."""
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> dict:
        """Perform a POST request against the Plaud API."""
        return await self._request("POST", path, **kwargs)

    async def patch(self, path: str, **kwargs) -> dict:
        """Perform a PATCH request against the Plaud API."""
        return await self._request("PATCH", path, **kwargs)

    async def aclose(self) -> None:
        """Close the underlying httpx.AsyncClient."""
        await self._client.aclose()
