"""
Plaud MCP Server — FastMCP application exposing Plaud cloud API data as tools.

Provides 8 tools:
  TOOL-01: check_connection     — verify token, return file count
  TOOL-02: get_file_count       — return total recordings count
  TOOL-03: get_recent_files     — list files from last N days
  TOOL-04: get_files            — list files with optional date range
  TOOL-05: get_file             — get metadata for a specific recording
  TOOL-06: get_transcript       — fetch transcript via signed S3 URL
  TOOL-07: get_summary          — fetch AI summary via signed S3 URL
  TOOL-08: search_transcripts   — client-side transcript search

Security:
  T-02-01: file_id validated non-empty before URL construction.
  T-02-02: S3 URLs only sourced from content_list[].data_link (Plaud API response).
  T-02-04: search_transcripts bounded to 50 files.
"""
from __future__ import annotations

import asyncio
import gzip
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .client import PlaudClient

mcp = FastMCP("plaud")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Health check endpoint for Kubernetes liveness probe (HTTP mode only)."""
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok"})


@mcp.tool()
async def check_connection() -> dict:
    """Verify the Plaud token is valid and return total file count.

    Returns a dict with status, user_id, email, and file_count.
    Raises PlaudAuthError if the token is invalid or expired.
    """
    async with PlaudClient() as client:
        user_resp = await client.get("/user/me")
        user_data = user_resp.get("data_user", {})
        count_resp = await client.get(
            "/file/simple/web",
            params={
                "skip": 0,
                "limit": 1,
                "is_trash": 2,
                "sort_by": "start_time",
                "is_desc": "true",
            },
        )
    return {
        "status": "connected",
        "user_id": user_data.get("id"),
        "email": user_data.get("email"),
        "file_count": count_resp.get("data_file_total", 0),
    }


@mcp.tool()
async def get_file_count() -> dict:
    """Return the total number of recordings in the account.

    Returns a dict with a single key: count.
    """
    async with PlaudClient() as client:
        resp = await client.get(
            "/file/simple/web",
            params={
                "skip": 0,
                "limit": 1,
                "is_trash": 2,
                "sort_by": "start_time",
                "is_desc": "true",
            },
        )
    return {"count": resp.get("data_file_total", 0)}


@mcp.tool()
async def get_recent_files(days: int = 7) -> dict:
    """Return files created in the last N days.

    Args:
        days: Number of days to look back (default 7).

    Returns a dict with files list, count, and days.
    """
    async with PlaudClient() as client:
        resp = await client.get(
            "/file/simple/web",
            params={
                "skip": 0,
                "limit": 100,
                "is_trash": 2,
                "sort_by": "start_time",
                "is_desc": "true",
            },
        )
    all_files = resp.get("data_file_list", [])
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000  # ms
    files = [f for f in all_files if f.get("start_time", 0) >= cutoff]
    return {"files": files, "count": len(files), "days": days}


@mcp.tool()
async def get_files(
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
) -> dict:
    """Return files with optional date range filter.

    Args:
        start_date: ISO date string "YYYY-MM-DD" (inclusive, UTC midnight).
        end_date: ISO date string "YYYY-MM-DD" (inclusive, end-of-day UTC).
        limit: Maximum files to return (clamped to 200).

    Returns a dict with files list and count.
    """
    limit = min(limit, 200)
    async with PlaudClient() as client:
        files = []
        skip = 0
        page_size = min(limit, 100)
        while len(files) < limit:
            resp = await client.get(
                "/file/simple/web",
                params={
                    "skip": skip,
                    "limit": page_size,
                    "is_trash": 2,
                    "sort_by": "start_time",
                    "is_desc": "true",
                },
            )
            batch = resp.get("data_file_list", [])
            if not batch:
                break
            files.extend(batch)
            skip += len(batch)
            if len(batch) < page_size:
                break
    if start_date is not None:
        start_ts = (
            datetime.strptime(start_date, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        ) * 1000  # start_time is in milliseconds
        files = [f for f in files if f.get("start_time", 0) >= start_ts]
    if end_date is not None:
        end_ts = (
            datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            + timedelta(days=1)
        ).timestamp() * 1000  # start_time is in milliseconds
        files = [f for f in files if f.get("start_time", 0) < end_ts]
    return {"files": files, "count": len(files)}


@mcp.tool()
async def get_file(file_id: str) -> dict:
    """Return metadata and content_list for a specific recording.

    Args:
        file_id: The Plaud file identifier (non-empty string).

    Returns the file detail data dict (includes content_list with S3 URLs).
    Raises ValueError if file_id is empty or whitespace.
    """
    if not file_id or not file_id.strip():
        raise ValueError("file_id must be a non-empty string")
    async with PlaudClient() as client:
        resp = await client.get(f"/file/detail/{file_id.strip()}")
    return resp.get("data", {})


def _fetch_s3_content(data_link: str) -> dict:
    """Fetch JSON content from a signed S3 URL (gzip or plain).

    This is a synchronous helper — call via asyncio.to_thread() from async tools.
    No Plaud auth headers are sent; S3 signed URLs are self-authenticating.

    Security: T-02-02 — data_link must be sourced from Plaud API content_list[].data_link,
    never from MCP caller input.

    Args:
        data_link: Signed S3 URL from content_list[].data_link.

    Returns the parsed JSON dict/list from the S3 object.
    """
    response = httpx.get(data_link, follow_redirects=True, timeout=30.0)
    response.raise_for_status()
    try:
        raw = gzip.decompress(response.content)
    except gzip.BadGzipFile:
        raw = response.content
    return json.loads(raw)


@mcp.tool()
async def get_transcript(file_id: str) -> dict:
    """Return the full transcript with speaker labels for a recording.

    Fetches the file detail from Plaud API, then downloads and decompresses
    the transcript from the signed S3 URL in content_list.

    Args:
        file_id: The Plaud file identifier (non-empty string).

    Returns a dict with file_id, transcript (full parsed JSON), and speaker_count.
    Raises ValueError if file_id is empty or no transcript is found.
    """
    if not file_id or not file_id.strip():
        raise ValueError("file_id must be a non-empty string")
    async with PlaudClient() as client:
        resp = await client.get(f"/file/detail/{file_id.strip()}")
        detail = resp.get("data", {})
    content_item = next(
        (c for c in detail.get("content_list", []) if c.get("data_type") == "transaction"),
        None,
    )
    if content_item is None:
        raise ValueError(f"No transcript found for file_id={file_id}")
    transcript_data = await asyncio.to_thread(_fetch_s3_content, content_item["data_link"])
    items = transcript_data.get("list", []) if isinstance(transcript_data, dict) else transcript_data
    speakers = {
        item.get("speaker")
        for item in items
        if item.get("speaker")
    }
    return {
        "file_id": file_id,
        "transcript": transcript_data,
        "speaker_count": len(speakers),
    }


@mcp.tool()
async def get_summary(file_id: str) -> dict:
    """Return the AI-generated summary for a recording.

    Fetches the file detail from Plaud API, then downloads and decompresses
    the summary from the signed S3 URL in content_list.

    Args:
        file_id: The Plaud file identifier (non-empty string).

    Returns a dict with file_id and summary (full parsed JSON).
    Raises ValueError if file_id is empty or no summary is found.
    """
    if not file_id or not file_id.strip():
        raise ValueError("file_id must be a non-empty string")
    async with PlaudClient() as client:
        resp = await client.get(f"/file/detail/{file_id.strip()}")
        detail = resp.get("data", {})
    content_item = next(
        (c for c in detail.get("content_list", []) if c.get("data_type") == "auto_sum_note"),
        None,
    )
    if content_item is None:
        raise ValueError(f"No summary found for file_id={file_id}")
    summary_data = await asyncio.to_thread(_fetch_s3_content, content_item["data_link"])
    return {"file_id": file_id, "summary": summary_data}


@mcp.tool()
async def search_transcripts(query: str, days: int = 30) -> dict:
    """Search transcript content across recent files (client-side).

    Fetches the last 50 files within the given day window, downloads each
    transcript, and performs a case-insensitive substring match. Files with
    no transcript or fetch errors are silently skipped.

    Args:
        query: Search term (non-empty string).
        days: How many days back to search (default 30).

    Returns a dict with query, days, matches list, and match_count.
    Raises ValueError if query is empty.

    Security: T-02-04 — bounded to 50 files per call.
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")
    async with PlaudClient() as client:
        resp = await client.get(
            "/file/simple/web",
            params={
                "skip": 0,
                "limit": 50,
                "is_trash": 2,
                "sort_by": "start_time",
                "is_desc": "true",
            },
        )
        all_files = resp.get("data_file_list", [])
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000  # ms
        files = [f for f in all_files if f.get("start_time", 0) >= cutoff]

        matches = []
        for f in files:
            try:
                detail_resp = await client.get(f"/file/detail/{f['id']}")
                detail = detail_resp.get("data", {})
                content_item = next(
                    (
                        c
                        for c in detail.get("content_list", [])
                        if c.get("data_type") == "transaction"
                    ),
                    None,
                )
                if content_item is None:
                    continue
                transcript_data = await asyncio.to_thread(
                    _fetch_s3_content, content_item["data_link"]
                )
            except Exception:
                continue  # skip files with missing or erroring transcripts

            items = transcript_data.get("list", []) if isinstance(transcript_data, dict) else transcript_data
            full_text = " ".join(
                item.get("text", "") or item.get("content", "") for item in items
            )
            if query.lower() in full_text.lower():
                idx = full_text.lower().index(query.lower())
                snippet = full_text[max(0, idx - 50) : idx + len(query) + 150].strip()
                matches.append(
                    {
                        "file_id": f["id"],
                        "title": f.get("filename", ""),
                        "start_time": f.get("start_time"),
                        "snippet": snippet,
                    }
                )

    return {"query": query, "days": days, "matches": matches, "match_count": len(matches)}


