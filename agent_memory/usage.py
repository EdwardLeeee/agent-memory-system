"""Whether the agent used a memory it was given (spec 實際使用回饋改善 F12).

Packs no longer ask the agent to write 依據 <id>; whether it used a record
is judged from what it wrote. A record counts as used when, before the next
message the user typed, the agent's output (its replies, and the input of
its tool calls) names the record's id, or repeats something particular to
the record: a path with three or more slashes, a commit hash, a measure
with a unit (312 ms, 1.5 GB), code in backticks that is more than one word,
or an eight-character Chinese phrase or four-word English phrase from it. A
feature counts only if few records hold it and the user's own messages of
that window do not, so neither a phrase every record shares nor the agent
repeating what it was told counts; a literal only where it stands on its
own, not inside a longer number or path; and nothing from an output that
writes memory (memoryctl record or supersede): writing a record is not
using it.

Rule v2 and v3 (2026-10-21), each after reading 30 content matches of the
rule before: about half of v1's were chance. A number matched inside a
longer one (11.6 in 2511.6), plain decimals, percentages and counts (4.2%)
and two-segment paths (/api/health) are common anywhere, as is a project's
folder at the head of a longer path; a filename alone in backticks named the
file, not the record; English phrases of small, command or number words (the
rest of the; origin main git diff) are everyone's; and an agent writing a
record repeats it. Rule v4, after v3's 30: a list of report names (kiln v13
cone v12) is a reference, not a phrase, and a path the agent cd's into is
where it works, not what it was told. Rule v5 (designer, 2026-10-21): an id
counts as named only in what the agent says (its replies, a SendMessage, an
AskUserQuestion), not in the input of Write, Edit or Bash, where writing
code and tests types ids anyway.

Replay and the weekly doctor measure with it. Until content matching is
shown right in at least four of five sampled matches (CONTENT_TRUSTED), the
ranking feedback credits a rating, and lifts a record for its use, only by
ids the agent named; and turns spent working on the memory system itself (a
window in which the agent worked in one of its worktrees) are counted
apart, since there ids are the subject, not the memories used.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
import subprocess
from typing import Any

DETECTOR = "v5"
# Whether the ranking feedback may count content matches as use: not until
# a sample of them is judged right four times in five (A1 ruling, 2026-10-21).
CONTENT_TRUSTED = False
# What the user wrote in one window is kept up to this many characters to
# tell a record's words from the agent repeating the user's.
ECHO_CHARS = 200_000

_CJK_RUN_RE = re.compile(r"[㐀-鿿]+")
_WORD_RE = re.compile(r"[a-z0-9_]+(?:[.-][a-z0-9_]+)*")
# A path starts where a run of its characters starts, and no part of it is
# given back: the regex is linear (it took 2.5 s for 10,000 characters of a
# run without a slash, minutes for 200,000; review 2, item 16).
_PATH_RE = re.compile(r"(?<![~.\w-])[~.\w-]*+(?:/[\w.@+-]++){2,}")
_HASH_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])")
_NUMBER_RE = re.compile(r"(?:\d+\.\d+|\d{3,})\s?(?:mb|gb|kb|ms|秒|分鐘|小時|tokens?|px)\b")
_WRITES_MEMORY_RE = re.compile(r"memoryctl\s+(?:record|supersede)\b")
_CODE_RE = re.compile(r"`([^`\n]{6,})`")
# English words too small or too common to make a phrase particular to a
# record: function words, and the words of everyday shell and git commands.
_SMALL_WORDS = frozenset(
    "a an and are as at be but by can do for from has have if in into is it its no not of on or our so than that the "
    "their then there these this those to too up was we were what when where which who will with you your rest also "
    "only just more most other some any all each".split()
)
_COMMAND_WORDS = frozenset(
    "git diff main origin stat log grep sed head tail cat echo ls cd rm cp mv mkdir npm npx node python python3 curl "
    "run test tests status add commit push pull fetch checkout branch merge rebase show oneline format short gh pr "
    "api jq find xargs awk sort uniq wc true false null none file files line lines http https www com dev rev-parse "
    "ls-files config".split()
)


def _phrase_counts(words: list[str]) -> bool:
    small = sum(word in _SMALL_WORDS for word in words)
    single = sum(len(word) == 1 for word in words)
    command = sum(word in _COMMAND_WORDS for word in words)
    numbered = sum(bool(re.search(r"\d", word)) for word in words)
    return small < 2 and single < 2 and command < 3 and numbered < 2 and len(set(words)) == len(words)
_METADATA_RE = re.compile(r"^- (?:kind|scope|source|priority|last_verified|evidence_counts|latest_counterevidence|read|selected_because|tags): ")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
GRAM = 8
WORDS = 4
DISTINCT_MAX = 3
GRAMS_MAX = 300
LITERALS_MAX = 40


def memory_repo_paths(home: Path | str | None = None) -> tuple[Path, ...]:
    """The memory system's own folders: ~/agent-memory and ~/agent-memory-dev
    (named in the A1 ruling), the store (`home`, the one --home names, else
    the default), and every worktree of the repository this code runs from
    and of the store's (live and dev are worktrees of one repository),
    wherever they were moved."""
    from .storage import memory_home

    return _memory_repo_paths(str(home or memory_home()))


@lru_cache(maxsize=8)
def _memory_repo_paths(home: str) -> tuple[Path, ...]:
    found: list[Path] = [(Path.home() / name).resolve() for name in ("agent-memory", "agent-memory-dev")]
    for place in (Path(home), Path(__file__).resolve().parents[1]):
        try:
            found.append(place.resolve())
            done = subprocess.run(["git", "-C", str(place), "worktree", "list", "--porcelain"],
                                  capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        found += [Path(line[9:]).resolve() for line in done.stdout.splitlines() if line.startswith("worktree ")]
    return tuple(dict.fromkeys(found))


def in_memory_repo(cwd: str | Path | None, home: Path | str | None = None) -> bool:
    """Whether a cwd is in the memory system's own folders, with `home` the
    store a command works on (F16 re-check: not always the default)."""
    if not cwd:
        return False
    from .storage import memory_home

    return _in_memory_repo(str(cwd), str(home or memory_home()))


@lru_cache(maxsize=4096)
def _in_memory_repo(cwd: str, home: str) -> bool:
    try:
        here = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return any(here == place or here.is_relative_to(place) for place in _memory_repo_paths(home))


def normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def grams(text: str) -> set[str]:
    """Eight-character Chinese runs and four-word English runs. A path is
    taken out first: it counts whole, as a literal, so the user's path cut
    into words is not a phrase of the record."""
    text = _PATH_RE.sub(" ", text)
    found = set()
    for run in _CJK_RUN_RE.findall(text):
        found.update(run[i:i + GRAM] for i in range(len(run) - GRAM + 1))
    words = _WORD_RE.findall(text)
    found.update(" ".join(words[i:i + WORDS]) for i in range(len(words) - WORDS + 1) if _phrase_counts(words[i:i + WORDS]))
    return found


def literals(text: str) -> set[str]:
    found = {match.group(0).rstrip(".") for match in _PATH_RE.finditer(text) if match.group(0).count("/") >= 3}
    found.update(h for h in _HASH_RE.findall(text) if re.search(r"\d", h) and re.search(r"[a-f]", h))
    found.update(match.group(0) for match in _NUMBER_RE.finditer(text))
    found.update(code.strip() for code in _CODE_RE.findall(text) if re.search(r"[\s/=(]", code.strip()))
    return {item for item in found if len(item) >= 4}


def _stands_alone(literal: str, text: str) -> bool:
    """The literal occurs in `text` not as part of a longer number, word, or
    path."""
    pattern = re.compile(r"(?<![\w./-])(?<!cd )" + re.escape(literal) + r"(?![\w/-])")
    return pattern.search(text) is not None


class FeatureFrequency:
    """How many records hold each feature, as a hashed count table: a few
    megabytes for the whole ledger; a collision only makes a feature look
    commoner, which is the safe side."""

    def __init__(self, bits: int = 22) -> None:
        self.mask = (1 << bits) - 1
        self.table = bytearray(1 << bits)

    def add_record(self, text: str) -> None:
        normalized = normalize(text)
        for feature in grams(normalized) | literals(normalized):
            index = hash(feature) & self.mask
            if self.table[index] < 255:
                self.table[index] += 1

    def count(self, feature: str) -> int:
        return self.table[hash(feature) & self.mask]

    @classmethod
    def from_index(cls, root: Path) -> "FeatureFrequency":
        import sqlite3

        frequency = cls()
        try:
            connection = sqlite3.connect(f"file:{root / '.cache' / 'index.sqlite3'}?mode=ro", uri=True)
        except sqlite3.Error:
            return frequency
        try:
            for (body,) in connection.execute("SELECT body FROM records"):
                frequency.add_record(str(body))
        except sqlite3.Error:
            pass
        finally:
            connection.close()
        return frequency


class UsageDetector:
    """The features of each record as a pack showed it."""

    def __init__(self, frequency: FeatureFrequency | None) -> None:
        self.frequency = frequency
        self.cache: dict[tuple[str, int], dict[str, Any]] = {}

    def features(self, record_id: str, block: str) -> dict[str, Any]:
        key = (record_id, hash(block))
        if key in self.cache:
            return self.cache[key]
        body = "\n".join(line for line in block.splitlines()[1:] if not _METADATA_RE.match(line))
        if block.startswith("- "):  # a PROJECT LESSONS line holds its text after the id
            body = block.split(": ", 1)[-1]
        normalized = normalize(body)

        def distinctive(feature: str) -> bool:
            return self.frequency is None or self.frequency.count(feature) <= DISTINCT_MAX

        found_grams = sorted(g for g in grams(normalized) if distinctive(g))[:GRAMS_MAX]
        found_literals = sorted(l for l in literals(normalized) if distinctive(l))[:LITERALS_MAX]
        found = {"id": record_id.casefold(), "grams": set(found_grams), "literals": found_literals}
        if len(self.cache) > 20_000:
            self.cache.clear()
        self.cache[key] = found
        return found


class Echo:
    """What the user wrote in one window: a feature found there is the
    user's, not the record's. Every message of the window counts, each
    whole, though a turn's hook saw only the latest."""

    def __init__(self) -> None:
        self.text = ""
        self.grams: set[str] = set()

    def add(self, message: str) -> None:
        if len(self.text) >= ECHO_CHARS:
            return
        normalized = normalize(message)[:ECHO_CHARS - len(self.text)]
        self.text += "\n" + normalized
        self.grams |= grams(normalized)


