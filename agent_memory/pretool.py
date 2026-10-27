"""The PreToolUse hook: failure records for this command, and speed bumps.

Runs before every shell command an agent issues (spec 記憶路由樹 v1.1, 4.2
with review 8.8-8.13), so it is its own entry point: bin/memoryctl hands
`hook pre-tool-use` straight to main() here, and this module imports only the
standard library and agent_memory.storage. It never opens SQLite; it reads
router.toml, machine.json, and .cache/triggers.json, which reindex writes.

Two things can happen, and only these three outputs exist:

  (a) silence:   {"continue":true}
  (b) injection: a `## FOR THIS COMMAND` block of at most two failure records
                 whose triggers match the command, each once per session
  (c) one deny:  the first time in a session a command segment matches a
                 speed bump (a one-step command that cannot be undone); the
                 same speed bump passes from then on

Every error, including the two-second self-imposed limit, ends in (a) with
exit code 0: a broken hook must never block a command.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sys
import threading
import time
from typing import Any, Callable, Iterator, TextIO

from .storage import (
    AGENT_OUTBOUND_TOOLS,
    AGENT_SHELL_TOOLS,
    CLAUDE_HOOK_CHAR_LIMIT,
    hook_chars,
    project_membership,
    ID_RE,
    _is_expired,
    _session_key,
    atomic_write_text,
    resolve_speed_bumps,
    config_home,
    estimate_tokens,
    load_router_settings,
    memory_home,
    utc_now,
)

SILENT = {"continue": True}
TIME_LIMIT_SECONDS = 2.0
MAX_INJECTED = 2
STATE_KEEP_SECONDS = 14 * 24 * 3600
STATE_PRUNE_LIMIT = 20
RELEASE_LINE = "本 session 尚未確認過這類指令；確認後再執行一次就會放行。"


class _TimeLimit(Exception):
    pass


# --- Command normalization (spec 8.10, shared with 8.2) ----------------------

_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_LEADING_ENV_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S)*\s+)+")
_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
_WRAPPERS = frozenset({"sudo", "doas", "command", "env", "nice", "nohup", "time", "timeout", "xargs", "builtin", "exec"})
_OPTIONS_WITH_ARGUMENT = frozenset({"-u", "-g", "-n", "-k", "-s", "-I", "-P", "-L", "-a"})
_GIT_OPTIONS_WITH_ARGUMENT = frozenset({"-C", "-c"})
_GIT_FLAGS = frozenset({"--no-pager", "-P", "-p"})
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_NESTING_LIMIT = 2


def strip_leading_env(command: str) -> str:
    """The command without leading NAME=value words, whitespace collapsed (spec 8.2)."""
    text = " ".join(command.split())
    return _LEADING_ENV_RE.sub("", text)


def _strip_heredocs(command: str) -> tuple[str, list[str]]:
    """Drop heredoc bodies; a body fed to a shell becomes its own lines."""
    lines = command.split("\n")
    kept: list[str] = []
    extra: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        match = _HEREDOC_RE.search(line)
        if not match:
            continue
        tag, strip_tabs = match.group(3), match.group(1) == "-"
        body = []
        while index < len(lines):
            candidate = lines[index].lstrip("\t") if strip_tabs else lines[index]
            index += 1
            if candidate.strip() == tag:
                break
            body.append(candidate)
        words = line[: match.start()].split()
        while words and _ENV_ASSIGNMENT_RE.match(words[0]):
            words.pop(0)
        if words and os.path.basename(words[0]) in _SHELLS:
            extra.extend(body)
    return "\n".join(kept), extra


def _substitutions(text: str) -> list[str]:
    """The bodies of $( ... ) and backtick substitutions outside single quotes."""
    found = []
    index, quote = 0, None
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "'" and quote is None:
            quote = "'"
        elif char == '"':
            quote = None if quote == '"' else '"'
        elif text.startswith("$(", index):
            depth, start = 1, index + 2
            index += 2
            while index < len(text) and depth:
                if text[index] == "(":
                    depth += 1
                elif text[index] == ")":
                    depth -= 1
                index += 1
            found.append(text[start: index - 1 if not depth else index])
            continue
        elif char == "`":
            end = text.find("`", index + 1)
            if end < 0:
                break
            found.append(text[index + 1: end])
            index = end + 1
            continue
        index += 1
    return found


def _split_segments(command: str) -> list[tuple[str, bool]]:
    """(segment, piped from the previous one), split outside quotes on
    newlines, `;`, `&&`, `||`, `|`, and `&` (not the `&` of `>&`, `&>`, `<&`)."""
    segments: list[tuple[str, bool]] = []
    current: list[str] = []
    piped = False

    def flush(next_piped: bool) -> None:
        nonlocal piped
        text = "".join(current).strip()
        if text:
            segments.append((text, piped))
        current.clear()
        piped = next_piped

    index, quote, length = 0, None, len(command)
    while index < length:
        char = command[index]
        if quote:
            current.append(char)
            if char == "\\" and quote == '"' and index + 1 < length:
                current.append(command[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            current.extend((char, command[index + 1]))
            index += 2
            continue
        if char in "'\"":
            quote = char
            current.append(char)
        elif char in "\n;":
            flush(False)
        elif command.startswith("&&", index) or command.startswith("||", index):
            flush(False)
            index += 2
            continue
        elif char == "|":
            flush(True)
            if command.startswith("|&", index):
                index += 1
        elif char == "&":
            previous = command[index - 1] if index else ""
            following = command[index + 1] if index + 1 < length else ""
            if previous in "<>" or following == ">":
                current.append(char)
            else:
                flush(False)
        else:
            current.append(char)
        index += 1
    flush(False)
    return segments


def _drop_wrapper(tokens: list[str]) -> list[str]:
    """Drop one wrapper word with its options (and timeout's duration)."""
    head = os.path.basename(tokens[0])
    rest = tokens[1:]
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)
        if option == "--":
            break
        if option in _OPTIONS_WITH_ARGUMENT and rest:
            rest.pop(0)
    if head == "timeout" and rest:
        rest.pop(0)
    return rest


