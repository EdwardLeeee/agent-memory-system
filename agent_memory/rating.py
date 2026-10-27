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
from .usage import DETECTOR

INJECTED_SAMPLE = 10
NOT_INJECTED_SAMPLE = 2
NOT_INJECTED_MIN_CHARS = 20
RESAMPLE_AFTER_WEEKS = 4
EXCERPT_CHARS = 80
MESSAGE_CHARS = 300
VALUE_CHARS = 120
# What the look-only keys show (spec 記憶評分：看全文與 AI 回答 v1). The AI's
# reply is kept while the transcript streams past, up to REPLY_KEEP_CHARS, so
# no transcript is read a second time.
REPLY_KEEP_CHARS = 4000
REPLY_SHOW_CHARS = 800
MESSAGE_FULL_CHARS = 2000
DETAIL_TOTAL_CHARS = 3000
DETAIL_SECTION_CHARS = 400
DETAIL_SOURCE_CHARS = 600
DETAIL_SECTIONS = {
    "failure": ("Symptom", "Root Cause", "Verified Fix", "Retry When"),
    "experience": ("Situation", "Expectation", "Observed Outcome", "Applicability"),
    "decision": ("Problem", "Decision", "Constraints"),
    "fact": ("Value",),
    "preference": ("Value",),
    "lesson": ("Finding", "Rule", "Applies To"),
}
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
_TEXT_PART_RE = re.compile(r'"type":\s*"text"')
_TYPE_USER_RE = re.compile(r'"type":\s*"user"')


