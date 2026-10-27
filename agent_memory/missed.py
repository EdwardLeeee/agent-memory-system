"""Moments a lesson was likely learned, and whether memory was written after
them; measured only, nothing is recorded for the agent (spec 騰訊借鏡改善 T1).

Three signals, read from the week's transcripts with replay's reader:
an AskUserQuestion the user answered; a rule word (以後, 記住, never, ...) in
a turn the user typed (F1's kinds), each prompt once a session, an English
one only in a prompt of at most 300 characters, and not when the reply was
only a usage, login, quota or permission error; a shell command that failed
and, within ten minutes in the same session, succeeded with the same key
(its first two words after a leading `cd <dir> &&` and `VAR=value`, split
as the shell does; a generic head such as `python3 -c`, `sed -i` or `git -C
<dir>` with the next meaningful word too), after two failures or two
minutes. A failure is the call's error with Claude's `Exit code N` or the
exit code Codex's wrapper heads the output with, never the output's words;
grep, diff, cmp or test ending a command with 1 found nothing, which is no
failure; a refused call is neither.

A signal counts as recorded when the same session, within thirty minutes,
wrote a record or a supersede (learned_in, from the record or the transcript
backfill) or made a commit (T6) touching a path a source's parser reads;
within ninety, as recorded late; a capsule saved in those ninety minutes
alone is listed apart, as capsule only (designer 2026-10-21, after the 20
positions were judged: none worth recording was recorded nowhere). Only
counts, session ids, times and kinds are kept, never what was said or run.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

SIGNALS = ("question", "rule-word", "retry")
SIGNAL_NAMES = {"question": "AskUserQuestion 的回答", "rule-word": "使用者打的規則用語", "retry": "指令失敗後改對"}
OUTCOMES = ("recorded", "late", "capsule", "none")
OUTCOME_NAMES = {"recorded": "有記", "late": "晚記", "capsule": "只在膠囊", "none": "沒記"}
RULE_WORDS = ("以後", "不要再", "記得", "記住", "改成", "always", "never", "from now on", "remember")
RECORD_WINDOW = timedelta(minutes=30)
LATE_WINDOW = timedelta(minutes=90)
RETRY_WINDOW = timedelta(minutes=10)
RETRY_SLOW = timedelta(minutes=2)
RETRY_FAILURES = 2
ENGLISH_PROMPT_CHARS = 300
POSITIONS = 20
# A reply that is only one of these said nothing about the rule.
_ERROR_REPLY_RE = re.compile(r"usage limit|rate limit|/login|log ?in|quota|credit balance|permission|api error|"
                             r"invalid api key|overloaded|用量|額度|登入|權限", re.I)
_ERROR_REPLY_CHARS = 400
# Heads that say little alone: the next meaningful word is part of the key.
_GENERIC_HEADS = frozenset({("python3", "-"), ("python3", "-c"), ("python3", "-I"), ("python3", "-m"),
                            ("python", "-"), ("python", "-c"), ("python", "-I"), ("python", "-m"),
                            ("sed", "-i"), ("git", "-C")})
# Ending a command with 1, these found nothing; that is no failure.
_FOUND_NOTHING = frozenset({"grep", "egrep", "fgrep", "rg", "diff", "cmp", "test", "["})
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=", re.S)


def rule_words(config_root: Path) -> list[str]:
    """The rule words: doctor/missed.toml's rule_words, else the default."""
    import tomllib

    try:
        words = tomllib.loads((config_root / "doctor" / "missed.toml").read_text(encoding="utf-8")).get("rule_words")
    except (OSError, ValueError):
        words = None
    if isinstance(words, list) and words and all(isinstance(word, str) and word.strip() for word in words):
        return [word.strip() for word in words]
    return list(RULE_WORDS)


def _rule_patterns(words: list[str]) -> tuple[re.Pattern[str] | None, re.Pattern[str] | None]:
    """(Chinese words, English words), each None when there are none."""
    chinese = [re.escape(word) for word in words if not word.isascii()]
    english = [rf"(?<![a-z]){re.escape(word.casefold())}(?![a-z])" for word in words if word.isascii()]
    return (re.compile("|".join(chinese)) if chinese else None), (re.compile("|".join(english)) if english else None)


