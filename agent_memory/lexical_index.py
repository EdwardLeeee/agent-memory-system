"""The derived lexical index: build, open, and bounded delta overlay.

Kept separate from the ledger because the two have opposite properties: the
ledger is canonical and must never be rebuilt, while everything here is
disposable and reconstructible from it.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import sqlite3
import tempfile
import time
from typing import Any
import unicodedata

from .semantic_document import build_semantic_document
from .federation import (
    FederationConfigError,
    collect_source_entries,
    list_source_configs,
    load_source_config,
)
from .storage import (
    git_environment,
    worktree_paths,
    config_home,
    load_router_settings,
    SCHEMA_VERSION,
    MemoryError,
    _extract_section,
    _sha256_file,
    _secure_dir,
    atomic_write_text,
    decode_record,
    estimate_tokens,
    utc_now,
)


INDEX_SCHEMA_VERSION = 6


QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "me",
    "my",
    "of",
    "on",
    "or",
    "our",
    "ours",
    "should",
    "so",
    "than",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "to",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
}


# Words too common in project names to count as naming the project.
GENERIC_PROJECT_NAME_TOKENS = frozenset(
    {
        "game", "games", "web", "website", "site", "app", "apps", "mobile",
        "personal", "project", "server", "client", "core", "front", "back",
        "test", "demo", "desktop", "work",
    }
)


# --- Routing (spec 記憶路由樹 v1.1, 8.1) -------------------------------------
#
# Every index row belongs to exactly one node. node_for is a pure function of
# a record's own metadata and a routing table computed from configuration
# alone (machine.json, sources.d, router.toml) plus the project scopes the
# ledger uses; it never looks at other records, cwd, or the prompt, so
# reindex, the incremental upsert, and the delta overlay all agree.

PROJECT_PREFIX = "project:"


def _split_name_tokens(name: str) -> set[str]:
    """A project name's distinctive parts (four or more letters, not generic)."""
    return {
        token
        for token in re.split(r"[:_-]+", name.casefold())
        if len(token) >= 4 and not token.isdigit() and token not in GENERIC_PROJECT_NAME_TOKENS
    }


