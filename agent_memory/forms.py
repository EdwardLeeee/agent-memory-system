"""What each write takes (spec 實際使用回饋改善 F10).

A record of each kind, a supersede, and a checkpoint name the fields they
need here. A write missing several is told all of them at once, with the
skeleton `memoryctl template <kind>` prints to copy from, instead of one
field per attempt.
"""

from __future__ import annotations

import json
from typing import Any

COMMON_FIELDS = ("scope", "source", "evidence")
STRING_FIELDS: dict[str, tuple[str, ...]] = {
    "preference": ("value",),
    "fact": ("value",),
    "task": ("value",),
    "decision": ("problem", "decision", "rationale"),
    "experience": ("situation", "expectation", "action", "observed_outcome", "applicability"),
    "failure": ("symptom", "attempt", "root_cause", "verified_fix", "retry_when"),
    "lesson": ("finding", "rule", "applies_to"),
}
LIST_FIELDS: dict[str, tuple[str, ...]] = {
    "decision": ("constraints", "alternatives_rejected", "consequences", "revisit_when"),
}
SUPERSEDE_FIELDS = ("change_driver", "change_reason")
CHECKPOINT_FIELDS = ("state", "active_task", "paused_task", "resume_point", "next_action", "validation", "rollback")

_COMMON = {
    "scope": "global | project:<name> | workspace:<name> | machine:<uuid>",
    "source": "user-stated | locally-verified | inference | migrated",
    "evidence": "where this was seen or checked",
    "tags": ["<word>"],
}
_KINDS: dict[str, dict[str, Any]] = {
    "preference": {"value": "the rule, as the user stated it", "priority": "critical | high | normal | low"},
    "fact": {"value": "what holds, checked", "expires_at": "YYYY-MM-DD, or leave out"},
    "task": {"value": "what is to be done"},
    "decision": {
        "problem": "what had to be decided", "decision": "what was decided", "rationale": "why",
        "constraints": ["what it must keep to"], "alternatives_rejected": ["what else was weighed, and why not"],
        "consequences": ["what follows from it"], "revisit_when": ["what would reopen it"],
    },
    "experience": {
        "situation": "what was going on", "expectation": "what was expected", "action": "what was done",
        "observed_outcome": "what happened", "applicability": "where it applies",
        "decision_links": [{"id": "decision.<project>.<slug>", "effect": "supports | contradicts | narrows | inconclusive"}],
    },
    "failure": {
        "symptom": "what went wrong", "attempt": "what was tried", "root_cause": "why",
        "verified_fix": "what fixed it, checked", "retry_when": "when to think of it again",
        "triggers": ["<regex for a shell command>, or leave out"],
        "outbound_triggers": ["<regex for what is sent>, or leave out"],
    },
    "lesson": {"finding": "what was found", "rule": "the rule it gives", "applies_to": "all | <subject>"},
}
_SUPERSEDE = {
    "change_driver": "user-correction | new-evidence | goal-change | constraint-change | expired",
    "change_reason": "why it changed, with the date",
}
_CHECKPOINT = {
    "state": "active | idle | done",
    "active_task": "the task, by name", "paused_task": "what waits, or none",
    "resume_point": "events, 「；」 between them; append each new one at the end (the oldest go first past the limit)",
    "next_action": "the next step", "validation": "how it was checked", "rollback": "how to undo it",
    "tasks": [{"id": "PR-3", "title": "up to 80 characters", "status": "todo | doing | waiting | done",
               "waiting_on": "who or what, or leave out"}],
}
TEMPLATE_KINDS = (*sorted(_KINDS), "supersede", "checkpoint")


def template(kind: str) -> dict[str, Any]:
    """The skeleton of a write: a record of `kind`, a supersede (the record's
    own fields plus why it changed), or a checkpoint."""
    if kind == "checkpoint":
        return dict(_CHECKPOINT)
    if kind == "supersede":
        return {"<the record's own fields>": "see memoryctl template <its kind>", **_SUPERSEDE}
    if kind not in _KINDS:
        raise KeyError(kind)
    return {"id": f"{kind}.<project or topic>.<slug>", **_COMMON, **_KINDS[kind]}


def _blank(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def missing(kind: str, payload: dict[str, Any], *, supersede: bool = False) -> list[str]:
    """Every field the write needs and lacks, in the skeleton's order."""
    if kind == "checkpoint":
        return [field for field in CHECKPOINT_FIELDS if _blank(payload.get(field))]
    gaps = [] if supersede or payload.get("id") else ["id"]
    gaps += [field for field in (*COMMON_FIELDS, *STRING_FIELDS.get(kind, ())) if _blank(payload.get(field))]
    gaps += [
        field for field in LIST_FIELDS.get(kind, ())
        if not isinstance(payload.get(field), list) or not payload[field]
    ]
    if kind == "experience" and not isinstance(payload.get("decision_links"), list):
        gaps.append("decision_links")
    if supersede:
        gaps += [field for field in SUPERSEDE_FIELDS if _blank(payload.get(field))]
    return gaps


def missing_message(kind: str, gaps: list[str], *, supersede: bool = False) -> str:
    plural = "s" if len(gaps) > 1 else ""
    shown = "supersede" if supersede else kind
    skeleton = json.dumps(template(shown if shown in TEMPLATE_KINDS else kind), ensure_ascii=False, indent=2)
    return (f"missing required {kind} field{plural}: {', '.join(gaps)}\n"
            f"a skeleton to copy from (memoryctl template {shown}):\n{skeleton}")
