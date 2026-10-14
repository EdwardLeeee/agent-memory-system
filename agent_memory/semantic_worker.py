from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys

from .semantic import embed_text, query_fingerprint, query_index


def _available_memory_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def main() -> int:
    available = _available_memory_bytes()
    if available is not None and available < 384 * 1024 * 1024:
        print("semantic worker skipped: low available memory", file=sys.stderr)
        return 3
    try:
        payload = json.load(sys.stdin)
        root = Path(os.environ["AGENT_MEMORY_HOME"]).expanduser().resolve()
        operation = str(payload.get("operation", "query"))
        if operation == "query":
            query = str(payload["query"])
            limit = max(1, min(64, int(payload.get("limit", 64))))
            result = {
                "candidates": query_index(root, query, limit),
                "fingerprint": query_fingerprint(root),
            }
        elif operation == "embed":
            raw = embed_text(root, str(payload["text"]))
            result = {"vector_b64": base64.b64encode(raw).decode("ascii")}
        else:
            raise ValueError("unsupported semantic worker operation")
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
        print(f"semantic worker failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
