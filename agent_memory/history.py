"""memoryctl search: the history layer, read on demand (spec 記憶路由樹 v1.1, 4.4).

Hooks inject only what a turn's routed nodes hold. Everything else stays
reachable here: any active record (including federation entries and nodes
the router did not open this turn), idle session capsules, and with
--include-archive the superseded versions and the event log. Nothing here is
injected into a model; it prints to the terminal. Transcripts are not read.
"""

from __future__ import annotations

from datetime import date
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


def _score(terms: set[str], identity: str, content: str, evidence: str) -> int:
    identity_hits = sum(1 for term in terms if term in identity)
    content_hits = sum(1 for term in terms if term in content)
    if identity_hits == 0 and content_hits == 0:
        return 0
    evidence_hits = sum(1 for term in terms if term in evidence)
    return identity_hits * 24 + content_hits * 20 + evidence_hits * 4


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
) -> list[dict[str, Any]]:
    """Ledger hits for a query, best first.

    Scored the way a node scores (identity 24, content 20, evidence 4 per
    matching query word), but over everything, with no floor beyond one
    word in the identity or the content. A record on another machine is
    not shown, as in every pack.
    """
    terms = core._query_terms(query)
    if not terms:
        return []
    machine = core.load_machine_config()
    hits: list[dict[str, Any]] = []
    connection = core._open_index(root, allow_reindex=True)
    try:
        routing = _routing(connection)
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
            if score:
                hits.append({
                    "id": str(metadata["id"]), "kind": metadata.get("kind"), "where": "active", "node": node,
                    "updated_at": str(metadata.get("updated_at") or ""), "score": score,
                    "excerpt": _excerpt(metadata, item["body"]),
                })
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


def _negated(moment: str) -> list[int]:
    return [-ord(char) for char in moment]


def _capsule_hits(root: Path, terms: set[str], since: date | None) -> list[dict[str, Any]]:
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
    root: Path, terms: set[str], kind: str | None, project: str | None, since: date | None,
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


def _event_hits(root: Path, terms: set[str], kind: str | None, since: date | None) -> list[dict[str, Any]]:
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
    fields = ("kind", "scope", "status", "version", "priority", "source", "updated_at", "last_verified", "expires_at")
    head = [f"# {metadata.get('id')}  [{where}; node={node or '-'}]"]
    head += [f"- {key}: {metadata.get(key)}" for key in fields if key in metadata]
    if metadata.get("tags"):
        head.append("- tags: " + ", ".join(str(tag) for tag in metadata["tags"]))
    text = re.sub(r"^# \S+\n+", "", body.strip(), count=1)
    return [*head, "", text]


def format_hits(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "No match.\n"
    lines = []
    for number, hit in enumerate(hits, start=1):
        moment = hit["updated_at"][:10] if hit["updated_at"] else "-"
        lines.append(f"{number}. {hit['id']}  [{hit['kind']}; {hit['where']}; node={hit['node'] or '-'}; {moment}; score {hit['score']}]")
        if hit["excerpt"]:
            lines.append(f"   {hit['excerpt']}")
    lines.append("")
    lines.append("Full record: memoryctl search --id <id>  (add --include-archive for older versions and events)")
    return "\n".join(lines) + "\n"
