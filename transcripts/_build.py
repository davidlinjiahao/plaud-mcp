#!/usr/bin/env python3
"""Build organized transcript markdown files from Plaud API data.

Run from project root:
    .venv/bin/python transcripts/_build.py

Features:
- Fetches transcripts and summaries from Plaud API
- Extracts tasks from Elliot's speaking segments via Ollama (phi4)
- Writes structured markdown with YAML frontmatter
- Builds INDEX.md with task descriptions and hours
"""

import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

# Add project src to path so we can import plaud_client
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from plaud_mcp.plaud_client import PlaudClient, PlaudAPIError

TRANSCRIPTS_DIR = Path(__file__).resolve().parent
DAYS = 14

# Keyword-to-tag mapping: if keyword appears in title or first 500 chars, add the tag
TAG_KEYWORDS: dict[str, list[str]] = {
    "team-sync": ["team", "sync", "standup", "stand-up", "huddle"],
    "AI": ["ai", "artificial intelligence", "machine learning", "llm", "chatbot", "gpt", "claude"],
    "MDM": ["mdm", "mobile device", "device management", "intune", "jamf"],
    "policy": ["policy", "policies", "compliance", "regulation", "governance"],
    "scrum": ["scrum", "sprint", "retrospective", "retro", "backlog", "grooming"],
    "prompt-engineering": ["prompt", "prompting", "prompt engineering"],
    "office-move": ["office move", "moving offices", "relocation", "new office"],
    "budget": ["budget", "funding", "cost", "expense", "fiscal"],
    "Power-BI": ["power bi", "powerbi", "dashboard", "report"],
    "1-on-1": ["one on one", "1-on-1", "1:1", "one-on-one", "check-in"],
    "meeting": ["meeting", "agenda", "minutes"],
    "interview": ["interview", "candidate", "hiring"],
    "training": ["training", "workshop", "learning", "onboarding"],
    "security": ["security", "cybersecurity", "vulnerability", "phishing", "mfa"],
    "infrastructure": ["infrastructure", "server", "network", "cloud", "azure", "aws"],
    "project-update": ["project update", "status update", "progress"],
    "presentation": ["presentation", "demo", "showcase"],
}

# Keywords that suggest personal content
NOTEPIN_SERIAL_PREFIX = "8820B"  # NotePin hardware serial starts with this


def detect_device(file: dict) -> str:
    """Determine device from scene code and serial number."""
    scene = file.get("scene")
    serial = file.get("serial_number", "")
    if scene == 1 or serial.startswith(NOTEPIN_SERIAL_PREFIX):
        return "NotePin"
    return "Desktop"


OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "phi4"

HF_INFERENCE_URL = "https://router.huggingface.co/hf-inference/models/microsoft/Phi-3.5-mini-instruct/v1/chat/completions"
HF_MODEL = "microsoft/Phi-3.5-mini-instruct"

# Speaker names that map to the user (Elliot)
ELLIOT_SPEAKERS = {"Elliot A. Jarbe", "Ej", "Elliot"}

# Verbs/phrases that signal a task in spoken updates
TASK_SIGNALS = [
    "working on", "worked on", "been working", "finishing", "finished",
    "meeting with", "met with", "had a meeting", "have a meeting",
    "submitted", "submitting", "sending", "sent", "deployed", "deploying",
    "reviewed", "reviewing", "created", "creating", "built", "building",
    "put together", "putting together", "set up", "setting up",
    "talking to", "talked to", "spoke with", "discussing",
    "looking at", "looked at", "going over", "went over",
    "helping", "helped", "assisted", "coordinating",
    "writing", "wrote", "drafting", "drafted",
    "testing", "tested", "debugging", "fixed", "fixing",
    "researching", "researched", "investigating",
    "preparing", "prepared", "planning", "planned",
    "presenting", "presented", "showing", "showed",
    "cleaning up", "refactoring", "updating", "updated",
]

