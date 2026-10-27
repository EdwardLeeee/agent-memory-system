"""memoryctl search: the history layer, read on demand (spec 記憶路由樹 v1.1, 4.4).

Hooks inject only what a turn's routed nodes hold. Everything else stays
reachable here: any active record (including federation entries and nodes
the router did not open this turn), idle session capsules, and with
--include-archive the superseded versions and the event log. Nothing here is
injected into a model; it prints to the terminal. Transcripts are not read.
"""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import re
from typing import Any

from . import core
from .lexical_index import compute_routing, load_index_routing, node_for

DEFAULT_LIMIT = 10
EXCERPT_CHARS = 160


def _excerpt(metadata: dict[str, Any], body: str) -> str:
    """The value line the record would show in a pack, shortened."""
    try:
        _, block = core._record_block(metadata, body, False)
        value = next((line[len("- value: "):] for line in block if line.startswith("- value: ")), "")
    except Exception:
        value = ""
    value = " ".join(value.split())
    return value if len(value) <= EXCERPT_CHARS else value[: EXCERPT_CHARS - 1] + "…"


def _since_ok(moment: str, since: date | None) -> bool:
    if since is None:
        return True
    try:
        return date.fromisoformat(str(moment)[:10]) >= since
    except ValueError:
        return False


def _project_ok(metadata: dict[str, Any], node: str, project: str | None) -> bool:
    if not project:
        return True
    tag = f"project:{project}"
    affinity = [str(item) for item in (metadata.get("affinity") or [])]
    return node == f"project/{project}" or node.startswith(f"project/{project}/") or metadata.get("scope") == tag or tag in affinity


def _visible(metadata: dict[str, Any], machine: dict[str, Any]) -> bool:
    visibility, _ = core._effective_visibility_and_affinity(metadata)
    return core._visibility_reason(visibility, machine) is not None


# Two query words found side by side in a record count once more, as a
# content word does; a record of the project the search is run from gains
# what a project's own record gains in the flat pack (spec 實際使用回饋改善 F9).
PHRASE_BONUS = 20
CWD_PROJECT_BONUS = 20
OTHER_PROJECT_PENALTY = 30
# A word ends at anything but a letter or digit: an id's underscores part its
# words (claude_idle_reaper holds reaper). A few English endings still match
# (chain: chains, chained; key: keyed), none that make another word, and a
# version number (python: python3); a word of two letters takes none (us is
# not user, ai not aid; retrieval review).
_WORD = "a-z0-9"
_ENDINGS = r"(?:s|es|ed|d|ing|er|ers|\d+)?"


def _ending(word: str) -> str:
    return _ENDINGS if word[-1:].isalpha() and len(word) > 2 else ""


class _Query:
    """A query's words and how each matches: a Chinese bigram anywhere; an
    English word or a number only whole, so play matches neither playwright
    nor display (F9). Each two words next to each other in the query are
    also a phrase."""

    def __init__(self, text: str | None) -> None:
        self.terms = core._query_terms(text) if text else set()
        self.patterns = {
            term: None if _CJK_RE.search(term) else re.compile(
                rf"(?<![{_WORD}]){re.escape(term)}{_ending(term)}(?![{_WORD}])")
            for term in self.terms
        }
        words = [word for word in re.findall(r"[a-z0-9_.-]+", core._normalize_search_text(text or "")) if word in self.terms]
        self.phrases = [
            re.compile(rf"(?<![{_WORD}]){re.escape(first)}[\s_-]+{re.escape(second)}{_ending(second)}(?![{_WORD}])")
            for first, second in zip(words, words[1:])
        ]

    def __bool__(self) -> bool:
        return bool(self.terms)

    def hits(self, text: str) -> int:
        return sum(1 for term, pattern in self.patterns.items() if (term in text if pattern is None else pattern.search(text)))

    def phrase_hits(self, text: str) -> int:
        return sum(1 for pattern in self.phrases if pattern.search(text))


_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _score(query: _Query, identity: str, content: str, evidence: str) -> int:
    identity_hits = query.hits(identity)
    content_hits = query.hits(content)
    if identity_hits == 0 and content_hits == 0:
        return 0
    evidence_hits = query.hits(evidence)
    phrases = query.phrase_hits(f"{identity}\n{content}")
    return identity_hits * 24 + content_hits * 20 + evidence_hits * 4 + phrases * PHRASE_BONUS