def iter_transcript_events(path: Path) -> Iterable[tuple[str, Any]]:
    """Stream the events this module needs from one transcript, line by line.

    Transcripts reach hundreds of megabytes each; reading one whole file into
    memory (as text and then as a list of lines) exhausted the machine's RAM
    and crashed it on 2026-10-07. Lines are prefiltered by plain substring
    before any JSON parsing, so large tool results are never parsed at all.
    Yields ("name", str), ("human", (entry, text)), ("prompt", text),
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
                elif (other := _message_text((entry.get("message") or {}).get("content"))) is not None:
                    yield "prompt", other
            elif '"agent-name"' in line or '"custom-title"' in line:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    name = entry.get("agentName") or entry.get("customTitle")
                    if name:
                        yield "name", str(name)


def _reply_text(line: str) -> str:
    """The text parts of one assistant line; tool calls are left out."""
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return ""
    content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text"
    )


# What the agent wrote in one assistant line is read up to this many
# characters, as replay reads it: a file written whole (4M characters took
# 469 MB) says no more about which memory it used (review 2, item 16).
OUTPUT_CHARS = 20_000


def _output_text(line: str) -> str:
    """What the agent wrote in one assistant line: its text and the input
    of its tool calls, the first OUTPUT_CHARS characters."""
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return ""
    content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
    if isinstance(content, str):
        return content[:OUTPUT_CHARS]
    parts: list[str] = []
    size = 0
    for part in content if isinstance(content, list) else []:
        if size >= OUTPUT_CHARS:
            break
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            values = [str(part.get("text", ""))]
        elif part.get("type") == "tool_use" and isinstance(part.get("input"), dict):
            values = [value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                      for value in part["input"].values()]
        else:
            continue
        for value in values:
            parts.append(value[: OUTPUT_CHARS - size])
            size += len(parts[-1]) + 1
    return "\n".join(parts)[:OUTPUT_CHARS]


def _spoken_text(line: str) -> str:
    """What the agent said in one assistant line: its text, and what a
    SendMessage or AskUserQuestion sends; where an id it writes counts as
    named (usage rule v5), not the input of Write, Edit or Bash."""
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return ""
    content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
    if isinstance(content, str):
        return content[:OUTPUT_CHARS]
    parts = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            parts.append(str(part.get("text", "")))
        elif part.get("type") == "tool_use" and part.get("name") in ("SendMessage", "AskUserQuestion") \
                and isinstance(part.get("input"), dict):
            parts.extend(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                         for value in part["input"].values())
    return "\n".join(parts)[:OUTPUT_CHARS]


_CWD_RE = re.compile(r'"cwd":\s*"([^"]*)"')


def read_transcript(path: Path, detector: Any = None, home: Path | None = None) -> list[dict[str, Any]]:
    """User messages in one transcript, each with what the prompt hook
    injected and the AI's reply up to the next prompt (its text only, at
    most REPLY_KEEP_CHARS). With a usage.UsageDetector, also which injected
    records the agent used before the next message the user typed, and how:
    "used" maps an id to ("id" | "literal" | "phrase", what matched) (spec
    實際使用回饋改善 F12)."""
    from . import turns, usage

    messages: list[dict[str, Any]] = []
    session_name = ""
    current: dict[str, Any] | None = None
    replying: dict[str, Any] | None = None
    # The message whose records may still be used, until the next one the
    # user types; messages from other sessions in between do not end it.
    window: dict[str, Any] | None = None
    pending: dict[str, dict[str, Any]] = {}
    echo = usage.Echo()
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
                "reply": "",
                "replied": False,
                "used": {},
                # Asked or worked on in the memory system's own folders (A1 ruling).
                "memory_repo": usage.in_memory_repo(str(entry.get("cwd") or ""), home),
            }
            messages.append(current)
            replying = window = current
            pending, echo = {}, usage.Echo()
            echo.add(text)
        elif kind == "prompt":
            # Any other prompt (a peer's message, a notification) starts a
            # turn of its own; what its hook injected is not the user's. What
            # it said is not the record's either: the agent repeating it is no
            # use, as the weekly measure counts it (review 2, item 15).
            current = replying = None
            if window is not None:
                echo.add(str(payload or ""))
        elif kind == "assistant":
            current = None
            if window is not None and not window["memory_repo"]:
                where = _CWD_RE.search(payload)
                window["memory_repo"] = bool(where) and usage.in_memory_repo(where.group(1), home)
            if replying is not None:
                text = _reply_text(payload).strip() if _TEXT_PART_RE.search(payload) else ""
                if text and len(replying["reply"]) < REPLY_KEEP_CHARS:
                    replying["replied"] = True
                    joined = f"{replying['reply']}\n{text}" if replying["reply"] else text
                    replying["reply"] = joined[:REPLY_KEEP_CHARS]
            waiting = {record_id: features for record_id, features in pending.items() if record_id not in window["used"]} if window else {}
            if waiting:
                normalized = usage.normalize(_output_text(payload))
                spoken = usage.normalize(_spoken_text(payload))
                output_grams: list[set[str] | None] = [None]
                for record_id, features in waiting.items():
                    found = usage.match(features, normalized, echo, output_grams, spoken)
                    if found:
                        window["used"][record_id] = found
        elif kind == "injection" and current is not None:
            current["injected"] = True
            current["records"].extend(parse_injection(payload))
            if detector is not None:
                for record_id, block in turns.pack_records(payload):
                    pending.setdefault(record_id, detector.features(record_id, block))
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
    projects_dir: Path, now: datetime, days: int = 7, detector: Any = None, home: Path | None = None
) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=days)
    candidates: list[dict[str, Any]] = []
    for path in sorted(projects_dir.glob("**/*.jsonl")):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                continue
        except OSError:
            continue
        for message in read_transcript(path, detector, home):
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
INJECTED_PROMPT = "1 有用　2 沒用　3 礙事　s 略過｜d 記憶全文　r AI 回答　m 完整訊息 > "
PLAIN_PROMPT = "1 沒差　2 它應該記得某件事　s 略過｜r AI 回答　m 完整訊息 > "


def _section_lines(metadata: dict[str, Any], body: str) -> list[str]:
    from .core import _extract_section

    lines = []
    for name in DETAIL_SECTIONS.get(str(metadata.get("kind", "")), ("Value",)):
        text = _extract_section(body, name)
        if text:
            lines.append(f"  {name}: {_clip(text, DETAIL_SECTION_CHARS)}")
    return lines or [f"  {_clip(body, DETAIL_SECTION_CHARS)}"]


def _index_entry(root: Path, record_id: str) -> dict[str, Any] | None:
    from . import core

    try:
        connection = core._open_index(root, allow_reindex=False)
    except Exception:
        return None
    try:
        row = connection.execute("SELECT * FROM records WHERE record_id = ?", (record_id,)).fetchone()
        return core._index_row(row) if row is not None else None
    except Exception:
        return None
    finally:
        connection.close()


def _learned_line(root: Path, record_id: str, names: dict[str, str] | None) -> str:
    from . import learned

    try:
        found = learned.learned_in_for(root, record_id)
    except Exception:
        found = None
    return f"  學到時的對話：memoryctl search --id {record_id} --context{learned.session_label(found, names)}"


def record_detail(
    root: Path, record_id: str, asked_at: datetime | None, names: dict[str, str] | None = None
) -> list[str]:
    """One injected record as it reads now, looked up the way `memoryctl
    search --id` does (ledger file, then index, then archive); read only.
    A ledger record also says where to read the conversation it was learned
    in (spec 論文借鏡改善 A2)."""
    from . import core

    for path in sorted((root / "records").glob(f"*/{record_id}.md")):
        try:
            metadata, body = core.decode_record(path)
        except Exception:
            continue
        if metadata.get("status") != "active":
            continue
        version = metadata.get("version", 1)
        lines = [f"■ {record_id}（{metadata.get('kind')}，目前 v{version}）"]
        updated = _parse_time(str(metadata.get("updated_at") or ""))
        if asked_at is not None and updated is not None and updated > asked_at and str(version) != "1":
            lines.append(f"  這筆在 {updated.date().isoformat()} 更正過，以下是目前的版本")
        return lines + _section_lines(metadata, body) + [_learned_line(root, record_id, names)]
    item = _index_entry(root, record_id)
    origin = item["metadata"].get("origin") if item is not None else None
    # Only an entry derived from a project's own documents lives in the index
    # alone; a ledger record whose file is gone falls through to the archive.
    if item is not None and isinstance(origin, dict) and origin.get("source"):
        metadata, body = item["metadata"], item["body"]
        lines = [f"■ {record_id}（{metadata.get('kind')}，來自專案文件）"]
        lines.append(f"  來源：{origin.get('source', '-')}　路徑：{origin.get('path', '-')}　標題：{origin.get('anchor', '-')}")
        text = core._extract_section(body, "Value") or body
        return lines + [f"  {_clip(text, DETAIL_SOURCE_CHARS)}"]
    archived = []
    for path in (root / "archive").glob(f"*/{record_id}/*.md"):
        try:
            archived.append(core.decode_record(path))
        except Exception:
            continue
    if archived:
        metadata, body = max(archived, key=lambda pair: int(pair[0].get("version", 0) or 0))
        lines = [
            f"■ {record_id}（{metadata.get('kind')}）",
            f"  這筆已不存在；以下是找得到的最後版本（v{metadata.get('version', '?')}）",
        ]
        return lines + _section_lines(metadata, body)
    return [f"■ {record_id}", "  這筆已不存在：帳本、索引與 archive 都找不到"]


def memory_details(root: Path | None, message: dict[str, Any], names: dict[str, str] | None = None) -> list[str]:
    """`d`: every injected record in full, DETAIL_TOTAL_CHARS at most."""
    if root is None:
        return ["（沒有指定記憶的位置，看不到全文）"]
    asked_at = _parse_time(message["timestamp"])
    lines: list[str] = []
    used = 0
    records = message["records"]
    for index, record in enumerate(records):
        block = record_detail(root, record["id"], asked_at, names)
        size = sum(len(line) for line in block)
        if lines and used + size > DETAIL_TOTAL_CHARS:
            left = records[index:]
            lines.append(f"其餘 {len(left)} 筆略過，可用 memoryctl search --id <id> 看（{left[0]['id']} 起）")
            break
        if not lines and size > DETAIL_TOTAL_CHARS:
            block = [_clip("\n".join(block), DETAIL_TOTAL_CHARS)]
            size = DETAIL_TOTAL_CHARS
        lines += block
        used += size
    return lines


def reply_details(message: dict[str, Any]) -> list[str]:
    """`r`: what the AI answered, and which injected records it named."""
    if not message.get("replied"):
        lines = ["找不到這則之後的 AI 回覆"]
    else:
        reply = message["reply"]
        shown = reply if len(reply) <= REPLY_SHOW_CHARS else reply[: REPLY_SHOW_CHARS - 1] + "…"
        lines = ["AI 回答（前 800 字）：", shown]
    if message["records"]:
        used = message.get("used") or {}
        lines.append("AI 有沒有用到（系統判斷）：")
        for record in message["records"]:
            how, what = used.get(record["id"], (None, ""))
            state = "明寫 id" if how == "id" else f"內容相符：{_clip(what, 60)}" if how else "沒有用到"
            lines.append(f"  - {record['id']}：{state}")
    return lines


def message_details(message: dict[str, Any]) -> list[str]:
    """`m`: the user's whole message, MESSAGE_FULL_CHARS at most."""
    text = message["text"]
    return [text if len(text) <= MESSAGE_FULL_CHARS else text[: MESSAGE_FULL_CHARS - 1] + "…"]


