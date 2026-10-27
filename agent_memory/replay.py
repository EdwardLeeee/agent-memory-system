"""What the hooks inject over whole sessions (spec 實際使用回饋改善 F16).

Golden queries are single turns. The waste the CEO sessions reported —
other sessions' messages and notices triggering full packs, one lesson in
241 of 313 turns, records let in by bonuses alone — shows only across long
sessions with many kinds of messages. A transcript is read once, line by
line, into a timeline of what the hooks saw and what the agent wrote, and
five numbers are measured over it: injected characters per session, repeated
full injections of one record, the share of injections on turns the user
did not type, the share of project records injected outside their project,
and how many injected records the agent went on to use.

The numbers are measured on what the transcripts recorded (the weekly
doctor), or on what a code tree would inject for the same events, replayed
against a copy of the ledger and its configuration (`memoryctl replay`).
Transcripts are only read; a copy is the only store a replay writes to.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Iterator

from . import turns, usage
from .pretool import RELEASE_LINE
from .storage import hook_chars
from .usage import FeatureFrequency, UsageDetector

OUTPUT_CHARS = 20_000
# A reminder about what is sent that comes back this soon for the same
# record is counted as a repeat in the weekly report (F13 ruling 3).
OUTBOUND_REPEAT = timedelta(minutes=10)
PROMPT_CHARS = usage.ECHO_CHARS
TOOL_NAMES = frozenset({"Bash", "SendMessage", "AskUserQuestion"})
# Where an id the agent writes is said, not typed into code (usage rule v5).
SAYING_TOOLS = frozenset({"SendMessage", "AskUserQuestion"})
CODEX_SHELLS = frozenset({"shell", "exec_command", "local_shell", "container.exec"})
DAYS = 7


def default_claude_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def default_claude_dirs() -> list[Path]:
    """~/.claude/projects and each account profile's projects folder: the
    user takes turns with two accounts. A profile's folder may be a link to
    the main one; transcript_files reads each file once."""
    return [default_claude_dir(), *sorted((Path.home() / ".claude-profiles").glob("*/projects"))]


def default_codex_dir() -> Path:
    return Path.home() / ".codex" / "sessions"


def missing_folders(claude_dir: Path | Iterable[Path] | None, codex_dir: Path | None) -> dict[str, list[str]]:
    """The transcript folders asked for that do not exist, by agent: a week
    read from a missing folder is not an empty week."""
    claude_dirs = [claude_dir] if isinstance(claude_dir, Path) else list(claude_dir or ())
    found: dict[str, list[str]] = {}
    for agent, base in [*(("claude", folder) for folder in claude_dirs), ("codex", codex_dir)]:
        if base is not None and not base.is_dir():
            found.setdefault(agent, []).append(str(base))
    return found


def transcript_files(
    claude_dir: Path | Iterable[Path] | None, codex_dir: Path | None, now: datetime, days: int = DAYS,
) -> list[tuple[Path, str]]:
    """Transcripts written to in the last `days` days, oldest first, each
    real file once however many of the folders reach it; a workflow's
    journal (…/subagents/workflows/<run>/journal.jsonl) is not a transcript,
    the agent-*.jsonl beside it are.
    A long session's file also holds older events; `Window` keeps only the
    days asked for."""
    cutoff = (now - timedelta(days=days)).timestamp()
    claude_dirs = [claude_dir] if isinstance(claude_dir, Path) else list(claude_dir or ())
    found = []
    seen: set[Path] = set()
    for agent, base in [*(("claude", folder) for folder in claude_dirs), ("codex", codex_dir)]:
        if base is None or not base.is_dir():
            continue
        for path in base.glob("**/*.jsonl"):
            if agent == "claude" and path.name == "journal.jsonl" and "workflows" in path.relative_to(base).parts:
                continue
            try:
                real = path.resolve()
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            if mtime >= cutoff:
                found.append((mtime, path, agent))
    return [(path, agent) for _, path, agent in sorted(found)]


class Window:
    """Which events to measure: those from `since` on (events with no time
    are kept), of the sessions asked for, if any."""

    def __init__(self, since: datetime | None = None, sessions: set[str] | None = None) -> None:
        self.since = since.astimezone(timezone.utc).isoformat()[:19] if since else ""
        self.sessions = sessions

    def keep(self, event: dict[str, Any]) -> bool:
        at = str(event.get("at") or "")
        if self.since and at and at[:19] < self.since:
            return False
        return not self.sessions or any(str(event.get("session", "")).startswith(s) for s in self.sessions)


# --- Reading a transcript into a timeline ------------------------------------
#
# Events: start (SessionStart, with its source), compact, prompt (what fired
# UserPromptSubmit, with its kind), tool (a tool call PreToolUse saw), output
# (what the agent wrote), inject (a memory pack the transcript recorded; a
# speed bump's hold is one with deny), persisted (a pack Claude Code saved
# to a file instead of delivering).


def _text_of(content: Any) -> tuple[str, bool]:
    if isinstance(content, str):
        return content, False
    parts, image = [], False
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("text", "input_text", "output_text"):
            parts.append(str(part.get("text", "")))
        elif part.get("type") in ("image", "input_image"):
            image = True
    return "\n".join(parts), image


def _tool_text(tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        return "\n".join(str(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                         for value in tool_input.values())[:OUTPUT_CHARS]
    return str(tool_input)[:OUTPUT_CHARS]


_ORIGIN_KIND = {"human": turns.HUMAN, "peer": turns.PEER, "task-notification": turns.SUBAGENT,
                "auto-continuation": turns.AUTO}


def _prompt_kind(origin: str | None, source: str | None, text: str, image: bool) -> str | None:
    """A user line's kind by where the transcript says it came from; None
    for a line that is not a prompt (skill bodies, command echoes, images
    a tool read)."""
    if origin in _ORIGIN_KIND:
        kind = _ORIGIN_KIND[origin]
        if kind == turns.HUMAN and ((image and not text.strip()) or turns.only_images(text)):
            return turns.IMAGE
        # A subagent's report reaches the session as a peer message.
        if kind == turns.PEER and turns.is_handback(text):
            return turns.SUBAGENT
        return kind
    if origin is None:
        return turns.SCHEDULED if source == "system" else None
    return turns.kind_from_text(text)


def _queued_kind(attachment: dict[str, Any], text: str) -> str:
    if attachment.get("commandMode") == "task-notification":
        return turns.SUBAGENT
    origin = attachment.get("origin")
    kind = _ORIGIN_KIND.get(origin.get("kind")) if isinstance(origin, dict) else None
    if kind == turns.PEER and turns.is_handback(text):
        return turns.SUBAGENT
    return kind or turns.kind_from_text(text)


def _hook_output(attachment: dict[str, Any]) -> tuple[str, bool]:
    """A PreToolUse hook's context or deny reason, from its recorded stdout."""
    try:
        output = json.loads(str(attachment.get("stdout") or "")).get("hookSpecificOutput") or {}
    except (ValueError, AttributeError):
        return "", False
    if output.get("permissionDecision") == "deny":
        return str(output.get("permissionDecisionReason") or ""), True
    return str(output.get("additionalContext") or ""), False


class ReadHealth:
    """Whether a transcript could be read: a file that cannot be opened, or
    that has lines but not one this reader recognised and parsed, is
    reported as unreadable, never as an empty week."""

    def __init__(self) -> None:
        self.opened = False
        self.lines = 0
        self.parsed = 0
        self.failed = 0

    def readable(self) -> bool:
        return self.opened and not (self.lines and not self.parsed)


# Messages that arrive together (queued while the agent was busy) are one
# turn: the transcript writes each as its own user line, but the hook fires
# once, with the last of them as its prompt. Replaying that turn with the
# messages joined, or with the first, reproduced none of the recorded packs
# checked; with the last, all of them; and a record only an earlier message
# would have brought was first marked as given on another day. "Last" is by
# time: a queued notice can be written after a message typed later (in the
# 20 such turns kept, one, on 10-20, where only the typed message,
# 45 seconds later than the notice, replays to the recorded silence).


_PEER_HEADER = "Another Claude session sent a message:\n"
_PEER_END_RE = re.compile(r"</(?:cross-session-message|agent-message)>")
_AGENT_FROM_RE = re.compile(r'<agent-message from="([0-9A-Za-z]+)"')
_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")


def _as_submitted(text: str) -> str:
    """A message from another session or a subagent as it was submitted:
    the transcript shows it with a heading line before it and a note for
    the agent after it, which the hook never saw."""
    if not text.startswith(_PEER_HEADER):
        return text
    text = text[len(_PEER_HEADER):]
    end = _PEER_END_RE.search(text)
    return text[:end.end()] if end else text


