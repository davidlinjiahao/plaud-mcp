"""One-shot smoke test: transcribe a 20s Plaud clip, print result."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plaud_mcp.audio.transcribe import transcribe  # noqa: E402

CLIP = Path(__file__).parent / "smoke_20s.wav"


def main() -> int:
    if not CLIP.exists():
        print(f"FAIL: {CLIP} missing", file=sys.stderr)
        return 1
    print(f"Transcribing {CLIP} ...", flush=True)
    t0 = time.monotonic()
    result = transcribe(CLIP, language=None)
    dt = time.monotonic() - t0
    out = {
        "duration_seconds": result.duration_seconds,
        "language": result.language,
        "model_version": result.model_version,
        "word_count": len(result.words),
        "first_10_words": " ".join(w.text for w in result.words[:10]),
        "wall_clock_s": round(dt, 2),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