def run_interactive(
    items: list[dict[str, Any]],
    ask: Callable[[str], str],
    say: Callable[[str], None],
    now: datetime,
    *,
    root: Path | None = None,
    save: Callable[[dict[str, Any]], None] | None = None,
    names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Ask each item in turn. d, r, and m only show more and ask again;
    each answer goes to `save` at once, so an interrupted round keeps what
    was answered (spec 記憶評分：看全文與 AI 回答 v1)."""
    results: list[dict[str, Any]] = []
    for number, message in enumerate(items, start=1):
        say("")
        say(f"[{number}/{len(items)}] {message['session_name'] or message['cwd']}")
        say(_clip(message["text"], MESSAGE_CHARS))
        if message["injected"]:
            say("被放進去的記憶：")
            for record in message["records"]:
                say(f"  - {record['id']}: {_clip(record['value'], VALUE_CHARS)}")
            choices, prompt = INJECTED_CHOICES, INJECTED_PROMPT
            looks = {"d": lambda m=message: memory_details(root, m, names)}
        else:
            say("（這則沒有注入任何記憶）")
            choices, prompt = PLAIN_CHOICES, PLAIN_PROMPT
            looks = {}
        looks.update({"r": lambda m=message: reply_details(m), "m": lambda m=message: message_details(m)})
        answer = ""
        while answer not in choices:
            answer = ask(prompt).strip().casefold()
            if answer in looks:
                for line in looks[answer]():
                    say(line)
        result = {
            "time": now.isoformat(),
            "key": message_key(message),
            "session_id": message["session_id"],
            "cwd": message["cwd"],
            "record_ids": [record["id"] for record in message["records"]],
            "injected": message["injected"],
            "rating": choices[answer],
            "excerpt": message["text"][:EXCERPT_CHARS],
            # Which memories the AI used, judged from what it wrote, so a
            # rating of the whole answer can be credited to the memory that
            # was used (spec 論文借鏡改善 A1, 實際使用回饋改善 F12): named by
            # id, and matched by content, apart, under the rule's version.
            "explicit_ids": sorted(record_id for record_id, (how, _) in (message.get("used") or {}).items() if how == "id"),
            "content_ids": sorted(record_id for record_id, (how, _) in (message.get("used") or {}).items() if how != "id"),
            "detector": DETECTOR,
            "memory_repo": bool(message.get("memory_repo")),
        }
        if result["rating"] == "should-have-remembered":
            note = ask("它應該記得什麼？（可留空）> ").strip()
            if note:
                result["note"] = note[:200]
        results.append(result)
        if save is not None:
            save(result)
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
