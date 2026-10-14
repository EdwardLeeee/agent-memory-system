"""Weekly rating of what the prompt hook injected, from the user's own sessions.

Reads Claude Code transcripts read-only, samples the user's own recent
messages, and asks the user, one at a time in the terminal, whether what was
injected helped. Ratings stay machine-local under the config home: each keeps
an 80-character excerpt of the message and nothing more, never enters Git,
and is never shown to a model.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
from typing import Any, Callable, Iterable

from .storage import atomic_write_text

INJECTED_SAMPLE = 10
NOT_INJECTED_SAMPLE = 2
NOT_INJECTED_MIN_CHARS = 20
RESAMPLE_AFTER_WEEKS = 4
EXCERPT_CHARS = 80
MESSAGE_CHARS = 300
VALUE_CHARS = 120
HUMAN_PROMPT_SOURCES = {"typed", "suggestion_accepted", "queued"}

_RECORD_HEADING_RE = re.compile(r"^### (\S+)\s*$")
_LESSON_LINE_RE = re.compile(r"^- ((?:lesson|federation)\.\S+?)(?: \([^)]*\))?: (.*)$")


def iso_week(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def ratings_dir(config_root: Path) -> Path:
    return config_root / "ratings"


def _message_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(part, dict) and part.get("type") == "tool_result" for part in content):
            return None
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return None


def _is_human_message(entry: dict[str, Any]) -> str | None:
    """The text of a message the user sent themselves, or None."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return None
    if entry.get("isCompactSummary"):
        return None
    origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
    if origin.get("kind") != "human":
        return None
    if entry.get("promptSource") not in HUMAN_PROMPT_SOURCES:
        return None
    text = _message_text((entry.get("message") or {}).get("content"))
    if not text or not text.strip():
        return None
    if text.lstrip().startswith("<command-"):
        return None
    return text.strip()


def parse_injection(text: str) -> list[dict[str, str]]:
    """Records an injected context pack showed, with the value line it showed."""
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        heading = _RECORD_HEADING_RE.match(line)
        if heading:
            current = {"id": heading.group(1), "value": ""}
            records.append(current)
            continue
        lesson = _LESSON_LINE_RE.match(line)
        if lesson:
            current = None
            records.append({"id": lesson.group(1), "value": lesson.group(2).strip()})
            continue
        if current is not None and line.startswith("- value: ") and not current["value"]:
            current["value"] = line[len("- value: ") :].strip()
        elif current is not None and line.startswith("- outcome: ") and not current["value"]:
            # A routed pack shows pulled evidence as a pointer block.
            current["value"] = line[len("- outcome: ") :].strip()
    seen: set[str] = set()
    unique = []
    for record in records:
        if record["id"] not in seen:
            seen.add(record["id"])
            unique.append(record)
    return unique


INJECTING_HOOK_EVENTS = frozenset({"UserPromptSubmit", "PreToolUse"})


def _attachment_text(entry: dict[str, Any]) -> str | None:
    attachment = entry.get("attachment") if entry.get("type") == "attachment" else None
    if not isinstance(attachment, dict):
        return None
    if attachment.get("type") != "hook_additional_context":
        return None
    # PreToolUse injections are stored the same way (verified with claude
    # --debug, 2026-10-10: hookName "PreToolUse:Bash"). read_transcript drops
    # them, since they follow an assistant turn; citation_signal counts them.
    if attachment.get("hookEvent") not in INJECTING_HOOK_EVENTS:
        return None
    content = attachment.get("content")
    if isinstance(content, list):
        return "\n".join(str(part) for part in content)
    return str(content or "")


_TYPE_ASSISTANT_RE = re.compile(r'"type":\s*"assistant"')
_TYPE_USER_RE = re.compile(r'"type":\s*"user"')