def _routing(connection: Any) -> dict[str, Any]:
    return load_index_routing(connection) or compute_routing()


def search(
    root: Path,
    query: str,
    *,
    kind: str | None = None,
    project: str | None = None,
    since: date | None = None,
    include_archive: bool = False,
    limit: int = DEFAULT_LIMIT,
    cwd: Path | None = None,
) -> list[dict[str, Any]]:
    """Ledger hits for a query, best first.

    Scored the way a node scores (identity 24, content 20, evidence 4 per
    matching query word), but over everything, with no floor beyond one
    word in the identity or the content; two query words side by side in a
    record add 20, and a record of the project `cwd` is in adds 50. A
    record on another machine is not shown, as in every pack.
    """
    terms = _Query(query)
    if not terms:
        return []
    machine = core.load_machine_config()
    hits: list[dict[str, Any]] = []
    connection = core._open_index(root, allow_reindex=True)
    try:
        routing = _routing(connection)
        here = _cwd_projects(root, cwd)
        for row in connection.execute("SELECT * FROM records"):
            item = core._index_row(row)
            metadata = item["metadata"]
            node = str(item.get("node") or "")
            if kind and metadata.get("kind") != kind:
                continue
            if not _visible(metadata, machine) or not _project_ok(metadata, node, project):
                continue
            if not _since_ok(metadata.get("updated_at") or metadata.get("created_at") or "", since):
                continue
            score = _score(terms, item["identity_text"], item["content_text"], item["evidence_text"])
            if score and here:
                # As routing does: a record scoped to the cwd's project first;
                # one scoped to another project behind one that belongs to no
                # project, never gone (retrieval review 4: one word, 20 points,
                # minus 30 was dropped). Where a record was learned (its
                # origin), its node or the project its id names is not where it
                # applies, either way (retrieval review 5 and re-check), and a
                # project named with --project is not another.
                scope = str(metadata.get("scope") or "")
                if scope.startswith("project:") and scope[len("project:"):] in here:
                    score += CWD_PROJECT_BONUS
                elif project is None and _scoped_elsewhere(metadata, here):
                    score = max(1, score - OTHER_PROJECT_PENALTY)
            if score > 0:
                hit = {
                    "id": str(metadata["id"]), "kind": metadata.get("kind"), "where": "active", "node": node,
                    "updated_at": str(metadata.get("updated_at") or ""), "score": score,
                    "excerpt": _excerpt(metadata, item["body"]),
                }
                if core.no_longer_true(metadata):
                    hit["note"] = f"已於 {metadata['valid_until']} 不再成立"
                hits.append(hit)
    finally:
        connection.close()

    if not kind or kind == "task":
        hits.extend(_capsule_hits(root, terms, since))
    if include_archive:
        hits.extend(_archive_hits(root, terms, kind, project, since, machine, routing))
        if not project:
            hits.extend(_event_hits(root, terms, kind, since))
    hits.sort(key=lambda hit: (-hit["score"], _negated(hit["updated_at"]), hit["id"]))
    return hits[: max(1, limit)]


def _cwd_projects(root: Path, cwd: Path | None) -> set[str]:
    if cwd is None:
        return set()
    from .pretool import cwd_projects

    try:
        return set(cwd_projects(str(cwd), root))
    except Exception:
        return set()


def _scoped_elsewhere(metadata: dict[str, Any], here: set[str]) -> bool:
    scope = str(metadata.get("scope") or "")
    return scope.startswith("project:") and scope[len("project:"):] not in here


def _negated(moment: str) -> list[int]:
    return [-ord(char) for char in moment]


def _capsule_hits(root: Path, terms: _Query, since: date | None) -> list[dict[str, Any]]:
    """Idle capsules: state not active, or untouched for 30 days (spec 8.7)."""
    hits = []
    for path, text, fields in core._capsule_candidates(root):
        state = fields.get("state", "active")
        updated = fields.get("updated_at", "")
        if state == "active" and not core._capsule_is_stale(updated):
            continue
        if not _since_ok(updated, since):
            continue
        normalized = core._normalize_search_text(text)
        score = _score(terms, core._normalize_search_text(fields.get("active_task", "")), normalized, "")
        if score:
            folder = path.parent.name
            hits.append({
                "id": f"capsule:{folder}", "kind": "capsule", "where": f"capsule ({state})", "node": "history",
                "updated_at": updated, "score": score,
                "excerpt": " ".join(fields.get("active_task", "").split())[:EXCERPT_CHARS],
            })
    return hits