@mcp.tool()
async def list_folders() -> dict:
    """Return all filetags (folders) the user has created.

    Returns a dict with total count and a list of folders.
    Each folder has id, name, icon, and color.
    """
    async with PlaudClient() as client:
        resp = await client.get("/filetag/")
    folder_list = resp.get("data_filetag_list", [])
    return {
        "total": resp.get("data_filetag_total", len(folder_list)),
        "folders": [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "icon": f.get("icon"),
                "color": f.get("color"),
            }
            for f in folder_list
        ],
    }


@mcp.tool()
async def create_folder(
    name: str,
    color: str | None = None,
    icon: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Create a new filetag (folder) in Plaud.

    Args:
        name: Folder name (required, non-empty after trimming).
        color: Hex color string like '#4c8eff' (optional).
        icon: Icon font code like 'e627' (optional).
        dry_run: If True (default), preflight only — no actual creation.

    Returns a dict with ok status, duplicate detection result, and created folder data.
    """
    trimmed = name.strip() if name else ""
    if not trimmed:
        return {
            "ok": False,
            "dry_run": dry_run,
            "name": name,
            "message": "name must be a non-empty string",
            "errors": "empty name",
        }

    async with PlaudClient() as client:
        # Pre-flight: detect duplicate name
        list_resp = await client.get("/filetag/")
        existing = list_resp.get("data_filetag_list", [])
        dup = next((f for f in existing if f.get("name") == trimmed), None)
        if dup:
            return {
                "ok": True,
                "dry_run": dry_run,
                "name": trimmed,
                "color": color,
                "icon": icon,
                "duplicate_found": True,
                "existing_folder_id": dup.get("id"),
                "new_folder": None,
                "message": f"Folder '{trimmed}' already exists",
            }

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "name": trimmed,
                "color": color,
                "icon": icon,
                "duplicate_found": False,
                "existing_folder_id": None,
                "new_folder": None,
                "message": f"Planned: create folder '{trimmed}'",
            }

        # Live creation
        body: dict[str, Any] = {"name": trimmed}
        if color is not None:
            body["color"] = color
        if icon is not None:
            body["icon"] = icon

        resp = await client.post("/filetag/", json=body)
        data = resp.get("data_filetag", {})
        return {
            "ok": True,
            "dry_run": False,
            "name": trimmed,
            "color": color,
            "icon": icon,
            "duplicate_found": False,
            "existing_folder_id": None,
            "new_folder": {
                "id": data.get("id"),
                "name": data.get("name"),
                "icon": data.get("icon"),
                "color": data.get("color"),
            },
            "message": f"Created folder '{trimmed}'",
        }


@mcp.tool()
async def move_file_to_folder(
    file_id: str,
    folder_id: str,
    dry_run: bool = True,
) -> dict:
    """Move a single Plaud recording to a single folder.

    Args:
        file_id: The Plaud file identifier (non-empty string).
        folder_id: The folder identifier from list_folders() (non-empty string).
        dry_run: If True (default), preflight only — no actual move.

    Returns a dict with ok status, previous and new filetag_id_list.
    """
    if not file_id or not file_id.strip():
        return {
            "ok": False,
            "dry_run": dry_run,
            "message": "file_id must be a non-empty string",
            "errors": "empty file_id",
        }
    if not folder_id or not folder_id.strip():
        return {
            "ok": False,
            "dry_run": dry_run,
            "message": "folder_id must be a non-empty string",
            "errors": "empty folder_id",
        }

    async with PlaudClient() as client:
        detail_resp = await client.get(f"/file/detail/{file_id.strip()}")
        detail = detail_resp.get("data", {})
        prev_list = detail.get("filetag_id_list", [])

        if folder_id in prev_list:
            return {
                "ok": True,
                "dry_run": dry_run,
                "file_id": file_id,
                "folder_id": folder_id,
                "previous_filetag_id_list": prev_list,
                "new_filetag_id_list": prev_list,
                "message": "File is already in the target folder",
            }

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "file_id": file_id,
                "folder_id": folder_id,
                "previous_filetag_id_list": prev_list,
                "new_filetag_id_list": [folder_id],
                "message": f"Planned: move file {file_id} to folder {folder_id}",
            }

        resp = await client.patch(
            "/file/filetag",
            json={
                "file_id": file_id,
                "filetag_id_list": [folder_id],
            },
        )
        data_file = resp.get("data_file", {})
        return {
            "ok": True,
            "dry_run": False,
            "file_id": file_id,
            "folder_id": folder_id,
            "previous_filetag_id_list": prev_list,
            "new_filetag_id_list": [folder_id],
            "data_file": data_file,
            "message": f"Moved file {file_id} to folder {folder_id}",
        }


@mcp.tool()
async def unset_file_folder(
    file_id: str,
    dry_run: bool = True,
) -> dict:
    """Remove a recording from all folders (set filetag_id_list to empty).

    Args:
        file_id: The Plaud file identifier (non-empty string).
        dry_run: If True (default), preflight only — no actual mutation.

    Returns a dict with ok status and previous/new filetag_id_list.
    """
    if not file_id or not file_id.strip():
        return {
            "ok": False,
            "dry_run": dry_run,
            "message": "file_id must be a non-empty string",
            "errors": "empty file_id",
        }

    async with PlaudClient() as client:
        detail_resp = await client.get(f"/file/detail/{file_id.strip()}")
        detail = detail_resp.get("data", {})
        prev_list = detail.get("filetag_id_list", [])

        if not prev_list:
            return {
                "ok": True,
                "dry_run": dry_run,
                "file_id": file_id,
                "previous_filetag_id_list": [],
                "new_filetag_id_list": [],
                "message": "File is not in any folder",
            }

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "file_id": file_id,
                "previous_filetag_id_list": prev_list,
                "new_filetag_id_list": [],
                "message": f"Planned: remove file {file_id} from all folders",
            }

        resp = await client.patch(
            "/file/filetag",
            json={
                "file_id": file_id,
                "filetag_id_list": [],
            },
        )
        data_file = resp.get("data_file", {})
        return {
            "ok": True,
            "dry_run": False,
            "file_id": file_id,
            "previous_filetag_id_list": prev_list,
            "new_filetag_id_list": [],
            "data_file": data_file,
            "message": f"Removed file {file_id} from all folders",
        }
