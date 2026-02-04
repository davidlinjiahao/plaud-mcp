"""Configuration management for Plaud MCP server."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class PlaudConfig(BaseSettings):
    """
    Plaud MCP Server configuration.

    No API credentials required - authentication is handled by extracting
    the JWT token from Plaud Desktop's local storage.

    Just ensure Plaud Desktop is installed and signed in.
    """

    # Logging configuration
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_prefix="PLAUD_",
        env_file=Path(__file__).parent.parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


# Global config instance
config = PlaudConfig()