def _read_machine_json() -> dict[str, Any]:
    try:
        value = json.loads((config_home() / "machine.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _source_configs() -> list[dict[str, Any]]:
    configs = []
    try:
        names = list_source_configs(config_home())
    except OSError:
        return []
    for name in names:
        try:
            configs.append(load_source_config(config_home(), name))
        except (FederationConfigError, OSError):
            continue
    return configs


def compute_routing(ledger_scopes: "Any" = ()) -> dict[str, Any]:
    """The routing table: each project's tokens and each source's project.

    Projects come from machine.json scope_paths and scope_aliases keys, source
    affinities, and project scopes the ledger uses; workspace scopes are not
    projects. A project's tokens are its name's distinctive parts, its
    aliases (machine.json and sources.d), and router.toml [project_tokens].
    Tokens are never inferred from records. A token two projects share is
    removed from both and reported.
    """
    machine = _read_machine_json()
    settings = load_router_settings()
    projects: set[str] = set()
    for table in ("scope_paths", "scope_aliases"):
        for key in (machine.get(table) or {}):
            if str(key).startswith(PROJECT_PREFIX):
                projects.add(str(key)[len(PROJECT_PREFIX):])
    sources: dict[str, str | None] = {}
    source_aliases: dict[str, set[str]] = {}
    for config in _source_configs():
        tags = [tag for tag in config["affinity"] if tag.startswith(PROJECT_PREFIX)]
        sources[config["name"]] = tags[0][len(PROJECT_PREFIX):] if tags else None
        for tag in tags:
            project = tag[len(PROJECT_PREFIX):]
            projects.add(project)
            source_aliases.setdefault(project, set()).update(config.get("aliases", []))
    for scope in ledger_scopes:
        if str(scope).startswith(PROJECT_PREFIX):
            projects.add(str(scope)[len(PROJECT_PREFIX):])
    machine_aliases = machine.get("scope_aliases") or {}
    candidates: dict[str, set[str]] = {}
    for project in projects:
        tokens = _split_name_tokens(project)
        aliases = machine_aliases.get(f"{PROJECT_PREFIX}{project}", [])
        tokens.update(str(a).casefold() for a in aliases if isinstance(a, str) and a.strip())
        tokens.update(str(a).casefold() for a in source_aliases.get(project, ()) if str(a).strip())
        tokens.update(t.casefold() for t in settings["project_tokens"].get(project, []) if t.strip())
        candidates[project] = tokens
    owners: dict[str, list[str]] = {}
    for project, tokens in candidates.items():
        for token in tokens:
            owners.setdefault(token, []).append(project)
    collisions = {token: sorted(names) for token, names in owners.items() if len(names) > 1}
    return {
        "projects": {
            project: sorted(tokens - set(collisions)) for project, tokens in sorted(candidates.items())
        },
        "sources": dict(sorted(sources.items())),
        "collisions": dict(sorted(collisions.items())),
    }


def _token_owner(routing: dict[str, Any]) -> dict[str, str]:
    owner = routing.get("_owner")
    if owner is None:
        owner = {
            token: project
            for project, tokens in routing.get("projects", {}).items()
            for token in tokens
        }
        routing["_owner"] = owner
    return owner


# --- Where a lesson was learned, where it applies (spec 學到的地方與適用範圍 v1) ---
#
# failure, experience, decision, and lesson records may carry three optional
# fields: origin (where it was learned), applies (global, or only its origin),
# and topic. Without them every record routes exactly as before.

LEARNING_KINDS = frozenset({"failure", "experience", "decision", "lesson"})
ORIGIN_RE = re.compile(r"^(global|project:[a-z0-9][a-z0-9._-]*|workspace:[a-z0-9][a-z0-9._-]*|machine:[A-Za-z0-9._-]+)$")
TOPIC_RE = re.compile(r"^[a-z0-9_]+$")
APPLIES_VALUES = ("global", "origin")


def _segment(metadata: dict[str, Any]) -> str:
    parts = str(metadata.get("id", "")).split(".")
    return parts[1].casefold() if len(parts) >= 2 and parts[1] else ""


def record_origins(metadata: dict[str, Any], routing: dict[str, Any]) -> list[str]:
    """Where a record was learned: its origin field, else derived from its
    scope, a promoted lesson's source, or the project its id names."""
    origin = metadata.get("origin")
    if isinstance(origin, str) and origin:
        return [origin]
    if isinstance(origin, list) and origin and all(isinstance(item, str) for item in origin):
        return list(origin)
    scope = str(metadata.get("scope", ""))
    if scope.startswith(("project:", "workspace:", "machine:")):
        return [scope]
    if isinstance(origin, dict) and origin.get("source"):
        project = routing.get("sources", {}).get(str(origin["source"]))
        if project:
            return [f"project:{project}"]
    owner = _token_owner(routing).get(_segment(metadata))
    return [f"project:{owner}"] if owner else ["global"]


def origin_projects(metadata: dict[str, Any], routing: dict[str, Any]) -> list[str]:
    return [item[len("project:"):] for item in record_origins(metadata, routing) if item.startswith("project:")]


def record_topic(metadata: dict[str, Any], routing: dict[str, Any]) -> str | None:
    """The record's topic field, else its id segment unless that names a project."""
    topic = metadata.get("topic")
    if isinstance(topic, str) and TOPIC_RE.fullmatch(topic):
        return topic
    segment = _segment(metadata)
    return segment if segment and segment not in _token_owner(routing) else None


def learning_field_problems(kind: str, metadata: dict[str, Any], routing: dict[str, Any]) -> list[str]:
    """What is wrong with a record's origin, applies, and topic (all optional)."""
    problems = []
    present = [key for key in ("origin", "applies", "topic") if key in metadata]
    if not present:
        return problems
    origin = metadata.get("origin")
    if kind == "lesson" and isinstance(origin, dict):
        pass  # a promoted lesson's origin is its provenance
    elif "origin" in metadata:
        if kind not in LEARNING_KINDS:
            problems.append(f"origin is only for {', '.join(sorted(LEARNING_KINDS))} records")
        elif kind == "lesson":
            problems.append("a lesson's origin is its provenance object; its source gives where it was learned")
        else:
            values = [origin] if isinstance(origin, str) else origin
            if not isinstance(values, list) or not values or not all(
                isinstance(item, str) and ORIGIN_RE.fullmatch(item) for item in values
            ):
                problems.append("origin must be global, project:<name>, workspace:<name>, machine:<id>, or a list of them")
            elif len(set(values)) != len(values):
                problems.append("origin lists a place twice")
    if "applies" in metadata:
        if kind not in LEARNING_KINDS:
            problems.append(f"applies is only for {', '.join(sorted(LEARNING_KINDS))} records")
        elif metadata["applies"] not in APPLIES_VALUES:
            problems.append("applies must be global or origin")
        elif metadata["applies"] == "global" and str(metadata.get("scope", "")).startswith("machine:"):
            problems.append("applies = global needs a scope that is not machine:*; supersede the scope too")
    if "topic" in metadata:
        topic = metadata["topic"]
        if kind not in LEARNING_KINDS:
            problems.append(f"topic is only for {', '.join(sorted(LEARNING_KINDS))} records")
        elif not isinstance(topic, str) or not TOPIC_RE.fullmatch(topic):
            problems.append("topic must be lowercase letters, digits, and underscores")
        elif topic in _token_owner(routing):
            problems.append(f"topic {topic} is a project token; name the project in origin instead")
    return problems


def unknown_origin_projects(metadata: dict[str, Any], known: "set[str]") -> list[str]:
    """With applies = origin, the projects the origin field names that routing
    does not know: the record would sit in a node nothing opens and its
    PreToolUse trigger would never fire (spec 8.0 ruling 52 f)."""
    if metadata.get("applies") != "origin":
        return []
    origin = metadata.get("origin")
    values = [origin] if isinstance(origin, str) else origin if isinstance(origin, list) else []
    return [
        str(item)[len(PROJECT_PREFIX):] for item in values
        if isinstance(item, str) and item.startswith(PROJECT_PREFIX) and item[len(PROJECT_PREFIX):] not in known
    ]


def known_projects(root: Path, extra_scopes: "Any" = ()) -> set[str]:
    """Every project routing knows: the snapshot's (which holds the ledger's
    project scopes), today's configuration, and the given scopes."""
    snapshot = _routing_from_snapshot(root) if _index_path(root).is_file() else {}
    return set(snapshot.get("projects", {})) | set(compute_routing(extra_scopes)["projects"])


def node_for(metadata: dict[str, Any], routing: dict[str, Any]) -> str:
    """The one node a record belongs to; never empty, never raises on odd input."""
    kind = str(metadata.get("kind", ""))
    record_id = str(metadata.get("id", ""))
    parts = record_id.split(".")
    segment = parts[1].casefold() if len(parts) >= 2 and parts[1] else "misc"
    if kind == "preference":
        return "rules"
    # An explicit applies decides first (spec 學到的地方與適用範圍 v1, section 3).
    applies = metadata.get("applies")
    if kind in LEARNING_KINDS and applies in APPLIES_VALUES and not str(metadata.get("scope", "")).startswith("machine:"):
        if applies == "global":
            return "failures" if kind == "failure" else f"knowledge/{record_topic(metadata, routing) or 'misc'}"
        projects = origin_projects(metadata, routing)
        if projects:
            if kind == "failure":
                return f"project/{projects[0]}/failures"
            return f"project/{projects[0]}/sources/lessons" if kind == "lesson" else f"project/{projects[0]}"
    if kind == "failure":
        # A failure whose id names a project is that project's (spec 8.0
        # ruling 29); the rest wait for PreToolUse or a prompt naming them.
        owner = _token_owner(routing).get(segment)
        return f"project/{owner}/failures" if owner else "failures"
    if kind == "fact":
        return "facts"
    if kind == "task":
        return "history"
    tags = [str(tag) for tag in (metadata.get("tags") or [])]
    source = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("federation:")), None)
    if source is None and record_id.startswith("federation.") and len(parts) >= 2:
        source = parts[1]
    if source is not None:
        affinity = [str(tag) for tag in (metadata.get("affinity") or []) if str(tag).startswith(PROJECT_PREFIX)]
        project = affinity[0][len(PROJECT_PREFIX):] if affinity else routing.get("sources", {}).get(source)
        if project:
            return f"project/{project}/sources" + ("/lessons" if kind == "lesson" else "")
        return f"knowledge/{source.casefold() or 'misc'}"
    if kind == "lesson":
        origin = metadata.get("origin") if isinstance(metadata.get("origin"), dict) else {}
        origin_source = str(origin.get("source") or "")
        if origin_source and origin_source in routing.get("sources", {}):
            project = routing["sources"][origin_source]
            if project:
                return f"project/{project}/sources/lessons"
            return f"knowledge/{origin_source.casefold()}"
    scope = str(metadata.get("scope", ""))
    if scope.startswith(PROJECT_PREFIX) and scope[len(PROJECT_PREFIX):]:
        project = scope[len(PROJECT_PREFIX):]
        return f"project/{project}/sources/lessons" if kind == "lesson" else f"project/{project}"
    if kind in {"experience", "decision", "lesson"}:
        owner = _token_owner(routing).get(segment)
        if owner:
            return f"project/{owner}/sources/lessons" if kind == "lesson" else f"project/{owner}"
    return f"knowledge/{segment}"


