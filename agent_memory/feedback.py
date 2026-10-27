"""Ranking feedback from the user's ratings and the agents' use of what
they were given (spec 論文借鏡改善 A1, 實際使用回饋改善 F12).

runtime/feedback.json is derived and machine-local, like the ratings it
comes from: per record, how often the user rated a pack holding it useful,
useless, or harmful (last RATING_WEEKS weeks), and how often it was
injected and used (last USAGE_WEEKS weekly doctor reports; "used" is judged
from what the agent wrote, see usage.py). The routed UserPromptSubmit path
reads it to reorder candidates that already passed a node's relevance
floor; it never lets a record in. PreToolUse ignores it. A missing or
broken file means no feedback.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any

from .storage import atomic_write_text
from .usage import CONTENT_TRUSTED, DETECTOR, in_memory_repo

FEEDBACK_NAME = "feedback.json"
RATING_WEEKS = 8
USAGE_WEEKS = 4
DOWN = 0.5
UP = 1.15
_JUDGED = ("useful", "useless", "harmful")


def feedback_path(config_root: Path) -> Path:
    return config_root / "runtime" / FEEDBACK_NAME


def load(config_root: Path) -> dict[str, dict[str, Any]] | None:
    try:
        data = json.loads(feedback_path(config_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    records = data.get("records") if isinstance(data, dict) else None
    return records if isinstance(records, dict) else None


def _blank() -> dict[str, Any]:
    return {"useful": 0, "useless": 0, "harmful": 0, "last_rated": "", "injected": 0, "used": 0}


def _used(counts: dict[str, Any]) -> int:
    """A week's use of one record, in any shape a report kept it: the ids
    the agent named (F12; content matches too only once CONTENT_TRUSTED, A1
    ruling), the citations weeks before F12 counted, or a plain used."""
    if "explicit" in counts or "content" in counts:
        return int(counts.get("explicit") or 0) + (int(counts.get("content") or 0) if CONTENT_TRUSTED else 0)
    if "used" in counts:
        return int(counts.get("used") or 0)
    return int(counts.get("cited") or 0)


def _week_counts(per_record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A report's per-record use as dicts: a report since F12 keeps each as
    the list replay counts, [injected, used by id, used by content]; one
    before kept a dict. Reading only dicts left every week but this one out
    (review 2, item 5)."""
    week: dict[str, dict[str, Any]] = {}
    for record_id, counts in per_record.items():
        if isinstance(counts, list) and len(counts) == 3 and all(isinstance(n, int) for n in counts):
            week[str(record_id)] = {"injected": counts[0], "explicit": counts[1], "content": counts[2]}
        elif isinstance(counts, dict):
            week[str(record_id)] = counts
    return week


def _report_usage(report_dir: Path | None, now: datetime, skip_date: str | None, limit: int) -> list[dict[str, Any]]:
    """Per-record use from the latest weekly reports: the whole-session
    numbers (F12), or the citations of a report from before them."""
    if report_dir is None or limit <= 0 or not report_dir.is_dir():
        return []
    found = []
    for path in sorted(report_dir.glob("????-??-??.json"), reverse=True):
        if len(found) >= limit:
            break
        if path.stem == skip_date:
            continue
        try:
            when = datetime.fromisoformat(path.stem).date()
        except ValueError:
            continue
        if when < (now - timedelta(weeks=USAGE_WEEKS)).date():
            break
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        signals = (report.get("signals") or {}) if isinstance(report, dict) else {}
        per_record = ((signals.get("sessions") or {}).get("usage") or {}).get("per_record")
        if not isinstance(per_record, dict):
            per_record = (signals.get("citation") or {}).get("per_record")
        if isinstance(per_record, dict):
            found.append(_week_counts(per_record))
    return found


USAGE_NAME = "rating_usage.json"


def _current(entry: dict[str, Any]) -> bool:
    """Whether a rating's named ids were judged by the rule in use."""
    return entry.get("detector") == DETECTOR and "explicit_ids" in entry


def _legacy_usage(config_root: Path, entries: list[dict[str, Any]], projects_dir: Path | None, root: Path | None) -> dict[str, Any]:
    """The named and content-matched ids of ratings judged by another rule
    (or saved before ids were stored), worked out again from each rated
    turn's transcript by the rule in use, streamed, and kept in
    runtime/rating_usage.json under the rule's version, so a transcript is
    read once a version. A transcript that is gone gives `found: false`."""
    from . import learned, rating
    from .storage import memory_home
    from .usage import FeatureFrequency, UsageDetector

    path = config_root / "runtime" / USAGE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    known = data.get("messages") if isinstance(data, dict) and data.get("detector") == DETECTOR else None
    known = known if isinstance(known, dict) else {}
    wanted: dict[str, set[str]] = {}
    for entry in entries:
        key = str(entry.get("key") or "")
        if key and not _current(entry) and key not in known:
            wanted.setdefault(str(entry.get("session_id") or ""), set()).add(key)
    if not wanted:
        return known
    detector = UsageDetector(FeatureFrequency.from_index(root or memory_home()))
    for session, keys in wanted.items():
        located = learned.find_transcript(session, projects_dir or rating.default_projects_dir(), Path("/nonexistent")) if session else None
        found: dict[str, dict[str, Any]] = {}
        if located is not None:
            for message in rating.read_transcript(located[0], detector, root):
                key = rating.message_key(message)
                if key in keys:
                    used = message["used"]
                    found[key] = {"explicit": sorted(i for i, (how, _) in used.items() if how == "id"),
                                  "content": sorted(i for i, (how, _) in used.items() if how != "id"), "found": True}
        for key in keys:
            known[key] = found.get(key, {"explicit": [], "content": [], "found": False})
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"detector": DETECTOR, "messages": known}, ensure_ascii=False, sort_keys=True) + "\n")
    return known