def _normalize_tokens(tokens: list[str], piped_input: list[str] | None) -> list[str]:
    while tokens:
        head = os.path.basename(tokens[0])
        if _ENV_ASSIGNMENT_RE.match(tokens[0]):
            tokens = tokens[1:]
        elif head in _WRAPPERS:
            tokens = _drop_wrapper(tokens)
            if head == "xargs" and piped_input:
                tokens = tokens + piped_input
        else:
            break
    if not tokens:
        return tokens
    tokens = [os.path.basename(tokens[0]), *tokens[1:]]
    if tokens[0] == "git":
        rest = tokens[1:]
        while rest:
            if rest[0] in _GIT_OPTIONS_WITH_ARGUMENT and len(rest) > 1:
                rest = rest[2:]
            elif rest[0] in _GIT_FLAGS or rest[0].startswith(("--git-dir=", "--work-tree=")):
                rest = rest[1:]
            else:
                break
        tokens = ["git", *rest]
    return tokens


def _nested(tokens: list[str]) -> list[str]:
    """Commands a segment runs inside itself: sh -c STR, eval, find -exec."""
    out = []
    if tokens and tokens[0] in _SHELLS:
        for position, token in enumerate(tokens[1:], start=1):
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", token) and position + 1 < len(tokens):
                out.append(tokens[position + 1])
                break
    if tokens and tokens[0] == "eval" and len(tokens) > 1:
        out.append(" ".join(tokens[1:]))
    if tokens and tokens[0] == "find":
        for position, token in enumerate(tokens):
            if token in _FIND_EXEC:
                command = []
                for item in tokens[position + 1:]:
                    if item in {";", "+"}:
                        break
                    command.append(item)
                if command:
                    out.append(shlex.join(command))
    return out


def _strip_comments(text: str) -> str:
    """The text without its shell comments: from an unquoted `#` that starts
    a word to the end of its line, as the shell reads it. `a#b`, `$#`, an
    escaped or quoted `#` stay; an apostrophe inside a comment (`# it's
    clean`) never opens a quote (spec 8.0 ruling 53 a)."""
    out: list[str] = []
    quote = ""
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if quote:
            out.append(char)
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"' and index + 1 < length:
                out.append(text[index + 1])
                index += 1
        elif char == "\\" and index + 1 < length:
            out.extend((char, text[index + 1]))
            index += 1
        elif char in "'\"":
            quote = char
            out.append(char)
        elif char == "#" and (index == 0 or text[index - 1].isspace() or text[index - 1] in ";&|()"):
            end = text.find("\n", index)
            if end < 0:
                break
            index = end
            continue
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _blank_quotes(text: str) -> str:
    """The text with every quoted content replaced by spaces; the quotes
    themselves stay in place."""
    out: list[str] = []
    quote = ""
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
                out.append(char)
            elif char == "\\" and quote == '"' and index + 1 < length:
                out.append("  ")
                index += 1
            else:
                out.append("\n" if char == "\n" else " ")
        elif char == "\\" and index + 1 < length:
            out.extend((char, text[index + 1]))
            index += 1
        elif char in "'\"":
            quote = char
            out.append(char)
        else:
            out.append(char)
        index += 1
    return "".join(out)


