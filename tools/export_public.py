#!/usr/bin/env python3
"""Export the program as the public snapshot: code only, no memory data.

    python3 tools/export_public.py --out DIR [--rev HEAD]

DIR must be absent, empty, or a git work tree (everything but .git is
replaced, so a later sync shows up as an ordinary diff). The export:

1. `git archive <rev>` of this repository into DIR;
2. drops the memory data and the private material (EXCLUDED);
3. moves the old Chinese handbook (README.md) to docs/HANDBOOK.zh-TW.md and
   copies public/ (README pair, LICENSE, CHANGELOG, CI, .gitignore) to the
   root;
4. replaces this machine's home path, and the author's project names in the
   prose documents, with neutral ones (project-a, project-b, … each project
   keeping its letter from export to export, in
   ~/.config/agent-memory/public-export-placeholders.json);
5. scans every file for identifying strings and key-shaped strings, and
   fails if any is left.

Nothing personal is written in this file: the home path, user name, machine
id, git identity, project names, every record id in the ledger, every
16-character stretch of the golden and probe query texts (the user's own
words), the shorter Chinese probe queries, and the start of every resume
capsule are read from this machine at run time (machine.json, router.toml,
sources.d, git config, the memory store, doctor/golden.toml and
doctor/probes.toml). tools/prerelease_transcript_check.py, which compares a
snapshot with this machine's transcripts, stays private. More strings to refuse,
or `old => new` replacements, go one per line in
~/.config/agent-memory/public-export.txt, which never leaves the machine.
"""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

EXCLUDED = (
    "records", "archive", "events", "sessions", "manifest.json", ".cache", ".write-lock",
    "migration",  # the author's own migration reports and manifests
    "evaluation",  # the retrieval and Codex suites stay private
    "public",  # copied to the root below
    "tools/prerelease_transcript_check.py",  # reads this machine's transcripts; run before each release
)
PROSE = ("docs/HANDBOOK.zh-TW.md", "docs/research/")
KEY_SHAPES = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----(?!\")"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[abps]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]
# The places the author's GitHub account may appear: the clone URL, the
# license notice, and the package metadata.
ALLOWED_LINE = re.compile(r"github\.com/[A-Za-z0-9-]+/agent-memory-system|^Required Notice: |^authors = ")


def _git(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(REPO), *args], text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _local_settings() -> tuple[list[str], list[tuple[str, str]]]:
    path = Path.home() / ".config" / "agent-memory" / "public-export.txt"
    refuse, replace = [], []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if " => " in line:
                old, new = line.split(" => ", 1)
                replace.append((old, new))
            else:
                refuse.append(line)
    return refuse, replace


PLACEHOLDERS = Path.home() / ".config" / "agent-memory" / "public-export-placeholders.json"


def _letters() -> "Iterator[str]":
    """project-a … project-z, project-aa, project-ab, …"""
    from itertools import count, product
    from string import ascii_lowercase

    for size in count(1):
        for letters in product(ascii_lowercase, repeat=size):
            yield "project-" + "".join(letters)