def build(
    config_root: Path,
    now: datetime,
    report_dir: Path | None = None,
    this_week: dict[str, Any] | None = None,
    projects_dir: Path | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """A rating answers for the whole pack, so useful and harmful go to the
    memories the agent named (A1 ruling: not content matches, until
    CONTENT_TRUSTED), or to the only memory when the pack held one;
    otherwise they are counted as not attributable to one memory. useless
    goes to every memory in the pack. A rating judged by another rule gets
    its named ids again from its transcript; with the transcript gone, from
    the citations it was saved with. A rating of a turn in the memory
    system's own folders counts for nothing: there ids are the subject."""
    from . import rating

    records: dict[str, dict[str, Any]] = {}
    unattributed = {"useful": 0, "harmful": 0}
    judged = [e for e in rating.load_ratings(config_root, rating.recent_weeks(now, RATING_WEEKS))
              if e.get("injected") and e.get("rating") in _JUDGED]
    entries = [e for e in judged if not e.get("memory_repo") and not in_memory_repo(e.get("cwd"), root)]
    memory_repo_ratings = len(judged) - len(entries)
    legacy = _legacy_usage(config_root, entries, projects_dir, root) if any(not _current(e) for e in entries) else {}
    worked_out = {"stored": 0, "recomputed": 0, "from_citations": 0}
    for entry in entries:
        verdict = entry["rating"]
        record_ids = [str(record_id) for record_id in entry.get("record_ids", [])]
        if _current(entry):
            named = entry.get("explicit_ids") or []
            worked_out["stored"] += 1
        else:
            again = legacy.get(str(entry.get("key") or ""), {})
            if again.get("found"):
                named = again.get("explicit", [])
                worked_out["recomputed"] += 1
            else:
                named = entry.get("cited_ids") or []
                worked_out["from_citations"] += 1
        used = [record_id for record_id in named if record_id in record_ids]
        if verdict == "useless":
            credited = record_ids
        elif used:
            credited = used
        elif len(record_ids) == 1:
            credited = record_ids
        else:
            unattributed[verdict] += 1
            continue
        for record_id in credited:
            item = records.setdefault(record_id, _blank())
            item[verdict] += 1
            item["last_rated"] = max(item["last_rated"], str(entry.get("time", ""))[:10])
    weeks = ([this_week] if this_week is not None else []) + _report_usage(
        report_dir, now, now.date().isoformat() if this_week is not None else None,
        USAGE_WEEKS - (1 if this_week is not None else 0),
    )
    for week in weeks:
        for record_id, counts in week.items():
            if not isinstance(counts, dict):
                continue
            item = records.setdefault(str(record_id), _blank())
            item["injected"] += int(counts.get("injected", 0) or 0)
            item["used"] += _used(counts)
    return {"generated_at": now.isoformat(), "rating_weeks": RATING_WEEKS, "usage_weeks": len(weeks), "detector": DETECTOR,
            "worked_out": worked_out, "memory_repo_ratings": memory_repo_ratings, "unattributed": unattributed,
            "records": records}


def rebuild(
    config_root: Path,
    now: datetime,
    report_dir: Path | None = None,
    this_week: dict[str, Any] | None = None,
    projects_dir: Path | None = None,
    root: Path | None = None,
) -> Path:
    data = build(config_root, now, report_dir, this_week, projects_dir, root)
    path = feedback_path(config_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
    return path


def judge(counts: dict[str, Any] | None) -> tuple[float, str, str]:
    """(multiplier, 降／升／無, reason) for one record's feedback."""
    if not counts:
        return 1.0, "無", ""
    harmful, useful = (int(counts.get(key, 0) or 0) for key in ("harmful", "useful"))
    used = _used(counts)
    if harmful >= 2 and harmful > useful:
        return DOWN, "降", f"礙事 {harmful} 次、有用 {useful} 次"
    if useful >= 2 and harmful == 0:
        return UP, "升", f"有用 {useful} 次、沒有礙事"
    if used >= 3:
        return UP, "升", f"近 {USAGE_WEEKS} 週被明寫用到 {used} 次"
    return 1.0, "無", ""


def unattributed(config_root: Path) -> dict[str, int]:
    """How many useful and harmful ratings could not be credited to one memory."""
    try:
        data = json.loads(feedback_path(config_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"useful": 0, "harmful": 0}
    counts = data.get("unattributed") if isinstance(data, dict) else None
    return {key: int((counts or {}).get(key, 0) or 0) for key in ("useful", "harmful")}


def flagged_harmful(config_root: Path, minimum: int = 2) -> list[dict[str, Any]]:
    """Records the user rated harmful `minimum` times or more, worst first."""
    records = load(config_root) or {}
    rows = [
        {"id": record_id, "harmful": int(item.get("harmful", 0) or 0), "last_rated": item.get("last_rated", "")}
        for record_id, item in records.items()
        if isinstance(item, dict) and int(item.get("harmful", 0) or 0) >= minimum
    ]
    return sorted(rows, key=lambda row: (-row["harmful"], row["id"]))