def explicit_text(command: str) -> str:
    """What an explicit trigger searches (spec 8.0 ruling 54): the command
    without heredoc bodies, comments, and quoted contents (the quotes stay
    where they were), leading NAME=value words dropped. A body fed to a
    shell (`bash <<EOF`) runs, so it stays, as for the speed bumps."""
    text, shell_bodies = _strip_heredocs(command)
    return strip_leading_env(_blank_quotes(_strip_comments("\n".join([text, *shell_bodies]))))


def _split_words(segment: str) -> list[str]:
    """shlex words; a quote left open is closed at the end of the segment,
    and a segment that still cannot be split falls back to its whitespace
    words, so one bad segment never turns the line into one blob."""
    try:
        return shlex.split(segment)
    except ValueError:
        pass
    for closing in ('"', "'", "\\"):
        try:
            return shlex.split(segment + closing if closing != "\\" else segment.rstrip("\\"))
        except ValueError:
            continue
    return segment.split()


def _asks_help(segment: str) -> bool:
    """Whether a raw segment is a help or version query, read from its own
    tokens: a quoted string stays one argument and a comment is no argument
    (spec 8.0 ruling 52 c), so `git commit -m "the --help output"` and
    `git reset --soft HEAD~3  # see --help` are not help queries."""
    return is_help_or_version(_normalize_tokens(_split_words(segment), None))


def _segments(command: str, depth: int) -> list[tuple[str, bool]]:
    text, shell_bodies = _strip_heredocs(command)
    raw = _split_segments(_strip_comments(text)) + [
        (line, False) for line in (_strip_comments(body).strip() for body in shell_bodies) if line
    ]
    out: list[tuple[str, bool]] = []
    previous: list[str] | None = None
    for segment, piped in raw:
        extra = _substitutions(segment)
        tokens = _normalize_tokens(_split_words(segment), previous if piped else None)
        previous = tokens
        if tokens:
            out.append((" ".join(tokens), _asks_help(segment)))
        if depth < _NESTING_LIMIT:
            for inner in [*extra, *_nested(tokens)]:
                out.extend(_segments(inner, depth + 1))
    return out


def command_segments(command: str, depth: int = 0) -> list[str]:
    """Each command the line would run, as tokens joined by single spaces.

    A segment starts at its command word: wrappers (sudo, env, timeout,
    xargs ...), NAME=value words, a path before the command word, and git's
    global options are removed, so a pattern anchored with `^` matches what
    actually runs and never a word inside an argument (`rg "git reset
    --hard"` is an rg command). Comments are dropped before the line is
    split, and a segment shlex cannot split keeps its own words, never the
    whole line (spec 8.0 ruling 53 a).
    """
    return [segment for segment, _ in _segments(command, depth)]