def _merged(events: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    pending: dict[str, Any] | None = None
    for event in events:
        if event["t"] == "prompt":
            if pending is None:
                pending = dict(event)
            else:
                typed = any(kind in (turns.HUMAN, turns.IMAGE) for kind in (pending["kind"], event["kind"]))
                latest = event if str(event.get("at") or "") >= str(pending.get("at") or "") else pending
                kind = latest["kind"] if latest["kind"] in (turns.HUMAN, turns.IMAGE) or not typed else turns.HUMAN
                # The agent saw every message of the turn, though the hook got
                # one: none of their words is a record's (usage.Echo).
                echo = [*pending.get("echo", [pending.get("text") or ""]), event.get("text") or ""]
                pending = {**latest, "parts": pending.get("parts", 1) + 1, "kind": kind, "echo": echo}
            continue
        if pending is not None:
            yield pending
            pending = None
        yield event
    if pending is not None:
        yield pending


def claude_timeline(path: Path, health: ReadHealth | None = None, results: bool = False,
                    hooks: bool = False) -> Iterator[dict[str, Any]]:
    return _merged(_claude_lines(path, health, results, hooks))


# With results=True, a timeline also says how each shell call and question
# ended (spec 騰訊借鏡改善 T1): a "result" event with the call's tool_id and
# is_error. Only those calls' results are parsed; others are never read.
RESULT_TOOLS = frozenset({"Bash", "AskUserQuestion"})


def _claude_lines(path: Path, health: ReadHealth | None = None, results: bool = False,
                  hooks: bool = False) -> Iterator[dict[str, Any]]:
    subagent = "subagents" in path.parts
    session, cwd = path.stem, ""
    last = ""  # the last of start, compact, prompt, tool
    reported: set[str] = set()  # subagents whose hand-back was delivered
    wanted: set[str] = set()  # calls whose result is to be read
    health = health or ReadHealth()
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    health.opened = True
    with handle:
        for line in handle:
            if line.isspace():
                continue
            health.lines += 1
            # Claude Code writes compact JSON; a file with a space after each
            # colon is matched the same way (only such a line is rewritten).
            probe = line if '"type":"' in line else line.replace('": "', '":"')
            if '"type":"assistant"' in probe:
                kind = "assistant"
            elif '"type":"user"' in probe and '"tool_result"' not in probe:
                kind = "user"
            elif '"type":"attachment"' in probe and (
                '"queued_command"' in probe or '"hookEvent":"SessionStart"' in probe
                or '"hookEvent":"UserPromptSubmit"' in probe
                or ('"hookEvent":"PreToolUse"' in probe
                    and (hooks or "additionalContext" in probe or "permissionDecision" in probe))
            ):
                kind = "attachment"
            elif '"compact_boundary"' in probe:
                kind = "compact"
            elif '"tool_result"' in probe and '"is_error":true' in probe and "hook error" in probe:
                kind = "held"
            elif wanted and '"tool_result"' in probe and any(tool_id in line for tool_id in wanted):
                kind = "result"
            else:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                health.failed += 1
                continue
            if not isinstance(entry, dict):
                health.failed += 1
                continue
            health.parsed += 1
            if kind in ("result", "held") and (entry.get("type") != "user" or not entry.get("timestamp")):
                # An api-request-blob line repeats earlier tool results, with
                # no time: read once, from the user line (F16 re-check R-c).
                continue
            session = str(entry.get("sessionId") or session)
            cwd = str(entry.get("cwd") or cwd)
            base = {"session": session, "cwd": cwd, "at": str(entry.get("timestamp") or "")}
            agent_id = entry.get("agentId") if subagent else None
            if kind == "compact":
                if entry.get("subtype") == "compact_boundary":
                    yield {"t": "compact", **base}
                    last = "compact"
                continue
            if kind == "result":
                for part in (entry.get("message") or {}).get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "tool_result" and part.get("tool_use_id") in wanted:
                        wanted.discard(part["tool_use_id"])
                        code, denied = _claude_exit(part.get("content")) if part.get("is_error") else (0, False)
                        yield {"t": "result", **base, "tool_id": part["tool_use_id"], "is_error": bool(part.get("is_error")),
                               "code": code, "denied": denied, "agent_id": agent_id}
                continue
            if kind == "held":
                # A speed bump's hold reaches the transcript only as the held
                # call's error ("PreToolUse:Bash hook error: <reason>"), never as
                # a hook attachment.
                for part in (entry.get("message") or {}).get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "tool_result" and part.get("is_error"):
                        text = _text_of(part.get("content"))[0]
                        if RELEASE_LINE in text:
                            yield {"t": "inject", **base, "event": "PreToolUse", "text": text.split("hook error: ", 1)[-1],
                                   "deny": True, "tool_id": part.get("tool_use_id"), "agent_id": agent_id}
                continue
            if kind == "assistant" and entry.get("type") == "assistant":
                texts, spoken = [], []  # what it wrote; of that, what it said (usage rule v5)
                for part in (entry.get("message") or {}).get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        texts.append(str(part.get("text", "")))
                        spoken.append(texts[-1])
                    elif part.get("type") == "tool_use":
                        texts.append(_tool_text(part.get("input")))
                        if part.get("name") in SAYING_TOOLS:
                            spoken.append(texts[-1])
                        if part.get("name") in TOOL_NAMES:
                            yield {"t": "tool", **base, "name": part.get("name"), "input": part.get("input"),
                                   "agent_id": agent_id, "tool_id": part.get("id")}
                            last = "tool"
                            if results and part.get("name") in RESULT_TOOLS and part.get("id"):
                                wanted.add(str(part["id"]))
                if texts and not subagent:
                    yield {"t": "output", **base, "text": "\n".join(texts)[:OUTPUT_CHARS],
                           "spoken": "\n".join(spoken)[:OUTPUT_CHARS]}
                continue
            if kind == "user" and entry.get("type") == "user" and not subagent:
                text, image = _text_of((entry.get("message") or {}).get("content"))
                prompt_kind = _prompt_kind((entry.get("origin") or {}).get("kind"), entry.get("promptSource"), text, image)
                if prompt_kind is None:
                    continue
                # A completion notice for a subagent whose report was already
                # delivered is dropped, whether queued or written as a user line.
                task = _TASK_ID_RE.search(text[:300]) if text.lstrip().startswith("<task-notification>") else None
                if task and task.group(1) in reported:
                    continue
                if last == "":
                    yield {"t": "start", **base, "source": "startup"}
                text = _as_submitted(text)
                reported.update(_AGENT_FROM_RE.findall(text[:300]))
                yield {"t": "prompt", **base, "text": text[:PROMPT_CHARS], "kind": prompt_kind, "via": "user"}
                last = "prompt"
                continue
            if kind != "attachment" or entry.get("type") != "attachment":
                continue
            attachment = entry.get("attachment") or {}
            event = attachment.get("hookEvent")
            if attachment.get("type") == "queued_command" and not subagent:
                prompt = attachment.get("prompt")
                text = prompt if isinstance(prompt, str) else _text_of(prompt)[0]
                # A subagent's completion notice after its hand-back was
                # delivered is dropped as a duplicate; no hook fires for it.
                task = _TASK_ID_RE.search(text[:300]) if attachment.get("commandMode") == "task-notification" else None
                if task and task.group(1) in reported:
                    continue
                reported.update(_AGENT_FROM_RE.findall(text[:300]))
                if last == "":
                    yield {"t": "start", **base, "source": "startup"}
                yield {"t": "prompt", **base, "text": text[:PROMPT_CHARS], "kind": _queued_kind(attachment, text), "via": "queued"}
                last = "prompt"
            elif event == "SessionStart" and not subagent:
                if last != "start":
                    yield {"t": "start", **base, "source": "compact" if last == "compact" else "startup" if last == "" else "resume"}
                    last = "start"
                text = "\n".join(str(item) for item in attachment.get("content") or [])
                if text.startswith(turns.PACK_HEADER):
                    yield {"t": "inject", **base, "event": "SessionStart", "text": text}
                elif (size := turns.persisted_pack(text)) is not None:
                    yield {"t": "persisted", **base, "event": "SessionStart", "chars": size}
            elif event == "UserPromptSubmit" and not subagent:
                if hooks:
                    yield {"t": "ran", **base, "event": "UserPromptSubmit"}
                text = "\n".join(str(item) for item in attachment.get("content") or [])
                if text.startswith((turns.PACK_HEADER, turns.CORRECTION_PREFIX)):
                    yield {"t": "inject", **base, "event": "UserPromptSubmit", "text": text}
                elif (size := turns.persisted_pack(text)) is not None:
                    yield {"t": "persisted", **base, "event": "UserPromptSubmit", "chars": size}
            elif event == "PreToolUse":
                if hooks:
                    yield {"t": "ran", **base, "event": "PreToolUse", "tool_id": attachment.get("toolUseID"),
                           "agent_id": agent_id}
                text, deny = _hook_output(attachment)
                if text and (deny or text.startswith((turns.PRETOOL_HEADER, turns.OUTBOUND_HEADER))):
                    yield {"t": "inject", **base, "event": "PreToolUse", "text": text, "deny": deny,
                           "tool_id": attachment.get("toolUseID"), "agent_id": agent_id}


_CODEX_CMD_RE = re.compile(r"\bcmd\s*:\s*\"((?:[^\"\\]|\\.)*)\"")


def _codex_command(payload: dict[str, Any]) -> str | None:
    if payload.get("type") == "custom_tool_call":
        match = _CODEX_CMD_RE.search(str(payload.get("input") or ""))
        if not match:
            return None
        try:
            return json.loads(f'"{match.group(1)}"')
        except ValueError:
            return match.group(1)
    if payload.get("name") not in CODEX_SHELLS:
        return None
    try:
        arguments = json.loads(str(payload.get("arguments") or "{}"))
    except ValueError:
        return None
    command = arguments.get("cmd") or arguments.get("command") if isinstance(arguments, dict) else None
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    return command if isinstance(command, str) else None


# How Codex's tool wrapper heads a shell call's output, before "Output:".
_CODEX_EXIT_RE = re.compile(r"^(?:Exit code: |Process exited with code )(-?\d+)\s*$", re.M)


_CLAUDE_EXIT_RE = re.compile(r"^Exit code (\d+)$")
_DENIED_HEADS = ("The user doesn't want to proceed", "Permission for this action was denied", "[Request interrupted")


def _claude_exit(content: Any) -> tuple[int | None, bool]:
    """A failed Claude call's exit code, from the first line Claude Code
    writes (Exit code 1), and whether the call was refused rather than run;
    the output itself is never read."""
    if isinstance(content, list):
        content = next((str(item.get("text") or "") for item in content if isinstance(item, dict)), "")
    head = str(content or "").split("\n", 1)[0].strip()
    found = _CLAUDE_EXIT_RE.match(head)
    return (int(found.group(1)) if found else None), head.startswith(_DENIED_HEADS)


def _codex_exit(output: Any) -> int | None:
    """The exit code Codex's wrapper wrote above a shell call's output; None
    when there is none. The command's own output is never read for it."""
    if isinstance(output, list):
        output = "\n".join(str(item.get("text") or "") for item in output if isinstance(item, dict))
    if not isinstance(output, str):
        return None
    found = _CODEX_EXIT_RE.search(output.split("\nOutput:", 1)[0])
    return int(found.group(1)) if found else None


def codex_timeline(path: Path, health: ReadHealth | None = None, results: bool = False,
                   hooks: bool = False) -> Iterator[dict[str, Any]]:
    session, cwd = path.stem, ""
    prompted = False
    last_prompt = None
    wanted: set[str] = set()
    health = health or ReadHealth()
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    health.opened = True
    with handle:
        for line in handle:
            if line.isspace():
                continue
            health.lines += 1
            if not any(marker in line for marker in ('"session_meta"', '"turn_context"', '"user_message"',
                                                      '"UserMessage"', '"response_item"', '"compacted"')):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                health.failed += 1
                continue
            if not isinstance(entry, dict):
                health.failed += 1
                continue
            health.parsed += 1
            payload = entry.get("payload") or {}
            kind = entry.get("type") if isinstance(entry, dict) else None
            if kind == "session_meta":
                session = str(payload.get("id") or session)
                cwd = str(payload.get("cwd") or cwd)
                yield {"t": "start", "session": session, "cwd": cwd, "source": "startup"}
                continue
            if kind == "turn_context":
                cwd = str(payload.get("cwd") or cwd)
                continue
            base = {"session": session, "cwd": cwd, "at": str(entry.get("timestamp") or "")}
            if kind == "compacted":
                yield {"t": "compact", **base}
                yield {"t": "start", **base, "source": "compact"}
                prompted = False  # the pack SessionStart gives after it comes before the next prompt
                continue
            text = None
            if kind == "event_msg" and payload.get("type") == "user_message":
                text = str(payload.get("message") or "")
            elif kind == "event_msg" and payload.get("type") == "item_completed" and (payload.get("item") or {}).get("type") == "UserMessage":
                text = _text_of((payload.get("item") or {}).get("content"))[0]
            if text is not None:
                if text != last_prompt:
                    yield {"t": "prompt", **base, "text": text[:PROMPT_CHARS], "kind": turns.kind_from_text(text)}
                    prompted = True
                last_prompt = text
                continue
            if kind != "response_item":
                continue
            ptype = payload.get("type")
            if ptype == "message":
                body = _text_of(payload.get("content"))[0]
                if payload.get("role") == "developer" and body.startswith(turns.PACK_HEADER):
                    yield {"t": "inject", **base, "event": "UserPromptSubmit" if prompted else "SessionStart", "text": body}
                elif payload.get("role") == "developer" and body.startswith(turns.PRETOOL_HEADER):
                    yield {"t": "inject", **base, "event": "PreToolUse", "text": body}
                elif payload.get("role") == "assistant" and body:
                    yield {"t": "output", **base, "text": body[:OUTPUT_CHARS], "spoken": body[:OUTPUT_CHARS]}
                    last_prompt = None
            elif ptype in ("function_call", "custom_tool_call"):
                command = _codex_command(payload)
                if command:
                    yield {"t": "tool", **base, "name": "Bash", "input": {"command": command}, "agent_id": None,
                           "tool_id": payload.get("call_id")}
                    yield {"t": "output", **base, "text": command[:OUTPUT_CHARS], "spoken": ""}
                    if results and payload.get("call_id"):
                        wanted.add(str(payload["call_id"]))
            elif ptype in ("function_call_output", "custom_tool_call_output") and payload.get("call_id") in wanted:
                wanted.discard(payload["call_id"])
                code = _codex_exit(payload.get("output"))
                if code is not None:
                    yield {"t": "result", **base, "tool_id": payload["call_id"], "is_error": code != 0, "code": code,
                           "denied": False, "agent_id": None}


def timeline(path: Path, agent: str, health: ReadHealth | None = None, results: bool = False,
             hooks: bool = False) -> Iterator[dict[str, Any]]:
    """A transcript's events. With `hooks`, a Claude one also says where a
    UserPromptSubmit or PreToolUse hook ran ("ran", with the call's id),
    output or not: a prompt or call the transcript shows no hook for had
    none (a completion notice delivered with a start, a SendMessage while
    the matcher was Bash only). Codex records no silent runs."""
    if agent == "codex":
        return codex_timeline(path, health, results, hooks)
    return claude_timeline(path, health, results, hooks)


# --- Which injected records the agent used: usage.py (rule v1, spec F12) -----------

# --- Measuring ----------------------------------------------------------------


def node_table(root: Path) -> dict[str, str]:
    """Every indexed record's node, read once."""
    import sqlite3

    try:
        connection = sqlite3.connect(f"file:{root / '.cache' / 'index.sqlite3'}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        return {str(record_id): str(node) for record_id, node in connection.execute("SELECT record_id, node FROM records")}
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


_SEARCH_COMMAND_RE = re.compile(r"^\s*(?:\S*/)?memoryctl\s+search\b")


def _searches_memory(event: dict[str, Any], home: Path | None = None) -> bool:
    """A shell call in which the agent ran `memoryctl search` itself.
    Working on the memory system itself, `memoryctl search` is development,
    not recall (spec 8.16 (c)); its folders are usage.memory_repo_paths."""
    if event.get("name") != "Bash":
        return False
    if usage.in_memory_repo(str(event.get("cwd") or ""), home):
        return False
    return bool(_SEARCH_COMMAND_RE.search(str((event.get("input") or {}).get("command") or "")))


def _project(node: str | None) -> str | None:
    return node.split("/")[1] if node and node.startswith("project/") and node.count("/") >= 1 else None


def record_projects(root: Path) -> dict[str, str]:
    """The project each indexed record belongs to, wherever its node is: a
    project:<p> scope, or a workspace scope with an id whose domain is a
    project's token (a project's preferences sit in rules). Routing knows
    every project the configuration or the ledger names: machine.json's
    scope_paths, the sources, the ledger's project scopes, so one with
    records before it has a source is one too (spec F11); no project is
    named in the program. A global record belongs to no project, though its
    id may name one: it is never counted as another project's (F16
    re-check); wide_scope_records lists those. Records of no project are
    left out."""
    import sqlite3

    from .lexical_index import _token_owner, compute_routing, load_index_routing

    try:
        connection = sqlite3.connect(f"file:{root / '.cache' / 'index.sqlite3'}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        owners = dict(_token_owner(load_index_routing(connection) or compute_routing()))
        found = {}
        for record_id, scope in connection.execute("SELECT record_id, scope FROM records"):
            parts = str(record_id).split(".")
            project = (str(scope).split(":", 1)[1] if str(scope).startswith("project:")
                       else owners.get(parts[1]) if str(scope).startswith("workspace:") and len(parts) > 2 else None)
            if project:
                found[str(record_id)] = project
        return found
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


def wide_scope_records(root: Path) -> list[dict[str, str]]:
    """Active records whose scope is global while their id names a
    project: a scope that may be wider than meant, for that project to
    decide on; never changed here (F16 re-check)."""
    import sqlite3

    from .lexical_index import _token_owner, compute_routing, load_index_routing

    try:
        connection = sqlite3.connect(f"file:{root / '.cache' / 'index.sqlite3'}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        owners = dict(_token_owner(load_index_routing(connection) or compute_routing()))
        found = []
        for record_id, scope in connection.execute("SELECT record_id, scope FROM records ORDER BY record_id"):
            parts = str(record_id).split(".")
            if str(scope) == "global" and len(parts) > 2 and parts[0] != "federation" and parts[1] in owners:
                found.append({"id": str(record_id), "project": owners[parts[1]]})
        return found
    except sqlite3.Error:
        return []
    finally:
        connection.close()


class _Session:
    def __init__(self, key: str, cwd: str) -> None:
        self.key, self.cwd = key, cwd
        self.chars: Counter[str] = Counter()
        self.packs: Counter[str] = Counter()
        self.pack_chars: Counter[str] = Counter()
        self.prompts: Counter[str] = Counter()
        self.seen: Counter[str] = Counter()
        self.segment = 0
        self.segment_seen: set[tuple[int, str]] = set()
        self.after_compact = 0
        self.cross: Counter[str] = Counter()
        self.project_records = 0
        self.unknown = 0
        self.injected = self.used_id = self.used_content = 0
        self.memory_repo = [0, 0, 0]  # injected, named, content: turns in the memory system's folders
        self.persisted: Counter[str] = Counter()
        self.persisted_chars = 0


class _Window:
    """Records injected since the last prompt the user typed, what was
    written to the agent since, and whether it searched the memory itself."""

    def __init__(self, memory: bool = False) -> None:
        self.pending: dict[str, dict[str, Any]] = {}
        self.echo = usage.Echo()
        self.searched = False
        # The prompt that opened it, or the agent's work in it, in the
        # memory system's own folders: counted apart, where ids are the
        # subject (A1 ruling).
        self.memory = memory

    def close(self, stats: _Session, measure: "Measure") -> None:
        apart = stats.memory_repo if self.memory else None
        for record_id, item in self.pending.items():
            if apart is not None:
                apart[0] += item["n"]
                apart[1] += item["n"] if item["used"] == "id" else 0
                apart[2] += item["n"] if item["used"] == "content" else 0
                continue
            stats.injected += item["n"]
            counts = measure.per_record.setdefault(record_id, [0, 0, 0])
            counts[0] += item["n"]
            if item["used"] == "id":
                stats.used_id += item["n"]
                counts[1] += item["n"]
            elif item["used"] == "content":
                stats.used_content += item["n"]
                counts[2] += item["n"]
        if self.searched:
            # By the nodes the turn's records came from, or "沒有注入"
            # (spec 論文借鏡改善 A5).
            for node in sorted({measure.nodes.get(record_id, "unknown") for record_id in self.pending}) or ["沒有注入"]:
                measure.search_after[node] += 1
        self.pending = {}
        self.echo = usage.Echo()
        self.searched = False


class Measure:
    """The five numbers over many timelines. `packs` maps an event's place
    in its timeline to the pack a replay produced for it; without it the
    packs the transcript recorded are measured."""

    def __init__(self, nodes: dict[str, str], cwd_projects: Callable[[str], set[str]],
                 frequency: FeatureFrequency | None, watch: set[str] | None = None,
                 projects: dict[str, str] | None = None, evidence: int = 0, home: Path | None = None) -> None:
        self.nodes = nodes
        self.home = home  # the store measured, for the memory system's own folders
        # Per record: injected, used by id, used by content (F12).
        self.per_record: dict[str, list[int]] = {}
        self.search_calls = 0
        # Reminders about what is sent (F13 ruling 3): each record's, and how
        # many came back within OUTBOUND_REPEAT of the same record's last.
        self.outbound = {"reminders": 0, "repeats": 0}
        self._outbound_last: dict[tuple[str, str], datetime] = {}
        self.search_after: Counter[str] = Counter()
        # A random sample of content matches with what matched, to check the
        # rule by eye: the record's feature and the agent's words only.
        import random

        self.evidence_size = evidence
        self.evidence: list[dict[str, Any]] = []
        self._evidence_seen = 0
        self._random = random.Random(1021)
        # Which project a record belongs to; by its node when not given.
        self.projects = projects
        self.watch = watch or set()
        self.watched: Counter[tuple[str, str, str]] = Counter()
        self.cwd_projects = cwd_projects
        self._cwd_cache: dict[str, set[str]] = {}
        self.detector = UsageDetector(frequency)
        self.sessions: dict[str, _Session] = {}
        self.files: Counter[str] = Counter()
        self.unreadable: Counter[str] = Counter()
        self.session_agent: dict[str, str] = {}
        self.ups_ids: dict[tuple[str, int], tuple[str, ...]] = {}

    def _projects_of(self, cwd: str) -> set[str]:
        if cwd not in self._cwd_cache:
            try:
                self._cwd_cache[cwd] = set(self.cwd_projects(cwd))
            except Exception:
                self._cwd_cache[cwd] = set()
        return self._cwd_cache[cwd]

    def _pack(self, stats: _Session, window: _Window, event: str, pack: "Pack | str", kind: str | None, cwd: str) -> list[str]:
        if isinstance(pack, str):
            pack = Pack(pack)
        if self._agent == "claude" and pack.hook_length > turns.CLAUDE_CODE_PERSISTS_OVER:
            # A replayed pack Claude Code would have saved to a file.
            self._persisted(stats, event, pack.length)
            return []
        stats.chars[event] += pack.length
        if event == "UserPromptSubmit":
            stats.packs[kind or turns.HUMAN] += 1
            stats.pack_chars[kind or turns.HUMAN] += pack.length
        ids = []
        here = self._projects_of(cwd)
        for record_id, block in pack.records:
            ids.append(record_id)
            if record_id in self.watch:
                self.watched[(record_id, event if event != "UserPromptSubmit" else (kind or turns.HUMAN), self.label(stats))] += 1
            stats.seen[record_id] += 1
            first_in_segment = (stats.segment, record_id) not in stats.segment_seen
            stats.segment_seen.add((stats.segment, record_id))
            if stats.seen[record_id] > 1 and first_in_segment:
                stats.after_compact += 1
            node = self.nodes.get(record_id)
            project = self.projects.get(record_id) if self.projects is not None else _project(node)
            if node is None:
                stats.unknown += 1
            elif project is not None:
                stats.project_records += 1
                if project not in here:
                    stats.cross[record_id] += 1
            if event == "SessionStart":
                continue
            pending = window.pending.get(record_id)
            if pending is None:
                window.pending[record_id] = {"n": 1, "used": None, "features": self.detector.features(record_id, block)}
            else:
                pending["n"] += 1
        return ids

    def _count_outbound(self, session: str, ids: list[str], at: str) -> None:
        try:
            moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            moment = None
        for record_id in ids:
            self.outbound["reminders"] += 1
            last = self._outbound_last.get((session, record_id))
            if moment is not None and last is not None and moment - last <= OUTBOUND_REPEAT:
                self.outbound["repeats"] += 1
            if moment is not None:
                self._outbound_last[(session, record_id)] = moment

    @staticmethod
    def _persisted(stats: _Session, event: str, chars: int) -> None:
        stats.persisted[event] += 1
        stats.persisted_chars += chars

    def _output(self, window: _Window, text: str, spoken: str | None = None) -> None:
        waiting = [item for item in window.pending.values() if item["used"] is None]
        if not waiting:
            return
        normalized = usage.normalize(text)
        said = usage.normalize(spoken) if spoken is not None else None
        output_grams: list[set[str] | None] = [None]
        for item in waiting:
            found = usage.match(item["features"], normalized, window.echo, output_grams, said)
            if found is None:
                continue
            how, what = found
            item["used"] = "id" if how == "id" else "content"
            if how != "id" and self.evidence_size:
                self._keep_evidence(item["features"]["id"], how, what, normalized)

    def _keep_evidence(self, record_id: str, how: str, what: str, normalized: str) -> None:
        words = usage.snippet(normalized, what)
        if words is None:  # what the agent wrote there holds a secret: not shown
            return
        self._evidence_seen += 1
        sample = {"id": record_id, "kind": how, "feature": what, "agent": words}
        if len(self.evidence) < self.evidence_size:
            self.evidence.append(sample)
        else:
            slot = self._random.randrange(self._evidence_seen)
            if slot < self.evidence_size:
                self.evidence[slot] = sample

    def begin(self, agent: str, packs: dict[int, str] | None = None) -> None:
        """Start one transcript; `packs` are a replay's, by event index."""
        self.files[agent] += 1
        self._agent = agent
        self._packs = packs
        self._windows: dict[str, _Window] = {}
        self._kind: dict[str, str] = {}
        self._last_prompt: dict[str, int] = {}

    def feed(self, index: int, event: dict[str, Any]) -> None:
        packs = self._packs
        key = str(event.get("session") or "")
        cwd = str(event.get("cwd") or "")
        stats = self.sessions.get(key)
        if stats is None:
            stats = self.sessions[key] = _Session(key, cwd)
            self.session_agent[key] = self._agent
        window = self._windows.setdefault(key, _Window())
        kind = event["t"]
        if kind == "compact":
            stats.segment += 1
        elif kind == "prompt":
            prompt_kind = event.get("kind") or turns.HUMAN
            stats.prompts[prompt_kind] += 1
            if prompt_kind in (turns.HUMAN, turns.IMAGE):
                window.close(stats, self)
                window.memory = usage.in_memory_repo(cwd, self.home)
            self._kind[key] = prompt_kind
            self._last_prompt[key] = index
            for message in event.get("echo") or [str(event.get("text") or "")]:
                window.echo.add(message)
            if packs is not None and packs.get(index):
                self.ups_ids[(key, index)] = tuple(self._pack(stats, window, "UserPromptSubmit", packs[index], prompt_kind, cwd))
        elif kind == "output":
            spoken = event.get("spoken")
            self._output(window, str(event.get("text") or ""), str(spoken) if spoken is not None else None)
        elif packs is not None and kind in ("tool", "start") and packs.get(index):
            self._pack(stats, window, "PreToolUse" if kind == "tool" else "SessionStart", packs[index], None, cwd)
        elif packs is None and kind == "persisted":
            self._persisted(stats, str(event.get("event")), int(event.get("chars") or 0))
        elif packs is None and kind == "inject":
            ids = self._pack(stats, window, str(event.get("event")), str(event.get("text") or ""), self._kind.get(key), cwd)
            if event.get("event") == "UserPromptSubmit" and key in self._last_prompt:
                self.ups_ids[(key, self._last_prompt[key])] = tuple(ids)
            if event.get("event") == "PreToolUse" and str(event.get("text") or "").startswith(turns.OUTBOUND_HEADER):
                self._count_outbound(key, ids, str(event.get("at") or ""))
        if kind in ("output", "tool") and not window.memory and usage.in_memory_repo(cwd, self.home):
            # The agent worked in the memory system's folders in this window,
            # whatever folder the prompt came from (the session moves); apart
            # from the chain above, whose replayed pack it once skipped (F16
            # re-check R-d).
            window.memory = True
        if kind == "tool" and _searches_memory(event, self.home):
            self.search_calls += 1
            window.searched = True

    def end(self, readable: bool = True) -> None:
        for key, window in self._windows.items():
            window.close(self.sessions[key], self)
        if not readable:
            self.unreadable[self._agent] += 1

    def timeline(self, events: Iterable[dict[str, Any]], packs: dict[int, str] | None = None, agent: str = "claude") -> None:
        self.begin(agent, packs)
        for index, event in enumerate(events):
            self.feed(index, event)
        self.end()

    def result(self) -> dict[str, Any]:
        sessions = [s for s in self.sessions.values() if s.chars or s.prompts or s.persisted]
        per_session = sorted((sum(s.chars.values()), s.key, s) for s in sessions)
        totals: Counter[str] = Counter()
        packs: Counter[str] = Counter()
        pack_chars: Counter[str] = Counter()
        prompts: Counter[str] = Counter()
        cross: Counter[tuple[str, str]] = Counter()
        repeats: list[tuple[int, str, str]] = []
        full = distinct = after = project_records = unknown = injected = used_id = used_content = persisted_chars = 0
        memory_repo = [0, 0, 0]
        persisted: Counter[str] = Counter()
        for s in sessions:
            totals.update(s.chars)
            packs.update(s.packs)
            pack_chars.update(s.pack_chars)
            prompts.update(s.prompts)
            full += sum(s.seen.values())
            distinct += len(s.seen)
            after += s.after_compact
            project_records += s.project_records
            unknown += s.unknown
            injected += s.injected
            used_id += s.used_id
            used_content += s.used_content
            memory_repo = [a + b for a, b in zip(memory_repo, s.memory_repo)]
            persisted.update(s.persisted)
            persisted_chars += s.persisted_chars
            for record_id, n in s.cross.items():
                cross[(self.label(s), record_id)] += n
            repeats += [(n, self.label(s), record_id) for record_id, n in s.seen.items() if n > 1]
        values = [chars for chars, _, _ in per_session]

        def ratio(part: int, whole: int) -> float | None:
            return round(part / whole, 4) if whole else None

        all_packs = sum(packs.values())
        non_user = all_packs - packs[turns.HUMAN]
        cross_total = sum(cross.values())
        by_agent = Counter(self.session_agent.get(s.key, "claude") for s in sessions)
        return {
            "files": dict(self.files),
            "unreadable": dict(self.unreadable),
            "sessions": len(sessions),
            "sessions_by_agent": dict(by_agent),
            "chars": {
                "total": dict(totals),
                "per_session": {
                    "median": values[len(values) // 2] if values else 0,
                    "p90": values[min(len(values) - 1, int(len(values) * 0.9))] if values else 0,
                    "max": values[-1] if values else 0,
                    "max_session": self.label(per_session[-1][2]) if per_session else "",
                },
            },
            "repeats": {
                "full": full, "distinct": distinct, "repeats": full - distinct, "after_compact": after,
                "top": [{"session": label, "id": record_id, "count": n}
                        for n, label, record_id in sorted(repeats, key=lambda r: (-r[0], r[1], r[2]))[:5]],
            },
            "non_user": {
                "packs": dict(packs), "chars": dict(pack_chars), "prompts": dict(prompts),
                "ratio": ratio(non_user, all_packs),
                "chars_ratio": ratio(sum(pack_chars.values()) - pack_chars[turns.HUMAN], sum(pack_chars.values())),
            },
            "cross_project": {
                "records": cross_total, "of_project_records": project_records, "of_all": full,
                "ratio": ratio(cross_total, full), "unknown": unknown,
                "top": [{"session": label, "id": record_id, "count": n}
                        for (label, record_id), n in sorted(cross.items(), key=lambda kv: (-kv[1], kv[0]))[:5]],
            },
            "usage": {
                "injected": injected, "used": used_id + used_content, "explicit": used_id, "content": used_content,
                "rate": ratio(used_id + used_content, injected), "detector": usage.DETECTOR,
                # Per record: injected, used by id, used by content; ids and numbers only.
                "per_record": dict(sorted(self.per_record.items())),
            },
            # Turns in the memory system's own folders, left out of usage (A1 ruling).
            "usage_memory_repo": {"injected": memory_repo[0], "explicit": memory_repo[1], "content": memory_repo[2]},
            "outbound": dict(self.outbound),
            "search": {"calls": self.search_calls,
                       "after": dict(sorted(self.search_after.items(), key=lambda item: (-item[1], item[0])))},
            # Packs saved to a file instead of delivered: not counted as injected.
            "persisted": {"packs": dict(persisted), "total": sum(persisted.values()), "chars": persisted_chars},
            "watch": [{"id": record_id, "turn": turn, "session": label, "count": n}
                      for (record_id, turn, label), n in sorted(self.watched.items())],
        }

    def label(self, stats: _Session) -> str:
        """A session as the report names it: the registered project or
        domain its cwd is in, not the folder's name, which can say what the
        work is about (F16 re-check R-g); and the start of its id."""
        projects = sorted(self._projects_of(stats.cwd))
        return f"{'+'.join(projects) if projects else '無專案'}:{stats.key[:8]}"


def measure_actual(
    files: list[tuple[Path, str]], root: Path, cwd_projects: Callable[[str], set[str]], window: Window | None = None,
    watch: set[str] | None = None, evidence: int = 0,
) -> dict[str, Any]:
    """The five numbers on the packs the transcripts recorded; with
    `evidence`, that many random content matches, to check the rule by eye."""
    window = window or Window()
    measure = Measure(node_table(root), cwd_projects, FeatureFrequency.from_index(root), watch, record_projects(root),
                      evidence, home=root)
    for path, agent in files:
        health = ReadHealth()
        measure.begin(agent)
        for index, event in enumerate(timeline(path, agent, health)):
            if window.keep(event):
                measure.feed(index, event)
        measure.end(health.readable())
    result = measure.result()
    if evidence:
        result["evidence"] = measure.evidence
    return result


# --- Replaying a code tree on a copy ----------------------------------------------

# Run as `python3 -c RUNNER <code>`, events on stdin and packs on stdout, so
# no prompt is ever written to disk. It needs from the code under test only
# core.handle_hook and, since the failure layer, pretool.decide.
RUNNER = r'''
import datetime as _datetime
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agent_memory import core
try:
    from agent_memory import pretool
except ImportError:  # a version before the failure layer had no PreToolUse hook
    pretool = None


class _AtEvent(_datetime.datetime):
    """The router's clock, set to each event's time: a brief's age (F7) is
    counted from when the hook ran, not from the replay."""
    moment = None

    @classmethod
    def now(cls, tz=None):
        if cls.moment is None:
            return super().now(tz)
        return cls.moment.astimezone(tz) if tz is not None else cls.moment.replace(tzinfo=None)


try:
    from agent_memory import router as _router
    if getattr(_router, "datetime", None) is _datetime.datetime:
        _router.datetime = _AtEvent
except Exception:
    pass
root = Path(os.environ["AGENT_MEMORY_HOME"])
names = {"start": "session-start", "prompt": "user-prompt-submit", "compact": "pre-compact"}
for line in sys.stdin:
    item = json.loads(line)
    kind, agent, event = item["t"], item["agent"], item["event"]
    try:
        _AtEvent.moment = _datetime.datetime.fromisoformat(str(item.get("at") or "").replace("Z", "+00:00"))
    except ValueError:
        _AtEvent.moment = None
    try:
        # PreToolUse's markers from before the replayed part of the session:
        # a compaction or resume clears none of them (a failure given a week
        # and six compactions before was skipped live, not in the replay).
        for agent_id, failure_ids in event.pop("pretool_marked", None) or ():
            if pretool:
                folder = pretool.state_dir(str(event.get("session_id") or ""), agent_id)
                for failure_id in failure_ids:
                    pretool.mark_injected(folder, failure_id, "PreToolUse", None)
        if kind == "start-given":
            # What a session start gave, as the transcript recorded it: the
            # replay's own start pack has no capsules to crowd it.
            from datetime import datetime, timezone
            from agent_memory import turns
            session = str(event.get("session_id") or "")
            if event.get("source") in ("compact", "resume", "clear"):
                turns.clear_given(session)
            turns.mark_started(session, datetime.now(timezone.utc).isoformat())
            turns.add_given(session, set(event.get("given") or ()))
            output = {}
        elif kind == "tool":
            output = pretool.decide(event, agent)[0] if pretool else {}
        else:
            output = core.handle_hook(root, names[kind], event, agent=agent)
        error = None
    except Exception as exc:
        output, error = {}, type(exc).__name__
    hso = output.get("hookSpecificOutput") or {}
    text = hso.get("additionalContext") or hso.get("permissionDecisionReason") or ""
    sys.stdout.write(json.dumps({"f": item["f"], "i": item["i"], "text": text, "error": error}, ensure_ascii=False) + "\n")
    sys.stdout.flush()
'''

_SESSION_ENV = ("AGENT_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "GEMINI_SESSION_ID")


_HOOK_KIND = {"UserPromptSubmit": "prompt", "PreToolUse": "tool", "SessionStart": "start", "PreCompact": "compact"}


def hook_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """The hook input a timeline event stands for, or None."""
    common = {"session_id": event.get("session"), "cwd": event.get("cwd"), "transcript_path": None}
    if event["t"] == "prompt":
        return {**common, "hook_event_name": "UserPromptSubmit", "prompt": event.get("text") or ""}
    if event["t"] == "start":
        return {**common, "hook_event_name": "SessionStart", "source": event.get("source") or "startup"}
    if event["t"] == "compact":
        return {**common, "hook_event_name": "PreCompact", "trigger": "auto"}
    if event["t"] == "tool":
        output = {**common, "hook_event_name": "PreToolUse", "tool_name": event.get("name"), "tool_input": event.get("input")}
        if event.get("agent_id"):
            output["agent_id"] = event["agent_id"]
        return output
    return None


def make_copy(root: Path, config_root: Path, destination: Path) -> tuple[Path, Path]:
    """A ledger and configuration copy for one replay, with no runtime
    state of any session (a marker would silence a first hit) and no model."""
    store = destination / "store"
    shutil.copytree(root, store, symlinks=True, ignore=shutil.ignore_patterns(".git", "semantic"))
    return store, copy_config(config_root, destination / "config")


def copy_config(config_root: Path, config: Path) -> Path:
    """The configuration without any session's runtime state or backups;
    the ranking feedback stays, since it is part of what a hook reads."""

    def skip(folder: str, names: list[str]) -> list[str]:
        here = Path(folder)
        drop = []
        for name in names:
            if here == config_root and name == "adapter-backups":
                drop.append(name)
            if here == config_root / "runtime" and name in ("pretooluse", "userprompt", "precompact"):
                drop.append(name)
        return drop

    shutil.copytree(config_root, config, symlinks=True, ignore=skip)
    return config


class Pack:
    """What is kept of one pack: its length and its records, each record's
    text kept once however many packs show it. A replay of a week holds a
    pack for every event of every tree; whole packs took about 26 MB a tree."""

    __slots__ = ("length", "hook_length", "pretool", "records", "pointers")

    def __init__(self, text: str, blocks: dict[str, str] | None = None) -> None:
        self.length = len(text)
        self.hook_length = hook_chars(text)
        # A shell command's failures or a reminder about what is sent; any other text a hold.
        self.pretool = text.startswith((turns.PRETOOL_HEADER, turns.OUTBOUND_HEADER))
        found = turns.pack_records(text)
        self.records = tuple((record_id, blocks.setdefault(block, block) if blocks is not None else block)
                             for record_id, block in found)
        self.pointers = _given_pointers(text)

    @property
    def ids(self) -> set[str]:
        return {record_id for record_id, _ in self.records}


class Runner:
    """One code tree answering hook events on a copy, through pipes."""

    def __init__(self, code: Path, store: Path, config: Path) -> None:
        import threading

        env = {key: value for key, value in os.environ.items() if key not in _SESSION_ENV}
        env.update({"AGENT_MEMORY_HOME": str(store), "AGENT_MEMORY_CONFIG_HOME": str(config),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        self.process = subprocess.Popen(
            [sys.executable, "-c", RUNNER, str(code)], env=env, cwd=str(code), text=True, encoding="utf-8",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        self.packs: dict[int, dict[int, Pack]] = {}
        self.blocks: dict[str, str] = {}
        self.errors = 0
        self.answered = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            item = json.loads(line)
            self.answered += 1
            self.errors += bool(item.get("error"))
            if item.get("text"):
                self.packs.setdefault(int(item["f"]), {})[int(item["i"])] = Pack(item["text"], self.blocks)

    def send(self, item: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(item, ensure_ascii=False) + "\n")

    def finish(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        self.reader.join()
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.wait() != 0:
            raise RuntimeError(f"replay runner exited with {self.process.returncode}")


def _end_on_signals() -> Callable[[], None]:
    """Let SIGTERM and SIGHUP end a run through its finally blocks, so the
    copies it made are removed; returns what puts the old handlers back."""
    import signal

    def stop(signum: int, frame: Any) -> None:
        raise SystemExit(128 + signum)

    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous[number] = signal.signal(number, stop)
        except (ValueError, OSError):  # not the main thread
            continue

    def restore() -> None:
        for number, handler in previous.items():
            signal.signal(number, handler)

    return restore


def _selected(files: list[tuple[Path, str]], window: Window) -> Iterator[tuple[int, str, int, dict[str, Any]]]:
    for number, (path, agent) in enumerate(files):
        for index, event in enumerate(timeline(path, agent)):
            if window.keep(event):
                yield number, agent, index, event


def replay(
    files: list[tuple[Path, str]], root: Path, config_root: Path, codes: list[Path], window: Window | None = None,
    watch: set[str] | None = None,
) -> dict[str, Any]:
    """The five numbers for what each code tree injects on the same events,
    next to what the transcripts recorded, and how often each tree's
    UserPromptSubmit pack holds the same records as the recorded one. The
    transcripts are read twice: once to feed every tree, once to measure."""
    from .pretool import cwd_projects

    window = window or Window()
    work = Path(tempfile.mkdtemp(prefix="memoryctl-replay-"))
    restore = _end_on_signals()
    try:
        runners = []
        for number, code in enumerate(codes):
            store, config = make_copy(root, config_root, work / f"copy-{number}")
            runners.append(Runner(code, store, config))
        try:
            for number, agent, index, event in _selected(files, window):
                hook = hook_event(event)
                if hook is not None:
                    item = {"f": number, "i": index, "t": event["t"], "agent": agent, "event": hook}
                    for runner in runners:
                        runner.send(item)
        finally:
            for runner in runners:
                runner.finish()
        nodes, projects = node_table(root), record_projects(root)
        frequency = FeatureFrequency.from_index(root)
        here = lambda cwd: cwd_projects(cwd, root)  # noqa: E731
        measures = [Measure(nodes, here, frequency, watch, projects, home=root) for _ in range(len(codes) + 1)]
        for number, (path, agent) in enumerate(files):
            health = ReadHealth()
            measures[0].begin(agent)
            for measure, runner in zip(measures[1:], runners):
                measure.begin(agent, runner.packs.get(number, {}))
            for index, event in enumerate(timeline(path, agent, health)):
                if window.keep(event):
                    for measure in measures:
                        measure.feed(index, event)
            for measure in measures:
                measure.end(health.readable())
        actual = measures[0]
        result: dict[str, Any] = {"actual": actual.result(), "codes": {}}
        for code, measure, runner in zip(codes, measures[1:], runners):
            same = sum(set(ids) == set(measure.ups_ids.get(key, ())) for key, ids in actual.ups_ids.items())
            result["codes"][str(code)] = {**measure.result(), "hook_events": runner.answered, "hook_errors": runner.errors,
                                          "fidelity": {"ups_events": len(actual.ups_ids), "same_records": same}}
        return result
    finally:
        restore()
        shutil.rmtree(work, ignore_errors=True)


# --- How faithful a replay is -------------------------------------------------------
#
# A replay of the code and ledger an event really ran on should inject what
# the transcript recorded. Sampled events are replayed on the program and
# records of the commit live was at when they happened, taken from the
# ledger's git history; every difference must have a known cause: something
# with no history (configuration, ranking feedback, the indexed project
# documents of a source read from a fetched ref), state a session had built
# (records already injected, commands already released), rules that depend
# on the date, or a working directory that is gone. A source read from its
# working tree has its HEAD's reflog: an unexplained difference is replayed
# again with it at the commit of then. A difference with no known cause is a
# fault of the replay.

STORE_PATHS = ("records", "archive", "agent_memory", "bin", "manifest.json", "events")
CAUSES = {
    "feedback": "feedback.json 沒有歷史",
    "sources": "聯邦來源的索引沒有歷史",
    "dedupe": "session 內已注入過（去重狀態）",
    "given": "session 內已完整給過（F2 的清單）",
    "release": "session 內的放行狀態",
    "prefix": "依序重放時，同一個 session 較早的事件用的是這個事件時的帳本和今天同步的聯邦來源，有紀錄只有重放或只有實際給過，session 狀態從那裡開始和實際不同",
    "config": "設定檔在事件之後改過（沒有歷史，未驗證）",
    "cwd": "工作目錄已不存在",
    "time": "依日期的規則（過期、有效期間）",
    "unknown": "原因不明（重放工具的錯）",
}
# How each cause is shown to be the reason, not assumed. A config change
# cannot be: the configuration has no history, so it is counted apart.
VERIFIED_BY = {
    "feedback": "不帶 feedback.json 重放，結果和紀錄相同",
    "sources": "差別全是聯邦紀錄，而且其中一筆讀的那個檔案在事件之後改過：讀 ref 的來源，事件前最後一個 commit 的內容和 ref 現在不同；讀工作目錄的來源，檔案在事件之後改過或刪掉。或：把讀工作目錄的來源釘在事件當時 HEAD 所在的 commit（reflog）重放，結果和紀錄相同（較晚的文件佔了名額、改了字詞的權重這類間接影響；differing 列出釘的 commit）",
    "dedupe": "重放多出的全是同一個 session、同一個代理裡先前給過的（PreToolUse 給的，或主代理的 UPS 包裡的 failure），紀錄裡的重放也都有",
    "given": "重放多出的全列在紀錄那包的「先前已提供」裡，紀錄裡的重放也都有",
    "release": "重放擋下、紀錄放行，而且同一個 session、同一個代理稍早被同一個減速帶擋過",
    "prefix": "差別裡（全文，或佔一個名額的「先前已提供」指標）的某一筆，在同一次依序重放、上次重置之後的較早事件裡只有一邊給過（重放給了、紀錄沒有，或反過來）；differing 列出那一筆、哪一邊和時間",
    "cwd": "事件的工作目錄已經不存在",
    "time": "差別紀錄的到期日或有效期限落在事件和現在之間",
}
UNVERIFIED = frozenset({"config"})
# When F2 went live: from then on what a session was given before changes
# its packs, so an event after it is replayed after the session's events
# since its last reset (a start, compaction or resume) or this moment, in
# order (F16 re-check R-b).
GIVEN_LIVE_AT = "2026-10-20T21:54:31Z"
# When a prompt pack began marking the failures it gave for PreToolUse to
# skip (the failure layer): one before gave them unmarked, and PreToolUse
# gave such a failure later all the same.
FAILURES_MARKED_FROM = "2026-10-10T01:02:45Z"


def _git(root: Path, *args: str) -> str:
    from .storage import git_environment

    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              env=git_environment(), timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _pack_ids(text: str | None) -> set[str]:
    return {record_id for record_id, _ in turns.pack_records(text or "")}


def _given_pointers(text: str | None) -> tuple[str, ...]:
    """The ids a prompt pack named as given earlier in the session (F2)."""
    from .router import GIVEN_HEADING

    section = (text or "").split(f"## {GIVEN_HEADING}", 1)
    if len(section) < 2:
        return ()
    body = section[1].split("\n## ", 1)[0]
    return tuple(sorted(line[2:].strip() for line in body.splitlines() if line.startswith("- ")))


def _fidelity_candidates(files: list[tuple[Path, str]], window: Window) -> list[dict[str, Any]]:
    """Every UserPromptSubmit and shell PreToolUse event in the window, with
    the records the transcript recorded for it (none when the hook stayed
    silent); a shell event also with its command's speed bumps and those
    that held a call earlier in its session and agent, within the 14 days a
    release lasts, read from the whole transcript: a hold before the window
    still released a call in it (F16 re-check R-a)."""
    from .pretool import STATE_KEEP_SECONDS, speed_bump_hits
    from .storage import load_router_settings, resolve_speed_bumps

    bumps, _ = resolve_speed_bumps(load_router_settings())
    found = []
    for number, (path, agent) in enumerate(files):
        last_prompt: dict[str, dict[str, Any]] = {}
        tools: dict[str, dict[str, Any]] = {}
        # A Codex shell hook's output names no call: it follows the call.
        latest_tool: dict[tuple[str, Any], dict[str, Any]] = {}
        held: dict[tuple[str, Any], dict[str, str]] = {}  # bump id -> when it last held a call
        latest: dict[str, dict[str, Any]] = {}  # each session's last prompt, for the hook run after it
        ran_calls: set[str] = set()
        for index, event in enumerate(timeline(path, agent, hooks=True)):
            key, kind = str(event.get("session") or ""), event["t"]
            if kind == "ran":
                if event.get("event") == "UserPromptSubmit" and key in latest:
                    latest[key]["ran"] = True
                elif event.get("tool_id"):
                    ran_calls.add(str(event["tool_id"]))
                continue
            if kind in ("prompt", "tool") and (kind == "prompt" or event.get("name") == "Bash"):
                item = {"f": number, "i": index, "agent": agent, "session": key, "at": str(event.get("at") or ""),
                        "cwd": str(event.get("cwd") or ""), "kind": event.get("kind") if kind == "prompt" else "tool",
                        "hook": "UserPromptSubmit" if kind == "prompt" else "PreToolUse", "actual": (), "deny": False}
                if window.keep(event):
                    found.append(item)
                if kind == "prompt":
                    last_prompt[key] = latest[key] = item
                else:
                    item["tool_id"] = str(event.get("tool_id") or "")
                    state = (key, event.get("agent_id"))
                    command = str((event.get("input") or {}).get("command") or "")
                    item["bumps"] = sorted(bump["id"] for bump in speed_bump_hits(command, bumps)) if bumps else []
                    item["held_before"] = sorted(bump for bump, at in held.get(state, {}).items()
                                                 if _seconds_between(at, item["at"]) <= STATE_KEEP_SECONDS)
                    item["state"] = state
                    tools[str(event.get("tool_id") or index)] = item
                    latest_tool[state] = item
            elif kind == "persisted" and event.get("event") == "UserPromptSubmit":
                # Only its preview is known, so it cannot be compared.
                target = last_prompt.pop(key, None)
                if target is not None:
                    target["persisted"] = True
            elif kind == "inject" and event.get("event") in ("UserPromptSubmit", "PreToolUse") and not str(
                event.get("text") or ""
            ).startswith(turns.OUTBOUND_HEADER):  # a reminder about what is sent belongs to no shell call
                if event.get("tool_id"):
                    ran_calls.add(str(event["tool_id"]))  # a hold shows only as the call's error
                # A shell hook's output names its tool call; parallel calls
                # would otherwise take each other's packs.
                if event["event"] == "UserPromptSubmit":
                    target = last_prompt.pop(key, None)
                elif event.get("tool_id"):
                    target = tools.pop(str(event["tool_id"]), None)
                else:
                    target = latest_tool.pop((key, event.get("agent_id")), None)
                if target is not None:
                    target["actual"] = tuple(sorted(_pack_ids(event.get("text"))))
                    target["files"] = _record_files(event.get("text"))
                    target["pointers"] = _given_pointers(event.get("text"))
                    target["deny"] = bool(event.get("deny"))
                    if target["deny"] and target["hook"] == "PreToolUse":
                        held.setdefault(target["state"], {}).update({bump: target["at"] for bump in target["bumps"]})
        if agent == "claude":
            # Only what the transcript shows a hook ran for (F16 re-check).
            for item in found:
                if item["f"] == number and not (item.get("ran") if item["hook"] == "UserPromptSubmit"
                                                else item.get("tool_id") in ran_calls):
                    item["persisted"] = True  # left out, as a pack only previewed is
    return [item for item in found if not item.get("persisted")]


def _seconds_between(earlier: str, later: str) -> float:
    """Seconds from one transcript time to a later one; 0 when either is
    unreadable (the hold is then taken as recent)."""
    try:
        return (datetime.fromisoformat(later.replace("Z", "+00:00"))
                - datetime.fromisoformat(earlier.replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return 0.0


def _sample(candidates: list[dict[str, Any]], size: int, cwd_projects: Callable[[str], set[str]], seed: int) -> list[dict[str, Any]]:
    """Round-robin over (project, kind) strata, so every project and every
    kind of turn is in the sample; shell events that injected count as their
    own kind, since silent ones say little."""
    import random

    rng = random.Random(seed)
    cache: dict[str, str] = {}
    strata: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in candidates:
        if item["cwd"] not in cache:
            try:
                cache[item["cwd"]] = sorted(cwd_projects(item["cwd"]))[0] if cwd_projects(item["cwd"]) else "-"
            except Exception:
                cache[item["cwd"]] = "-"
        kind = ("tool+" if item["actual"] or item["deny"] else "tool") if item["hook"] == "PreToolUse" else str(item["kind"])
        item["stratum"] = f"{cache[item['cwd']]}/{kind}"
        strata.setdefault((cache[item["cwd"]], kind), []).append(item)
    for group in strata.values():
        rng.shuffle(group)
    picked: list[dict[str, Any]] = []
    keys = sorted(strata)
    while len(picked) < size and any(strata[key] for key in keys):
        for key in keys:
            if strata[key] and len(picked) < size:
                picked.append(strata[key].pop())
    return picked


def _payloads(files: list[tuple[Path, str]], picked: list[dict[str, Any]]) -> dict[tuple[int, str], list[tuple[int, dict[str, Any], str]]]:
    """The hook input of each sampled event, and for a shell event the
    records its session had been given before it; kept in memory only.
    For an event after F2 went live, where its session's replay starts
    (`since`); returned, the hook inputs of each such session from then on,
    with their event index and time, up to its last sampled event."""
    wanted: dict[int, dict[int, dict[str, Any]]] = {}
    for item in picked:
        wanted.setdefault(item["f"], {})[item["i"]] = item
    ordered: dict[tuple[int, str], list[tuple[int, dict[str, Any], str]]] = {}
    for number, items in wanted.items():
        path, agent = files[number]
        last = {}  # session -> its last sampled event after F2 went live
        for index, item in items.items():
            if item["at"] >= GIVEN_LIVE_AT:
                last[item["session"]] = max(last.get(item["session"], -1), index)
        since: dict[str, int] = {}
        marked: dict[str, list[list[Any]]] = {}  # PreToolUse's markers at `since`, by agent
        calls: dict[int, str] = {}
        starts: dict[str, dict[str, Any]] = {}  # a start whose recorded pack comes next
        asked: dict[str, dict[str, Any]] = {}  # each session's last prompt, while it is one of the pass
        called: dict[str, dict[str, Any]] = {}  # calls of the pass by id
        # PreToolUse gives a failure once a session and agent, and nothing
        # clears that (compaction clears only what UserPromptSubmit gave). A
        # failure the main agent's prompt pack gave counts as given there too
        # (F16 re-check R-f).
        given: dict[tuple[str, Any], set[str]] = {}
        unran: set[int] = set()  # Claude prompts and calls no hook ran for: not replayed
        prompt_at: dict[str, int] = {}
        for index, event in enumerate(timeline(path, agent, hooks=True)):
            state = (str(event.get("session") or ""), event.get("agent_id"))
            session = state[0]
            if event["t"] == "ran" or (event["t"] == "inject" and event.get("event") == "PreToolUse" and event.get("tool_id")):
                # A hold shows only as the call's error, an inject naming the call.
                if event.get("event") == "UserPromptSubmit":
                    unran.discard(prompt_at.get(session, -1))
                else:
                    unran.difference_update(i for i, call in calls.items() if call == str(event.get("tool_id")))
                if event["t"] == "ran":
                    continue
            if agent == "claude" and event["t"] in ("prompt", "tool"):
                unran.add(index)
                if event["t"] == "prompt":
                    prompt_at[session] = index
                else:
                    calls[index] = str(event.get("tool_id"))
            if event["t"] == "prompt":
                asked.pop(session, None)  # the pack after it is its own, replayed or not
            if session in last and index <= last[session] and str(event.get("at") or "") >= GIVEN_LIVE_AT:
                if session not in since or event["t"] == "start":
                    since[session] = index  # F2 live, or a start, compaction or resume cleared it
                    # What PreToolUse would skip from here on: nothing clears its
                    # markers, so the replay starts from the recorded ones.
                    marked[session] = [[agent_id, sorted(ids)] for (other, agent_id), ids in given.items()
                                       if other == session and ids]
                hook = hook_event(event)
                if hook is not None:
                    if event["t"] == "start":
                        hook = {**hook, "given": []}  # filled from the start pack recorded next
                        starts[session] = hook
                    elif event["t"] == "prompt":
                        asked[session] = hook
                    elif event.get("tool_id"):
                        called[str(event["tool_id"])] = hook
                    ordered.setdefault((number, session), []).append((index, hook, str(event.get("at") or "")))
                elif event["t"] == "inject" and event.get("event") == "SessionStart" and session in starts:
                    starts.pop(session)["given"] = sorted(turns.given_in(str(event.get("text") or "")))
            # What each prompt and call of the pass was recorded giving: where
            # the replay's session and the real one were given apart (F16 re-check).
            if event["t"] == "inject" and event.get("event") == "UserPromptSubmit" and session in asked:
                asked[session]["_recorded"] = sorted(_pack_ids(event.get("text")))
            elif event["t"] == "inject" and event.get("event") == "PreToolUse" and str(event.get("tool_id")) in called:
                called[str(event["tool_id"])]["_recorded"] = sorted(_pack_ids(event.get("text")))
            if index in items:
                items[index]["event"] = hook_event(event)
                items[index]["earlier"] = set(given.get(state, ()))
                if session in since and items[index]["at"] >= GIVEN_LIVE_AT:
                    items[index]["since"] = since[session]
                    items[index]["pretool_marked"] = marked[session]
            if event["t"] == "inject" and event.get("event") == "PreToolUse" and not event.get("deny"):
                given.setdefault(state, set()).update(_pack_ids(event.get("text")))
            elif (event["t"] == "inject" and event.get("event") == "UserPromptSubmit"
                  and str(event.get("at") or "") >= FAILURES_MARKED_FROM):
                given.setdefault((state[0], None), set()).update(
                    record_id for record_id in _pack_ids(event.get("text")) if record_id.startswith("failure."))
        for key in [key for key in ordered if key[0] == number]:
            ordered[key] = [entry for entry in ordered[key] if entry[0] not in unran]
            if agent == "claude":  # each of them ran: one with no pack recorded gave nothing
                for _, hook, _ in ordered[key]:
                    if "given" not in hook:
                        hook.setdefault("_recorded", [])
    return ordered


def _metadata(store: Path, record_id: str) -> dict[str, Any]:
    from .core import decode_record

    for path in store.glob(f"records/*/{record_id}.md"):
        try:
            return decode_record(path)[0]
        except Exception:
            return {}
    return {}


_READ_RE = re.compile(r"^- read: ([^:\n]+): (?!commit )([^#\n]+)", re.M)


def _record_files(text: str | None) -> dict[str, tuple[str, str]]:
    """The source and file each federated record of a pack was read from,
    from its `- read: <source>: <path>` line."""
    found = {}
    for record_id, block in turns.pack_records(text or ""):
        match = _READ_RE.search(block)
        if match:
            found[record_id] = (match.group(1).strip(), match.group(2).strip())
    return found


def _index_origin(store: Path, record_id: str) -> tuple[str, str] | None:
    """A federated record's source and file, from a store's index."""
    import sqlite3

    try:
        connection = sqlite3.connect(f"file:{store / '.cache' / 'index.sqlite3'}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute("SELECT metadata_json FROM records WHERE record_id = ?", (record_id,)).fetchone()
        origin = json.loads(row[0]).get("origin") if row else None
    except (sqlite3.Error, ValueError):
        return None
    finally:
        connection.close()
    if isinstance(origin, dict) and origin.get("source") and origin.get("path"):
        return str(origin["source"]), str(origin["path"])
    return None


def _file_changed(config_root: Path, source: str, path: str, when: datetime) -> bool:
    """Whether a source's file changed after `when`: read from a fetched ref,
    its blob in the last commit before `when` differs from the ref's now (or
    it was added or removed since); read from the working tree, it was
    modified or removed since."""
    from .federation import parse_minimal_toml, read_ref_commit

    try:
        config = parse_minimal_toml((config_root / "sources.d" / f"{source}.toml").read_text(encoding="utf-8"))
    except Exception:
        return False
    repo = Path(str((config.get("source") or {}).get("repo") or "")).expanduser()
    if not repo.is_dir():
        return False
    ref = str((config.get("source") or {}).get("ref") or "")
    if not ref:
        file = repo / path
        return not file.exists() or file.stat().st_mtime > when.timestamp()
    tip = read_ref_commit(repo, ref)
    if not tip:
        return False
    then = _git(repo, "rev-list", "-1", f"--before={when.isoformat()}", tip)
    blob_then = _git(repo, "rev-parse", "--verify", "--quiet", f"{then}:{path}") if then else ""
    return blob_then != _git(repo, "rev-parse", "--verify", "--quiet", f"{tip}:{path}")


def _sources_changed(record_ids: set[str], when: datetime, config_root: Path, store: Path,
                     files: dict[str, tuple[str, str]] | None = None) -> bool:
    """Whether the file one of these federated records was read from (its
    line in the recorded pack, or its origin in the replayed store) changed
    after `when`. Not the repository as a whole: an active one changes every
    day (F16 re-check)."""
    for record_id in sorted(record_ids):
        where = (files or {}).get(record_id) or _index_origin(store, record_id)
        if where and _file_changed(config_root, where[0], where[1], when):
            return True
    return False


def _shown_apart(item: dict[str, Any], replayed: set[str]) -> set[str]:
    """What only one of the recorded and the replayed pack shows: a record in
    full, or a pointer to one given before, which takes a slot too (F2)."""
    return (set(item["actual"]) ^ replayed) | (set(item.get("pointers", ())) ^ set(item.get("replayed_pointers", ())))


def _cause(item: dict[str, Any], replayed: set[str], deny: bool, store: Path, config_root: Path, now: datetime) -> str:
    actual = set(item["actual"])
    if item["hook"] == "PreToolUse":
        # A replay starts with no session state: it holds a call the session
        # had let through only if the same speed bump held one earlier there.
        if deny and not item["deny"] and set(item.get("bumps", ())) & set(item.get("held_before", ())):
            return "release"
        # At most two failures a command, each once a session and agent:
        # what the replay adds beyond the recorded pack had been given there.
        extra = replayed - actual
        if deny == item["deny"] and extra and not actual - replayed and extra <= item.get("earlier", set()):
            return "dedupe"
    # A session's prompt pack names what it gave in full before (F2); a
    # replay starts with no such list and gives those again, and loses
    # nothing (F16 re-check R-e).
    extra = replayed - actual
    if item["hook"] == "UserPromptSubmit" and extra and extra <= set(item.get("pointers", ())) and not actual - replayed:
        return "given"
    # Replayed in order after F2, the session's earlier events ran on this
    # event's ledger and today's synced sources, not their own: a record only
    # one of the replay and the recording had given by now is in full on one
    # side and a pointer on the other, or as a pointer takes another's slot
    # (F16 re-check).
    if set(item.get("given_apart") or ()) & _shown_apart(item, replayed):
        return "prefix"
    if item["cwd"] and not Path(item["cwd"]).exists():
        return "cwd"
    differing = actual ^ replayed
    when = None
    try:
        when = datetime.fromisoformat(item["at"].replace("Z", "+00:00"))
    except ValueError:
        pass
    if (when is not None and differing and all(record_id.startswith(("federation.", "lesson.")) for record_id in differing)
            and _sources_changed(differing, when, config_root, store, item.get("files"))):
        return "sources"
    if when is not None:
        for record_id in differing:
            metadata = _metadata(store, record_id)
            for field in ("expires_at", "valid_until"):
                value = str(metadata.get(field) or "")
                if value and when.isoformat()[:10] <= value[:10] <= now.isoformat()[:10]:
                    return "time"
        settings = [config_root / "router.toml", config_root / "machine.json", *sorted((config_root / "sources.d").glob("*"))]
        if any(path.exists() and path.stat().st_mtime > when.timestamp() for path in settings):
            return "config"
    return "unknown"


def _source_reflogs(config_root: Path) -> dict[str, list[tuple[float, str]]]:
    """Each source read from its working tree, with where its HEAD moved and
    when, newest first, from the repository's reflog. A source read from a
    fetched ref has no such history (its private ref keeps none)."""
    from .federation import parse_minimal_toml

    found: dict[str, list[tuple[float, str]]] = {}
    for path in sorted((config_root / "sources.d").glob("*.toml")):
        try:
            source = parse_minimal_toml(path.read_text(encoding="utf-8")).get("source") or {}
        except Exception:
            continue
        repo = Path(str(source.get("repo") or "")).expanduser()
        if source.get("ref") or not repo.is_dir():
            continue
        moves = [(float(match.group(2)), match.group(1))
                 for match in re.finditer(r"^([0-9a-f]{40}) HEAD@\{(\d+)\}$",
                                          _git(repo, "log", "-g", "--date=unix", "--format=%H %gd", "HEAD"), re.M)]
        if moves:
            found[path.stem] = moves
    return found


def _pins_at(reflogs: dict[str, list[tuple[float, str]]], at: str) -> dict[str, str]:
    """The commit each working-tree source's HEAD was at at `at`."""
    try:
        when = datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return {}
    pins = {name: next((commit for moved, commit in moves if moved <= when), "") for name, moves in reflogs.items()}
    return {name: commit for name, commit in pins.items() if commit}


def _pin_sources(config: Path, pins: dict[str, str]) -> None:
    """A configuration copy that reads each named source at its commit."""
    for name, commit in pins.items():
        path = config / "sources.d" / f"{name}.toml"
        path.write_text(path.read_text(encoding="utf-8").replace("[source]\n", f'[source]\nref = "{commit}"\n', 1),
                        encoding="utf-8")


def fidelity(
    files: list[tuple[Path, str]], root: Path, config_root: Path, size: int = 200, seed: int = 1021,
    now: datetime | None = None, window: Window | None = None,
) -> dict[str, Any]:
    """Replay a stratified sample of recorded hook events on the program and
    records live had at the time, and give every difference its cause."""
    import tarfile

    from .pretool import cwd_projects

    now = now or datetime.now(timezone.utc)
    candidates = _fidelity_candidates(files, window or Window())
    # An event after the ledger's newest commit may have run on a version
    # this ledger (a copy, say) does not hold.
    newest = _git(root, "log", "-1", "--format=%cI")
    try:
        newest_at = datetime.fromisoformat(newest).astimezone(timezone.utc).isoformat()[:19] if newest else ""
    except ValueError:
        newest_at = ""
    later = [item for item in candidates if newest_at and item["at"][:19] > newest_at]
    candidates = [item for item in candidates if item not in later]
    picked = _sample(candidates, size, lambda cwd: cwd_projects(cwd, root), seed)
    ordered = _payloads(files, picked)
    reflogs = _source_reflogs(config_root)

    def reindex(folder: Path, config: Path) -> None:
        env = {key: value for key, value in os.environ.items() if key not in _SESSION_ENV}
        env.update({"AGENT_MEMORY_HOME": str(folder), "AGENT_MEMORY_CONFIG_HOME": str(config), "PYTHONDONTWRITEBYTECODE": "1"})
        subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
                        "from agent_memory import core; core.reindex(Path(sys.argv[1]))", str(folder)],
                       env=env, check=True, cwd=str(folder), capture_output=True)

    def replay_items(items: list[dict[str, Any]], runners: list[Runner]) -> None:
        sessions: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for item in items:
            if item.get("event") and "since" in item:
                sessions.setdefault((item["f"], item["session"]), []).append(item)
            elif item.get("event"):
                # Each event alone, as the first of its session: two
                # sampled from one session must not share what the
                # replay marked as injected or given.
                event = {**item["event"], "session_id": f"{item['event'].get('session_id') or 'session'}~{item['f']}~{item['i']}"}
                for runner in runners:
                    runner.send({"f": item["f"], "i": item["i"], "t": "prompt" if item["hook"] == "UserPromptSubmit" else "tool",
                                 "agent": item["agent"], "event": event, "at": item["at"]})
        for (number, session), members in sessions.items():
            # After F2 went live: the session's events since its last
            # reset, in order, so what it was given builds up as it did.
            start, end = min(item["since"] for item in members), max(item["i"] for item in members)
            for item in members:
                item["start"] = start
            marked = next((item.get("pretool_marked") for item in members if item["since"] == start), None)
            for index, hook, at in ordered.get((number, session), []):
                if start <= index <= end:
                    event = {**{key: value for key, value in hook.items() if key != "_recorded"},
                             "session_id": f"{hook.get('session_id') or 'session'}~{number}~ordered"}
                    if marked:
                        event["pretool_marked"], marked = marked, None  # with the pass's first event
                    kind = "start-given" if "given" in hook else _HOOK_KIND[hook["hook_event_name"]]
                    for runner in runners:
                        runner.send({"f": number, "i": index, "t": kind, "agent": members[0]["agent"], "event": event, "at": at})

    versions: dict[str, dict[str, Any]] = {}
    for item in picked:
        commit = (_git(root, "rev-list", "-1", "--first-parent", f"--before={item['at'][:19]}Z", "HEAD")
                  if re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", item["at"]) else "")
        if not commit:
            item["result"] = "no-version"
            continue
        key = _git(root, "ls-tree", commit, *STORE_PATHS) or commit
        versions.setdefault(key, {"commit": commit, "items": []})["items"].append(item)
    work = Path(tempfile.mkdtemp(prefix="memoryctl-fidelity-"))
    restore = _end_on_signals()
    try:
        for number, version in enumerate(versions.values()):
            folder = work / f"v{number}"
            folder.mkdir()
            paths = [name for name in STORE_PATHS
                     if subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{version['commit']}:{name}"],
                                       capture_output=True).returncode == 0]
            archive = subprocess.Popen(["git", "-C", str(root), "archive", "--format=tar", version["commit"], *paths],
                                       stdout=subprocess.PIPE)
            with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
                tar.extractall(folder, filter="data")
            archive.wait()
            with_feedback = copy_config(config_root, work / f"c{number}")
            without = work / f"n{number}"
            shutil.copytree(with_feedback, without, symlinks=True)
            (without / "runtime" / "feedback.json").unlink(missing_ok=True)
            reindex(folder, with_feedback)
            runners = [Runner(folder, folder, with_feedback), Runner(folder, folder, without)]
            try:
                replay_items(version["items"], runners)
            finally:
                for runner in runners:
                    runner.finish()
            for item in version["items"]:
                if "start" in item:
                    # What the replay and the recording had given since the
                    # session's last reset, each record when first: those only
                    # one side had, with which side and when.
                    packs = runners[0].packs.get(item["f"], {})
                    first: dict[str, dict[str, str]] = {"replay": {}, "recorded": {}}
                    for index, hook, at in ordered.get((item["f"], item["session"]), []):
                        if item["since"] <= index < item["i"] and "_recorded" in hook:
                            then = packs.get(index)
                            for record_id in (then.ids if then else ()):
                                first["replay"].setdefault(record_id, at)
                            for record_id in hook["_recorded"]:
                                first["recorded"].setdefault(record_id, at)
                    item["given_apart"] = {record_id: [side, at] for side, other in (("replay", "recorded"), ("recorded", "replay"))
                                           for record_id, at in first[side].items() if record_id not in first[other]}
                pack = runners[0].packs.get(item["f"], {}).get(item["i"])
                replayed = pack.ids if pack else set()
                item["replayed_pointers"] = pack.pointers if pack else ()
                deny = bool(pack and pack.length) and not pack.pretool and item["hook"] == "PreToolUse"
                actual = set(item["actual"])
                without_feedback = runners[1].packs.get(item["f"], {}).get(item["i"])
                if replayed == actual and deny == item["deny"]:
                    item["result"] = "same"
                elif item["hook"] == "UserPromptSubmit" and (without_feedback.ids if without_feedback else set()) == actual:
                    item["result"] = "feedback"  # PreToolUse never reads the ranking feedback
                else:
                    item["result"] = _cause(item, replayed, deny, folder, config_root, now)
                if item["result"] == "prefix":  # the records that link it
                    item["given_apart"] = {record_id: how for record_id, how in item["given_apart"].items()
                                           if record_id in _shown_apart(item, replayed)}
                item["replayed"] = tuple(sorted(replayed))
                item["version"] = version["commit"][:8]
            # Unexplained, or only by a setting changed since: replayed again
            # with each source read from its working tree at the commit its
            # HEAD was at then. A later document reaches past its own record:
            # it takes a slot, or changes what the prompt's words weigh
            # (F16 re-check).
            retry: dict[tuple[tuple[str, str], ...], list[dict[str, Any]]] = {}
            for item in version["items"]:
                if item.get("result") in ("config", "unknown") and item.get("event"):
                    pins = _pins_at(reflogs, item["at"])
                    if pins:
                        retry.setdefault(tuple(sorted(pins.items())), []).append(item)
            for attempt, (pins, items) in enumerate(retry.items()):
                pinned = copy_config(config_root, work / f"p{number}-{attempt}")
                _pin_sources(pinned, dict(pins))
                reindex(folder, pinned)
                runner = Runner(folder, folder, pinned)
                try:
                    replay_items(items, [runner])
                finally:
                    runner.finish()
                for item in items:
                    pack = runner.packs.get(item["f"], {}).get(item["i"])
                    deny = bool(pack and pack.length) and not pack.pretool and item["hook"] == "PreToolUse"
                    if (pack.ids if pack else set()) == set(item["actual"]) and deny == item["deny"]:
                        item["result"], item["pinned"] = "sources", dict(pins)
                shutil.rmtree(pinned, ignore_errors=True)
            shutil.rmtree(folder, ignore_errors=True)
    finally:
        restore()
        shutil.rmtree(work, ignore_errors=True)
    results = Counter(item.get("result", "no-version") for item in picked)
    differing = [{**{key: item.get(key) for key in ("stratum", "session", "at", "version", "result", "actual", "replayed",
                                                    "pointers", "replayed_pointers")},
                  **({"given_apart": item["given_apart"]} if item.get("result") == "prefix" else {}),
                  **({"pinned": item["pinned"]} if item.get("pinned") else {})}
                 for item in picked if item.get("result") not in ("same",)]
    return {"sampled": len(picked), "versions": len(versions), "results": dict(results),
            "after_ledger": len(later), "unverified": sum(n for result, n in results.items() if result in UNVERIFIED),
            "strata": dict(Counter(item["stratum"] for item in picked)), "differing": differing,
            "holds": sum(item["deny"] for item in picked)}


# --- Report lines ---------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def watch_lines(result: dict[str, Any]) -> list[str]:
    """Where each watched record was injected, by kind of turn and session."""
    lines = []
    for item in result.get("watch") or []:
        turn = turns.LABELS.get(item["turn"], item["turn"])
        lines.append(f"- 注入 {item['id']}：{item['session']} 的{turn}回合 {item['count']} 次")
    return lines


def reading_lines(result: dict[str, Any]) -> list[str]:
    """How many transcripts and sessions were read, by agent; unreadable
    ones are named as such, never folded into an empty week."""
    files, sessions, unreadable = result.get("files") or {}, result.get("sessions_by_agent") or {}, result.get("unreadable") or {}
    missing = result.get("missing") or {}
    agents = (("claude", "Claude"), ("codex", "Codex"))
    parts = [f"{label} {files.get(agent, 0):,} 份紀錄（{sessions.get(agent, 0):,} 個 session）" for agent, label in agents]
    bad = "、".join(f"{label} {unreadable[agent]} 份" for agent, label in agents if unreadable.get(agent))
    gone = "、".join(f"{label}（{'、'.join(missing[agent])}）" for agent, label in agents if missing.get(agent))
    return [f"- 讀了 {'、'.join(parts)}" + (f"；讀不到：{bad}" if bad else "") + (f"；資料夾不存在：{gone}" if gone else "")]


def summary_lines(result: dict[str, Any], last: dict[str, Any] | None = None, label: str = "上週") -> list[str]:
    """The five numbers as report lines, each with last week's (or another
    run's) beside it."""
    last = last or {}

    def before(path: tuple[str, ...], percent: bool = False) -> str:
        value: Any = last
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            return "n/a"
        return _pct(value) if percent else f"{value:,}" if isinstance(value, int) else str(value)

    chars = result["chars"]
    per = chars["per_session"]
    repeats = result["repeats"]
    non_user = result["non_user"]
    cross = result["cross_project"]
    usage = result["usage"]
    kinds = "、".join(f"{turns.LABELS.get(kind, kind)} {n}" for kind, n in sorted(non_user["packs"].items(), key=lambda kv: -kv[1])
                     if kind != turns.HUMAN)
    lines = [
        f"- 每個 session 的注入字數：中位數 {per['median']:,}、最多 {per['max']:,}（{per['max_session']}）；"
        f"{result['sessions']} 個 session 合計 UPS {chars['total'].get('UserPromptSubmit', 0):,}、"
        f"PreToolUse {chars['total'].get('PreToolUse', 0):,}、SessionStart {chars['total'].get('SessionStart', 0):,}"
        f"（{label}中位數：{before(('chars', 'per_session', 'median'))}）",
        f"- 同一筆重複完整注入：{repeats['repeats']:,} 次（完整注入 {repeats['full']:,} 次、不同的 {repeats['distinct']:,} 筆；"
        f"其中壓縮後重新給的 {repeats['after_compact']:,} 次）（{label}：{before(('repeats', 'repeats'))}）",
    ]
    lines += [f"  - {item['id']} 在 {item['session']} {item['count']} 次" for item in repeats["top"][:3]]
    persisted = result.get("persisted") or {}
    packs = persisted.get("packs") or {}
    lines.append(f"- 超過 Claude Code 上限、被存成檔案（沒送到）的注入包：SessionStart {packs.get('SessionStart', 0):,} 次、"
                 f"UPS {packs.get('UserPromptSubmit', 0):,} 次（{label}：{before(('persisted', 'total'))} 次）")
    lines.append(f"- 非使用者回合的注入：{_pct(non_user['ratio'])}（字數 {_pct(non_user['chars_ratio'])}）"
                 + (f"；{kinds}" if kinds else "") + f"（{label}：{before(('non_user', 'ratio'), True)}）")
    lines.append(f"- 跨專案注入：{_pct(cross['ratio'])}（{cross['records']:,}/{cross['of_all']:,} 筆次）"
                 f"（{label}：{before(('cross_project', 'ratio'), True)}）")
    lines += [f"  - {item['id']} 在 {item['session']} {item['count']} 次" for item in cross["top"][:3]]
    injected = usage["injected"]
    lines.append(f"- 使用率：{_pct(usage['rate'])}（{usage['used']:,}/{injected:,}；明寫 id {usage['explicit']:,}"
                 f"（{_pct(usage['explicit'] / injected if injected else None)}）、內容相符 {usage['content']:,}"
                 f"（{_pct(usage['content'] / injected if injected else None)}））（{label}：{before(('usage', 'rate'), True)}）")
    return lines