def _time(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _words(command: str) -> list[str]:
    """The words of a command's first line as the shell splits them, without
    a leading `cd <dir> &&` or `VAR=value` (quoted, spaces and all)."""
    text = command.strip()
    while (lead := re.match(r"cd\s+\S+\s*(?:&&|;)\s*", text)) is not None:
        text = text[lead.end():]
    line = text.split("\n", 1)[0]
    try:
        words = shlex.split(line, posix=True)
    except ValueError:
        words = line.split()
    while words and _ASSIGNMENT_RE.match(words[0]):
        words.pop(0)
    return words


def _command_key(command: str) -> str:
    """What makes two commands the same for a retry: the first two words, a
    generic head with the next meaningful word (a script fed on stdin has
    none and is compared with nothing); never kept."""
    words = _words(command)
    if not words:
        return ""
    head = tuple(words[:2])
    if head not in _GENERIC_HEADS:
        return " ".join(head)
    rest = words[3:] if head == ("git", "-C") else words[2:]
    meaningful = next((word for word in rest if not word.startswith(("-", "<", ">"))), "")
    if not meaningful:
        return ""
    return " ".join([*head, " ".join(meaningful.split()[:2])])


def _last_command(command: str) -> str:
    """The program the command ends with: after its last |, &&, || or ;."""
    tail = re.split(r"\|\||&&|[|;]", command.strip().split("\n", 1)[0])[-1]
    words = _words(tail)
    return Path(words[0]).name if words else ""


def writes(root: Path) -> dict[str, list[datetime]]:
    """When each session wrote a record version: the versions' learned_in,
    current and archived, and the transcript backfill's cache."""
    from .learned import load_cache
    from .storage import decode_record

    found: dict[str, set[datetime]] = defaultdict(set)
    for path in [*root.glob("records/*/*.md"), *root.glob("archive/*/*/*.md")]:
        try:
            learned = decode_record(path)[0].get("learned_in")
        except Exception:
            continue
        if isinstance(learned, dict) and learned.get("session_id") and (moment := _time(learned.get("at"))):
            found[str(learned["session_id"])].add(moment)
    for versions in load_cache(root)["records"].values():
        for item in versions if isinstance(versions, list) else []:
            if isinstance(item, dict) and item.get("session_id") and (moment := _time(item.get("at"))):
                found[str(item["session_id"])].add(moment)
    return {session: sorted(moments) for session, moments in found.items()}


def project_commits(config_root: Path, since: datetime | None = None) -> dict[str, list[datetime]]:
    """When each session made a commit (T6's notes) that touched a path one
    of its source's parsers reads: written in the project's own records."""
    from .commits import _read
    from .federation import FederationConfigError, _glob_match, load_source_config
    from .storage import git_environment

    sources: dict[str, Any] = {}
    found: dict[str, list[datetime]] = defaultdict(list)
    for note in _read(config_root):
        moment = _time(note.get("at"))
        if not note.get("session_id") or moment is None or (since is not None and moment < since):
            continue
        name = str(note["repo"])
        if name not in sources:
            try:
                sources[name] = load_source_config(config_root, name)
            except (FederationConfigError, OSError):
                sources[name] = None
        config = sources[name]
        if not config:
            continue
        try:
            done = subprocess.run(["git", "-c", "core.quotepath=off", "-C", str(config["repo"]), "show", "--name-only",
                                   "--format=", str(note["commit"])], capture_output=True, text=True, timeout=10,
                                  env=git_environment(), check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        globs = [str(parser.get("glob") or "") for parser in config.get("parsers") or []]
        if any(_glob_match(path, glob) for path in done.stdout.splitlines() if path for glob in globs if glob):
            found[str(note["session_id"])].append(moment)
    return {session: sorted(moments) for session, moments in found.items()}


def _outcome(signal: dict[str, Any], written: dict[str, list[datetime]], committed: dict[str, list[datetime]],
             capsules: dict[str, list[datetime]]) -> str:
    moment, session = signal["at"], signal["session"]
    times = [*written.get(session, ()), *committed.get(session, ())]
    if any(moment <= at <= moment + RECORD_WINDOW for at in times):
        return "recorded"
    if any(moment + RECORD_WINDOW < at <= moment + LATE_WINDOW for at in times):
        return "late"
    if any(moment <= at <= moment + LATE_WINDOW for at in capsules.get(session, ())):
        return "capsule"
    return "none"


def measure(
    files: Iterable[tuple[Path, str]], root: Path, cwd_projects: Callable[[str], set[str]], window: Any,
    config_root: Path,
) -> dict[str, Any]:
    from . import turns
    from .replay import timeline

    chinese, english = _rule_patterns(rule_words(config_root))
    signals: list[dict[str, Any]] = []
    capsules: dict[str, list[datetime]] = defaultdict(list)

    def signal(kind: str, event: dict[str, Any], session: str, moment: datetime) -> dict[str, Any]:
        projects = sorted(cwd_projects(str(event.get("cwd") or ""))) or ["(沒有專案)"]
        return {"signal": kind, "session": session, "at": moment, "project": projects[0]}

    def settle(pending: dict[str, Any] | None) -> None:
        """A rule word counts unless its turn's reply was only an error."""
        if pending is None:
            return
        outputs = pending["outputs"]
        if outputs and all(len(text) <= _ERROR_REPLY_CHARS and _ERROR_REPLY_RE.search(text) for text in outputs):
            return
        signals.append(pending["signal"])

    for path, agent in files:
        calls: dict[str, tuple[str, str, str]] = {}
        failing: dict[tuple[str, str], dict[str, Any]] = {}
        asked: set[tuple[str, str]] = set()
        pending: dict[str, dict[str, Any]] = {}
        for event in timeline(path, agent, results=True):
            if not window.keep(event):
                continue
            kind, session, moment = event["t"], str(event.get("session") or ""), _time(event.get("at"))
            if moment is None:
                continue
            if kind == "prompt":
                settle(pending.pop(session, None))
                text = str(event.get("text") or "")
                said = (session, hashlib.sha256(text.encode("utf-8")).hexdigest())
                if event.get("kind") != turns.HUMAN or said in asked:
                    continue
                asked.add(said)  # the same prompt again counts once
                folded = text.casefold()
                if (chinese and chinese.search(text)) or (english and len(text) <= ENGLISH_PROMPT_CHARS and english.search(folded)):
                    pending[session] = {"signal": signal("rule-word", event, session, moment), "outputs": []}
            elif kind == "output":
                if session in pending:
                    pending[session]["outputs"].append(str(event.get("text") or ""))
            elif kind == "tool" and event.get("tool_id") and event.get("name") in ("Bash", "AskUserQuestion"):
                command = str((event.get("input") or {}).get("command") or "") if event["name"] == "Bash" else ""
                if command and re.search(r"memoryctl\b.*\bcheckpoint\b", command) and "--show" not in command:
                    capsules[session].append(moment)
                calls[str(event["tool_id"])] = (str(event["name"]), _command_key(command), _last_command(command))
            elif kind == "result" and (call := calls.pop(str(event.get("tool_id")), None)) is not None:
                name, key, last = call
                if name == "AskUserQuestion":
                    if not event.get("is_error"):
                        signals.append(signal("question", event, session, moment))
                    continue
                if not key or event.get("denied"):
                    continue
                code = event.get("code")
                failed = bool(event.get("is_error")) and not (code == 1 and last in _FOUND_NOTHING)
                if failed:
                    entry = failing.setdefault((session, key), {"first": moment, "count": 0})
                    entry["count"] += 1
                    entry["last"] = moment
                elif (entry := failing.pop((session, key), None)) is not None and moment - entry["last"] <= RETRY_WINDOW \
                        and (entry["count"] >= RETRY_FAILURES or moment - entry["first"] >= RETRY_SLOW):
                    signals.append(signal("retry", event, session, moment))
        for left in pending.values():
            settle(left)
    since = min((item["at"] for item in signals), default=None)
    written = writes(root)
    committed = project_commits(config_root, since)
    for item in signals:
        item["outcome"] = _outcome(item, written, committed, capsules)
    blank = {"count": 0, **{outcome: 0 for outcome in OUTCOMES}}
    totals = {name: dict(blank) for name in SIGNALS}
    by_project: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: {name: dict(blank) for name in SIGNALS})
    for item in signals:
        for table in (totals, by_project[item["project"]]):
            table[item["signal"]]["count"] += 1
            table[item["signal"]][item["outcome"]] += 1
    # Retries only as counts: what was retried is not shown (designer 2026-10-21).
    missing = sorted((item for item in signals if item["outcome"] == "none" and item["signal"] != "retry"),
                     key=lambda item: item["at"], reverse=True)
    return {
        "totals": totals,
        "by_project": {project: dict(rows) for project, rows in sorted(by_project.items())},
        "unrecorded": [{"session": item["session"][:8], "at": item["at"].astimezone().strftime("%m-%d %H:%M"),
                        "signal": item["signal"]} for item in missing[:POSITIONS]],
    }


def lines(missed: dict[str, Any]) -> list[str]:
    """The weekly report's section: counts and positions only."""
    if missed.get("skipped") or missed.get("error"):
        return [f"- 略過（{missed.get('skipped') or missed.get('error')}）"]

    def share(row: dict[str, int]) -> str:
        if not row["count"]:
            return "0"
        return f"{row['count']}（" + "、".join(f"{OUTCOME_NAMES[outcome]} {row.get(outcome, 0)}" for outcome in OUTCOMES) + "）"

    out = ["- 三種訊號（近 7 天）：" + "、".join(f"{SIGNAL_NAMES[s]} {share(missed['totals'][s])}" for s in SIGNALS)]
    for project, rows in missed.get("by_project", {}).items():
        out.append(f"  - {project}：" + "、".join(f"{SIGNAL_NAMES[s]} {share(rows[s])}" for s in SIGNALS))
    unrecorded = missed.get("unrecorded") or []
    if unrecorded:
        out.append(f"- 沒記的位置（最近的 {len(unrecorded)} 個；只列 session 前 8 碼、時間、訊號種類；指令失敗後改對只列次數）：")
        out.extend(f"  - {item['session']} {item['at']} {SIGNAL_NAMES[item['signal']]}" for item in unrecorded)
    out.append("- 有記：同一個 session 在 30 分鐘內寫了紀錄或 supersede，或 commit 改到專案來源的 parser 會讀的路徑；"
               "30～90 分鐘才記是晚記；這段時間只存了膠囊是只在膠囊。這一節只量測。")
    return out