# Keywords for estimating hours
QUICK_KEYWORDS = ["email", "ping", "quick", "brief", "short", "minute"]
MEETING_KEYWORDS = ["meeting", "call", "sync", "check-in", "session", "discussion"]
PROJECT_KEYWORDS = [
    "labs", "policy", "guidelines", "framework", "roadmap", "strategy",
    "connector", "deployment", "architecture", "design", "react", "site",
    "cookbook", "curriculum", "rollout",
]


def extract_elliot_segments(segments: list[dict]) -> str:
    """Pull out segments where Elliot is speaking."""
    parts = []
    for seg in segments:
        speaker = seg.get("speaker", "").strip()
        if speaker in ELLIOT_SPEAKERS:
            content = seg.get("content", "").strip()
            if content:
                parts.append(content)
    return " ".join(parts)


def _estimate_hours(sentence: str) -> float:
    """Estimate hours for a task based on keyword signals."""
    s = sentence.lower()
    if any(kw in s for kw in QUICK_KEYWORDS):
        return 0.5
    if any(kw in s for kw in MEETING_KEYWORDS):
        return 1.0
    if any(kw in s for kw in PROJECT_KEYWORDS):
        return 2.0
    if "all day" in s or "full day" in s or "most of the day" in s:
        return 6.0
    if "half day" in s or "half a day" in s or "this morning" in s or "this afternoon" in s:
        return 3.0
    return 1.0


def _split_into_sentences(text: str) -> list[str]:
    """Split text into rough sentences."""
    # Split on sentence-ending punctuation, keeping reasonable chunks
    raw = re.split(r"(?<=[.!?])\s+", text)
    sentences = []
    for s in raw:
        s = s.strip()
        if len(s) > 20:
            sentences.append(s)
    return sentences


def extract_tasks_heuristic(elliot_text: str) -> list[dict]:
    """Extract tasks from Elliot's segments using pattern matching."""
    if not elliot_text or len(elliot_text) < 30:
        return []

    sentences = _split_into_sentences(elliot_text)
    tasks = []
    seen = set()

    for sentence in sentences:
        s_lower = sentence.lower()
        # Check if this sentence contains a task signal
        if not any(signal in s_lower for signal in TASK_SIGNALS):
            continue

        # Clean up the sentence into a task description
        desc = sentence.strip()
        # Truncate long sentences
        if len(desc) > 150:
            desc = desc[:147] + "..."

        # Deduplicate similar tasks (first 40 chars)
        key = desc[:40].lower()
        if key in seen:
            continue
        seen.add(key)

        hours = _estimate_hours(desc)
        tasks.append({"description": desc, "hours": hours})

    return tasks


async def _ollama_available() -> bool:
    """Check if Ollama is reachable."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://localhost:11434/api/tags", timeout=2.0)
            return resp.status_code == 200
    except Exception:
        return False


TASK_EXTRACTION_PROMPT = """You are a time-tracking assistant. Given a person's spoken update from a meeting transcript, extract their work tasks.

For each task, output a JSON array of objects with:
- "description": one sentence describing what was done or is being worked on
- "hours": estimated hours spent (float, use 0.5 for quick tasks, 1-2 for meetings, 2-4 for project work, 4-8 for full-day efforts)

Only extract concrete work tasks. Skip small talk, questions to others, and non-work discussion.
If there are no work tasks, return an empty array: []

Respond with ONLY the JSON array, no other text.