def assigned_placeholders(projects: list[str]) -> dict[str, str]:
    """Each project's placeholder, kept from export to export in a table on
    this machine (never exported): one assigned before keeps its letter, a
    new one takes the next letter free, so a document no longer changes
    only because a project was added and the names sort anew (designer
    2026-10-28)."""
    try:
        table = json.loads(PLACEHOLDERS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        table = {}
    table = {str(key): str(value) for key, value in table.items()} if isinstance(table, dict) else {}
    used = set(table.values())
    free = (letter for letter in _letters() if letter not in used)
    new = {project: next(free) for project in sorted(projects) if project not in table}
    if new:
        table.update(new)
        PLACEHOLDERS.parent.mkdir(parents=True, exist_ok=True)
        PLACEHOLDERS.write_text(json.dumps(table, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        PLACEHOLDERS.chmod(0o600)
    return table


def project_placeholders() -> dict[str, str]:
    """Each of the author's projects, and every name or alias routing knows
    it by, mapped to its placeholder (`assigned_placeholders`; longest names
    first)."""
    from agent_memory.lexical_index import compute_routing

    routing = compute_routing()
    projects = routing.get("projects", {})
    assigned = assigned_placeholders(list(projects))
    mapping: dict[str, str] = {}
    for project, tokens in sorted(projects.items()):
        placeholder = assigned[project]
        mapping[project] = placeholder
        for token in tokens:
            if len(token) >= 4 or re.search(r"[\u3400-\u9fff]", token):
                mapping.setdefault(token, placeholder)
    for source, project in routing.get("sources", {}).items():
        mapping.setdefault(source, mapping.get(project or "", "project-x"))
    return dict(sorted(mapping.items(), key=lambda item: (-len(item[0]), item[0])))


def _word(needle: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(needle)}(?![\w-])", re.IGNORECASE)


def identifying_strings() -> list[re.Pattern[str]]:
    """Substrings for paths, ids, and the git identity; whole words for
    project names, so a project called `art` does not flag `party`. The
    strings from public-export.txt are matched as written (case kept)."""
    home = Path.home()
    found = {str(home), home.name}
    try:
        machine = json.loads((home / ".config" / "agent-memory" / "machine.json").read_text(encoding="utf-8"))
        machine_id = str(machine.get("machine_id", ""))
        if machine_id:
            found.update({machine_id, machine_id[:8]})
    except (OSError, ValueError):
        pass
    for key in ("user.email", "user.name"):
        value = _git("config", key)
        if value:
            found.add(value)
    patterns = [re.compile(re.escape(item), re.IGNORECASE) for item in found if len(item) >= 3]
    patterns += [re.compile(re.escape(item)) for item in _local_settings()[0] if len(item) >= 3]
    patterns += [re.compile(re.escape(item)) for item in ledger_strings()]
    sessions = session_prefixes()
    if sessions:
        patterns.append(re.compile(r"(?<![0-9a-f])(?:" + "|".join(sorted(sessions)) + r")(?![0-9a-f])"))
    return patterns + [_word(name) for name in project_placeholders()]


CAPSULE_PREFIX = 16
WINDOW = 16
_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def session_prefixes() -> set[str]:
    """The first eight characters of every session this machine knows, as
    reports and notes quote them (a whole id starts with them): the memory
    store's capsule folders, and the Claude and Codex transcripts' names. A
    real one had been left in a test (designer 2026-10-28)."""
    from agent_memory.replay import default_claude_dirs, default_codex_dir
    from agent_memory.storage import memory_home

    names = []
    folders = [(memory_home() / "sessions" / "by-session", False), *((Path(folder), True) for folder in default_claude_dirs()),
               (Path(default_codex_dir()), True)]
    for folder, deep in folders:
        try:
            names += [path.name for path in (folder.rglob("*") if deep else folder.iterdir())]
        except OSError:
            continue
    return {match.group(0)[:8] for name in names for match in _SESSION_ID.finditer(name)}


def golden_texts() -> list[str]:
    from agent_memory.federation import parse_minimal_toml
    from agent_memory.storage import config_home

    try:
        golden = parse_minimal_toml((config_home() / "doctor" / "golden.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(query.get("text", "")) for query in golden.get("query", []) if isinstance(query, dict)]


def probe_texts() -> list[str]:
    """The probe questions (doctor/probes.toml), the user's own words as
    the golden ones are."""
    from agent_memory.federation import parse_minimal_toml
    from agent_memory.storage import config_home

    try:
        probes = parse_minimal_toml((config_home() / "doctor" / "probes.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(probe.get("text", "")) for probe in probes.get("probe", []) if isinstance(probe, dict)]


def golden_windows() -> set[str]:
    """Every WINDOW-character stretch of every golden and probe query: a
    line that quotes part of the user's own words is caught too, not only
    the whole sentence."""
    return {text[i:i + WINDOW] for text in [*golden_texts(), *probe_texts()] for i in range(len(text) - WINDOW + 1)}


def ledger_strings() -> set[str]:
    """What this machine's memory holds and the snapshot must never echo:
    every record id (records and archive), every golden query text, and the
    start of every resume capsule's active_task. Read at run time only."""
    from agent_memory.federation import parse_minimal_toml
    from agent_memory.storage import config_home, memory_home

    root, found = memory_home(), set()
    for path in (root / "records").glob("*/*.md"):
        found.add(path.stem)
    for path in (root / "archive").glob("*/*"):
        if path.is_dir():
            found.add(path.name)
    found.update(text for text in golden_texts() if len(text) < WINDOW)
    for path in (root / "sessions").glob("**/HOT.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        match = re.search(r"^- active_task: (.+)$", text, re.M)
        if match:
            found.add(match.group(1).strip()[:CAPSULE_PREFIX])
    # A short probe query in Chinese is the user's sentence, however short;
    # one in English alone is a tool's name (deploy, TestFlight), common in
    # the code.
    probes = {text for text in probe_texts() if len(text) < WINDOW and re.search(r"[\u3400-\u9fff]", text)}
    return {item for item in found if len(item.strip()) >= 8} | {text for text in probes if len(text.strip()) >= 4}


def _clear(out: Path) -> None:
    if not out.exists():
        out.mkdir(parents=True)
        return
    entries = [entry for entry in out.iterdir() if entry.name != ".git"]
    if entries and not (out / ".git").exists():
        raise SystemExit(f"{out} is not empty and not a git work tree")
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def export(out: Path, rev: str) -> None:
    _clear(out)
    archive = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", rev], capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(out, filter="data")
    if (out / "README.md").exists():
        (out / "docs").mkdir(exist_ok=True)
        (out / "README.md").rename(out / "docs" / "HANDBOOK.zh-TW.md")
    public = out / "public"
    if public.is_dir():
        for source in public.rglob("*"):
            if source.is_file():
                target = out / source.relative_to(public)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    for name in EXCLUDED:
        path = out / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    home = str(Path.home())
    placeholders = project_placeholders()
    extra = _local_settings()[1]
    for path in sorted(out.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(out).parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(out).as_posix()
        new = text.replace(f"{home}/agent-memory", "~/agent-memory-system").replace(home + "/", "~/")
        for old, replacement in extra:
            new = new.replace(old, replacement)
        if relative.startswith(PROSE):
            for name, placeholder in placeholders.items():
                new = _word(name).sub(placeholder, new)
        if new != text:
            path.write_text(new, encoding="utf-8")


def scan(out: Path) -> list[str]:
    needles = identifying_strings()
    windows = golden_windows()
    problems = []
    for name in EXCLUDED:
        if (out / name).exists():
            problems.append(f"{name}: excluded path is present")
    for path in sorted(out.rglob("*")):
        if ".git" in path.relative_to(out).parts:
            continue
        relative = path.relative_to(out).as_posix()
        # Byte code holds the absolute paths it was compiled from: a snapshot
        # something already ran in is not one to publish.
        if path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}:
            problems.append(f"{relative}: compiled Python left by a run; export again into a fresh directory")
            continue
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            problems.append(f"{relative}: binary file, check by hand")
            continue
        for number, line in enumerate(lines, 1):
            if ALLOWED_LINE.search(line):
                continue
            for needle in needles:
                match = needle.search(line)
                if match:
                    found = match.group(0)
                    problems.append(f"{relative}:{number}: identifying string ({len(found)} chars, starts {found[:2]!r})")
            for pattern in KEY_SHAPES:
                if pattern.search(line):
                    problems.append(f"{relative}:{number}: key-shaped string")
            if any(line[i:i + WINDOW] in windows for i in range(len(line) - WINDOW + 1)):
                problems.append(f"{relative}:{number}: quotes {WINDOW} characters of a golden or probe query")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--rev", default="HEAD")
    parser.add_argument("--scan-only", action="store_true", help="scan an existing export without rewriting it")
    args = parser.parse_args()
    out = args.out.expanduser().resolve()
    if not args.scan_only:
        export(out, args.rev)
    problems = scan(out)
    for problem in problems:
        print(problem)
    files = sum(1 for path in out.rglob("*") if path.is_file() and ".git" not in path.relative_to(out).parts)
    print(f"{'FAILED' if problems else 'OK'}: {files} files in {out}, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