def _routing_for_storage(routing: dict[str, Any]) -> str:
    return json.dumps(
        {key: value for key, value in routing.items() if not key.startswith("_")},
        ensure_ascii=False,
        sort_keys=True,
    )


def load_index_routing(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute("SELECT value FROM meta WHERE key = 'routing_json'").fetchone()
    if row is None:
        return None
    try:
        value = json.loads(str(row[0]))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _routing_from_snapshot(root: Path) -> dict[str, Any]:
    """The routing the current snapshot was built with, else one from config."""
    try:
        connection = sqlite3.connect(_index_path(root))
        try:
            routing = load_index_routing(connection)
        finally:
            connection.close()
    except sqlite3.Error:
        routing = None
    return routing if routing is not None else compute_routing()


def routing_report(root: Path) -> list[str]:
    """What `memoryctl reindex` prints about routing, read from the built index.

    Per-node record counts, each project's tokens, token collisions, sources
    with no project, knowledge topics with their word-list sizes (a topic of
    five or more records without words is only reachable by its own name),
    and the fact entity list (spec 8.1, 8.4, 8.6).
    """
    connection = sqlite3.connect(_index_path(root))
    try:
        routing = load_index_routing(connection) or {}
        counts = connection.execute("SELECT node, COUNT(*) FROM records GROUP BY node ORDER BY node").fetchall()
    finally:
        connection.close()
    lines = ["Nodes:"] + [f"  {node}: {count}" for node, count in counts]
    lines.append("Project tokens:")
    lines += [f"  {project}: {', '.join(tokens) or '(none)'}" for project, tokens in routing.get("projects", {}).items()]
    for token, projects in routing.get("collisions", {}).items():
        lines.append(
            f"WARN token {token} is claimed by {', '.join(projects)}; neither gets it "
            "until router.toml [project_tokens] settles it"
        )
    for source, project in routing.get("sources", {}).items():
        if not project:
            lines.append(f"WARN source {source} has no project:* affinity; its entries are in knowledge/{source}")
    words = {str(topic["name"]).casefold(): topic["words"] for topic in load_router_settings()["topics"]}
    lines.append("Knowledge topics (records, words):")
    for node, count in counts:
        if str(node).startswith("knowledge/"):
            topic = str(node)[len("knowledge/"):]
            size = len(words.get(topic, []))
            warn = " WARN no words; reachable only by its own name" if count >= 5 and not size else ""
            lines.append(f"  {topic}: {count}, {size}{warn}")
    path = _fact_entity_path(root)
    try:
        entities = json.loads(path.read_text(encoding="utf-8")).get("entities", [])
        lines.append(f"Fact entities: {len(entities)} in {path}")
    except (OSError, ValueError, AttributeError):
        lines.append(f"WARN fact entities missing at {path}; the facts node stays closed")
    return lines


# --- Failure triggers (spec 8.2, 4.2.4) ----------------------------------------
#
# The PreToolUse hook never opens SQLite: it reads .cache/triggers.json, which
# reindex writes and every failure write refreshes. Each entry carries what
# the hook prints, so the hook needs nothing else.

# Command words whose subcommand or flag makes a phrase; a tool name alone is
# never a trigger (`git` would match every git command).
COMMAND_TOOLS = (
    "apt-get", "apt", "adb", "bash", "cargo", "chmod", "chown", "claude", "codex", "cp", "curl",
    "dart", "docker", "dropdb", "find", "firebase", "flutter", "gcloud", "gh", "git", "go",
    "gradle", "herdr", "journalctl", "kill", "killall", "make", "memoryctl", "mv", "node", "npm",
    "npx", "pgrep", "pip", "pip3", "pkill", "playwright", "pnpm", "podman", "psql", "python",
    "python3", "rm", "rsync", "scp", "sed", "sqlite3", "ssh", "sudo", "systemctl", "tar", "tmux",
    "uv", "xargs", "xcodebuild", "yarn",
)
_PHRASE_STOPWORDS = frozenset(
    {
        "a", "after", "again", "all", "also", "an", "and", "any", "are", "as", "at", "be", "been",
        "before", "but", "by", "can", "could", "did", "do", "does", "each", "every", "for", "from",
        "had", "has", "have", "here", "if", "in", "into", "is", "it", "its", "may", "might", "no",
        "not", "of", "on", "only", "or", "per", "should", "so", "still", "than", "that", "the",
        "them", "then", "there", "they", "this", "to", "via", "was", "we", "were", "when", "which",
        "while", "will", "with", "within", "without", "would", "you",
    }
)
_TOOL_PHRASE_RE = re.compile(
    r"(?<![\w./-])(" + "|".join(re.escape(tool) for tool in sorted(COMMAND_TOOLS, key=len, reverse=True)) + r")"
    r"((?:[ \t]+(?:--?[A-Za-z][\w-]*|[a-z][a-z0-9_-]*)){1,2})(?![\w-])"
)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
# Where a failure says what was being run. Verified Fix is left out: it
# names the right way to do it, and doing the right thing must not trigger
# the lesson (spec 8.0 ruling 32).
TRIGGER_SECTIONS = ("Retry When", "Symptom")


def command_phrases(text: str, stoplist: "Any" = ()) -> list[str]:
    """Command phrases a failure's prose names (spec 4.2.4, ruling 32).

    A known command word followed by at least one subcommand or flag, from
    backtick spans and from prose (`git reset --soft`, `gh pr checks`,
    `pkill -f`); a command word alone, or a phrase in the stoplist, is never
    a phrase.
    """
    stop = {" ".join(str(item).split()) for item in stoplist}
    phrases: list[str] = []
    for span in _BACKTICK_RE.findall(text):
        words = span.split()
        if len(words) >= 2 and words[0] in COMMAND_TOOLS:
            phrases.append(" ".join(words))
    for match in _TOOL_PHRASE_RE.finditer(text):
        words = match.group(2).split()
        while words and words[-1] in _PHRASE_STOPWORDS:
            words.pop()
        if words and words[0] not in _PHRASE_STOPWORDS:
            phrases.append(" ".join([match.group(1), *words]))
    from .pretool import phrase_is_inspection, phrase_mentions_help

    return [
        phrase for phrase in dict.fromkeys(phrases)
        if phrase not in stop and not phrase_mentions_help(phrase) and not phrase_is_inspection(phrase)
    ]


def _usable_outbound(triggers: "Any") -> list[str]:
    from .core import outbound_trigger_problem

    return [str(item) for item in triggers if isinstance(item, str) and not outbound_trigger_problem(item)]


def trigger_entry(
    metadata: dict[str, Any], body: str, stoplist: "Any" = (), routing: dict[str, Any] | None = None
) -> dict[str, Any]:
    from .pretool import failure_block

    text = "\n".join(_extract_section(body, section) for section in TRIGGER_SECTIONS)
    routing = routing if routing is not None else {}
    entry = {
        "id": str(metadata["id"]),
        "scope": str(metadata.get("scope", "global")),
        "expires_at": metadata.get("expires_at"),
        # Where it was learned and whether it applies only there.
        "origins": origin_projects(metadata, routing),
        "applies": metadata.get("applies"),
        "created_at": str(metadata.get("created_at") or ""),
        "explicit": [str(item) for item in (metadata.get("triggers") or [])],
        # What the agent sends or replies, not what it runs (F13); one
        # validate names is left out (F13 re-check 1).
        "outbound": _usable_outbound(metadata.get("outbound_triggers") or []),
        "phrases": command_phrases(text, stoplist),
        "symptom": " ".join(_extract_section(body, "Symptom").split()),
        "verified_fix": " ".join(_extract_section(body, "Verified Fix").split()),
        "retry_when": " ".join(_extract_section(body, "Retry When").split()),
    }
    entry["tokens"] = estimate_tokens("\n".join(failure_block(entry)))
    return entry


def _triggers_path(root: Path) -> Path:
    return root / ".cache" / "triggers.json"


def write_triggers(root: Path, failures: "Any", routing: dict[str, Any] | None = None) -> None:
    stoplist = load_router_settings()["phrase_stoplist"]
    if routing is None:
        routing = _routing_from_snapshot(root)
    entries = [
        trigger_entry(metadata, body, stoplist, routing) for metadata, body in failures if metadata.get("kind") == "failure"
    ]
    atomic_write_text(
        _triggers_path(root),
        json.dumps({"generated_at": utc_now(), "entries": sorted(entries, key=lambda e: e["id"])}, ensure_ascii=False)
        + "\n",
    )


def _refresh_triggers(root: Path) -> None:
    """Recompute from the index after a failure changed; keep the old file on failure."""
    try:
        connection = sqlite3.connect(_index_path(root))
        try:
            rows = connection.execute("SELECT metadata_json, body FROM records WHERE kind = 'failure'").fetchall()
        finally:
            connection.close()
        write_triggers(root, [(json.loads(str(row[0])), str(row[1])) for row in rows])
    except (OSError, sqlite3.Error, ValueError):
        return


# --- Ledger decisions a project has since recorded itself (F8) -------------
#
# A project's own decision record (an ADR, a decision log's entry) that
# appeared after a ledger decision of that project was last updated, and says
# much the same: at least NEWER_SHARED_MIN words that fewer than rare_share of
# all records hold (and fewer than rare_min_docs, as for the relevance floor),
# and that many a fifth or more of the shorter's. Computed
# when the index is built, so the prompt hook only looks it up.

NEWER_SHARED_MIN = 5
NEWER_SHARE_MIN = 0.2
NEWER_SHOWN = 2


def _newer_decisions_path(root: Path) -> Path:
    return root / ".cache" / "newer_decisions.json"


def read_newer_decisions(root: Path) -> dict[str, list[dict[str, str]]]:
    """ledger decision id -> the newer project records, best first; {} when
    not computed."""
    try:
        value = json.loads(_newer_decisions_path(root).read_text(encoding="utf-8")).get("decisions")
    except (OSError, ValueError, AttributeError):
        return {}
    return value if isinstance(value, dict) else {}


def _moment(value: str) -> datetime | None:
    """A time to compare: an ISO time with its zone, or a day alone as its
    start where the user is (a log's heading); None when neither (F8,
    retrieval review: one side was a UTC day, the other a local one)."""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.now().astimezone().tzinfo)


