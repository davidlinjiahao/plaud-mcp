class PlaudError(Exception):
    """Base exception for all Plaud API errors."""


class PlaudAuthError(PlaudError):
    """Raised when the Plaud API returns status -10000 (token invalid or expired)."""


class PlaudAPIError(PlaudError):
    """Raised for non-zero API status codes other than -10000."""
