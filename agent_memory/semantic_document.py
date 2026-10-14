from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from typing import Any


SEMANTIC_DOCUMENT_SCHEMA_VERSION = 2
_EXCLUDED_SECTIONS = frozenset({"decision links", "evidence"})
_CONTRACT = {
    "schema_version": SEMANTIC_DOCUMENT_SCHEMA_VERSION,
    "title": "excluded",
    "metadata_identity": "excluded",
    "section_labels": "preserved",
    "excluded_sections": sorted(_EXCLUDED_SECTIONS),
    "normalization": "unicode-nfkc-trim-lines-collapse-blank-lines",
}
SEMANTIC_DOCUMENT_CONTRACT_SHA256 = hashlib.sha256(
    json.dumps(_CONTRACT, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


@dataclass(frozen=True)
class SemanticDocument:
    record_id: str
    text: str
    content_hash: str


def _normalized_lines(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text)
    lines = [line.strip() for line in normalized.splitlines()]
    compact: list[str] = []
    for line in lines:
        if line or (compact and compact[-1]):
            compact.append(line)
    while compact and not compact[-1]:
        compact.pop()
    return compact


def _fallback_content(body: str) -> str:
    lines = _normalized_lines(body)
    if lines and re.fullmatch(r"#\s+.+", lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def build_semantic_document(
    metadata: dict[str, Any], body: str
) -> SemanticDocument:
    """Project one canonical record into the model-facing passage contract."""

    record_id = str(metadata.get("id", "")).strip()
    if not record_id:
        raise ValueError("semantic document requires a record id")

    sections: list[tuple[str, list[str]]] = []
    heading: str | None = None
    content: list[str] = []
    for line in unicodedata.normalize("NFKC", body).splitlines():
        match = re.fullmatch(r"##\s+(.+?)\s*", line.strip())
        if match:
            if heading is not None:
                sections.append((heading, content))
            heading = match.group(1).strip()
            content = []
        elif heading is not None:
            content.append(line)
    if heading is not None:
        sections.append((heading, content))

    rendered: list[str] = []
    for section_heading, section_lines in sections:
        if section_heading.casefold() in _EXCLUDED_SECTIONS:
            continue
        normalized_content = "\n".join(_normalized_lines("\n".join(section_lines)))
        if normalized_content:
            rendered.append(f"{section_heading}\n{normalized_content}")

    text = "\n\n".join(rendered).strip() if sections else _fallback_content(body)
    if not text:
        raise ValueError(f"semantic document has no retrievable content: {record_id}")
    return SemanticDocument(
        record_id=record_id,
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