def write_newer_decisions(root: Path) -> None:
    from .core import _query_terms

    connection = sqlite3.connect(_index_path(root))
    try:
        routing = load_index_routing(connection) or compute_routing()
        rows = [(json.loads(str(meta)), str(body)) for meta, body in connection.execute(
            "SELECT metadata_json, body FROM records WHERE kind = 'decision' OR record_id LIKE 'federation.%'")]
        total = int(connection.execute("SELECT count(*) FROM records").fetchone()[0])
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.records_vocab USING fts5vocab(main, records_fts, 'row')")
        held = {str(term): int(docs) for term, docs in connection.execute("SELECT term, doc FROM temp.records_vocab")}
    finally:
        connection.close()
    relevance = load_router_settings().get("relevance") or {}
    limit = max(float(relevance.get("rare_share") or 0.05) * total, int(relevance.get("rare_min_docs") or 0))
    function_chars = set(str(relevance.get("function_chars") or ""))

    def uncommon(text: str) -> set[str]:
        """The uncommon meaningful words of `text`: a Chinese bigram with a
        function character (的記, 在的) is a fragment, as in F3 (retrieval
        review), not a word two records could share."""
        found = set()
        for term in _query_terms(text):
            if len(term) == 2 and re.search(r"[\u3400-\u9fff]", term) and function_chars & set(term):
                continue
            pieces = [piece for piece in re.split(r"[^0-9a-z\u3400-\u9fff]+", term) if piece]
            if pieces and min(held.get(piece, 0) for piece in pieces) < limit:
                found.add(term)
        return found

    records: dict[str, list[tuple[str, str, set[str]]]] = {}
    ledger = []
    for metadata, body in rows:
        record_id = str(metadata.get("id", ""))
        if record_id.startswith("federation."):
            if metadata.get("decision_record") and metadata.get("recorded_on"):
                anchor = str((metadata.get("origin") or {}).get("anchor") or "")
                terms = uncommon(f"{anchor} {_extract_section(body, 'Decision') or _extract_section(body, 'Value')}")
                for tag in metadata.get("affinity") or []:
                    if str(tag).startswith("project:"):
                        records.setdefault(str(tag)[len("project:"):], []).append(
                            (record_id, str(metadata["recorded_on"]), terms))
        elif metadata.get("kind") == "decision":
            words = record_id.split(".", 2)[-1].replace("_", " ")
            terms = uncommon(f"{words} {_extract_section(body, 'Problem')} {_extract_section(body, 'Decision')}")
            ledger.append((record_id, str(metadata.get("updated_at") or ""), origin_projects(metadata, routing), terms))
    found: dict[str, list[dict[str, str]]] = {}
    for record_id, updated, projects, terms in ledger:
        matches = []
        updated_at = _moment(updated)
        for project in projects:
            for other, day, other_terms in records.get(project, []):
                shared = len(terms & other_terms)
                recorded = _moment(day)
                if (updated_at is not None and recorded is not None and recorded > updated_at
                        and shared >= NEWER_SHARED_MIN and shared >= NEWER_SHARE_MIN * min(len(terms), len(other_terms))):
                    matches.append((shared / min(len(terms), len(other_terms)), other, day[:10]))
        if matches:
            found[record_id] = [{"id": other, "recorded_on": day} for _, other, day in sorted(matches, reverse=True)[:NEWER_SHOWN]]
    atomic_write_text(_newer_decisions_path(root),
                      json.dumps({"generated_at": utc_now(), "decisions": found}, ensure_ascii=False, sort_keys=True) + "\n")


