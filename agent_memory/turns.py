"""What kind of turn a prompt is, and which records a pack holds (spec
實際使用回饋改善 F1, F16).

Hooks fire on every submitted prompt, not only on what the user typed: other
sessions' messages, subagent and background-task reports, usage-limit
notices, and image-only turns arrive the same way. The hook sees only the
text, so the kind is read from its leading marker; a transcript also records
where a line came from, which replay prefers when it is there.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import textwrap
import time
from typing import Any, Iterator

HUMAN = "human"
IMAGE = "image"
PEER = "peer"
SUBAGENT = "subagent"
NOTICE = "notice"
AUTO = "auto"
SCHEDULED = "scheduled"
LABELS = {
    HUMAN: "使用者", IMAGE: "只有圖片", PEER: "同事訊息", SUBAGENT: "子代理／背景工作回報",
    NOTICE: "系統通知", AUTO: "usage 重置", SCHEDULED: "排程提醒",
}

# No pack on these turns; a pack trimmed to the cwd's project on these (F1).
SILENT = frozenset({NOTICE, AUTO, IMAGE})
LEAN = frozenset({PEER, SUBAGENT})

_PEER_MARKS = ("Another Claude session sent a message", "<cross-session-message", "<agent-message")
_SUBAGENT_MARKS = ("[Subagent hand-back]", "<task-notification>")
_NOTICE_MARKS = ("[SYSTEM NOTIFICATION",)
_AUTO_MARKS = ("Your claude.ai usage limit has reset",)
_IMAGE_RE = re.compile(r"\[Image(?:\s*#\d+\]|:[^\]]*\])")
# A subagent's report comes as an agent message whose text opens with this
# marker and a note from the harness, ending at "The report follows:"; every
# line of the report after it is indented by the harness.
HANDBACK_MARK = "[Subagent hand-back]"
_REPORT_FOLLOWS = "The report follows:"
_HANDBACK_RE = re.compile(r"(?:Another Claude session sent a message:?\s*)?<agent-message\b[^>]*>\s*\[Subagent hand-back\]")


def is_handback(text: str) -> bool:
    """A subagent's report, in the agent-message wrapper the hook sees."""
    return bool(_HANDBACK_RE.match(text.lstrip()))


def only_images(text: str) -> bool:
    """Image placeholders and nothing else."""
    return bool(_IMAGE_RE.search(text)) and not _IMAGE_RE.sub("", text).strip()


def kind_from_text(text: str) -> str:
    head = text.lstrip()
    if head.startswith(_PEER_MARKS):
        return SUBAGENT if is_handback(head) else PEER
    if head.startswith(_SUBAGENT_MARKS):
        return SUBAGENT
    if head.startswith(_NOTICE_MARKS):
        return NOTICE
    if head.startswith(_AUTO_MARKS):
        return AUTO
    if only_images(head):
        return IMAGE
    return HUMAN


_MESSAGE_RE = re.compile(r"<(cross-session-message|agent-message)\b[^>]*>(.*?)(?:</\1>|$)", re.S)
_TASK_PART_RE = re.compile(r"<(summary|result)>(.*?)</\1>", re.S)


def message_body(text: str) -> str:
    """What another session, a subagent, or a background task said, without
    the wrapper, its address, or the note for the agent: the text a lean
    pack is looked up by."""
    head = text.lstrip()
    if head.startswith("Another Claude session sent a message"):
        head = head.split("\n", 1)[1] if "\n" in head else ""
    match = _MESSAGE_RE.match(head.lstrip())
    if match:
        head = match.group(2)
    elif head.startswith("<task-notification>"):
        head = "\n".join(part for _, part in _TASK_PART_RE.findall(head))
    head = head.strip()
    if head.startswith(HANDBACK_MARK):
        # The harness's note is not the report, and its indent is not either.
        head = head[len(HANDBACK_MARK):]
        note, follows, report = head.partition(_REPORT_FOLLOWS)
        if follows:
            head = textwrap.dedent(report.lstrip("\n"))
    return head.strip()


PACK_HEADER = "# Durable Memory Context Pack"
PRETOOL_HEADER = "## FOR THIS COMMAND"
# What leads a reminder about what the agent is about to send, and the line
# that leads a prompt's pack when the reply before it matched a failure (F13).
OUTBOUND_HEADER = "送出的內容可能需要再查："
CORRECTION_PREFIX = "上一則回覆可能需要更正："
_ID = r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+"
_HEADING_RE = re.compile(rf"^### ({_ID})\s*$")
_LESSON_LINE_RE = re.compile(rf"^- ({_ID})(?:\s[^:]*)?: ")