def iter_transcript_events(path: Path) -> Iterable[tuple[str, Any]]:
    """Stream the events this module needs from one transcript, line by line.

    Transcripts reach hundreds of megabytes each; reading one whole file into
    memory (as text and then as a list of lines) exhausted the machine's RAM
    and crashed it on 2026-10-07. Lines are prefiltered by plain substring
    before any JSON parsing, so large tool results are never parsed at all.
    Yields ("name", str), ("human", (entry, text)), ("prompt", None),
    ("injection", text), and ("assistant", raw_line).
    """
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            if '"hook_additional_context"' in line:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = _attachment_text(entry) if isinstance(entry, dict) else None
                if text is not None:
                    yield "injection", text
            elif _TYPE_ASSISTANT_RE.search(line) and not _TYPE_USER_RE.search(line):
                yield "assistant", line
            elif '"promptSource"' in line and _TYPE_USER_RE.search(line):
                # A prompt of some origin; tool results carry no promptSource
                # and are skipped without being parsed.
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                text = _is_human_message(entry)
                if text is not None:
                    yield "human", (entry, text)
                elif _message_text((entry.get("message") or {}).get("content")) is not None:
                    yield "prompt", None
            elif '"agent-name"' in line or '"custom-title"' in line:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    name = entry.get("agentName") or entry.get("customTitle")
                    if name:
                        yield "name", str(name)


def read_transcript(path: Path) -> list[dict[str, Any]]:
    """User messages in one transcript, each with what the prompt hook injected."""
    messages: list[dict[str, Any]] = []
    session_name = ""
    current: dict[str, Any] | None = None
    for kind, payload in iter_transcript_events(path):
        if kind == "name":
            session_name = session_name or payload
        elif kind == "human":
            entry, text = payload
            current = {
                "session_id": str(entry.get("sessionId") or entry.get("session_id") or ""),
                "cwd": str(entry.get("cwd") or ""),
                "timestamp": str(entry.get("timestamp") or ""),
                "text": text,
                "records": [],
                "injected": False,
            }
            messages.append(current)
        elif kind in ("assistant", "prompt"):
            # Any other prompt (a peer's message, a notification) starts a
            # turn of its own; what its hook injected is not the user's.
            current = None
        elif kind == "injection" and current is not None:
            current["injected"] = True
            current["records"].extend(parse_injection(payload))
    for message in messages:
        message["session_name"] = session_name
    return messages


def _parse_time(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def message_key(message: dict[str, Any]) -> str:
    raw = f"{message['session_id']}|{message['timestamp']}|{message['text'][:EXCERPT_CHARS]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_ratings(config_root: Path, weeks: Iterable[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for week in weeks:
        path = ratings_dir(config_root) / f"{week}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, list):
            entries.extend(item for item in data if isinstance(item, dict))
    return entries


def recent_weeks(now: datetime, count: int) -> list[str]:
    return [iso_week(now - timedelta(weeks=offset)) for offset in range(count)]


_MEMORY_REPO = Path.home() / "agent-memory"
_PROBE_RE = re.compile(r"(^|\s|/)memoryctl\s|\"hook_event_name\"")


def _is_probe(message: dict[str, Any]) -> bool:
    """A verification probe typed in the memory repo, not real work: the
    message itself is a memoryctl command or a hook's JSON payload."""
    cwd = message.get("cwd", "")
    if not (cwd == str(_MEMORY_REPO) or cwd.startswith(str(_MEMORY_REPO) + os.sep)):
        return False
    return bool(_PROBE_RE.search(message["text"]))


def collect_candidates(
    projects_dir: Path, now: datetime, days: int = 7
) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=days)
    candidates: list[dict[str, Any]] = []
    for path in sorted(projects_dir.glob("**/*.jsonl")):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                continue
        except OSError:
            continue
        for message in read_transcript(path):
            if message["session_id"].startswith("probe") or _is_probe(message):
                continue
            moment = _parse_time(message["timestamp"])
            if moment is None or not (cutoff <= moment <= now):
                continue
            candidates.append(message)
    return candidates