# --- Fact entities (spec 8.6) -----------------------------------------------

FACT_ENTITY_FIXED_WORDS = ("host", "machine", "主機", "本機")


def _fact_entity_path(root: Path) -> Path:
    return root / ".cache" / "fact_entities.json"


def fact_entity_words(fact_metadatas: "Any") -> list[str]:
    """Words that, in a prompt, mean a fact may be relevant.

    From each active fact visible on this machine (global or this machine's
    scope): the id's second segment and every tag, split on [._-], keeping
    parts of three or more characters that are not digits, generic project
    words, or stopwords; plus every configured project alias and the fixed
    words for the host itself.
    """
    machine = _read_machine_json()
    local_scope = f"machine:{machine.get('machine_id')}" if machine.get("machine_id") else None
    words: set[str] = set()
    for metadata in fact_metadatas:
        if metadata.get("kind") != "fact" or metadata.get("status", "active") != "active":
            continue
        scope = str(metadata.get("scope", ""))
        if scope != "global" and scope != local_scope:
            continue
        parts = str(metadata.get("id", "")).split(".")
        raw = ([parts[1]] if len(parts) >= 2 else []) + [str(tag) for tag in (metadata.get("tags") or [])]
        for item in raw:
            for token in re.split(r"[._-]+", _normalize_search_text(item)):
                if (
                    len(token) >= 3
                    and not token.isdigit()
                    and token not in GENERIC_PROJECT_NAME_TOKENS
                    and token not in QUERY_STOPWORDS
                ):
                    words.add(token)
    for names in (machine.get("scope_aliases") or {}).values():
        if isinstance(names, list):
            words.update(_normalize_search_text(str(n)) for n in names if str(n).strip())
    for config in _source_configs():
        words.update(_normalize_search_text(str(a)) for a in config.get("aliases", []) if str(a).strip())
    words.update(FACT_ENTITY_FIXED_WORDS)
    return sorted(words)


def write_fact_entities(root: Path, fact_metadatas: "Any") -> None:
    atomic_write_text(
        _fact_entity_path(root),
        json.dumps(
            {"generated_at": utc_now(), "entities": fact_entity_words(fact_metadatas)},
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
    )


def _refresh_fact_entities(root: Path) -> None:
    """Recompute from the index after a fact changed; keep the old file on failure."""
    try:
        connection = sqlite3.connect(_index_path(root))
        try:
            rows = connection.execute("SELECT metadata_json FROM records WHERE kind = 'fact'").fetchall()
        finally:
            connection.close()
        write_fact_entities(root, [json.loads(str(row[0])) for row in rows])
    except (OSError, sqlite3.Error, ValueError):
        return


def _normalize_search_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _record_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root / "records").glob("*/*.md")):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _index_path(root: Path) -> Path:
    return root / ".cache" / "index.sqlite3"


def _index_dirty_path(root: Path) -> Path:
    return root / ".cache" / "index.dirty"


LEXICAL_DELTA_MAX_RECORDS = 32


LEXICAL_DELTA_BUDGET_SECONDS = 0.05