def _archive_hits(
    root: Path, terms: _Query, kind: str | None, project: str | None, since: date | None,
    machine: dict[str, Any], routing: dict[str, Any],
) -> list[dict[str, Any]]:
    from .lexical_index import _search_fields

    hits = []
    for path in sorted((root / "archive").glob("*/*/*.md")):
        try:
            metadata, body = core.decode_record(path)
        except Exception:
            continue
        if kind and metadata.get("kind") != kind:
            continue
        node = node_for(metadata, routing)
        if not _visible(metadata, machine) or not _project_ok(metadata, node, project):
            continue
        if not _since_ok(metadata.get("updated_at") or "", since):
            continue
        identity, content, evidence = _search_fields(metadata, body)
        score = _score(terms, identity, content, evidence)
        if score:
            hits.append({
                "id": str(metadata.get("id")), "kind": metadata.get("kind"),
                "where": f"archive v{metadata.get('version', '?')}", "node": node,
                "updated_at": str(metadata.get("updated_at") or ""), "score": score,
                "excerpt": _excerpt(metadata, body), "path": str(path.relative_to(root)),
            })
    return hits


def _event_hits(root: Path, terms: _Query, kind: str | None, since: date | None) -> list[dict[str, Any]]:
    hits = []
    for path in sorted((root / "events").glob("*/*/*.json")):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(event, dict) or (kind and event.get("record_kind") != kind):
            continue
        if not _since_ok(event.get("timestamp") or "", since):
            continue
        identity = core._normalize_search_text(f"{event.get('record_id', '')} {event.get('action', '')}")
        content = core._normalize_search_text(str(event.get("change_reason") or ""))
        score = _score(terms, identity, content, "")
        if score:
            hits.append({
                "id": str(event.get("record_id", "")), "kind": event.get("record_kind"),
                "where": f"event {event.get('action', '')}", "node": "history",
                "updated_at": str(event.get("timestamp") or ""), "score": score,
                "excerpt": str(event.get("change_reason") or event.get("event_id") or "")[:EXCERPT_CHARS],
            })
    return hits


def show(root: Path, record_id: str, *, include_archive: bool = False) -> str:
    """One record in full: the active version, and with --include-archive its
    superseded versions and events. Raises MemoryError when nothing matches."""
    machine = core.load_machine_config()
    lines: list[str] = []
    paths = sorted((root / "records").glob(f"*/{record_id}.md"))
    connection = core._open_index(root, allow_reindex=True)
    try:
        routing = _routing(connection)
        if not paths:
            row = connection.execute("SELECT * FROM records WHERE record_id = ?", (record_id,)).fetchone()
            if row is not None:
                item = core._index_row(row)
                lines += _full(item["metadata"], item["body"], str(item.get("node") or ""), "active (derived from a registered source)")
    finally:
        connection.close()
    for path in paths:
        metadata, body = core.decode_record(path)
        if metadata.get("status") != "active":
            continue
        if not _visible(metadata, machine):
            lines.append(f"{record_id} belongs to another machine ({metadata.get('scope')}).")
            continue
        lines += _full(metadata, body, node_for(metadata, routing), "active")
    if include_archive:
        for path in sorted((root / "archive").glob(f"*/{record_id}/*.md")):
            try:
                metadata, body = core.decode_record(path)
            except Exception:
                continue
            lines += ["", f"--- archived: {path.relative_to(root)}", *_full(metadata, body, node_for(metadata, routing), f"archive v{metadata.get('version', '?')}")]
        events = []
        for path in sorted((root / "events").glob("*/*/*.json")):
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(event, dict) and event.get("record_id") == record_id:
                events.append(event)
        if events:
            lines += ["", "--- events"]
            for event in sorted(events, key=lambda e: str(e.get("timestamp", ""))):
                reason = f" ({event['change_reason']})" if event.get("change_reason") else ""
                lines.append(f"- {event.get('timestamp')} {event.get('action')} {event.get('event_id')}{reason}")
    if not lines:
        raise core.MemoryError(f"no record found: {record_id}")
    return "\n".join(lines).rstrip() + "\n"