Meeting transcript excerpt (speaker's segments only):
{text}"""


async def extract_tasks_via_ollama(elliot_text: str) -> list[dict]:
    """Use local Ollama to extract tasks. Falls back to heuristic if unavailable."""
    if not elliot_text or len(elliot_text) < 30:
        return []

    text = elliot_text[:3000]
    prompt = TASK_EXTRACTION_PROMPT.format(text=text)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                OLLAMA_URL,
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
                timeout=60.0,
            )
            if resp.status_code != 200:
                return []
            body = resp.json()
            raw = body.get("response", "").strip()
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if match:
                tasks = json.loads(match.group())
                return [
                    {"description": t.get("description", ""), "hours": float(t.get("hours", 0))}
                    for t in tasks
                    if t.get("description")
                ]
    except Exception:
        pass
    return []


def _get_hf_token() -> str | None:
    """Read HF token from environment or cache."""
    import os
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        return token
    # Check common file locations
    for p in ["~/.cache/huggingface/token", "~/.huggingface/token"]:
        fp = Path(p).expanduser()
        if fp.exists():
            return fp.read_text().strip()
    return None


async def _hf_inference_available() -> bool:
    """Check if HF Inference API is reachable with valid permissions."""
    token = _get_hf_token()
    if not token:
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                HF_INFERENCE_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": HF_MODEL,
                    "messages": [{"role": "user", "content": "Say OK"}],
                    "max_tokens": 5,
                },
                timeout=10.0,
            )
            return resp.status_code == 200
    except Exception:
        return False


async def extract_tasks_via_hf(elliot_text: str) -> list[dict]:
    """Use HuggingFace Inference API to extract tasks."""
    if not elliot_text or len(elliot_text) < 30:
        return []

    token = _get_hf_token()
    if not token:
        return []

    text = elliot_text[:3000]
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                HF_INFERENCE_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": HF_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Extract work tasks from this employee's meeting update. "
                                "Output ONLY a JSON array of objects with "
                                '"description" (one professional sentence for a timesheet) '
                                'and "hours" (float: 0.5 quick, 1.0 meeting, 2.0 project work, '
                                "4.0 half-day). No markdown, no explanation, just the JSON array."
                            ),
                        },
                        {"role": "user", "content": f"MEETING UPDATE:\n{text}"},
                    ],
                    "max_tokens": 512,
                    "temperature": 0.1,
                },
                timeout=30.0,
            )
            if resp.status_code != 200:
                return []
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if match:
                tasks = json.loads(match.group())
                return [
                    {"description": t.get("description", ""), "hours": float(t.get("hours", 0))}
                    for t in tasks
                    if t.get("description")
                ]
    except Exception as e:
        print(f"    HF inference failed: {e}")
    return []


async def extract_tasks(elliot_text: str, use_ollama: bool, use_hf: bool) -> list[dict]:
    """Extract tasks — Ollama first, then HF Inference, then heuristic."""
    if use_ollama:
        tasks = await extract_tasks_via_ollama(elliot_text)
        if tasks:
            return tasks
    if use_hf:
        tasks = await extract_tasks_via_hf(elliot_text)
        if tasks:
            return tasks
    return extract_tasks_heuristic(elliot_text)


PERSONAL_KEYWORDS = [
    "apartment", "moving", "lease", "landlord", "rent",
    "relationship", "dating", "wedding", "divorce",
    "finances", "mortgage", "loan", "credit",
    "doctor", "dentist", "medical", "appointment",
    "vacation", "holiday", "travel plan",
    "family", "kids", "daycare",
    "gym", "workout", "diet",
]


def sanitize_filename(title: str) -> str:
    """Turn a title into a filesystem-safe string."""
    s = title.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "_", s)
    s = s.strip("_")
    return s[:80] if s else "untitled"


def format_duration(ms: int | None) -> str:
    if not ms:
        return "0s"
    seconds = ms // 1000
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def generate_tags(title: str, transcript_preview: str) -> list[str]:
    """Scan title and first 500 chars of transcript for keyword matches."""
    search_text = (title + " " + transcript_preview[:500]).lower()
    tags = []
    for tag, keywords in TAG_KEYWORDS.items():
        if any(kw in search_text for kw in keywords):
            tags.append(tag)
    return sorted(set(tags))


def classify_type(title: str, transcript_text: str, duration_ms: int | None) -> str:
    """Classify as work, personal, or trivial."""
    # Trivial: under 30 seconds or minimal content
    if duration_ms is not None and duration_ms < 30_000:
        return "trivial"
    if len(transcript_text.strip()) < 50:
        return "trivial"

    # Personal: check for personal keywords
    search_text = (title + " " + transcript_text[:1000]).lower()
    personal_hits = sum(1 for kw in PERSONAL_KEYWORDS if kw in search_text)
    if personal_hits >= 2:
        return "personal"

    return "work"


def extract_speakers(segments: list[dict]) -> list[str]:
    """Extract unique speaker names from transcript segments."""
    speakers = []
    seen = set()
    for seg in segments:
        speaker = seg.get("speaker", "").strip()
        if speaker and speaker not in seen:
            seen.add(speaker)
            speakers.append(speaker)
    return speakers


def build_transcript_text(segments: list[dict]) -> str:
    """Build readable transcript from segments."""
    lines = []
    for seg in segments:
        speaker = seg.get("speaker", "").strip()
        content = seg.get("content", "").strip()
        if not content:
            continue
        if speaker:
            lines.append(f"**{speaker}:** {content}")
        else:
            lines.append(content)
    return "\n\n".join(lines)


def format_tasks_section(tasks: list[dict]) -> str:
    """Format tasks as a markdown table."""
    if not tasks:
        return "No tasks extracted"
    lines = [
        "| Describe what you did | Hours |",
        "|----------------------|-------|",
    ]
    for t in tasks:
        desc = t["description"].replace("|", "\\|")
        hours = f"{t['hours']:.1f}"
        lines.append(f"| {desc} | {hours} |")
    total = sum(t["hours"] for t in tasks)
    lines.append(f"| **Total** | **{total:.1f}** |")
    return "\n".join(lines)


def write_markdown(
    file_id: str,
    title: str,
    date_str: str,
    duration: str,
    device: str,
    speakers: list[str],
    tags: list[str],
    file_type: str,
    tasks: list[dict],
    summary_text: str,
    transcript_text: str,
    output_path: Path,
) -> None:
    speakers_yaml = ", ".join(speakers) if speakers else ""
    tags_yaml = ", ".join(tags) if tags else ""

    # Escape quotes in title for YAML
    safe_title = title.replace('"', '\\"')
    tasks_section = format_tasks_section(tasks)

    content = f"""---
id: {file_id}
title: "{safe_title}"
date: {date_str}
duration: "{duration}"
device: {device}
speakers: [{speakers_yaml}]
tags: [{tags_yaml}]
type: {file_type}
---

## Tasks (Elliot)

{tasks_section}

## Summary

{summary_text}

## Transcript

{transcript_text}
"""
    output_path.write_text(content, encoding="utf-8")


async def process_file(client: PlaudClient, file: dict, results: list[dict]) -> None:
    """Process a single file: fetch transcript and summary, write markdown."""
    file_id = file.get("id", "")
    title = file.get("filename", "Untitled")
    start_time = file.get("start_time")
    duration_ms = file.get("duration")

    if not file_id:
        print(f"  SKIP: no file ID for '{title}'")
        return

    # Parse date
    if start_time:
        dt = datetime.fromtimestamp(start_time / 1000, tz=UTC)
        date_str = dt.strftime("%Y-%m-%d")
    else:
        date_str = "unknown"

    duration = format_duration(duration_ms)
    print(f"  Processing: {title} ({date_str}, {duration})")

    # Fetch transcript
    segments: list[dict] = []
    transcript_text = ""
    try:
        data = await client.get_transcript(file_id)
        if isinstance(data, list):
            segments = data
            transcript_text = build_transcript_text(segments)
        else:
            transcript_text = str(data)
    except Exception as e:
        transcript_text = f"*Transcript unavailable: {e}*"
        print(f"    No transcript: {e}")

    # Fetch summary
    summary_text = "No summary available"
    try:
        summary_data = await client.get_summary(file_id)
        ai_content = summary_data.get("ai_content", "")
        if ai_content:
            summary_text = ai_content
    except Exception:
        pass

    # Extract metadata
    speakers = extract_speakers(segments)
    tags = generate_tags(title, transcript_text)
    file_type = classify_type(title, transcript_text, duration_ms)
    device = detect_device(file)

    # Extract tasks from Elliot's segments (skip personal/trivial)
    tasks: list[dict] = []
    if file_type == "work" and segments:
        elliot_text = extract_elliot_segments(segments)
        if elliot_text:
            tasks = await extract_tasks(elliot_text, use_ollama=_USE_OLLAMA, use_hf=_USE_HF)
            if tasks:
                print(f"    Extracted {len(tasks)} tasks ({sum(t['hours'] for t in tasks):.1f}h)")

    # Write markdown
    safe_name = sanitize_filename(title)
    filename = f"{date_str}_{safe_name}.md"
    output_path = TRANSCRIPTS_DIR / filename

    write_markdown(
        file_id=file_id,
        title=title,
        date_str=date_str,
        duration=duration,
        device=device,
        speakers=speakers,
        tags=tags,
        file_type=file_type,
        tasks=tasks,
        summary_text=summary_text,
        transcript_text=transcript_text,
        output_path=output_path,
    )

    # Build task summary for index
    task_desc = "; ".join(t["description"] for t in tasks) if tasks else "-"
    task_hours = f"{sum(t['hours'] for t in tasks):.1f}" if tasks else "-"

    results.append({
        "file_id": file_id,
        "title": title,
        "date": date_str,
        "duration": duration,
        "device": device,
        "speakers": speakers,
        "tags": tags,
        "type": file_type,
        "task_desc": task_desc,
        "task_hours": task_hours,
        "filename": filename,
    })
    print(f"    Wrote: {filename}")


def write_index(results: list[dict]) -> None:
    """Write INDEX.md with a table of all files sorted by date descending."""
    results.sort(key=lambda r: r["date"], reverse=True)

    lines = [
        "# Plaud Transcripts Index",
        "",
        f"Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Total files: {len(results)}",
        "",
        "| Date | Title | Duration | Device | Type | Describe what you did | Hours | Tags |",
        "|------|-------|----------|--------|------|-----------------------|-------|------|",
    ]

    for r in results:
        link = f"[{r['title']}]({r['filename']})"
        tags = ", ".join(r["tags"]) if r["tags"] else "-"
        device = r.get("device", "?")
        task_desc = r.get("task_desc", "-")
        task_hours = r.get("task_hours", "-")
        lines.append(
            f"| {r['date']} | {link} | {r['duration']} | {device} | {r['type']} | {task_desc} | {task_hours} | {tags} |"
        )

    lines.append("")
    index_path = TRANSCRIPTS_DIR / "INDEX.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote INDEX.md with {len(results)} entries")


_USE_OLLAMA = False  # Set in main() after detection
_USE_HF = False


async def main() -> None:
    global _USE_OLLAMA, _USE_HF

    print(f"Fetching Plaud files from the last {DAYS} days...")
    client = PlaudClient()

    if not client.is_available():
        print("ERROR: Plaud Desktop not available. Is it installed and signed in?")
        sys.exit(1)

    # Detect LLM providers for task extraction (priority: Ollama > HF > heuristic)
    _USE_OLLAMA = await _ollama_available()
    if _USE_OLLAMA:
        print(f"Ollama detected — using {OLLAMA_MODEL} for task extraction")
    else:
        _USE_HF = await _hf_inference_available()
        if _USE_HF:
            print(f"HuggingFace Inference detected — using {HF_MODEL} for task extraction")
        else:
            print("No LLM available — using heuristic task extraction")

    files = await client.get_recent_files(days=DAYS)
    print(f"Found {len(files)} files\n")

    if not files:
        print("No files found in the specified period.")
        return

    results: list[dict] = []

    # Process files with concurrency limit to avoid hammering the API
    sem = asyncio.Semaphore(3)

    async def bounded_process(file: dict) -> None:
        async with sem:
            await process_file(client, file, results)

    await asyncio.gather(*[bounded_process(f) for f in files])

    write_index(results)
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