class LexicalIndexUnavailable(MemoryError):
    """The lexical snapshot cannot be served without an explicit rebuild."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _read_index_delta(root: Path) -> dict[str, Any] | None:
    """Which records the snapshot is behind on, or None when it is current.

    An unreadable, malformed, or legacy timestamp-only marker is reported as
    unbounded: the safe reading is that the delta cannot be replayed.
    """
    path = _index_dirty_path(root)
    if not path.exists():
        return None
    unbounded = {"bounded": False, "record_ids": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return unbounded
    if not isinstance(value, dict) or not isinstance(value.get("record_ids"), list):
        return unbounded
    return {
        "bounded": bool(value.get("bounded")),
        "record_ids": [str(item) for item in value["record_ids"]],
    }


def _invalidate_index(root: Path, record_id: str | None = None) -> None:
    journal = _read_index_delta(root) or {"bounded": True, "record_ids": []}
    if record_id is None or not journal["bounded"]:
        # Bulk operations cannot be replayed record by record.
        journal = {"bounded": False, "record_ids": []}
    else:
        ids = list(dict.fromkeys([*journal["record_ids"], record_id]))
        journal = (
            {"bounded": True, "record_ids": ids}
            if len(ids) <= LEXICAL_DELTA_MAX_RECORDS
            else {"bounded": False, "record_ids": []}
        )
    journal["marked_at"] = utc_now()
    atomic_write_text(
        _index_dirty_path(root),
        json.dumps(journal, ensure_ascii=False, sort_keys=True) + "\n",
    )


def _canonical_item(
    root: Path,
    path: Path,
    metadata: dict[str, Any],
    body: str,
    routing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity_text, content_text, evidence_text = _search_fields(metadata, body)
    return {
        "node": node_for(metadata, routing if routing is not None else _routing_from_snapshot(root)),
        "path": str(path.relative_to(root)),
        "metadata": metadata,
        "body": body,
        "identity_text": identity_text,
        "content_text": content_text,
        "evidence_text": evidence_text,
        "semantic_content_hash": build_semantic_document(metadata, body).content_hash,
        "search_text": " ".join((identity_text, content_text, evidence_text)),
    }


def _delta_overlay(
    root: Path, record_ids: list[str], keep: "Any", routing: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Re-read the changed records straight from canonical bytes.

    Reading canonical rather than any cached copy is what makes the overlay
    correct: it cannot serve bytes the ledger has already replaced.
    """
    deadline = time.monotonic() + LEXICAL_DELTA_BUDGET_SECONDS
    rows: list[dict[str, Any]] = []
    if routing is None and record_ids:
        routing = _routing_from_snapshot(root)
    for record_id in record_ids:
        if time.monotonic() > deadline:
            raise LexicalIndexUnavailable(
                "lexical delta overlay exceeded its latency budget",
                code="delta-budget-exceeded",
            )
        for path in sorted((root / "records").glob(f"*/{record_id}.md")):
            try:
                metadata, body = decode_record(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise LexicalIndexUnavailable(
                    f"cannot read canonical record for delta overlay: {record_id}",
                    code="delta-unreadable",
                ) from exc
            if metadata.get("status") != "active":
                continue
            item = _canonical_item(root, path, metadata, body, routing)
            if keep(item):
                rows.append(item)
    return rows


def _fts_document(text: str) -> str:
    normalized = _normalize_search_text(text)
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    bigrams = [
        run[index : index + 2]
        for run in cjk_runs
        for index in range(max(0, len(run) - 1))
    ]
    return normalized + ("\n" + " ".join(bigrams) if bigrams else "")


# Words every derived entry of a kind shares: they say where an entry came
# from, not what it is about, so letting them count as an identity match made
# any prompt containing "note" or "RD" pull in a whole source's sections.
FEDERATION_GENERIC_ID_TOKENS = frozenset(
    {"federation", "note", "commit", "settlement", "group", "entry", "adr", "rd", "research"}
)
FEDERATION_GENERIC_TAGS = frozenset({"research-note", "commit-message", "settlement", "adr"})


def _federation_identity(metadata: dict[str, Any]) -> str:
    origin = metadata.get("origin") if isinstance(metadata.get("origin"), dict) else {}
    generic = FEDERATION_GENERIC_ID_TOKENS | {str(origin.get("source", "")).casefold()}
    tokens = [
        token
        for token in re.split(r"[^a-z0-9]+", str(metadata.get("id", "")).casefold())
        if token and token not in generic and not token.isdigit()
    ]
    tags = [
        str(tag)
        for tag in metadata.get("tags", [])
        if not str(tag).startswith("federation:") and str(tag) not in FEDERATION_GENERIC_TAGS
    ]
    return " ".join([*tokens, *tags])


def _search_fields(metadata: dict[str, Any], body: str) -> tuple[str, str, str]:
    if str(metadata.get("id", "")).startswith("federation."):
        identity_source = _federation_identity(metadata)
    else:
        identity_source = " ".join(
            [
                str(metadata.get("id", "")),
                str(metadata.get("scope", "")),
                " ".join(str(tag) for tag in metadata.get("tags", [])),
            ]
        )
    identity = _normalize_search_text(identity_source)
    evidence = _normalize_search_text(_extract_section(body, "Evidence"))
    marker = "\n## Evidence\n"
    content = body.split(marker, 1)[0] if marker in body else body
    return identity, _normalize_search_text(content), evidence


def _clear_index_delta(root: Path, record_id: str) -> None:
    journal = _read_index_delta(root)
    if journal is None or not journal["bounded"]:
        return
    remaining = [item for item in journal["record_ids"] if item != record_id]
    if not remaining:
        _index_dirty_path(root).unlink(missing_ok=True)
        return
    atomic_write_text(
        _index_dirty_path(root),
        json.dumps(
            {"bounded": True, "record_ids": remaining, "marked_at": utc_now()},
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
    )


def _index_upsert(root: Path, record_id: str) -> bool:
    """Bring the snapshot up to date for one record. False if it could not."""
    if not _index_path(root).is_file():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(_index_path(root))
        connection.row_factory = sqlite3.Row
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        if (
            meta.get("index_schema_version") != str(INDEX_SCHEMA_VERSION)
            or meta.get("record_schema_version") != str(SCHEMA_VERSION)
        ):
            return False
        row = connection.execute(
            "SELECT rowid FROM records WHERE record_id = ?", (record_id,)
        ).fetchone()
        if row is not None:
            connection.execute(
                "DELETE FROM records_fts WHERE rowid = ?", (int(row["rowid"]),)
            )
            connection.execute(
                "DELETE FROM records WHERE record_id = ?", (record_id,)
            )
        connection.execute(
            "DELETE FROM decision_links WHERE experience_id = ?", (record_id,)
        )
        routing = load_index_routing(connection)
        if routing is None:
            return False
        for path in sorted((root / "records").glob(f"*/{record_id}.md")):
            metadata, body = decode_record(path)
            if metadata.get("status") != "active":
                continue
            _index_insert_record(connection, root, path, metadata, body, routing)
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("source_fingerprint", _record_fingerprint(root)),
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (
                "record_count",
                str(
                    int(
                        connection.execute(
                            "SELECT COUNT(*) FROM records"
                        ).fetchone()[0]
                    )
                ),
            ),
        )
        connection.commit()
        return True
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
        return False
    finally:
        if connection is not None:
            connection.close()


def _refresh_lexical_index(root: Path, record_id: str) -> None:
    """Keep the snapshot current after an explicit write.

    record and supersede are explicit commands, so paying index cost here is
    appropriate; the prompt path must never pay it. Runs outside the canonical
    write lock, and any failure simply leaves the delta journal in place.
    """
    try:
        delta = _read_index_delta(root)
        if not _index_path(root).is_file() or (
            delta is not None and not delta["bounded"]
        ):
            # An unbounded journal cannot be replayed record by record. Without
            # a rebuild here the store would stay degraded forever, because
            # every later write inherits the unbounded state and hooks keep
            # injecting nothing until someone runs reindex by hand.
            reindex(root)
        elif _index_upsert(root, record_id):
            _clear_index_delta(root, record_id)
            if (root / "records" / "fact" / f"{record_id}.md").exists():
                _refresh_fact_entities(root)
            if (root / "records" / "failure" / f"{record_id}.md").exists():
                _refresh_triggers(root)
            if (root / "records" / "decision" / f"{record_id}.md").exists():
                try:
                    write_newer_decisions(root)
                except Exception:
                    pass
    except (MemoryError, OSError, sqlite3.Error):
        return


def _index_insert_record(
    connection: sqlite3.Connection,
    root: Path,
    path: Path,
    metadata: dict[str, Any],
    body: str,
    routing: dict[str, Any],
) -> None:
    """Write one active canonical record into an open index connection."""
    _index_insert_row(connection, str(path.relative_to(root)), metadata, body, routing)


def _index_insert_federation_entry(
    connection: sqlite3.Connection,
    path_label: str,
    metadata: dict[str, Any],
    body: str,
    routing: dict[str, Any],
) -> None:
    """Write one derived federation entry, identical shape but no canonical file."""
    _index_insert_row(connection, path_label, metadata, body, routing)


def _index_insert_row(
    connection: sqlite3.Connection,
    path_label: str,
    metadata: dict[str, Any],
    body: str,
    routing: dict[str, Any],
) -> None:
    identity_text, content_text, evidence_text = _search_fields(metadata, body)
    semantic_document = build_semantic_document(metadata, body)
    search_text = " ".join((identity_text, content_text, evidence_text))
    cursor = connection.execute(
        """
        INSERT INTO records(
            record_id, path, kind, scope, updated_at,
            metadata_json, body, identity_text, content_text,
            evidence_text, semantic_content_hash, search_text, node
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(metadata["id"]),
            path_label,
            str(metadata["kind"]),
            str(metadata["scope"]),
            str(metadata.get("updated_at", "")),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            body,
            identity_text,
            content_text,
            evidence_text,
            semantic_document.content_hash,
            search_text,
            node_for(metadata, routing),
        ),
    )
    rowid = int(cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO records_fts(
            rowid, record_id, identity, content, evidence
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            rowid,
            str(metadata["id"]),
            _fts_document(identity_text),
            _fts_document(content_text),
            _fts_document(evidence_text),
        ),
    )
    if metadata.get("kind") == "experience":
        for link in metadata.get("decision_links", []):
            if isinstance(link, dict):
                connection.execute(
                    """
                    INSERT INTO decision_links(
                        experience_id, decision_id, effect, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        str(metadata["id"]),
                        str(link.get("id", "")),
                        str(link.get("effect", "")),
                        str(metadata.get("updated_at", "")),
                    ),
                )


# --- Source worktrees (spec 記憶路由樹 v1.1, ruling 46) ----------------------
#
# Most sessions of a project work in one of its git worktrees, not in the
# repository directory sources.d names. reindex (and so every `sources sync`)
# records each source's worktree paths here; deciding whether a cwd belongs
# to a source reads this file and never runs git on the hook path.

WORKTREE_LIST_TIMEOUT_SECONDS = 5


def source_worktrees_path(root: Path) -> Path:
    return root / ".cache" / "source_worktrees.json"


def _git_worktrees(repo: Path) -> list[str] | None:
    """The repository's worktree paths, or None when git cannot tell. Paths
    are decoded as the filesystem does, so a name that is not UTF-8 never
    stops reindex or sync (ruling 51 f)."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"], env=git_environment(),
            capture_output=True, timeout=WORKTREE_LIST_TIMEOUT_SECONDS, check=False,
        )
        if result.returncode != 0:
            return None
        return [
            os.fsdecode(field[len(b"worktree "):])
            for field in result.stdout.split(b"\0")
            if field.startswith(b"worktree ")
        ]
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        return None


def project_paths_path(root: Path) -> Path:
    return root / ".cache" / "project_paths.json"


def write_project_paths(root: Path, routing: dict[str, Any]) -> None:
    """Every project's scope_paths and source directories, for the PreToolUse
    hook, which may read only small JSON files (spec 學到的地方與適用範圍 v1)."""
    machine = _read_machine_json()
    try:
        worktrees = worktree_paths(json.loads(source_worktrees_path(root).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        worktrees = {}
    configs = _source_configs()
    projects = {}
    for name in routing.get("projects", {}):
        tag = f"{PROJECT_PREFIX}{name}"
        mapped = (machine.get("scope_paths") or {}).get(tag)
        if isinstance(mapped, str):
            scope_paths = [mapped]
        elif isinstance(mapped, list):
            scope_paths = [str(item) for item in mapped if isinstance(item, str)]
        else:
            scope_paths = []
        dirs = []
        for config in configs:
            if tag in config["affinity"]:
                dirs.append(str(config["repo"]))
                dirs.extend(worktrees.get(config["name"], []))
        projects[name] = {"scope_paths": scope_paths, "source_dirs": sorted(set(dirs))}
    atomic_write_text(
        project_paths_path(root),
        json.dumps({"generated_at": utc_now(), "projects": projects}, indent=1) + "\n",
    )


def write_source_worktrees(root: Path) -> None:
    """Refresh every source's worktree list; a source git cannot answer for
    keeps the paths it had."""
    path = source_worktrees_path(root)
    try:
        previous = worktree_paths(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        previous = {}
    sources: dict[str, list[str]] = {}
    for config in _source_configs():
        paths = _git_worktrees(config["repo"])
        if paths is None:
            paths = previous.get(config["name"], [])
        sources[config["name"]] = sorted(set(paths))
    # ASCII escapes keep a path that is not UTF-8 (a lone surrogate after
    # os.fsdecode) writable and readable back unchanged (ruling 51 f).
    atomic_write_text(path, json.dumps({"generated_at": utc_now(), "sources": sources}, indent=1) + "\n")


def _iter_federation_entries() -> "Any":
    """Yield (source_name, entry) for every registered, valid sources.d config.

    A broken or missing source must never take canonical reindex down with
    it: derived federation content is optional, best-effort, and always
    layered on top of an unaffected canonical index.
    """
    config_root = config_home()
    for name in list_source_configs(config_root):
        try:
            config = load_source_config(config_root, name)
            entries = collect_source_entries(config)
        except (FederationConfigError, OSError) as exc:
            print(f"memoryctl: skipping federation source {name!r}: {exc}", file=sys.stderr)
            continue
        for entry in entries:
            yield name, entry


def reindex(root: Path) -> int:
    cache = root / ".cache"
    _secure_dir(cache)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="index-", suffix=".sqlite3", dir=cache
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    connection: sqlite3.Connection | None = None
    count = 0
    try:
        connection = sqlite3.connect(temporary)
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE records (
                rowid INTEGER PRIMARY KEY,
                record_id TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL,
                kind TEXT NOT NULL,
                scope TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                body TEXT NOT NULL,
                identity_text TEXT NOT NULL,
                content_text TEXT NOT NULL,
                evidence_text TEXT NOT NULL,
                semantic_content_hash TEXT NOT NULL,
                search_text TEXT NOT NULL,
                node TEXT NOT NULL
            );
            CREATE INDEX records_by_node ON records(node);
            CREATE VIRTUAL TABLE records_fts USING fts5(
                record_id UNINDEXED,
                identity,
                content,
                evidence,
                tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TABLE decision_links (
                experience_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                effect TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (experience_id, decision_id)
            );
            CREATE INDEX decision_links_by_decision
                ON decision_links(decision_id, effect, updated_at DESC);
            """
        )
        active = []
        for path in sorted((root / "records").glob("*/*.md")):
            metadata, body = decode_record(path)
            if metadata.get("status") != "active":
                continue
            active.append((path, metadata, body))
        routing = compute_routing(str(metadata.get("scope", "")) for _, metadata, _ in active)
        for path, metadata, body in active:
            _index_insert_record(connection, root, path, metadata, body, routing)
            count += 1
        for source_name, entry in _iter_federation_entries():
            origin = entry["metadata"].get("origin", {})
            path_label = f"federation/{source_name}/{origin.get('path') or origin.get('anchor', '')}"
            _index_insert_federation_entry(
                connection, path_label, entry["metadata"], entry["body"], routing
            )
        generated_at = utc_now()
        for key, value in (
            ("index_schema_version", str(INDEX_SCHEMA_VERSION)),
            ("record_schema_version", str(SCHEMA_VERSION)),
            ("generated_at", generated_at),
            ("source_fingerprint", _record_fingerprint(root)),
            ("record_count", str(count)),
            ("routing_json", _routing_for_storage(routing)),
        ):
            connection.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (key, value))
        connection.commit()
        connection.execute("PRAGMA optimize")
        connection.close()
        connection = None
        temporary.chmod(0o600)
        os.replace(temporary, _index_path(root))
        _index_dirty_path(root).unlink(missing_ok=True)
        (root / ".cache" / "index.json").unlink(missing_ok=True)
        # Derived files the hooks read; a failed write keeps the old file.
        try:
            write_fact_entities(root, [metadata for _, metadata, _ in active])
        except OSError:
            pass
        try:
            write_triggers(root, [(metadata, body) for _, metadata, body in active], routing)
        except OSError:
            pass
        try:
            write_newer_decisions(root)
        except Exception:  # a derived hint never stops reindex
            pass
        # Listing worktrees must never stop reindex or sync (ruling 51 f).
        try:
            write_source_worktrees(root)
        except Exception:
            pass
        try:
            write_project_paths(root, routing)
        except Exception:
            pass
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)
        raise MemoryError(f"cannot build SQLite FTS index: {exc}") from exc
    return count


