#!/usr/bin/env python3
"""
Plaud MCP Server - CDP proxy through Plaud Desktop.

Connects to the running Plaud Desktop app via Chrome DevTools Protocol
and executes API calls through the app's own authenticated session.
No token extraction needed - just have Plaud Desktop running and logged in.
"""

import logging
import os
import sys
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from .plaud_client import PlaudClient, PlaudAPIError

# Configure logging
logging.basicConfig(
    level=getattr(logging, os.environ.get("PLAUD_LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize FastMCP server
mcp = FastMCP(name="plaud-mcp")

# Global client instance
client = PlaudClient()


# ============================================================================
# File Operations - Direct API Wrappers
# ============================================================================


@mcp.tool()
async def get_recent_files(days: int = 7) -> list[dict[str, Any]]:
    """
    Get Plaud files from the last N days.

    Args:
        days: Number of days to look back (default: 7)

    Returns:
        List of file objects with metadata
    """
    files = await client.get_recent_files(days=days)
    return _format_files(files)


@mcp.tool()
async def get_files(
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Get Plaud files with optional filters.

    Args:
        start_date: Start date (ISO format, e.g., '2024-01-01')
        end_date: End date (ISO format)
        limit: Maximum number of results (default: 100)

    Returns:
        List of file objects with metadata
    """
    files = await client.get_files(limit=limit)

    # Filter by date if specified
    if start_date:
        start_ms = _parse_date_to_ms(start_date)
        files = [f for f in files if f.get("start_time", 0) >= start_ms]
    if end_date:
        end_ms = _parse_date_to_ms(end_date) + 86400000  # Include entire end day
        files = [f for f in files if f.get("start_time", 0) <= end_ms]

    return _format_files(files)


@mcp.tool()
async def get_file(file_id: str) -> dict[str, Any]:
    """
    Get metadata for a specific Plaud file.

    Args:
        file_id: File ID

    Returns:
        File metadata object
    """
    file = await client.get_file(file_id)
    return _format_file(file)


@mcp.tool()
async def get_transcript(file_id: str) -> dict[str, Any]:
    """
    Get full transcript for a Plaud file.

    Args:
        file_id: File ID

    Returns:
        Object with transcript text and segments
    """
    try:
        data = await client.get_transcript(file_id)

        if isinstance(data, dict) and "error" in data:
            return {"file_id": file_id, "error": data["error"]}

        # Format: list of segments with start_time, end_time, content, speaker
        if isinstance(data, list):
            # Build transcript text with speaker labels
            lines = []
            for seg in data:
                speaker = seg.get("speaker", "")
                content = seg.get("content", "")
                if content:
                    if speaker:
                        lines.append(f"**{speaker}:** {content}")
                    else:
                        lines.append(content)

            return {
                "file_id": file_id,
                "transcript": "\n\n".join(lines),
                "segment_count": len(data),
                "segments": data[:10],  # First 10 for reference
            }

        return {"file_id": file_id, "transcript": str(data)}
    except PlaudAPIError as e:
        return {"file_id": file_id, "error": str(e)}


@mcp.tool()
async def get_summary(file_id: str) -> dict[str, Any]:
    """
    Get AI-generated summary for a Plaud file.

    Args:
        file_id: File ID

    Returns:
        Summary object with content, header, and category
    """
    try:
        data = await client.get_summary(file_id)

        if isinstance(data, dict) and "error" in data:
            return {"file_id": file_id, "error": data["error"]}

        # Format: {"ai_content": "markdown text", "header": "...", "category": "..."}
        return {
            "file_id": file_id,
            "content": data.get("ai_content", ""),
            "header": data.get("header", ""),
            "category": data.get("category", ""),
        }
    except PlaudAPIError as e:
        return {"file_id": file_id, "error": str(e)}


@mcp.tool()
async def search_transcripts(query: str, days: int = 30) -> list[dict[str, Any]]:
    """
    Search through recent transcripts for matching content.

    This fetches recent files and searches client-side.
    For large result sets, this may take a few seconds.

    Args:
        query: Search query to match against transcript content and titles
        days: Number of days to search back (default: 30)

    Returns:
        List of matching files with transcript excerpts
    """
    # Get recent files
    files = await client.get_recent_files(days=days)

    results = []
    query_lower = query.lower()

    for file in files:
        try:
            # Check title first (fast)
            title = file.get("filename", "")
            title_match = query_lower in title.lower()

            # Get transcript data
            data = await client.get_transcript(file["id"])

            # Build transcript text from segments
            transcript_text = ""
            if isinstance(data, list):
                transcript_text = "\n".join(
                    seg.get("content", "") for seg in data if seg.get("content")
                )

            transcript_match = query_lower in transcript_text.lower()

            if title_match or transcript_match:
                excerpt = _extract_excerpt(transcript_text, query)

                results.append(
                    {
                        "file_id": file["id"],
                        "title": title,
                        "date": _format_timestamp(file.get("start_time")),
                        "duration": _format_duration(file.get("duration")),
                        "excerpt": excerpt,
                    }
                )

        except Exception as e:
            logger.warning(f"Failed to fetch data for file {file.get('id')}: {e}")
            continue

    return results


@mcp.tool()
async def get_file_count() -> dict[str, int]:
    """
    Get the total number of Plaud files.

    Returns:
        Object with total count
    """
    count = await client.get_file_count()
    return {"total": count}


@mcp.tool()
async def check_connection() -> dict[str, Any]:
    """
    Check if Plaud Desktop is available and authenticated.

    Returns:
        Connection status and user info
    """
    try:
        if client.is_available():
            # Get some files to verify
            count = await client.get_file_count()
            return {
                "status": "connected",
                "total_files": count,
                "message": "Connected to Plaud via Desktop app CDP proxy",
            }
        else:
            return {
                "status": "unavailable",
                "message": "Plaud Desktop not running or not signed in. Launch the app and try again.",
            }
    except PlaudAPIError as e:
        return {"status": "error", "message": str(e)}


# ============================================================================
# Helper Functions
# ============================================================================


def _format_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format a list of files for output."""
    return [_format_file(f) for f in files]


def _format_file(file: dict[str, Any]) -> dict[str, Any]:
    """Format a single file for output."""
    return {
        "id": file.get("id"),
        "filename": file.get("filename"),
        "date": _format_timestamp(file.get("start_time")),
        "duration": _format_duration(file.get("duration")),
        "has_transcript": file.get("is_trans", False),
        "has_summary": file.get("is_summary", False),
    }


def _format_timestamp(ts: int | None) -> str:
    """Format timestamp (ms) to ISO string."""
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(ts / 1000)
        return dt.isoformat()
    except Exception:
        return str(ts)


def _format_duration(ms: int | None) -> str:
    """Format duration in milliseconds to human-readable string."""
    if not ms:
        return ""
    seconds = ms // 1000
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"


def _parse_date_to_ms(date_str: str) -> int:
    """Parse ISO date string to milliseconds since epoch."""
    try:
        # Try ISO format
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        # Try simple date format
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return int(dt.timestamp() * 1000)


def _extract_excerpt(text: str, query: str, context_chars: int = 200) -> str:
    """Extract an excerpt around the first match of query in text."""
    if not text:
        return ""

    query_lower = query.lower()
    text_lower = text.lower()

    pos = text_lower.find(query_lower)
    if pos == -1:
        # Return start of text if no match found
        return text[: context_chars * 2] + "..." if len(text) > context_chars * 2 else text

    # Get context around the match
    start = max(0, pos - context_chars)
    end = min(len(text), pos + len(query) + context_chars)

    excerpt = text[start:end]

    # Add ellipsis if truncated
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(text):
        excerpt = excerpt + "..."

    return excerpt


# ============================================================================
# Entry Point
# ============================================================================


def main():
    """Run the Plaud MCP server."""
    # Check if Plaud Desktop is available
    if not client.is_available():
        logger.warning(
            "Plaud Desktop not found or not signed in. "
            "Please install Plaud Desktop and sign in to use this MCP."
        )

    # Default to stdio transport for Claude Code integration
    transport = "stdio"

    # Check for HTTP mode (for development/testing)
    if "--http" in sys.argv:
        transport = "streamable-http"
        logger.info("Running in HTTP mode")
    else:
        logger.info("Running in stdio mode (Claude Code compatible)")

    try:
        mcp.run(transport=transport)
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.exception(f"Server error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
