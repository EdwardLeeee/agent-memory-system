"""Which commits a session made (spec 騰訊借鏡改善 T6).

A source's post-commit hook runs `memoryctl sources sync <name>` in the
background, in the worktree the commit was made in, with the committing
agent's environment. Before sync fetches anything, it notes the session id,
the source, the commit and the time, when the sync runs inside that
source's repository or one of its worktrees. Which commits: HEAD may have
moved since the hook started (another commit, a checkout), so not HEAD but
the worktree's HEAD reflog: every entry of the last minute that made a
commit (a commit, amend, revert, cherry-pick, a rebase's pick, a merge
commit; not a fast-forward), made no later than this sync began (a second's
slack: the reflog keeps seconds) and not noted yet (review 2, item 4; N1).
An amend takes the commit it replaced off the notes. A commit made in the
user's own terminal (no session id) is noted as nobody's, so a session's
sync a moment later does not take it.

Limits, accepted (review 2, N2-N4): the hook starts sync in the background,
about half a second to a second after the commit. A sync without a session
takes the newest commit, so a terminal commit followed within that delay
by a session's commit in the same worktree is noted the wrong way round;
two sessions committing in one worktree within that delay cannot be told
apart. A reflog commit no hook ever noted (made before the hook was
installed, or with core.hooksPath going around it) is taken by the next
session's sync within the minute. Nothing of the commit itself is kept: its
title is read from git when shown. Notes are kept a year; the weekly doctor
prunes them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

COMMITS_NAME = "commits.jsonl"
FRESH_SECONDS = 60
KEEP = timedelta(days=366)


def _path(config_root: Path) -> Path:
    return config_root / "runtime" / COMMITS_NAME


def _git(cwd: Path, *args: str) -> str:
    from .storage import git_environment

    try:
        done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=5,
                              env=git_environment(), check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _read(config_root: Path) -> list[dict[str, Any]]:
    try:
        lines = _path(config_root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    notes = []
    for line in lines:
        try:
            note = json.loads(line)
        except ValueError:
            continue
        if isinstance(note, dict) and {"session_id", "repo", "commit", "at"} <= set(note):
            notes.append(note)
    return notes


# Reflog subjects of entries that made a commit: commit (amend, merge,
# initial, cherry-pick: a conflict resolved by hand), revert, cherry-pick, a
# rebase's picks (pull --rebase's too; continue after a conflict), and a
# merge or pull that made a merge commit. Not "merge x: Fast-forward", a
# checkout, a reset, or a rebase's start and finish.
_COMMIT_SUBJECT = re.compile(
    r"^(?:commit(?: \((?:amend|merge|initial|cherry-pick)\))?|revert|cherry-pick"
    r"|(?:rebase|pull\b[^:]*?)(?: -i)? \((?:pick|reword|edit|squash|fixup|continue|merge)\)): "
    r"|^(?:merge|pull)\b[^:]*: Merge made by "
)


def _fresh_commits(top: Path, now: datetime) -> tuple[list[str], set[str]]:
    """The commits of the worktree's HEAD reflog made in the minute before
    `now` (and up to a second after), oldest first; and those an amend among
    them replaced."""
    found: list[str] = []
    replaced: set[str] = set()
    entries = []
    for line in _git(top, "log", "-g", "-n", "50", "--date=unix", "--format=%H%x1f%gd%x1f%gs", "HEAD").splitlines():
        parts = line.split("\x1f")
        moment = re.search(r"@\{(\d+)\}", parts[1]) if len(parts) == 3 else None
        if moment:
            entries.append((parts[0], int(moment.group(1)), parts[2]))
    start = now.timestamp()
    for index, (commit, at, subject) in enumerate(entries):  # newest first
        if at < start - FRESH_SECONDS:
            break
        if at > start + 1 or not _COMMIT_SUBJECT.match(subject):
            continue  # made after this sync began (its own hook notes it), or not a commit
        found.append(commit)
        if subject.startswith("commit (amend)") and index + 1 < len(entries):
            replaced.add(entries[index + 1][0])
    # A cherry-pick onto the same parent in the same second makes the very
    # commit it picked: one hash, one note (designer re-check of N1).
    return list(dict.fromkeys(reversed(found))), replaced


def note_commit(config_root: Path, name: str, repo: Path, cwd: Path, session: str | None, now: datetime) -> bool:
    """Note this sync's commits for `session` (nobody's without one, and
    then only the latest); whether a session's commit was noted."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return False
    shared = _git(Path(top), "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not shared or Path(shared).resolve() != Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir") or "/").resolve():
        return False  # not this source's repository nor a worktree of it
    commits, replaced = _fresh_commits(Path(top), now)
    if not session:
        commits, replaced = commits[-1:], set()
    if not commits and not replaced:
        return False
    path = _path(config_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        notes = _read(config_root)
        known = {note["commit"] for note in notes if note["repo"] == name}
        if replaced & known:
            from .storage import atomic_write_text

            notes = [note for note in notes if not (note["repo"] == name and note["commit"] in replaced)]
            atomic_write_text(path, "".join(json.dumps(note) + "\n" for note in notes))
        added = [commit for commit in commits if commit not in known and commit not in replaced]
        if added:
            with open(path, "a", encoding="utf-8") as handle:
                for commit in added:
                    handle.write(json.dumps({"session_id": session or "", "repo": name, "commit": commit,
                                             "at": now.replace(microsecond=0).isoformat()}) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    return bool(session and added)


def prune(config_root: Path, now: datetime) -> int:
    """Drop notes older than a year; how many."""
    path = _path(config_root)
    if not path.exists():
        return 0
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        notes = _read(config_root)
        kept = [note for note in notes if (stamp := _time(note["at"])) is None or now - stamp <= KEEP]
        if len(kept) != len(notes):
            from .storage import atomic_write_text

            atomic_write_text(path, "".join(json.dumps(note) + "\n" for note in kept))
    return len(notes) - len(kept)


def _time(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def session_lines(config_root: Path, session: str) -> list[str]:
    """`memoryctl search --id <id> --context`'s list of the commits the
    record's session made: the source, the short hash, and the title git
    gives now (the hash alone when it cannot)."""
    from .federation import FederationConfigError, list_source_configs, load_source_config

    notes = [note for note in _read(config_root) if session and note["session_id"] == session]
    if not notes:
        return []
    repos: dict[str, Path] = {}
    for name in list_source_configs(config_root):
        try:
            repos[name] = load_source_config(config_root, name)["repo"]
        except FederationConfigError:
            continue
    lines = ["", "這個 session 做過的 commit："]
    for note in notes:
        title = _git(repos[note["repo"]], "log", "-1", "--format=%s", note["commit"]) if note["repo"] in repos else ""
        lines.append(f"- {note['repo']} {note['commit'][:8]}" + (f" {title}" if title else ""))
    return lines