def _open_index(root: Path, *, allow_reindex: bool = True) -> sqlite3.Connection:
    """Open the lexical index.

    Hooks pass allow_reindex=False: a full rebuild on the prompt path costs
    time proportional to the whole corpus, which is exactly the work a
    transport layer must not do. They serve the last-known-good snapshot and
    correct it with a bounded overlay instead, or inject nothing.
    """
    delta = _read_index_delta(root)
    has_snapshot = _index_path(root).is_file()
    if not allow_reindex:
        if not has_snapshot:
            raise LexicalIndexUnavailable(
                "no lexical snapshot to serve", code="no-snapshot"
            )
        if delta is not None and not delta["bounded"]:
            raise LexicalIndexUnavailable(
                "lexical delta is too large to replay on the prompt path",
                code="delta-unbounded",
            )
    elif delta is not None or not has_snapshot:
        reindex(root)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(_index_path(root))
        connection.row_factory = sqlite3.Row
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        if (
            meta.get("index_schema_version") != str(INDEX_SCHEMA_VERSION)
            or meta.get("record_schema_version") != str(SCHEMA_VERSION)
        ):
            raise sqlite3.DatabaseError("stale index schema")
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        if not allow_reindex:
            raise LexicalIndexUnavailable(
                "lexical snapshot is unusable without an explicit reindex",
                code="snapshot-unusable",
            ) from exc
        reindex(root)
        connection = sqlite3.connect(_index_path(root))
        connection.row_factory = sqlite3.Row
        return connection


def _matches_terms(item: dict[str, Any], terms: set[str]) -> bool:
    if not terms:
        return False
    return any(
        term in item["identity_text"]
        or term in item["content_text"]
        or term in item["evidence_text"]
        for term in terms
    )


def _index_row(row: sqlite3.Row, *, reason: str | None = None) -> dict[str, Any]:
    item = {
        "path": str(row["path"]),
        "metadata": json.loads(str(row["metadata_json"])),
        "body": str(row["body"]),
        "identity_text": str(row["identity_text"]),
        "content_text": str(row["content_text"]),
        "evidence_text": str(row["evidence_text"]),
        "semantic_content_hash": str(row["semantic_content_hash"]),
        "search_text": str(row["search_text"]),
    }
    if "node" in row.keys():
        item["node"] = str(row["node"])
    if reason:
        item["index_reason"] = reason
    return item
