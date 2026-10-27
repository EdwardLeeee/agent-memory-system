"""Storage primitives shared by every layer above the ledger.

These have no dependency on retrieval, sessions, or hooks, which is what lets
the index and the ledger both use them without importing each other.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import hashlib
import re
import tempfile
import time
from typing import Any, Iterator
import uuid


SCHEMA_VERSION = 3


SUPPORTED_SCHEMA_VERSIONS = {2, 3}


META_START = "<!-- agent-memory-meta\n"


META_END = "\n-->\n"


ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")


ALLOWED_KINDS = {
    "preference",
    "fact",
    "decision",
    "experience",
    "failure",
    "task",
    "lesson",
}
ALLOWED_LESSON_STATUSES = {"open", "enforced", "retired"}


ALLOWED_SOURCES = {"user-stated", "locally-verified", "inference", "migrated"}


ALLOWED_PRIORITIES = {"critical", "high", "normal", "low"}


ALLOWED_CHANGE_DRIVERS = {
    "user-correction",
    "new-evidence",
    "goal-change",
    "constraint-change",
    "expired",
}


ALLOWED_EXPERIENCE_EFFECTS = {"supports", "contradicts", "narrows", "inconclusive"}


SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(
        r"(?i)\b(?:password|passwd|api[_ -]?key|access[_ -]?token|secret)"
        r"\s*[:=]\s*[^\s`'\"]{8,}"
    ),
]


class MemoryError(RuntimeError):
    """A user-facing durable-memory error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def memory_home(explicit: str | None = None) -> Path:
    value = explicit or os.environ.get("AGENT_MEMORY_HOME")
    return Path(value).expanduser().resolve() if value else (Path.home() / "agent-memory")


def config_home() -> Path:
    value = os.environ.get("AGENT_MEMORY_CONFIG_HOME")
    return (
        Path(value).expanduser().resolve()
        if value
        else (Path.home() / ".config" / "agent-memory")
    )


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    _secure_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def write_lock(root: Path, timeout: float = 5.0) -> Iterator[None]:
    lock = root / ".write-lock"
    if lock.is_dir():
        try:
            lock.rmdir()
        except OSError as exc:
            raise MemoryError("legacy memory lock directory is not empty") from exc
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MemoryError("memory store is locked by another writer")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def encode_record(metadata: dict[str, Any], body: str) -> str:
    return (
        META_START
        + json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2)
        + META_END
        + body
    )