def names_id(record_id: str, text: str) -> bool:
    """Whether `text` names `record_id` whole, not as the start of a longer
    id (…-record-1 in …-record-10-entg; review 2, A1), the boundary
    `_fresh_lessons` checks too. A period after it may end the sentence."""
    at = text.find(record_id)
    while at >= 0:
        end = at + len(record_id)
        if not re.match(r"[a-z0-9._-]", text[at - 1:at]) and not re.match(r"[a-z0-9_-]|\.[a-z0-9]", text[end:end + 2]):
            return True
        at = text.find(record_id, at + 1)
    return False


def match(features: dict[str, Any], normalized: str, echo: Echo, output_grams: list[set[str] | None],
          spoken: str | None = None) -> tuple[str, str] | None:
    """How one output shows a record used: ("id", id), ("literal", it), or
    ("phrase", it); None when it does not. `output_grams` holds the output's
    phrases once computed, shared across the records checked against it. An
    id is looked for in `spoken`, what the agent said (normalized), when
    given; the rest in the whole output."""
    if names_id(features["id"], normalized if spoken is None else spoken):
        return "id", features["id"]
    if _WRITES_MEMORY_RE.search(normalized):
        return None
    literal = next((l for l in features["literals"]
                    if l in normalized and _stands_alone(l, normalized) and l not in echo.text), None)
    if literal is not None:
        return "literal", literal
    if features["grams"]:
        if output_grams[0] is None:
            output_grams[0] = grams(normalized)
        hits = (features["grams"] & output_grams[0]) - echo.grams
        if hits:
            return "phrase", sorted(hits)[0]
    return None


def snippet(normalized: str, feature: str, width: int = 60) -> str | None:
    """The agent's words around a matched feature, to check a match by eye:
    None when they hold anything SECRET_PATTERNS catches; email addresses
    become [email]."""
    from .storage import SECRET_PATTERNS

    at = normalized.find(feature)
    if at < 0:  # a phrase of English words is matched word by word
        at = normalized.find(feature.split(" ")[0])
    text = normalized[max(0, at - width):at + len(feature) + width] if at >= 0 else ""
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        return None
    return _EMAIL_RE.sub("[email]", text)
