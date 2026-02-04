#!/usr/bin/env python3
"""Plaud MCP Server - CDP proxy through Plaud Desktop."""

import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from .plaud_client import PlaudAPIError, PlaudClient

logging.basicConfig(
    level=getattr(logging, os.environ.get("PLAUD_LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP(name="plaud-mcp")
client = PlaudClient()


@mcp.tool()
async def get_recent_files(days: int = 7) -> list[dict[str, Any]]:
    """Get Plaud files from the last N days."""
    files = await client.get_recent_files(days=days)
    return [_format_file(f) for f in files]


@mcp.tool()
async def get_files(
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get Plaud files with optional date filters (ISO format, e.g. '2024-01-01')."""
    files = await client.get_files(limit=limit)
    if start_date:
        start_ms = _parse_date_to_ms(start_date)
        files = [f for f in files if f.get("start_time", 0) >= start_ms]
    if end_date:
        end_ms = _parse_date_to_ms(end_date) + 86400000
        files = [f for f in files if f.get("start_time", 0) <= end_ms]
    return [_format_file(f) for f in files]


@mcp.tool()
async def get_file(file_id: str) -> dict[str, Any]:
    """Get metadata for a specific Plaud file."""
    return _format_file(await client.get_file(file_id))


@mcp.tool()
async def get_transcript(file_id: str) -> dict[str, Any]:
    """Get full transcript with speaker labels for a Plaud file."""
    try:
        data = await client.get_transcript(file_id)
        if isinstance(data, list):
            lines = []
            for seg in data:
                speaker = seg.get("speaker", "")
                content = seg.get("content", "")
                if content:
                    lines.append(f"**{speaker}:** {content}" if speaker else content)
            return {
                "file_id": file_id,
                "transcript": "\n\n".join(lines),
                "segment_count": len(data),
                "segments": data[:10],
            }
        return {"file_id": file_id, "transcript": str(data)}
    except PlaudAPIError as e:
        return {"file_id": file_id, "error": str(e)}


@mcp.tool()
async def get_summary(file_id: str) -> dict[str, Any]:
    """Get AI-generated summary for a Plaud file."""
    try:
        data = await client.get_summary(file_id)
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
    """Search recent transcripts for matching content. Searches client-side."""
    files = await client.get_recent_files(days=days)
    query_lower = query.lower()
    sem = asyncio.Semaphore(5)

    async def _search_file(file: dict[str, Any]) -> dict[str, Any] | None:
        async with sem:
            try:
                title = file.get("filename", "")
                title_match = query_lower in title.lower()
                data = await client.get_transcript(file["id"])
                transcript_text = ""
                if isinstance(data, list):
                    transcript_text = "\n".join(
                        seg.get("content", "") for seg in data if seg.get("content")
                    )
                if title_match or query_lower in transcript_text.lower():
                    return {
                        "file_id": file["id"],
                        "title": title,
                        "date": _format_timestamp(file.get("start_time")),
                        "duration": _format_duration(file.get("duration")),
                        "excerpt": _extract_excerpt(transcript_text, query),
                    }
            except Exception as e:
                logger.warning(f"Failed to search file {file.get('id')}: {e}")
            return None

    matches = await asyncio.gather(*[_search_file(f) for f in files])
    return [m for m in matches if m is not None]


@mcp.tool()
async def get_file_count() -> dict[str, int]:
    """Get the total number of Plaud files."""
    return {"total": await client.get_file_count()}


@mcp.tool()
async def check_connection() -> dict[str, Any]:
    """Check if Plaud Desktop is available and authenticated."""
    try:
        if client.is_available():
            count = await client.get_file_count()
            return {
                "status": "connected",
                "total_files": count,
                "message": "Connected to Plaud via Desktop app CDP proxy",
            }
        return {
            "status": "unavailable",
            "message": "Plaud Desktop not running or not signed in. Launch the app and try again.",
        }
    except PlaudAPIError as e:
        return {"status": "error", "message": str(e)}


def _format_file(file: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": file.get("id"),
        "filename": file.get("filename"),
        "date": _format_timestamp(file.get("start_time")),
        "duration": _format_duration(file.get("duration")),
        "has_transcript": file.get("is_trans", False),
        "has_summary": file.get("is_summary", False),
    }


def _format_timestamp(ts: int | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts / 1000, tz=UTC).isoformat()
    except Exception:
        return str(ts)


def _format_duration(ms: int | None) -> str:
    if not ms:
        return ""
    seconds = ms // 1000
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _parse_date_to_ms(date_str: str) -> int:
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return int(dt.timestamp() * 1000)


def _extract_excerpt(text: str, query: str, context_chars: int = 200) -> str:
    if not text:
        return ""
    pos = text.lower().find(query.lower())
    if pos == -1:
        return text[: context_chars * 2] + "..." if len(text) > context_chars * 2 else text
    start = max(0, pos - context_chars)
    end = min(len(text), pos + len(query) + context_chars)
    excerpt = text[start:end]
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(text):
        excerpt = excerpt + "..."
    return excerpt


def main():
    if not client.is_available():
        logger.warning("Plaud Desktop not found or not signed in.")
    transport = "streamable-http" if "--http" in sys.argv else "stdio"
    try:
        mcp.run(transport=transport)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.exception(f"Server error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