# Claude Code saves a hook context longer than this many characters to a
# file: the transcript then holds a <persisted-output> note with the path
# and a 2,000-character preview, and the agent is not asked to read it.
CLAUDE_CODE_PERSISTS_OVER = 10_000
PERSISTED_MARK = "<persisted-output>"
_PERSISTED_RE = re.compile(r"Output too large \(([\d.]+)KB\)\. Full output saved to")


def persisted_pack(text: str) -> int | None:
    """The size in characters of a memory pack Claude Code saved to a file
    instead of delivering, or None for anything else."""
    head = text[:1200]
    match = _PERSISTED_RE.search(head)
    if not (text.startswith(PERSISTED_MARK) or match):
        return None
    preview = text.split("Preview (first", 1)[-1][:400]
    if PACK_HEADER not in preview and PRETOOL_HEADER not in preview:
        return None
    return round(float(match.group(1)) * 1024) if match else 0


def pack_records(text: str) -> list[tuple[str, str]]:
    """(record id, the text shown for it) for every record a pack shows in
    full: `### <id>` blocks, and PROJECT LESSONS lines. SOURCE IDS and the
    pointers to records given earlier are not records shown."""
    found: list[tuple[str, list[str]]] = []
    section = ""
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            current = None
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            current = [line]
            found.append((heading.group(1), current))
            continue
        if section == "PROJECT LESSONS":
            lesson = _LESSON_LINE_RE.match(line)
            if lesson:
                found.append((lesson.group(1), [line]))
                current = None
                continue
        if current is not None:
            current.append(line)
    return [(record_id, "\n".join(lines)) for record_id, lines in found]


# --- What this session was already given in full (F2) ------------------------------
#
# A record, a PROJECT LESSONS line, or an orientation line given in full once
# is named only as "given before" for the rest of the session; compaction
# or a resume starts the list again. Only record ids and line hashes are
# kept, never a prompt.

GIVEN_KEEP_SECONDS = 14 * 24 * 3600
GIVEN_PRUNE_LIMIT = 20


def given_path(session_id: str) -> Path:
    from .storage import _session_key, config_home

    return config_home() / "runtime" / "userprompt" / _session_key(session_id or "unknown") / "given.json"


