"""Where a record was learned: the session and moment that wrote each
version, and the conversation around that moment (spec 論文借鏡改善 A2).

New versions carry `learned_in` in their metadata (core.record and
core.supersede). Older ones are found again in the agents' transcripts by the
tool output the write printed (`Recorded <id>`, `Superseded <id> version N`)
and kept in the derived cache .cache/learned_in.json; the ledger is never
changed. Transcripts are streamed line by line and prefiltered by plain
substring before anything is parsed (failure.memory.doctor_read_whole_
transcripts_oom).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator

from .storage import atomic_write_text

CACHE_NAME = "learned_in.json"
CONTEXT_CHARS = 1000
_KINDS = "preference|fact|decision|experience|failure|task|lesson"
_ID = rf"(?:{_KINDS})\.[A-Za-z0-9_.-]*[A-Za-z0-9_]"
RECORDED_RE = re.compile(rf"Recorded ({_ID})")
SUPERSEDED_RE = re.compile(rf"Superseded ({_ID}) version (\d+)")
_TIMESTAMP_RE = re.compile(r'"timestamp":\s*"([^"]+)"')
_SESSION_RE = re.compile(r'"sessionId":\s*"([^"]+)"')
_UUID_TAIL_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


def default_claude_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def default_codex_dir() -> Path:
    return Path.home() / ".codex" / "sessions"


def _cache_path(root: Path) -> Path:
    return root / ".cache" / CACHE_NAME


def _is_tool_output(line: str, agent: str) -> bool:
    return '"tool_result"' in line if agent == "claude" else '_call_output"' in line


# A write's tool output follows the write within seconds. The window keeps a
# copy of the ledger written under the same id in another session (a trial
# run on a copy) from being taken for the real write.
WRITE_WINDOW = timedelta(minutes=10)


def _ledger_versions(root: Path) -> dict[str, dict[int, datetime | None]]:
    """Every ledger id, with when each of its versions was written."""
    from .core import decode_record

    versions: dict[str, dict[int, datetime | None]] = {}
    paths = [*(root / "records").glob("*/*.md"), *(root / "archive").glob("*/*/*.md")]
    for path in paths:
        try:
            metadata, _ = decode_record(path)
        except Exception:
            continue
        record_id = str(metadata.get("id") or "")
        if not record_id:
            continue
        try:
            version = int(metadata.get("version", 1) or 1)
        except (TypeError, ValueError):
            continue
        versions.setdefault(record_id, {})[version] = _time(str(metadata.get("updated_at") or ""))
    return versions


def _matches_write(event: dict[str, Any], versions: dict[str, dict[int, datetime | None]]) -> bool:
    known = versions.get(event["id"])
    if known is None:
        return False
    written = known.get(event["version"])
    seen = _time(event["at"]) if event["at"] else None
    if written is None or seen is None:
        return event["version"] in known or not known
    return written - timedelta(minutes=1) <= seen <= written + WRITE_WINDOW


def _codex_session(path: Path, line: str) -> str | None:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        entry = None
    if isinstance(entry, dict) and entry.get("type") == "session_meta":
        payload = entry.get("payload") or {}
        if payload.get("id"):
            return str(payload["id"])
    match = _UUID_TAIL_RE.search(path.name)
    return match.group(1) if match else None


def scan_transcript(path: Path, agent: str) -> Iterator[dict[str, Any]]:
    """Every record write a transcript's tool output reports."""
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    session = None
    with handle:
        for number, line in enumerate(handle):
            if agent == "codex" and number == 0:
                session = _codex_session(path, line)
            if ("Recorded " not in line and "Superseded " not in line) or not _is_tool_output(line, agent):
                continue
            stamp = _TIMESTAMP_RE.search(line)
            sid = _SESSION_RE.search(line) if agent == "claude" else None
            base = {
                "session_id": sid.group(1) if sid else (session or path.stem),
                "at": stamp.group(1) if stamp else "",
                "agent": agent,
            }
            for match in RECORDED_RE.finditer(line):
                yield {"id": match.group(1), "version": 1, **base}
            for match in SUPERSEDED_RE.finditer(line):
                yield {"id": match.group(1), "version": int(match.group(2)) + 1, **base}