def speed_bump_hits(command: str, bumps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments = command_segments(command)
    return [bump for bump in bumps if any(bump["regex"].search(segment) for segment in segments)]


# --- Failure triggers (spec 8.2) -----------------------------------------------


def _visible(scope: str, machine_id: str | None) -> bool:
    if scope.startswith("machine:"):
        return machine_id is not None and scope == f"machine:{machine_id}"
    return True


# A command asking for help or its version runs nothing a lesson is about
# (spec 記憶路由樹 v1.1, rulings 45 and 51 e): `gh --version` must not inject
# the lesson whose Retry When says "check gh --version first".
HELP_FLAG = "--help"
# Help words that count after a subcommand too (`gh pr -h`); `help` and
# `version` count only right after the command word (`git help`, `gh
# version`), since `git checkout help` switches to a branch named help.
SUBCOMMAND_HELP_WORDS = frozenset({"-h", "-V", "--version"})
SOLE_HELP_WORDS = SUBCOMMAND_HELP_WORDS | {"help", "version"}
_REDIRECT_RE = re.compile(r"^(\d*>>?|&>>?|\d*<|\d*>&\d*-?|\d*<&\d*-?)(\S*)$")
_BARE_REDIRECT_RE = re.compile(r"^(\d*>>?|&>>?|\d*<)$")


def _without_trailing_redirects(words: list[str]) -> list[str]:
    words = list(words)
    while len(words) > 1:
        if len(words) > 2 and _BARE_REDIRECT_RE.fullmatch(words[-2]):
            words = words[:-2]
        elif _REDIRECT_RE.fullmatch(words[-1]):
            words = words[:-1]
        else:
            break
    return words


def is_help_or_version(words: list[str]) -> bool:
    """Given a command's words: `--help` anywhere after the command word;
    -h, -V, --version as the only argument after the command and an
    optional subcommand; help, version as the only argument after the
    command. Trailing redirections are dropped first."""
    words = _without_trailing_redirects(words)
    rest = words[1:]
    if HELP_FLAG in rest:
        return True
    if len(rest) == 1 and rest[0] in SOLE_HELP_WORDS:
        return True
    return len(rest) == 2 and not rest[0].startswith("-") and rest[1] in SUBCOMMAND_HELP_WORDS


# Git subcommands that only look (spec 記憶路由樹 v1.1, ruling 49): in a lesson
# they are the after-the-fact check, not the action that went wrong, so a
# phrase starting with one never triggers it. A lesson about one of these
# commands itself names it in explicit triggers.
READ_ONLY_GIT = frozenset({
    "show", "diff", "log", "status", "blame", "rev-parse", "merge-base",
    "ls-files", "ls-tree", "cat-file", "describe", "shortlog",
})


def phrase_is_inspection(phrase: str) -> bool:
    words = phrase.split()
    return len(words) >= 2 and words[0] == "git" and words[1] in READ_ONLY_GIT


def phrase_mentions_help(phrase: str) -> bool:
    """An extracted phrase that is itself a help or version query, by the
    rule the hook applies to commands."""
    return is_help_or_version(phrase.split())


def trigger_hits(
    command: str, entries: list[dict[str, Any]], machine_id: str | None, stoplist: Any = ()
) -> list[dict[str, Any]]:
    """Failure entries whose explicit regex or extracted phrase matches,
    explicit matches first, then by the longest matching phrase.

    Explicit regexes search the command without its heredoc bodies,
    comments, and quoted contents (ruling 54). A phrase must start a command
    segment (spec 8.0 ruling 36), so a command only mentioned in a quoted
    string or a heredoc body never matches; a phrase in the stoplist never
    matches, even in a triggers.json older than the list.
    """
    normalized = strip_leading_env(command)
    try:
        searched = explicit_text(command)
    except Exception:
        searched = normalized
    try:
        segments = [segment for segment, asks_help in _segments(command, 0) if not asks_help]
    except Exception:
        segments = [normalized]
    stop = set(stoplist)
    ranked = []
    for entry in entries:
        if not _visible(str(entry.get("scope", "")), machine_id) or _is_expired(entry.get("expires_at")):
            continue
        explicit = False
        for pattern in entry.get("explicit") or []:
            try:
                if re.search(str(pattern), searched):
                    explicit = True
                    break
            except re.error:
                continue
        phrase = max(
            (
                len(str(p)) for p in entry.get("phrases") or []
                if str(p) and str(p) not in stop and not phrase_is_inspection(str(p))
                and any(segment.startswith(str(p)) for segment in segments)
            ),
            default=0,
        )
        if explicit or phrase:
            ranked.append((0 if explicit else 1, -phrase, str(entry.get("id", "")), entry))
    return [item[3] for item in sorted(ranked, key=lambda item: item[:3])]


def origin_line(origins: list[str], created_at: str, cwd_projects: "set[str] | list[str]") -> str | None:
    """`- 學到的地方：alpha，2026-10-03` when the lesson was learned in a
    project other than the one the cwd is in, also when it was learned here
    and elsewhere (spec 學到的地方與適用範圍 v1)."""
    if not origins or not set(origins) - set(cwd_projects):
        return None
    names = "、".join(origins[:2]) + (f" 等 {len(origins)} 處" if len(origins) > 2 else "")
    date = created_at[:10]
    return f"- 學到的地方：{names}" + (f"，{date}" if date else "")


def failure_block(entry: dict[str, Any]) -> list[str]:
    lines = [
        "",
        f"### {entry['id']}",
        "- kind: failure",
        f"- value: {entry.get('verified_fix', '')}",
    ]
    if entry.get("origin_line"):
        lines.append(entry["origin_line"])
    return lines + [
        f"- symptom: {entry.get('symptom', '')}",
        f"- retry_when: {entry.get('retry_when', '')}",
    ]


# --- What the agent sends (spec 實際使用回饋改善 F13) --------------------------------
#
# A failure may carry outbound_triggers: regexes for what the agent says,
# not what it runs (a universal word, a schedule nobody estimated). What a
# SendMessage or AskUserQuestion call is about to send is checked here and
# the lesson attached, never held; the agent's plain reply cannot be caught
# by a tool, so the next prompt hook checks it (core._reply_notice). Each
# record reminds a session at most once every turns.OUTBOUND_GAP turns.


def outbound_text(tool: str, tool_input: Any) -> str:
    """What a SendMessage or AskUserQuestion call sends."""
    if not isinstance(tool_input, dict):
        return ""
    if tool == "SendMessage":
        return "\n".join(str(tool_input.get(key) or "") for key in ("message", "summary"))
    parts = []
    for question in tool_input.get("questions") or []:
        if not isinstance(question, dict):
            continue
        parts.append(str(question.get("question") or ""))
        for option in question.get("options") or []:
            if isinstance(option, dict):
                parts.extend(str(option.get(key) or "") for key in ("label", "description"))
    return "\n".join(parts)


# What a message or a reply is checked by: its first and last 10,000
# characters. A pattern that is slow on long text (\d+\s*天 on a run of
# digits takes two seconds at 10,000) stays bounded; a week's replies were
# under 4,000 (F13 review 1).
OUTBOUND_TEXT_MAX = 20_000


def outbound_hits(text: str, cwd: str | None, entries: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The failures whose outbound_triggers match `text`, of those that
    apply in `cwd` (one judged to apply only where it was learned, only
    there)."""
    entries = [entry for entry in (_load_triggers() if entries is None else entries) if entry.get("outbound")]
    if not entries or not text.strip():
        return []
    if len(text) > OUTBOUND_TEXT_MAX:
        text = text[: OUTBOUND_TEXT_MAX // 2] + "\n" + text[-(OUTBOUND_TEXT_MAX // 2):]
    machine_id = _machine_id()
    here = cwd_projects(cwd)
    hits = []
    for entry in entries:
        if _is_expired(entry.get("expires_at")) or not _visible(str(entry.get("scope", "")), machine_id):
            continue
        if entry.get("applies") == "origin" and entry.get("origins") and not set(entry["origins"]) & here:
            continue
        for pattern in entry["outbound"]:
            try:
                if re.search(pattern, text):
                    hits.append(entry)
                    break
            except re.error:
                continue
    return hits


def one_sentence(entry: dict[str, Any], limit: int = 80) -> str:
    """The first sentence of a failure's verified fix: what to do about it."""
    text = re.split(r"[。；;\n]", str(entry.get("verified_fix") or entry.get("symptom") or ""), maxsplit=1)[0].strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _outbound(event: dict[str, Any], agent: str) -> tuple[dict[str, Any], str, str | None]:
    settings = load_router_settings()
    if not settings["enabled"]:
        return SILENT, "ok", None
    hits = outbound_hits(outbound_text(str(event.get("tool_name")), event.get("tool_input")), event.get("cwd"))
    if not hits:
        return SILENT, "ok", None
    from .turns import due_reminders

    session = str(event.get("session_id") or "")
    agent_id = str(event.get("agent_id") or "") or None  # a subagent's reminders are its own
    try:
        due = set(due_reminders(session, [str(entry["id"]) for entry in hits], mark=False, agent_id=agent_id))
    except OSError:
        due = {str(entry["id"]) for entry in hits}
    if not due:
        return SILENT, "ok", "outbound-spaced"
    # The same budget and limit as a shell command's failures.
    budget = int(settings["budgets"].get(agent, settings["budgets"]["claude"])["failures"])
    char_limit = CLAUDE_HOOK_CHAR_LIMIT if agent == "claude" else None
    chosen: list[dict[str, Any]] = []
    for entry in hits:
        if len(chosen) >= MAX_INJECTED:
            break
        text = outbound_injection([*chosen, entry])
        if str(entry["id"]) in due and estimate_tokens(text) <= budget and (char_limit is None or hook_chars(text) <= char_limit):
            chosen.append(entry)
    if not chosen:
        return SILENT, "ok", None
    try:
        # Marked under the lock: a send in the same message may have just reminded one.
        granted = set(due_reminders(session, [str(entry["id"]) for entry in chosen], agent_id=agent_id, sent=True))
    except OSError:
        granted = set()
    chosen = [entry for entry in chosen if str(entry["id"]) in granted]
    if not chosen:
        return SILENT, "ok", "outbound-spaced"
    return (
        {"continue": True, "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": outbound_injection(chosen)}},
        "ok",
        None,
    )


def outbound_injection(entries: list[dict[str, Any]]) -> str:
    from .turns import OUTBOUND_HEADER

    lines = [OUTBOUND_HEADER]
    for entry in entries:
        lines.extend(failure_block(entry))
    return "\n".join(lines)


def injection_text(entries: list[dict[str, Any]]) -> str:
    lines = ["## FOR THIS COMMAND"]
    for entry in entries:
        lines.extend(failure_block(entry))
    lines.extend(["", "## SOURCE IDS", *[f"- {entry['id']}" for entry in entries]])
    return "\n".join(lines)


# --- Session state (spec 8.12) ---------------------------------------------------


def state_dir(session_id: str, agent_id: str | None) -> Path:
    return (
        config_home() / "runtime" / "pretooluse" / _session_key(session_id or "unknown")
        / _session_key(agent_id or "main")
    )


def _prune_state(parent: Path) -> None:
    """Remove session folders untouched for 14 days, at most 20 per call."""
    try:
        cutoff = time.time() - STATE_KEEP_SECONDS
        removed = 0
        for folder in parent.iterdir():
            if removed >= STATE_PRUNE_LIMIT:
                break
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                for path in sorted(folder.rglob("*"), reverse=True):
                    path.rmdir() if path.is_dir() else path.unlink()
                folder.rmdir()
                removed += 1
    except OSError:
        pass


def _ensure_state_dir(folder: Path) -> None:
    session = folder.parent
    if not session.exists():
        _prune_state(session.parent)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)