def load_given(session_id: str) -> set[str]:
    if not session_id:
        return set()
    try:
        value = json.loads(given_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    items = value.get("given") if isinstance(value, dict) else None
    return {str(item) for item in items} if isinstance(items, list) else set()


def _prune(parent: Path) -> None:
    """Session folders untouched for 14 days, at most 20 a call."""
    try:
        cutoff = time.time() - GIVEN_KEEP_SECONDS
        removed = 0
        for folder in parent.iterdir():
            if removed >= GIVEN_PRUNE_LIMIT:
                break
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                for path in sorted(folder.rglob("*"), reverse=True):
                    path.rmdir() if path.is_dir() else path.unlink()
                folder.rmdir()
                removed += 1
    except OSError:
        pass


def add_given(session_id: str, items: set[str]) -> None:
    if not session_id or not items:
        return
    from .storage import atomic_write_text

    known = load_given(session_id)
    if items <= known:
        return
    path = given_path(session_id)
    if not path.parent.exists():
        _prune(path.parent.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_text(path, json.dumps({"given": sorted(known | items)}, ensure_ascii=False) + "\n")


def clear_given(session_id: str) -> None:
    if not session_id:
        return
    try:
        given_path(session_id).unlink()
    except OSError:
        pass


def orientation_marker(line: str) -> str:
    return "orientation:" + hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:16]


def given_in(text: str) -> set[str]:
    """What a pack gave in full: its records, its PROJECT LESSONS lines, and
    each line of the block that is not a lesson (an orientation line, or the
    pointer to a project's indexed documents), as a marker."""
    found = {record_id for record_id, _ in pack_records(text)}
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        elif section == "PROJECT LESSONS" and line.strip() and not _LESSON_LINE_RE.match(line):
            found.add(orientation_marker(line))
    return found


# --- When the session last started (spec 騰訊借鏡改善 T4) -------------------------
#
# A task that falls due after the session's last start is named once at a
# prompt; one due before it was listed by that start.


def _started_path(session_id: str) -> Path:
    return given_path(session_id).parent / "started.json"


def mark_started(session_id: str, at: str) -> None:
    if not session_id:
        return
    from .storage import atomic_write_text

    path = _started_path(session_id)
    if not path.parent.exists():
        _prune(path.parent.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_text(path, json.dumps({"at": at}) + "\n")


def started_at(session_id: str) -> str | None:
    try:
        value = json.loads(_started_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return str(value.get("at")) if isinstance(value, dict) and value.get("at") else None


# --- Reminders about what the agent sends (F13) -------------------------------------
#
# A failure with outbound_triggers reminds the agent when what it is about to
# send (SendMessage, AskUserQuestion) or the reply it just gave matches; one
# record reminds a session at most once every OUTBOUND_GAP turns. A turn is a
# prompt the hook saw. Only the turn count and record ids are kept, the main
# agent's and each subagent's apart (F13 review 6).

OUTBOUND_GAP = 5
# How long a hook waits for another one's turn at outbound.json before it
# gives up (and reminds nothing).
OUTBOUND_LOCK_SECONDS = 1.0


def _outbound_path(session_id: str) -> Path:
    return given_path(session_id).parent / "outbound.json"


def _turns(value: Any) -> dict[str, int]:
    return {str(k): v for k, v in value.items() if isinstance(v, int)} if isinstance(value, dict) else {}


def _outbound_state(session_id: str) -> dict[str, Any]:
    try:
        value = json.loads(_outbound_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    agents = value.get("agents")
    return {"turn": int(value.get("turn") or 0) if str(value.get("turn") or "0").isdigit() else 0,
            "reminded": _turns(value.get("reminded")),
            "agents": {str(k): _turns(v) for k, v in agents.items()} if isinstance(agents, dict) else {},
            "sent": _turns(value.get("sent"))}


def _write_outbound(session_id: str, state: dict[str, Any]) -> None:
    from .storage import atomic_write_text

    state = {key: value for key, value in state.items() if key not in ("agents", "sent") or value}
    atomic_write_text(_outbound_path(session_id), json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n")


@contextmanager
def _outbound_lock(session_id: str) -> Iterator[None]:
    """One hook at a time reads and rewrites outbound.json: two sends in one
    message run their hooks together (F13 review 7). OSError when another
    holds it past OUTBOUND_LOCK_SECONDS."""
    import fcntl

    path = _outbound_path(session_id)
    if not path.parent.exists():
        _prune(path.parent.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.with_name("outbound.lock"), "a") as handle:
        deadline = time.monotonic() + OUTBOUND_LOCK_SECONDS
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("outbound.json is held by another hook") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def next_turn(session_id: str) -> int:
    """Count one more prompt for the session; the turn it starts."""
    if not session_id:
        return 0
    with _outbound_lock(session_id):
        state = _outbound_state(session_id)
        state["turn"] += 1
        _write_outbound(session_id, state)
    return state["turn"]


def due_reminders(session_id: str, record_ids: list[str], mark: bool = True, agent_id: str | None = None,
                  *, sent: bool = False, reply: bool = False) -> list[str]:
    """Those of `record_ids` that did not remind this agent of the session
    (the main one, or the subagent `agent_id`) in the last OUTBOUND_GAP
    turns, marked as reminding it now unless `mark` is false. Marking reads
    and writes under the lock, so what it returns is what may be shown.
    `sent` marks a reminder at a send; `reply` asks for the reply check, for
    which one the main agent was reminded of at a send in the turn before,
    and that its reply said again, is due regardless (F13 ruling 2)."""
    if not session_id or not record_ids:
        return list(record_ids)

    def due_now(state: dict[str, Any]) -> list[str]:
        reminded = state["agents"].get(agent_id, {}) if agent_id else state["reminded"]
        return [record_id for record_id in dict.fromkeys(record_ids)
                if record_id not in reminded or state["turn"] - reminded[record_id] >= OUTBOUND_GAP
                or (reply and not agent_id and state["sent"].get(record_id) == reminded[record_id] == state["turn"] - 1)]

    if not mark:
        return due_now(_outbound_state(session_id))
    with _outbound_lock(session_id):
        state = _outbound_state(session_id)
        due = due_now(state)
        if due:
            reminded = state["agents"].setdefault(agent_id, {}) if agent_id else state["reminded"]
            reminded.update({record_id: state["turn"] for record_id in due})
            if sent and not agent_id:
                state["sent"].update({record_id: state["turn"] for record_id in due})
            _write_outbound(session_id, state)
    return due