def load_cache(root: Path) -> dict[str, Any]:
    try:
        data = json.loads(_cache_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    records = data.get("records") if isinstance(data.get("records"), dict) else {}
    return {"files": files, "records": records}


def backfill(
    root: Path,
    claude_dir: Path | None = None,
    codex_dir: Path | None = None,
    days: int | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Add every ledger record's writes found in the transcripts to the cache.
    A transcript unchanged since the last run (same mtime and size) is not
    read again, so the weekly doctor run only reads what is new."""
    cache = load_cache(root)
    versions = _ledger_versions(root)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days) if days else None
    scanned = found = 0
    for agent, base in (("claude", claude_dir or default_claude_dir()), ("codex", codex_dir or default_codex_dir())):
        if not base.is_dir():
            continue
        for path in sorted(base.glob("**/*.jsonl")):
            try:
                stat = path.stat()
            except OSError:
                continue
            if cutoff is not None and datetime.fromtimestamp(stat.st_mtime, timezone.utc) < cutoff:
                continue
            stamp = [stat.st_mtime_ns, stat.st_size]
            if cache["files"].get(str(path)) == stamp:
                continue
            scanned += 1
            for event in scan_transcript(path, agent):
                if not _matches_write(event, versions):
                    continue
                events = cache["records"].setdefault(event["id"], [])
                same = [e for e in events if e.get("version") == event["version"]]
                if same and str(same[0].get("at", "")) <= event["at"]:
                    continue
                for old in same:
                    events.remove(old)
                events.append({k: event[k] for k in ("version", "session_id", "at", "agent")})
                events.sort(key=lambda e: int(e.get("version", 0)))
                found += 1
            cache["files"][str(path)] = stamp
    cache["generated_at"] = (now or datetime.now(timezone.utc)).isoformat()
    path = _cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(cache, ensure_ascii=False, sort_keys=True) + "\n")
    return {"files_scanned": scanned, "events": found, "records": len(cache["records"])}


def _current(root: Path, record_id: str) -> dict[str, Any] | None:
    from .core import decode_record

    for path in sorted((root / "records").glob(f"*/{record_id}.md")):
        try:
            metadata, _ = decode_record(path)
        except Exception:
            continue
        if metadata.get("status") == "active":
            return metadata
    return None


def learned_in_for(root: Path, record_id: str) -> dict[str, Any] | None:
    """The session and moment that wrote the record's current version: its
    own metadata, else the transcript cache (the matching version, else the
    latest one found)."""
    metadata = _current(root, record_id)
    version = int(metadata.get("version", 1) or 1) if metadata else None
    learned = metadata.get("learned_in") if metadata else None
    if isinstance(learned, dict) and learned.get("session_id"):
        return {"session_id": str(learned["session_id"]), "at": str(learned.get("at", "")),
                "version": version, "source": "record"}
    events = [e for e in load_cache(root)["records"].get(record_id, []) if isinstance(e, dict)]
    if not events:
        return None
    match = [e for e in events if e.get("version") == version] or events[-1:]
    event = match[0]
    return {"session_id": str(event.get("session_id", "")), "at": str(event.get("at", "")),
            "version": int(event.get("version", 1) or 1), "source": "transcript", "agent": event.get("agent")}


def find_transcript(session_id: str, claude_dir: Path | None = None, codex_dir: Path | None = None) -> tuple[Path, str] | None:
    claude = claude_dir or default_claude_dir()
    for pattern in (f"*/{session_id}.jsonl", f"**/{session_id}.jsonl"):
        hits = sorted(claude.glob(pattern)) if claude.is_dir() else []
        if hits:
            return hits[0], "claude"
    codex = codex_dir or default_codex_dir()
    hits = sorted(codex.glob(f"**/*{session_id}.jsonl")) if codex.is_dir() else []
    return (hits[0], "codex") if hits else None


def _time(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _parse(line: str) -> dict[str, Any] | None:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    return entry if isinstance(entry, dict) else None


USER, PEER, SUBAGENT = "使用者", "其他 session 的訊息", "子代理回報"
_FROM_NAME_RE = re.compile(r'from-name="([^"]+)"')


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("text"))
    return ""


def _message(line: str, agent: str) -> tuple[str, str, str] | None:
    """(source, text, timestamp) when the line is a message the AI acted on:
    the user's own, another session's, or a subagent's report. Lines are
    prefiltered by substring; tool output other than a subagent's report is
    never parsed."""
    if agent == "codex":
        if '"user_message"' not in line:
            return None
        entry = _parse(line) or {}
        payload = entry.get("payload") or {}
        if entry.get("type") == "event_msg" and payload.get("type") == "user_message":
            text = str(payload.get("message") or "").strip()
            return (USER, text, str(entry.get("timestamp") or "")) if text else None
        return None
    if '"promptSource"' in line:
        entry = _parse(line)
        if not entry or entry.get("type") != "user":
            return None
        from .rating import _is_human_message

        stamp = str(entry.get("timestamp") or "")
        human = _is_human_message(entry)
        if human is not None:
            return USER, human, stamp
        kind = (entry.get("origin") or {}).get("kind") if isinstance(entry.get("origin"), dict) else None
        text = _content_text((entry.get("message") or {}).get("content")).strip()
        if kind == "peer" and text:
            # A subagent hands its report back as a session message.
            if "[Subagent hand-back]" in text[:2000]:
                return SUBAGENT, text, stamp
            name = _FROM_NAME_RE.search(text)
            return (f"{PEER}（{name.group(1)}）" if name else PEER), text, stamp
        if kind == "task-notification" and text:
            return SUBAGENT, text, stamp
        return None
    if '"tool_result"' in line and '"agentId"' in line:
        entry = _parse(line) or {}
        result = entry.get("toolUseResult")
        if isinstance(result, dict) and result.get("agentId") and not result.get("isAsync"):
            parts = (entry.get("message") or {}).get("content") or []
            text = "\n".join(
                _content_text(part.get("content")) for part in parts if isinstance(part, dict) and part.get("type") == "tool_result"
            ).strip()
            if text:
                return SUBAGENT, text, str(entry.get("timestamp") or "")
    return None


def _ai_text(line: str, agent: str) -> str:
    if agent == "claude":
        if '"assistant"' not in line or not re.search(r'"type":\s*"text"', line):
            return ""
        from .rating import _reply_text

        return _reply_text(line).strip()
    if '"agent_message"' not in line:
        return ""
    entry = _parse(line)
    payload = (entry or {}).get("payload") or {}
    if (entry or {}).get("type") == "event_msg" and payload.get("type") == "agent_message":
        return str(payload.get("message") or "").strip()
    return ""


def _before(stamp: str, moment: datetime | None) -> str:
    """`寫入前 3 分鐘` for a message's time against the write's."""
    seen = _time(stamp) if stamp else None
    if seen is None or moment is None:
        return "時間不明"
    seconds = max(0, int((moment - seen).total_seconds()))
    if seconds < 60:
        return f"寫入前 {seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"寫入前 {minutes} 分鐘"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"寫入前 {hours} 小時 {minutes} 分"
    return f"寫入前 {hours // 24} 天"


def conversation_at(path: Path, agent: str, record_id: str, version: int, at: str) -> dict[str, Any] | None:
    """Around the write: the latest message before it, whatever its source
    (the user, another session, a subagent's report), the user's own last
    message when that one was not the user's, and the AI's text after the
    write up to the next message. The write is found by the tool output that
    reported it, else by the time it was written."""
    marker = f"Recorded {record_id}" if version == 1 else f"Superseded {record_id} version {version - 1}"
    last: tuple[str, str, str] | None = None
    last_user: tuple[str, str, str] | None = None
    name = ""
    found: dict[str, Any] | None = None
    by_time: dict[str, Any] | None = None
    written = _time(at) if at else None

    def snapshot(how: str, moment: datetime | None) -> dict[str, Any]:
        return {"last": last, "last_user": last_user, "moment": moment, "ai": "", "how": how}

    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            if not name and ('"agent-name"' in line or '"custom-title"' in line):
                entry = _parse(line) or {}
                name = str(entry.get("agentName") or entry.get("customTitle") or "")
            message = _message(line, agent)
            if message is not None:
                if found is not None:
                    break
                if by_time is not None:
                    by_time["closed"] = True
                last = message
                if message[0] == USER:
                    last_user = message
                continue
            for state in (found, by_time):
                if state is not None and not state.get("closed") and len(state["ai"]) < CONTEXT_CHARS:
                    text = _ai_text(line, agent)
                    if text:
                        state["ai"] = (f"{state['ai']}\n{text}" if state["ai"] else text)[:CONTEXT_CHARS]
            if found is None and marker in line and _is_tool_output(line, agent):
                stamp = _TIMESTAMP_RE.search(line)
                found = snapshot("marker", (_time(stamp.group(1)) if stamp else None) or written)
                continue
            if found is None and by_time is None and written is not None:
                stamp = _TIMESTAMP_RE.search(line)
                moment = _time(stamp.group(1)) if stamp else None
                if moment is not None and moment >= written:
                    by_time = snapshot("time", written)
    result = found or by_time
    if result is not None:
        result["name"] = name
    return result


def context_text(root: Path, record_id: str, claude_dir: Path | None = None, codex_dir: Path | None = None) -> str:
    """`memoryctl search --id <id> --context`."""
    learned = learned_in_for(root, record_id)
    if learned is None:
        return (
            f"{record_id}：沒有記下學到時的對話（learned_in），對話紀錄的回填快取裡也沒有。"
            "可先跑 memoryctl learned-in backfill。\n"
        )
    session, version = learned["session_id"], int(learned.get("version") or 1)
    located = find_transcript(session, claude_dir, codex_dir)
    head = f"{record_id} v{version}：學到時的 session {session}，{learned.get('at') or '時間不明'}"
    # The commits that session made (spec 騰訊借鏡改善 T6).
    try:
        from .commits import session_lines
        from .storage import config_home

        made = "\n".join(session_lines(config_home(), session))
    except Exception:
        made = ""
    tail = f"{made}\n" if made else ""
    if located is None:
        return f"{head}\n找不到這個 session 的對話紀錄（可能已被清掉）。\n{tail}"
    path, agent = located
    talk = conversation_at(path, agent, record_id, version, learned.get("at", ""))
    if talk is None:
        return f"{head}\n在這個 session 的對話紀錄裡找不到寫入這筆的那一刻。\n{tail}"
    lines = [f"{head}（{agent}{'，' + talk['name'] if talk.get('name') else ''}）"]
    if talk["how"] == "time":
        lines.append("（對話紀錄裡沒有寫入時的輸出，依寫入時間對位）")
    moment = talk["moment"]
    last, last_user = talk["last"], talk["last_user"]
    if last is None:
        lines += ["", "寫入前沒有找到任何訊息"]
    else:
        source, text, stamp = last
        lines += ["", f"寫入前最近的一則訊息（{source}，{_before(stamp, moment)}）：", text[:CONTEXT_CHARS]]
        if source != USER:
            if last_user is None:
                lines += ["", "使用者在這之前沒有打過訊息"]
            else:
                lines += ["", f"使用者最後一則訊息（{_before(last_user[2], moment)}）：", last_user[1][:CONTEXT_CHARS]]
    lines += ["", "AI（寫入之後的文字回覆）：", talk["ai"] or "（之後沒有文字回覆）"]
    return "\n".join(lines) + "\n" + tail


def session_label(learned: dict[str, Any] | None, names: dict[str, str] | None = None) -> str:
    """`（<session 名稱或 id 開頭>，<日期>）` for a record's learned_in."""
    if not learned:
        return ""
    session = learned.get("session_id", "")
    name = (names or {}).get(session) or session[:8]
    return f"（{name}，{str(learned.get('at', ''))[:10]}）"