def decode_record(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith(META_START):
        raise MemoryError(f"{path}: missing JSON metadata header")
    end = text.find(META_END, len(META_START))
    if end < 0:
        raise MemoryError(f"{path}: unterminated JSON metadata header")
    metadata = json.loads(text[len(META_START) : end])
    return metadata, text[end + len(META_END) :]


def _extract_section(body: str, heading: str) -> str:
    marker = f"## {heading}\n"
    start = body.find(marker)
    if start < 0:
        return ""
    content_start = start + len(marker)
    next_heading = body.find("\n## ", content_start)
    return body[content_start : next_heading if next_heading >= 0 else None].strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- Shared with the lightweight PreToolUse entry point ----------------------
#
# The PreToolUse hook runs before every shell command, so its entry point may
# import nothing heavier than this module (spec 記憶路由樹 v1.1, 8.9). What it
# shares with core lives here; core re-exports each under its old name.

# One line in every pack so the weekly report can tell which memories were
# used: the agent names a record when it acts on it.
CITATION_REQUEST = "採用某筆記憶時，請在回覆寫一次「依據 <id>」。"

# The shell tool each agent's PreToolUse event names; anything else is ignored.
# Verified 2026-10-10/11: Claude Code 2.1.296 and Codex CLI 0.162.1 both send
# "Bash" (Codex's model calls its function exec_command; the hook still
# receives tool_name "Bash").
AGENT_SHELL_TOOLS: dict[str, frozenset[str]] = {
    "claude": frozenset({"Bash"}),
    "codex": frozenset({"Bash"}),
}
# Tools that send what the agent wrote to someone: checked against failures'
# outbound_triggers, never held (spec 實際使用回饋改善 F13). Not Codex yet.
AGENT_OUTBOUND_TOOLS: dict[str, frozenset[str]] = {
    "claude": frozenset({"SendMessage", "AskUserQuestion"}),
}


# What git hands a hook about the repository it runs in. A post-commit
# hook in a worktree exports GIT_DIR and GIT_INDEX_FILE; a sync it starts in
# the background would otherwise point every `git -C <other repo>` at this
# repository (spec 記憶路由樹 v1.1, ruling 52 a).
HOOK_GIT_VARIABLES = (
    "GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_PREFIX", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def git_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """os.environ without the repository a hook inherited; every git call of
    this package runs with it."""
    env = {key: value for key, value in os.environ.items() if key not in HOOK_GIT_VARIABLES}
    env.update(extra or {})
    return env


def worktree_paths(value: Any) -> dict[str, list[str]]:
    """The `sources` table of .cache/source_worktrees.json, each source a list
    of path strings. Anything else is dropped: a damaged cache falls back to
    scope_paths and repository paths, never to an error (ruling 51 a)."""
    sources = value.get("sources") if isinstance(value, dict) else None
    if not isinstance(sources, dict):
        return {}
    return {
        str(name): [item for item in paths if isinstance(item, str) and item]
        for name, paths in sources.items()
        if isinstance(paths, list)
    }


def project_membership(
    resolved_cwd: Path,
    name: str,
    scope_paths: list[str],
    source_dirs: list[str],
    *,
    directory_name: bool = True,
) -> str | None:
    """Whether a resolved cwd is inside project `name` (spec 記憶路由樹 v1.1,
    ruling 46): its scope_paths; without them, a directory named after it
    (or after it plus "-worktrees"); and either way a registered source's
    repository or worktree. Routing, federation, and the PreToolUse hook all
    decide through this one function. directory_name=False keeps only the
    strong reasons, which is what federation accepts (ruling 51 b)."""
    tag = f"project:{name}"

    def within(directory: str) -> bool:
        try:
            resolved_cwd.relative_to(Path(directory).expanduser().resolve())
            return True
        except (ValueError, OSError, RuntimeError):
            return False

    if scope_paths:
        if any(within(path) for path in scope_paths):
            return f"matched {tag}"
    elif directory_name:
        segments = {part.casefold() for part in resolved_cwd.parts}
        if name and (name.casefold() in segments or f"{name.casefold()}-worktrees" in segments):
            return f"matched {tag} (directory name)"
    if any(within(path) for path in source_dirs):
        return f"matched {tag} (registered source repo)"
    return None


def estimate_tokens(text: str) -> int:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


# Claude Code saves a hook context longer than 10,000 characters to a file
# and gives the agent only its path and a 2,000-character preview (three
# SessionStart packs of 10,075 to 10,467 characters in one session's
# transcript, 2026-10-19 and 20; one of 9,930 arrived whole). A token budget
# allows twice that in English, so a routed Claude hook's context is also
# kept under this many characters (spec 實際使用回饋改善, F1/F2 review A).
CLAUDE_HOOK_CHAR_LIMIT = 9_500


def hook_chars(text: str) -> int:
    """A hook context's length as Claude Code counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _session_key(session_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", session_id):
        return session_id
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return f"session-{digest}"


# When a fact is true in the world, apart from when the system learned it
# (created_at, updated_at) and when it should be checked again (expires_at)
# (spec 論文借鏡改善 A3). Both bounds are optional dates, inclusive.
VALIDITY_FIELDS = ("valid_from", "valid_until")


def _day(value: Any) -> date | None:
    text = str(value or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def validity_problems(kind: str, metadata: dict[str, Any]) -> list[str]:
    present = [field for field in VALIDITY_FIELDS if metadata.get(field) not in (None, "")]
    if not present:
        return []
    if kind != "fact":
        return ["valid_from and valid_until are only for fact records"]
    problems = [f"{field} must be a date (YYYY-MM-DD)" for field in present if _day(metadata[field]) is None]
    start, end = _day(metadata.get("valid_from")), _day(metadata.get("valid_until"))
    if not problems and start and end and start > end:
        problems.append("valid_from must not be after valid_until")
    return problems


def no_longer_true(metadata: dict[str, Any], today: date | None = None) -> bool:
    """A fact whose valid_until has passed: known not to hold any more."""
    end = _day(metadata.get("valid_until")) if metadata.get("kind") == "fact" else None
    return end is not None and end < (today or date.today())


def _is_expired(value: Any) -> bool:
    if value in {None, "", "none"}:
        return False
    text = str(value)
    try:
        if "T" in text:
            instant = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            return instant < datetime.now(timezone.utc)
        return date.fromisoformat(text) < date.today()
    except ValueError:
        return True


# --- router.toml (spec 記憶路由樹 v1.1) ------------------------------------
#
# Read here, in the standard-library-only layer, because the PreToolUse hook's
# lightweight entry point may import nothing heavier than this module. The
# budget validation that needs the agents' total budgets lives in core.

ROUTER_TOML_NAME = "router.toml"
ROUTER_BUDGET_KEYS = ("rules", "project", "project_second", "knowledge", "facts", "failures", "project_borrow_max")
ROUTER_DEFAULT_BUDGETS: dict[str, dict[str, int]] = {
    "claude": {
        "rules": 200, "project": 1200, "project_second": 300, "knowledge": 500,
        "facts": 350, "failures": 600, "project_borrow_max": 400,
    },
    "codex": {
        "rules": 120, "project": 700, "project_second": 150, "knowledge": 260,
        "facts": 200, "failures": 400, "project_borrow_max": 200,
    },
}
# Ids name their project in the second segment. A project whose name splits
# into nothing distinctive (my-app -> myapp) needs its segment listed in
# router.toml [project_tokens]; nothing is built in.
ROUTER_DEFAULT_PROJECT_TOKENS: dict[str, list[str]] = {}
# router.toml [cwd_topics] maps a directory to a knowledge topic; none built in.
ROUTER_DEFAULT_CWD_TOPICS: dict[str, str] = {}
# Every knowledge topic with five or more records needs words that open it.
ROUTER_DEFAULT_TOPICS: list[dict[str, Any]] = [
    {"name": "memory", "words": ["記憶", "檢索", "hook", "memoryctl", "doctor", "golden", "索引"]},
    {"name": "web", "words": ["web", "網站", "css", "html", "瀏覽器", "前端"]},
    {"name": "ci", "words": ["ci", "workflow", "runner", "actions", "流水線", "工作流"]},
    {"name": "agents", "words": ["agent", "subagent", "herdr", "codex", "代理"]},
    {"name": "ui", "words": ["ui", "介面", "按鈕", "排版", "版面"]},
    # Shell and machine checks: gates, memory, disk (spec 學到的地方與適用範圍 v1).
    {"name": "shell", "words": ["shell", "bash", "zsh", "閘門", "記憶體", "磁碟"]},
    # git lives only in global failures; its topic shows just one of them.
    {"name": "git", "words": ["git", "commit", "rebase", "reset", "merge", "stash", "worktree", "分支", "合併", "提交"]},
]
ROUTER_DEFAULT_KNOWLEDGE_MAX_TOPICS = 2
# Extracted command phrases too common to mean a failure is near (spec 8.0
# ruling 32); explicit triggers are never filtered.
ROUTER_DEFAULT_PHRASE_STOPLIST = ["git status", "git add", "git fetch", "git log", "git diff", "gh api", "ls", "cat", "cd"]
# A Chinese bigram holding one of these is a fragment such as 的記 or 在的,
# not a word: it never counts toward a record's relevance (spec
# 實際使用回饋改善 F3). English function words are already query stopwords.
ROUTER_DEFAULT_FUNCTION_CHARS = "的了是在嗎呢吧啊喔呀著和與及或而並就也都很把被這那"
# Words of conversation, not of a subject: taken out of a prompt before it is
# routed, so 繼續 alone opens nothing (designer 2026-10-21). Only words of two
# or more characters: one character (好, 對) is part of other words (好處, 對手).
ROUTER_DEFAULT_FILLER_WORDS = ("繼續", "好的", "好喔", "可以", "謝謝", "收到", "沒問題", "了解", "知道了", "是的",
                               "ok", "okay", "thanks", "thank you")
# A word in fewer than this share of the records is uncommon; in a small
# ledger, where that share is less than a handful, so is a word in fewer than
# RARE_MIN_DOCS records.
ROUTER_DEFAULT_RARE_SHARE = 0.05
ROUTER_DEFAULT_RARE_MIN_DOCS = 5
# The removal order names knowledge topics 0 and 1; more seats are not served.
ROUTER_MAX_KNOWLEDGE_TOPICS = 2


# The speed bumps (spec 8.10): one-step commands that cannot be undone. A
# pattern is matched against one normalized command segment and must be
# anchored at the segment's command word. The list is the user's to edit in
# router.toml; this is the version the spec ships.
SPEED_BUMP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DEFAULT_SPEED_BUMPS: list[dict[str, Any]] = [
    {
        "id": "git-force-push",
        "pattern": r"^git\s+push\b.*(\s(--force(-with-lease)?(=\S*)?|-[A-Za-z]*f[A-Za-z]*)(\s|$)|\s\+\S+)",
        "reason": "強制推送會覆蓋遠端歷史；先確認分支與遠端狀態。",
    },
    {
        "id": "git-reset-hard",
        "pattern": r"^git\s+reset\s+--hard\b",
        "reason": "git reset --hard 會丟掉工作區與暫存的全部改動，無法復原。",
    },
    {
        "id": "git-clean",
        "pattern": r"^git\s+clean\b.*\s(-[A-Za-z]*f[A-Za-z]*|--force)(\s|$)",
        "reason": "git clean 會刪掉未追蹤的檔案，無法復原。",
    },
    {
        "id": "git-discard-all",
        "pattern": r"^git\s+(checkout\s+(--\s+)?\.|restore\s+(--worktree\s+)?\.)(\s|$)",
        "reason": "這會丟掉工作區的全部改動。",
    },
    {
        "id": "git-branch-d",
        "pattern": r"^git\s+branch\s+-D\b",
        "reason": "強制刪分支後，未合併的 commit 會失去參照。",
    },
    {
        "id": "git-stash-destroy",
        "pattern": r"^git\s+stash\s+(drop|clear)\b",
        "reason": "stash 由所有 worktree 共用，可能刪到別的 session 的 stash。",
    },
    {
        # Recursive with or without -f: GNU rm with a non-tty stdin removes
        # read-only files under -r without asking (user, 2026-10-10).
        "id": "rm-rf",
        "pattern": r"^rm(?=\s)(?=.*\s(-[A-Za-z]*[rR][A-Za-z]*|--recursive)(\s|$))",
        "reason": "遞迴刪除無法復原。",
    },
    {
        "id": "herdr-stop",
        "pattern": r"^herdr\s+server\s+stop\b",
        "reason": "herdr server stop 會結束這台機器上所有的 Claude session。",
    },
    {
        "id": "kill-by-name",
        "pattern": r"^(pkill|killall)\b|^kill\b.*\bpgrep\s+-[A-Za-z]*f\b",
        "reason": "依名稱或指令列殺程序，可能殺到自己的 shell 或別的 session。",
    },
    {
        "id": "gh-delete",
        "pattern": r"^gh\s+repo\s+delete\b|^gh\s+api\b.*-X\s*DELETE",
        "reason": "這會刪除遠端資源。",
    },
    {
        "id": "podman-destroy",
        "pattern": r"^podman\s+(system\s+prune|volume\s+rm|rm\s+-f)\b",
        "reason": "這會刪除容器或資料卷。",
    },
    {
        "id": "db-drop",
        "pattern": r"^(psql|mysql|mariadb|sqlite3|duckdb)\b.*\bdrop\s+(table|database)\b|^dropdb\b",
        "reason": "這會刪除資料表或資料庫。",
        "ignore_case": True,
    },
]


class RouterTomlInvalid(ValueError):
    """router.toml exists but cannot be used as a whole."""


def router_defaults() -> dict[str, Any]:
    return {
        "enabled": True,
        "knowledge_max_topics": ROUTER_DEFAULT_KNOWLEDGE_MAX_TOPICS,
        "budgets": {agent: dict(values) for agent, values in ROUTER_DEFAULT_BUDGETS.items()},
        "project_tokens": {name: list(tokens) for name, tokens in ROUTER_DEFAULT_PROJECT_TOKENS.items()},
        "cwd_topics": dict(ROUTER_DEFAULT_CWD_TOPICS),
        "topics": [{"name": t["name"], "words": list(t["words"])} for t in ROUTER_DEFAULT_TOPICS],
        "speed_bump_enabled": True,
        "speed_bumps": None,
        "speed_bump_failures": {},
        "phrase_stoplist": list(ROUTER_DEFAULT_PHRASE_STOPLIST),
        "relevance": {"function_chars": ROUTER_DEFAULT_FUNCTION_CHARS, "rare_share": ROUTER_DEFAULT_RARE_SHARE,
                      "rare_min_docs": ROUTER_DEFAULT_RARE_MIN_DOCS, "filler_words": list(ROUTER_DEFAULT_FILLER_WORDS)},
        "code": None,
        "problems": [],
        "source": "default",
    }


def _string_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RouterTomlInvalid(f"{where} must be an array of strings")
    return [item for item in value if item.strip()]


ROUTER_TOP_LEVEL_KEYS = frozenset(
    {"enabled", "knowledge_max_topics", "budgets", "project_tokens", "cwd_topics", "knowledge", "failures", "relevance"}
)


def _parse_router_toml(data: dict[str, Any]) -> dict[str, Any]:
    """Apply a parsed router.toml over the defaults, one section at a time.

    `enabled` is read first and a deliberate `enabled = false` always holds.
    A section of the wrong shape falls back to its default alone (a budget
    group only for its agent) and is reported, so one typo elsewhere never
    turns the router or another section back to defaults (spec 8.0 ruling 30).
    """
    settings = router_defaults()
    settings["source"] = "file"
    problems: list[str] = settings["problems"]
    if "enabled" in data:
        if isinstance(data["enabled"], bool):
            settings["enabled"] = data["enabled"]
        else:
            problems.append("enabled must be a boolean")
    unknown = sorted(set(data) - ROUTER_TOP_LEVEL_KEYS)
    if unknown:
        # A misspelt key (say `enable = false`) would otherwise be ignored and
        # leave the router on; report it while still using the rest.
        problems.append("unknown top-level keys: " + ", ".join(unknown))
    if "knowledge_max_topics" in data:
        value = data["knowledge_max_topics"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            problems.append("knowledge_max_topics must be a non-negative integer")
        elif value > ROUTER_MAX_KNOWLEDGE_TOPICS:
            problems.append(f"knowledge_max_topics is at most {ROUTER_MAX_KNOWLEDGE_TOPICS}")
            settings["knowledge_max_topics"] = ROUTER_MAX_KNOWLEDGE_TOPICS
        else:
            settings["knowledge_max_topics"] = value
    budgets = data.get("budgets", {})
    if not isinstance(budgets, dict):
        problems.append("budgets must be a table")
        budgets = {}
    for agent, values in budgets.items():
        if agent not in ROUTER_DEFAULT_BUDGETS:
            problems.append(f"budgets.{agent} is not budgets.claude or budgets.codex")
            continue
        try:
            if not isinstance(values, dict):
                raise RouterTomlInvalid(f"budgets.{agent} must be a table")
            group = dict(ROUTER_DEFAULT_BUDGETS[agent])
            for key, value in values.items():
                if key not in ROUTER_BUDGET_KEYS:
                    raise RouterTomlInvalid(f"budgets.{agent}.{key} is not a budget key")
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise RouterTomlInvalid(f"budgets.{agent}.{key} must be a non-negative integer")
                group[key] = value
            settings["budgets"][agent] = group
        except RouterTomlInvalid as exc:
            problems.append(f"{exc}; budgets.{agent} uses the defaults")
    # A table present in the file replaces the shipped default as a whole.
    if "project_tokens" in data:
        try:
            table = data["project_tokens"]
            if not isinstance(table, dict):
                raise RouterTomlInvalid("project_tokens must be a table")
            settings["project_tokens"] = {
                str(name): _string_list(tokens, f"project_tokens.{name}") for name, tokens in table.items()
            }
        except RouterTomlInvalid as exc:
            problems.append(f"{exc}; project_tokens uses the defaults")
    if "cwd_topics" in data:
        table = data["cwd_topics"]
        if isinstance(table, dict) and all(isinstance(v, str) for v in table.values()):
            settings["cwd_topics"] = {str(path): value for path, value in table.items()}
        else:
            problems.append("cwd_topics must map paths to topic names; cwd_topics uses the defaults")
    knowledge = data.get("knowledge", {})
    if not isinstance(knowledge, dict):
        problems.append("knowledge must be a table")
        knowledge = {}
    if "topic" in knowledge:
        try:
            topics = knowledge["topic"]
            if not isinstance(topics, list) or not all(isinstance(item, dict) for item in topics):
                raise RouterTomlInvalid("knowledge.topic must be an array of tables")
            parsed = []
            for index, item in enumerate(topics):
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise RouterTomlInvalid(f"knowledge.topic[{index}].name must be a string")
                parsed.append({"name": name.strip(), "words": _string_list(item.get("words", []), f"knowledge.topic[{index}].words")})
            settings["topics"] = parsed
        except RouterTomlInvalid as exc:
            problems.append(f"{exc}; the topic word lists use the defaults")
    relevance = data.get("relevance", {})
    if not isinstance(relevance, dict):
        problems.append("relevance must be a table; relevance uses the defaults")
        relevance = {}
    if "function_chars" in relevance:
        if isinstance(relevance["function_chars"], str) and relevance["function_chars"].strip():
            settings["relevance"]["function_chars"] = "".join(relevance["function_chars"].split())
        else:
            problems.append("relevance.function_chars must be a non-empty string; the default list applies")
    if "rare_share" in relevance:
        share = relevance["rare_share"]
        if isinstance(share, (int, float)) and not isinstance(share, bool) and 0 < share < 1:
            settings["relevance"]["rare_share"] = float(share)
        else:
            problems.append("relevance.rare_share must be a number between 0 and 1; the default applies")
    if "filler_words" in relevance:
        try:
            settings["relevance"]["filler_words"] = [word.strip() for word in _string_list(relevance["filler_words"], "relevance.filler_words")
                                                     if word.strip()]
        except RouterTomlInvalid as exc:
            problems.append(f"{exc}; the default filler words apply")
    if "rare_min_docs" in relevance:
        floor = relevance["rare_min_docs"]
        if isinstance(floor, int) and not isinstance(floor, bool) and floor >= 0:
            settings["relevance"]["rare_min_docs"] = floor
        else:
            problems.append("relevance.rare_min_docs must be a whole number of records; the default applies")
    failures = data.get("failures", {})
    if not isinstance(failures, dict):
        problems.append("failures must be a table")
        failures = {}
    if "speed_bump_enabled" in failures:
        if isinstance(failures["speed_bump_enabled"], bool):
            settings["speed_bump_enabled"] = failures["speed_bump_enabled"]
        else:
            problems.append("failures.speed_bump_enabled must be a boolean")
    if "phrase_stoplist" in failures:
        try:
            settings["phrase_stoplist"] = [" ".join(item.split()) for item in _string_list(failures["phrase_stoplist"], "failures.phrase_stoplist")]
        except RouterTomlInvalid as exc:
            problems.append(f"{exc}; the default phrase stoplist applies")
    if "speed_bump" in failures:
        bumps = failures["speed_bump"]
        if isinstance(bumps, list) and all(isinstance(item, dict) for item in bumps):
            settings["speed_bumps"] = bumps
        else:
            problems.append("failures.speed_bump must be an array of tables; the built-in list applies")
    if "speed_bump_failures" in failures:
        # Which of your own failure records a held command shows, by speed-bump
        # id, without copying the whole built-in list (spec 公開repo #1).
        links = failures["speed_bump_failures"]
        if isinstance(links, dict) and all(
            isinstance(value, list) and all(isinstance(item, str) for item in value) for value in links.values()
        ):
            settings["speed_bump_failures"] = {str(key): list(value) for key, value in links.items()}
        else:
            problems.append("failures.speed_bump_failures must map speed-bump ids to arrays of failure ids")
    if problems:
        settings["code"] = "router-toml-invalid"
    return settings


_ENABLED_FALSE_RE = re.compile(r"^\s*enabled\s*=\s*false\s*(#.*)?$", re.M)


def resolve_speed_bumps(settings: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """The speed bumps in force: router.toml's list, else the built-in one,
    each with the failure records `[failures.speed_bump_failures]` links to
    it; then checked like check_speed_bumps."""
    configured = settings.get("speed_bumps")
    links = settings.get("speed_bump_failures") or {}
    base = DEFAULT_SPEED_BUMPS if configured is None else configured
    merged = [
        {**entry, "failure_ids": [*entry.get("failure_ids", []), *links.get(str(entry.get("id")), [])]}
        if isinstance(entry, dict) and links.get(str(entry.get("id"))) else entry
        for entry in base
    ]
    usable, problems = check_speed_bumps(merged)
    known = {str(entry.get("id")) for entry in base if isinstance(entry, dict)}
    problems += [f"failures.speed_bump_failures names no speed bump: {name}" for name in links if name not in known]
    return usable, problems


def check_speed_bumps(bumps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """The usable speed-bump entries, compiled, and one problem per entry skipped.

    An entry without a non-empty id, pattern, and reason, with an id that
    cannot name a marker file, a pattern not anchored with `^`, a pattern
    that does not compile, or failure_ids / ignore_case of the wrong type is
    skipped alone; the rest of the file still applies (spec 8.10, 8.13 c),
    so one typo never turns a deliberate setting back. Each usable entry
    carries its compiled pattern under "regex".
    """
    usable, problems = [], []
    for index, entry in enumerate(bumps):
        where = f"failures.speed_bump[{index}]"
        missing = [key for key in ("id", "pattern", "reason") if not isinstance(entry.get(key), str) or not entry[key].strip()]
        if missing:
            problems.append(f"{where} lacks " + ", ".join(missing))
            continue
        where = f"{where} ({entry['id']})"
        if not SPEED_BUMP_ID_RE.fullmatch(entry["id"]):
            problems.append(f"{where} id must match {SPEED_BUMP_ID_RE.pattern}")
            continue
        if not entry["pattern"].startswith("^"):
            problems.append(f"{where} pattern must start with ^ (it is matched at a command word)")
            continue
        failure_ids = entry.get("failure_ids", [])
        ignore_case = entry.get("ignore_case", False)
        if not isinstance(failure_ids, list) or not all(isinstance(item, str) for item in failure_ids):
            problems.append(f"{where} failure_ids must be an array of strings")
            continue
        if not isinstance(ignore_case, bool):
            problems.append(f"{where} ignore_case must be a boolean")
            continue
        try:
            regex = re.compile(entry["pattern"], re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            problems.append(f"{where} pattern does not compile: {exc}")
            continue
        usable.append({**entry, "failure_ids": list(failure_ids), "regex": regex})
    return usable, problems


def load_router_settings(path: Path | None = None) -> dict[str, Any]:
    """router.toml over the spec defaults; never raises.

    Missing file: the defaults. Unreadable file, invalid TOML, or a top-level
    key of the wrong type: the whole file is treated as absent, and the result
    carries code router-toml-invalid so health and validate can say so.
    Note that "\b" is a legal TOML escape (backspace): a regex must be written
    as a single-quoted literal string, or it parses into something else.
    """
    import tomllib

    target = path or (config_home() / ROUTER_TOML_NAME)
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return router_defaults()
    except OSError as exc:
        settings = router_defaults()
        settings.update(code="router-toml-invalid", problems=[f"cannot read {target}: {exc}"])
        return settings
    try:
        text = raw.decode("utf-8")
        return _parse_router_toml(tomllib.loads(text))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, RouterTomlInvalid) as exc:
        settings = router_defaults()
        settings.update(code="router-toml-invalid", problems=[f"{target}: {exc}"])
        # A file that does not parse still never turns a deliberate
        # `enabled = false` (a top-level line before any table) back on.
        head = raw.decode("utf-8", errors="replace").split("\n[", 1)[0]
        if _ENABLED_FALSE_RE.search(head):
            settings["enabled"] = False
        return settings