def _full(metadata: dict[str, Any], body: str, node: str, where: str) -> list[str]:
    fields = ("kind", "scope", "status", "version", "priority", "source", "updated_at", "last_verified", "expires_at",
              "valid_from", "valid_until")
    head = [f"# {metadata.get('id')}  [{where}; node={node or '-'}]"]
    head += [f"- {key}: {metadata.get(key)}" for key in fields if key in metadata]
    if metadata.get("tags"):
        head.append("- tags: " + ", ".join(str(tag) for tag in metadata["tags"]))
    learned = metadata.get("learned_in")
    if isinstance(learned, dict) and learned.get("session_id"):
        head.append(f"- learned_in: session {learned['session_id']}, {learned.get('at', '')}"
                    f"（對話：memoryctl search --id {metadata.get('id')} --context）")
    text = re.sub(r"^# \S+\n+", "", body.strip(), count=1)
    return [*head, "", text]


def as_of(root: Path, day: date, query: str | None = None) -> list[dict[str, Any]]:
    """The facts that held on `day` (spec 論文借鏡改善 A3), each by the newest
    version whose window covers it. A version's window is its valid_from and
    valid_until; without either, it is when the ledger held that version (from
    its write to the next version's write), and without valid_until, it ends
    where the next version was written."""
    from .storage import _day

    terms = _Query(query)
    machine = core.load_machine_config()
    versions: dict[str, list[tuple[dict[str, Any], str]]] = {}
    paths = [*(root / "records" / "fact").glob("*.md"), *(root / "archive" / "fact").glob("*/*.md")]
    for path in paths:
        try:
            metadata, body = core.decode_record(path)
        except Exception:
            continue
        if metadata.get("kind") == "fact" and _visible(metadata, machine):
            versions.setdefault(str(metadata.get("id")), []).append((metadata, body))
    hits = []
    for record_id, found in versions.items():
        found.sort(key=lambda pair: int(pair[0].get("version", 1) or 1))
        chosen = None
        for index, (metadata, body) in enumerate(found):
            written = _day(str(metadata.get("updated_at") or "")[:10])
            replaced = _day(str(found[index + 1][0].get("updated_at") or "")[:10]) if index + 1 < len(found) else None
            start = _day(metadata.get("valid_from"))
            end = _day(metadata.get("valid_until"))
            if start is None and end is None:
                start = written  # no world window given: from when the ledger held it
            if end is None and replaced is not None:
                end = replaced - timedelta(days=1)
            if (start is None or start <= day) and (end is None or day <= end):
                chosen = (metadata, body, start, end)
        if chosen is None:
            continue
        metadata, body, start, end = chosen
        if terms and not _score(terms, f"{record_id} {' '.join(metadata.get('tags') or [])}", body, ""):
            continue
        hits.append({
            "id": record_id, "version": metadata.get("version", 1), "from": start.isoformat() if start else "",
            "until": end.isoformat() if end else "", "excerpt": _excerpt(metadata, body),
        })
    return sorted(hits, key=lambda hit: hit["id"])


def format_as_of(hits: list[dict[str, Any]], day: date) -> str:
    if not hits:
        return f"{day.isoformat()} 沒有成立的事實。\n"
    lines = [f"{day.isoformat()} 成立的事實："]
    for hit in hits:
        window = f"{hit['from'] or '…'} ～ {hit['until'] or '至今'}"
        lines.append(f"- {hit['id']} v{hit['version']}（{window}）")
        if hit["excerpt"]:
            lines.append(f"   {hit['excerpt']}")
    return "\n".join(lines) + "\n"


def format_hits(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "No match.\n"
    lines = []
    for number, hit in enumerate(hits, start=1):
        moment = hit["updated_at"][:10] if hit["updated_at"] else "-"
        lines.append(f"{number}. {hit['id']}  [{hit['kind']}; {hit['where']}; node={hit['node'] or '-'}; {moment}; score {hit['score']}]")
        if hit.get("note"):
            lines.append(f"   ({hit['note']})")
        if hit["excerpt"]:
            lines.append(f"   {hit['excerpt']}")
    lines.append("")
    lines.append("Full record: memoryctl search --id <id>  (add --include-archive for older versions and events)")
    return "\n".join(lines) + "\n"