def _create_exclusive(path: Path) -> bool:
    """True when this call created the file: the first time in this session."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.close(descriptor)
    return True


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def mark_injected(folder: Path, failure_id: str, event: str, prompt_id: str | None) -> bool:
    """Create injected.<id>.json; True when this failure was not yet injected."""
    if not ID_RE.fullmatch(failure_id):
        return False
    _ensure_state_dir(folder)
    path = folder / f"injected.{failure_id}.json"
    if not _create_exclusive(path):
        return False
    atomic_write_text(path, json.dumps({"at": utc_now(), "event": event, "prompt_id": prompt_id}) + "\n")
    return True


def _command_sha256(command: str) -> str:
    return hashlib.sha256(strip_leading_env(command).strip().encode("utf-8")).hexdigest()


def settle_speed_bumps(
    folder: Path, hits: list[dict[str, Any]], command: str, event: dict[str, Any]
) -> list[dict[str, Any]]:
    """The speed bumps this call is the first to meet; counts the rest as passed.

    "First" is decided by O_EXCL file creation alone, so a marker that is
    unreadable or damaged never changes what is allowed. Raises OSError when
    the state cannot be written; the caller then never denies.
    """
    _ensure_state_dir(folder)
    digest = _command_sha256(command)
    fresh = [bump for bump in hits if _create_exclusive(folder / f"bump.{bump['id']}.json")]
    for bump in hits:
        path = folder / f"bump.{bump['id']}.json"
        state = _read_json(path) if bump not in fresh else {}
        if bump in fresh:
            state = {
                "first_at": utc_now(),
                "first_tool_use_id": event.get("tool_use_id"),
                "first_prompt_id": event.get("prompt_id") or event.get("turn_id"),
                "first_command_sha256": digest,
                "deny_count": 0,
                "released_at": None,
                "released_command_sha256": None,
                "pass_count": 0,
            }
        if fresh:
            state["deny_count"] = int(state.get("deny_count") or 0) + 1
        else:
            state["pass_count"] = int(state.get("pass_count") or 0) + 1
            if not state.get("released_at"):
                state["released_at"] = utc_now()
                state["released_command_sha256"] = digest
        atomic_write_text(path, json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n")
    return fresh


# --- Health ----------------------------------------------------------------------


def health_path() -> Path:
    return config_home() / "runtime" / "health" / "pre_tool_use.json"


def record_health(status: str, code: str | None, extra: dict[str, Any] | None = None) -> None:
    """This hook's own health file; never read-modify-write health.json."""
    try:
        item: dict[str, Any] = {"status": status, "checked_at": utc_now()}
        if code:
            item["code"] = code
        item.update(extra or {})
        atomic_write_text(health_path(), json.dumps(item, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --- The hook ----------------------------------------------------------------------


def _load_triggers() -> list[dict[str, Any]]:
    path = memory_home() / ".cache" / "triggers.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    entries = value.get("entries") if isinstance(value, dict) else None
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("id")] if isinstance(entries, list) else []


def cwd_projects(cwd: str | None, root: Path | None = None) -> set[str]:
    """The projects a cwd is in, from the table reindex writes in `root`
    (the memory home by default)."""
    if not cwd:
        return set()
    try:
        table = json.loads(((root or memory_home()) / ".cache" / "project_paths.json").read_text(encoding="utf-8"))
        resolved = Path(cwd).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return set()
    projects = table.get("projects", {}) if isinstance(table, dict) else {}
    if not isinstance(projects, dict):
        return set()

    def strings(value: Any) -> list[str]:
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    return {
        str(name) for name, item in projects.items()
        if isinstance(item, dict)
        and project_membership(resolved, str(name), strings(item.get("scope_paths")), strings(item.get("source_dirs")))
    }


def _machine_id() -> str | None:
    try:
        value = json.loads((config_home() / "machine.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    machine_id = value.get("machine_id") if isinstance(value, dict) else None
    return str(machine_id) if machine_id else None


def _deny_reason(fresh: list[dict[str, Any]], by_id: dict[str, dict[str, Any]], machine_id: str | None) -> str:
    lines, cited = [], []
    for bump in fresh:
        lines.append(bump["reason"])
        for failure_id in bump.get("failure_ids", []):
            entry = by_id.get(failure_id)
            if entry is None or not _visible(str(entry.get("scope", "")), machine_id):
                continue
            if failure_id not in cited:
                cited.append(failure_id)
            if not _is_expired(entry.get("expires_at")):
                lines.append(f"{failure_id}：{entry.get('symptom', '')} 驗證過的做法：{entry.get('verified_fix', '')}")
    lines.append(RELEASE_LINE)
    if cited:
        lines.append("依據 " + "、".join(cited))
    return "\n".join(lines)


def decide(event: dict[str, Any], agent: str) -> tuple[dict[str, Any], str, str | None]:
    """(output, health status, health code) for one PreToolUse event."""
    if event.get("hook_event_name") != "PreToolUse":
        return SILENT, "ok", "not-pre-tool-use"
    tool = event.get("tool_name")
    if tool in AGENT_OUTBOUND_TOOLS.get(agent, ()):
        return _outbound(event, agent)
    if tool not in AGENT_SHELL_TOOLS.get(agent, AGENT_SHELL_TOOLS["claude"]):
        return SILENT, "ok", None
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return SILENT, "ok", None
    settings = load_router_settings()
    codes = [settings["code"]] if settings.get("code") else []
    bumps: list[dict[str, Any]] = []
    if settings["speed_bump_enabled"]:
        bumps, problems = resolve_speed_bumps(settings)
        if problems and "router-toml-invalid" not in codes:
            codes.append("router-toml-invalid")
    entries = _load_triggers()
    by_id = {str(entry["id"]): entry for entry in entries}
    machine_id = _machine_id()
    folder = state_dir(str(event.get("session_id") or ""), event.get("agent_id"))
    prompt_id = event.get("prompt_id") or event.get("turn_id")
    status = "ok"

    hits = speed_bump_hits(command, bumps) if bumps else []
    released: list[str] = []
    if hits:
        try:
            fresh = settle_speed_bumps(folder, hits, command, event)
        except OSError:
            fresh = []
            status, codes = "error", [*codes, "state-write-failed"]
        else:
            if fresh:
                return (
                    {
                        "continue": True,
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": _deny_reason(fresh, by_id, machine_id),
                        },
                    },
                    status,
                    ",".join(codes) or None,
                )
        released = [failure_id for bump in hits for failure_id in bump.get("failure_ids", [])]

    if not settings["enabled"]:
        return SILENT, status, ",".join(codes) or None
    candidates = [by_id[i] for i in released if i in by_id and _visible(str(by_id[i].get("scope", "")), machine_id)]
    candidates = [c for c in candidates if not _is_expired(c.get("expires_at"))]
    hits_by_trigger = trigger_hits(command, entries, machine_id, settings.get("phrase_stoplist", ()))
    candidates += [entry for entry in hits_by_trigger if entry not in candidates]
    here = cwd_projects(event.get("cwd"))
    # Every failure follows the command across projects, except one someone
    # judged to apply only where it was learned.
    candidates = [
        entry for entry in candidates
        if not (entry.get("applies") == "origin" and entry.get("origins") and not set(entry["origins"]) & here)
    ]
    candidates = [
        {**entry, "origin_line": origin_line(list(entry.get("origins") or []), str(entry.get("created_at") or ""), here)}
        for entry in candidates
    ]
    budget = int(settings["budgets"].get(agent, settings["budgets"]["claude"])["failures"])
    char_limit = CLAUDE_HOOK_CHAR_LIMIT if agent == "claude" else None
    chosen: list[dict[str, Any]] = []
    for entry in candidates:
        if len(chosen) >= MAX_INJECTED:
            break
        text = injection_text([*chosen, entry])
        if estimate_tokens(text) > budget or (char_limit is not None and hook_chars(text) > char_limit):
            continue
        try:
            first = mark_injected(folder, str(entry["id"]), "PreToolUse", prompt_id)
        except OSError:
            first = True
            if "state-write-failed" not in codes:
                status, codes = "error", [*codes, "state-write-failed"]
        if first:
            chosen.append(entry)
    if not chosen:
        return SILENT, status, ",".join(codes) or None
    return (
        {
            "continue": True,
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": injection_text(chosen)},
        },
        status,
        ",".join(codes) or None,
    )


def _agent_from(argv: list[str]) -> str:
    for index, value in enumerate(argv):
        if value == "--agent" and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith("--agent="):
            return value.split("=", 1)[1]
    # Without --agent, the conservative budgets, as for the other hooks.
    return "codex"


def _on_time_limit(signum: int, frame: Any) -> None:
    raise _TimeLimit()


@contextmanager
def time_limit(seconds: float) -> Iterator[None]:
    """Raise _TimeLimit in the block after `seconds`. A SIGALRM timer, so
    only where one can be set: POSIX, the main thread, no timer running
    already (main()'s covers what it calls); elsewhere the block runs as is."""
    if (not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread()
            or signal.getitimer(signal.ITIMER_REAL)[0] > 0):
        yield
        return
    previous = signal.signal(signal.SIGALRM, _on_time_limit)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        finally:
            signal.signal(signal.SIGALRM, previous)


def main(
    argv: list[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    decide_fn: Callable[[dict[str, Any], str], tuple[dict[str, Any], str, str | None]] | None = None,
) -> int:
    """Read one event, print one of the three outputs, and return 0 — always."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    output: dict[str, Any] = SILENT
    timer = hasattr(signal, "setitimer")
    previous = None
    try:
        if timer:
            previous = signal.signal(signal.SIGALRM, _on_time_limit)
            signal.setitimer(signal.ITIMER_REAL, TIME_LIMIT_SECONDS)
        try:
            event = json.loads(stdin.read())
            if not isinstance(event, dict):
                raise ValueError("event is not an object")
            output, status, code = (decide_fn or decide)(event, _agent_from(list(argv or [])))
            record_health(status, code)
        except _TimeLimit:
            output = SILENT
            record_health("error", "time-limit")
        except Exception as exc:  # fail open: a broken hook never blocks a command
            output = SILENT
            record_health("error", f"failed:{type(exc).__name__}")
    except _TimeLimit:
        output = SILENT
    finally:
        if timer:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if previous is not None:
                signal.signal(signal.SIGALRM, previous)
    try:
        stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")
        stdout.flush()
    except Exception:
        pass
    return 0