def sample(
    candidates: list[dict[str, Any]],
    already_rated: set[str],
    seed: str,
    injected_count: int = INJECTED_SAMPLE,
    plain_count: int = NOT_INJECTED_SAMPLE,
) -> list[dict[str, Any]]:
    """Injected messages spread across sessions, then a few plain ones."""
    rng = random.Random(seed)
    fresh = [m for m in candidates if message_key(m) not in already_rated]
    injected = [m for m in fresh if m["injected"] and m["records"]]
    plain = [m for m in fresh if not m["injected"] and len(m["text"]) >= NOT_INJECTED_MIN_CHARS]
    by_session: dict[str, list[dict[str, Any]]] = {}
    for message in injected:
        by_session.setdefault(message["session_id"], []).append(message)
    for messages in by_session.values():
        rng.shuffle(messages)
    sessions = sorted(by_session)
    rng.shuffle(sessions)
    chosen: list[dict[str, Any]] = []
    while len(chosen) < injected_count and any(by_session[s] for s in sessions):
        for session in sessions:
            if by_session[session] and len(chosen) < injected_count:
                chosen.append(by_session[session].pop())
    rng.shuffle(plain)
    return chosen + plain[:plain_count]


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


INJECTED_CHOICES = {"1": "useful", "2": "useless", "3": "harmful", "s": "skipped"}
PLAIN_CHOICES = {"1": "no-difference", "2": "should-have-remembered", "s": "skipped"}


def run_interactive(
    items: list[dict[str, Any]],
    ask: Callable[[str], str],
    say: Callable[[str], None],
    now: datetime,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for number, message in enumerate(items, start=1):
        say("")
        say(f"[{number}/{len(items)}] {message['session_name'] or message['cwd']}")
        say(_clip(message["text"], MESSAGE_CHARS))
        if message["injected"]:
            say("被放進去的記憶：")
            for record in message["records"]:
                say(f"  - {record['id']}: {_clip(record['value'], VALUE_CHARS)}")
            choices, prompt = INJECTED_CHOICES, "1 有用　2 沒用　3 礙事　s 略過 > "
        else:
            say("（這則沒有注入任何記憶）")
            choices, prompt = PLAIN_CHOICES, "1 沒差　2 它應該記得某件事　s 略過 > "
        answer = ""
        while answer not in choices:
            answer = ask(prompt).strip().casefold()
        result = {
            "time": now.isoformat(),
            "key": message_key(message),
            "session_id": message["session_id"],
            "cwd": message["cwd"],
            "record_ids": [record["id"] for record in message["records"]],
            "injected": message["injected"],
            "rating": choices[answer],
            "excerpt": message["text"][:EXCERPT_CHARS],
        }
        if result["rating"] == "should-have-remembered":
            note = ask("它應該記得什麼？（可留空）> ").strip()
            if note:
                result["note"] = note[:200]
        results.append(result)
    return results


def save_ratings(config_root: Path, week: str, results: list[dict[str, Any]]) -> Path:
    directory = ratings_dir(config_root)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / f"{week}.json"
    existing = load_ratings(config_root, [week])
    atomic_write_text(path, json.dumps(existing + results, ensure_ascii=False, indent=2) + "\n")
    os.chmod(path, 0o600)
    return path


def summarize(config_root: Path, now: datetime) -> dict[str, Any]:
    weeks = recent_weeks(now, RESAMPLE_AFTER_WEEKS + 1)
    per_week = []
    for week in weeks:
        entries = [e for e in load_ratings(config_root, [week]) if e.get("injected")]
        judged = [e for e in entries if e.get("rating") in ("useful", "useless", "harmful")]
        count = len(judged)
        per_week.append(
            {
                "week": week,
                "rated": count,
                "useful_rate": round(sum(e["rating"] == "useful" for e in judged) / count, 3) if count else None,
                "harmful_rate": round(sum(e["rating"] == "harmful" for e in judged) / count, 3) if count else None,
            }
        )
    flagged: dict[str, int] = {}
    for entry in load_ratings(config_root, weeks):
        if entry.get("rating") in ("useless", "harmful"):
            for record_id in entry.get("record_ids", []):
                flagged[record_id] = flagged.get(record_id, 0) + 1
    return {
        "weeks": per_week,
        "flagged_records": sorted(
            ({"id": rid, "count": n} for rid, n in flagged.items() if n >= 3),
            key=lambda item: (-item["count"], item["id"]),
        ),
    }


def already_rated_keys(config_root: Path, now: datetime) -> set[str]:
    return {
        str(entry.get("key"))
        for entry in load_ratings(config_root, recent_weeks(now, RESAMPLE_AFTER_WEEKS))
        if entry.get("key")
    }
