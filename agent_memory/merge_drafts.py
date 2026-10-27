"""Merge drafts for lessons that may be one lesson learned in several places
(spec 論文借鏡改善 A4, after DeltaMem: a general version plus what differs).

doctor's cross-project pairs are joined into groups in which every two
members are paired. For each group a folder of drafts is written next to the
weekly report: the members side by side, a skeleton for the general record,
a supersede skeleton per member (applies = origin, pointing at the general
one), and the commands to run once a person has rewritten them. Nothing is
recorded or superseded here.

A record already folded into a general lesson (applies = origin, its text
says 通用教訓見 <id>) stands for that lesson in doctor's pairs. A general
lesson that others were folded into is the centre of a group of its own:
every record paired with it joins, without being paired with the others,
since each is judged on its own, and drafts join it instead of making a new
one. Those groups come first; the rest are grouped as above.
A group the person dismissed (`memoryctl doctor dismiss`) is not drafted again
while all its members are in one dismissed set; dismissing a general lesson
with one candidate leaves only that candidate out. Each member's origin is
listed, machine ids included; a member that names no project gets the
project of the folder its first version was written in, read from the
transcript of the session that wrote it.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from .storage import atomic_write_text

REWRITE = "請改寫："
FIELDS = {
    "failure": (("symptom", "Symptom"), ("attempt", "Attempt"), ("root_cause", "Root Cause"),
                ("verified_fix", "Verified Fix"), ("retry_when", "Retry When")),
    "experience": (("situation", "Situation"), ("expectation", "Expectation"), ("action", "Action"),
                   ("observed_outcome", "Observed Outcome"), ("applicability", "Applicability")),
}
FIX_FIELD = {"failure": "verified_fix", "experience": "applicability"}
GENERAL_RE = re.compile(r"通用教訓見 ([a-z][a-z0-9]*(?:[._-][a-z0-9]+)+)")
DISMISSED_NAME = "dismissed.json"
INFERRED = "由學到時的對話推得"
_CWD_RE = re.compile(r'"cwd":\s*"(/[^"]*)"')


def merged_into(metadata: dict[str, Any], body: str) -> str | None:
    """The general lesson a record was folded into, as these drafts and the
    2026-10-14 cleanup write it: applies = origin and 通用教訓見 <id>."""
    if metadata.get("applies") != "origin":
        return None
    match = GENERAL_RE.search(body)
    return match.group(1) if match and match.group(1) != metadata.get("id") else None


def merged_targets(root: Path) -> Counter[str]:
    """Each general lesson with how many active records were folded into it."""
    from .core import decode_record

    targets: Counter[str] = Counter()
    for kind in FIELDS:
        for path in sorted((root / "records" / kind).glob("*.md")):
            try:
                metadata, body = decode_record(path)
            except Exception:
                continue
            if metadata.get("status") == "active" and (general := merged_into(metadata, body)):
                targets[general] += 1
    return targets


def dismissed_path(config_root: Path) -> Path:
    return config_root / "runtime" / "doctor" / DISMISSED_NAME


def _dismissed_entries(config_root: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(dismissed_path(config_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("sets") if isinstance(data, dict) else None
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("ids"), list)] if isinstance(entries, list) else []


def load_dismissed(config_root: Path) -> list[set[str]]:
    return [{str(record_id) for record_id in entry["ids"]} for entry in _dismissed_entries(config_root)]


def dismiss(config_root: Path, ids: list[str], now: datetime) -> bool:
    """Remember, on this machine, that these records are not one lesson.
    False when a set already dismissed holds them all."""
    wanted = set(ids)
    if any(wanted <= known for known in load_dismissed(config_root)):
        return False
    entries = _dismissed_entries(config_root) + [{"ids": sorted(wanted), "at": now.isoformat()}]
    path = dismissed_path(config_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"sets": entries}, ensure_ascii=False, indent=1) + "\n")
    return True


def first_learned(root: Path, record_id: str) -> dict[str, Any] | None:
    """The session that wrote the record's earliest version found, from the
    versions' learned_in (archive included) or the transcript cache: where
    the lesson was learned. A later version may be a cleanup elsewhere."""
    from . import learned
    from .core import decode_record

    found = []
    for path in [*(root / "records").glob(f"*/{record_id}.md"), *(root / "archive").glob(f"*/{record_id}/*.md")]:
        try:
            metadata, _ = decode_record(path)
            version = int(metadata.get("version", 1) or 1)
        except Exception:
            continue
        info = metadata.get("learned_in")
        if metadata.get("id") == record_id and isinstance(info, dict) and info.get("session_id"):
            found.append((version, 0, str(info["session_id"]), str(info.get("at", ""))))
    for event in learned.load_cache(root)["records"].get(record_id, []):
        try:
            version = int(event.get("version", 1) or 1)
        except (AttributeError, TypeError, ValueError):
            continue
        if event.get("session_id"):
            found.append((version, 1, str(event["session_id"]), str(event.get("at", ""))))
    if not found:
        return None
    version, _, session, at = min(found)
    return {"session_id": session, "at": at, "version": version}


def session_cwds(
    wanted: dict[str, list[str]], claude_dir: Path | None, codex_dir: Path | None,
) -> dict[tuple[str, str], str]:
    """The folder each session was in at each moment asked (session id ->
    times): the last cwd its transcript shows up to that moment, the first
    one if none comes before it. Each transcript is streamed once for all of
    its moments, up to the last; a session with none found is left out."""
    from . import learned

    found: dict[tuple[str, str], str] = {}
    for session_id, moments in wanted.items():
        located = learned.find_transcript(session_id, claude_dir, codex_dir)
        if located is None:
            continue
        # Earliest first; a time that does not parse is answered at the end.
        pending = sorted(set(moments), key=lambda at: (learned._time(at) is None, learned._time(at) or 0))
        cwd = None
        try:
            with located[0].open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not pending:
                        break
                    if '"cwd"' not in line or (match := _CWD_RE.search(line)) is None:
                        continue
                    stamp = learned._TIMESTAMP_RE.search(line)
                    seen = learned._time(stamp.group(1)) if stamp else None
                    while (pending and cwd is not None and seen is not None
                           and (moment := learned._time(pending[0])) is not None and seen > moment):
                        found[(session_id, pending.pop(0))] = cwd
                    cwd = match.group(1)
        except OSError:
            pass
        for at in pending if cwd is not None else ():
            found[(session_id, at)] = cwd
    return found


def session_cwd(session_id: str, at: str, claude_dir: Path | None, codex_dir: Path | None) -> str | None:
    """The folder a session was in at `at`."""
    return session_cwds({session_id: [at]}, claude_dir, codex_dir).get((session_id, at))


_FOLDERS: dict[str, str] = {}


def folder_place(cwd: str) -> str:
    """A folder no project claims, as a place: a git worktree or a folder in
    a repository as the repository's main folder (~/agent-memory-dev is
    ~/agent-memory), under home written from ~ (designer 2026-10-27)."""
    from .storage import git_environment

    if cwd not in _FOLDERS:
        path = Path(cwd)
        try:
            done = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                  capture_output=True, text=True, timeout=10, env=git_environment())
            if path.is_dir() and done.returncode == 0 and done.stdout.strip():
                path = Path(done.stdout.strip()).parent
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            relative = path.relative_to(Path.home())
            _FOLDERS[cwd] = "~" if relative == Path(".") else f"~/{relative.as_posix()}"
        except ValueError:
            _FOLDERS[cwd] = path.as_posix()
    return _FOLDERS[cwd]


def learned_places(root: Path, record_ids: list[str], transcripts: tuple[Path, Path]) -> dict[str, dict[str, Any]]:
    """Where each record's first version was written, from that session's
    folder then: the projects it is in, or, in none, the folder itself
    (`folder`); with the session, time and folder. Each session's transcript
    is read once; a record whose session left none is left out."""
    from .pretool import cwd_projects

    firsts = {record_id: first_learned(root, record_id) for record_id in record_ids}
    wanted: dict[str, list[str]] = {}
    for first in firsts.values():
        if first:
            wanted.setdefault(first["session_id"], []).append(first["at"])
    cwds = session_cwds(wanted, *transcripts)
    found = {}
    for record_id, first in firsts.items():
        cwd = cwds.get((first["session_id"], first["at"])) if first else None
        if cwd:
            projects = sorted(cwd_projects(cwd, root))
            found[record_id] = {"projects": projects, "folder": None if projects else folder_place(cwd),
                                "session_id": first["session_id"], "at": first["at"], "cwd": cwd}
    return found


def inferred_projects(root: Path, record_ids: list[str], transcripts: tuple[Path, Path]) -> dict[str, dict[str, Any]]:
    """The records of `learned_places` whose folder is in a project."""
    return {record_id: place for record_id, place in learned_places(root, record_ids, transcripts).items()
            if place["projects"]}


def _origin_order(values: list[str]) -> list[str]:
    rank = {"project": 0, "workspace": 1, "machine": 2}
    places = sorted(set(values) - {"global"}, key=lambda v: (rank.get(v.split(":", 1)[0], 3), v))
    return places or ["global"]


def member_origins(
    root: Path, record_id: str, metadata: dict[str, Any], routing: dict[str, Any],
    transcripts: tuple[Path, Path] | None,
) -> tuple[list[str], dict[str, Any] | None]:
    """A member's origins, and for one that names no project, the projects
    of the folder its first version was written in."""
    from .lexical_index import record_origins

    origins = record_origins(metadata, routing)
    if transcripts is None or any(origin.startswith("project:") for origin in origins):
        return origins, None
    return origins, inferred_projects(root, [record_id], transcripts).get(record_id)


def _origin_lines(origins: dict[str, tuple[list[str], dict[str, Any] | None]]) -> list[str]:
    lines = ["## 學到的地方", ""]
    for record_id, (places, inferred) in origins.items():
        line = f"- {record_id}：" + "、".join(places)
        if inferred:
            projects = "、".join(f"project:{p}" for p in inferred["projects"])
            line += (f"；{projects} {INFERRED}（session {inferred['session_id'][:8]}，"
                     f"{inferred['at'][:10]}，cwd {inferred['cwd']}）")
        lines.append(line)
    return lines + [""]


def groups(pairs: list[dict[str, Any]]) -> list[list[str]]:
    """Groups in which every two members are paired, strongest first.

    Joining every chain of pairs merged lessons that only shared a
    neighbour (seven memory and disk records through one OOM note). A
    group starts from the strongest pair not yet used (named before
    similar, then by similarity) and takes in, best average first, only a
    record paired with every member already in it; a record joins one group.
    """
    strength: dict[frozenset[str], tuple[int, float]] = {}
    for pair in pairs:
        strength[frozenset(pair["ids"])] = (1 if pair.get("named") else 0, float(pair.get("overlap", 0.0)))
    used: set[str] = set()
    found: list[list[str]] = []
    for pair in sorted(pairs, key=lambda p: (not p.get("named"), -float(p.get("overlap", 0.0)), p["ids"])):
        if used & set(pair["ids"]):
            continue
        group = list(pair["ids"])
        while True:
            candidates = {
                other
                for key in strength
                for other in key
                if other not in group and other not in used and all(frozenset((other, m)) in strength for m in group)
            }
            if not candidates:
                break
            best = max(
                sorted(candidates),
                key=lambda c: sum(strength[frozenset((c, m))][1] + strength[frozenset((c, m))][0] for m in group),
            )
            group.append(best)
        used.update(group)
        found.append(sorted(group))
    return sorted(found, key=lambda g: (-len(g), g))


def _load(root: Path, record_id: str) -> tuple[dict[str, Any], str] | None:
    from .core import decode_record

    for path in sorted((root / "records").glob(f"*/{record_id}.md")):
        try:
            metadata, body = decode_record(path)
        except Exception:
            continue
        if metadata.get("status") == "active":
            return metadata, body
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.casefold()).strip("_")[:40] or "lesson"


def _payload_from(metadata: dict[str, Any], body: str) -> dict[str, Any]:
    """A supersede payload carrying the record over unchanged."""
    from .core import _extract_section

    kind = str(metadata.get("kind"))
    payload: dict[str, Any] = {
        "scope": metadata.get("scope"), "source": metadata.get("source"),
        "priority": metadata.get("priority", "normal"), "tags": metadata.get("tags", []),
        "evidence": _extract_section(body, "Evidence") or "(none)",
    }
    for key, section in FIELDS.get(kind, ()):
        payload[key] = _extract_section(body, section)
    if kind == "experience":
        payload["decision_links"] = metadata.get("decision_links", [])
    if kind == "failure" and metadata.get("triggers"):
        payload["triggers"] = metadata["triggers"]
    for key in ("origin", "topic"):
        if metadata.get(key):
            payload[key] = metadata[key]
    return payload


def _side_by_side(title: str, members: list[tuple[str, dict[str, Any], str]], kind: str,
                  general_id: str | None = None) -> str:
    side = [title, ""]
    side += [f"- {record_id}（{metadata.get('kind')}，v{metadata.get('version', 1)}，scope {metadata.get('scope')}）"
             + ("（既有通用教訓）" if record_id == general_id else "")
             for record_id, metadata, _ in members]
    for _, section in FIELDS[kind]:
        side += ["", f"## {section}", ""]
        for record_id, metadata, body in members:
            text = _extract(body, section) if str(metadata.get("kind")) == kind else ""
            side += [f"### {record_id}", "", text or "（這筆沒有這一段）", ""]
    return "\n".join(side) + "\n"


def _extract(body: str, section: str) -> str:
    from .core import _extract_section

    return _extract_section(body, section)


def _member_supersede(metadata: dict[str, Any], body: str, general_id: str, reason: str) -> str:
    """A member kept as the event where it happened, pointing at the general one."""
    payload = _payload_from(metadata, body)
    payload["applies"] = "origin"
    field = FIX_FIELD.get(str(metadata.get("kind")), "verified_fix")
    payload[field] = f"通用教訓見 {general_id}。" + str(payload.get(field) or "")
    payload["change_driver"] = "new-evidence"
    payload["change_reason"] = reason
    return json.dumps(payload, ensure_ascii=False, indent=1) + "\n"


def build_group(
    root: Path, ids: list[str], date: str, routing: dict[str, Any],
    generals: Counter[str] | None = None, transcripts: tuple[Path, Path] | None = None,
    links: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The drafts of one group, as file name → text. With a general lesson
    others were already folded into, the drafts join that one."""
    from .lexical_index import record_topic

    loaded = [(record_id, _load(root, record_id)) for record_id in ids]
    members = [(record_id, found[0], found[1]) for record_id, found in loaded if found is not None]
    if len(members) < 2:
        return None
    origins = {record_id: member_origins(root, record_id, metadata, routing, transcripts)
               for record_id, metadata, _ in members}
    places = _origin_order([o for found, inferred in origins.values()
                            for o in found + [f"project:{p}" for p in (inferred or {}).get("projects", [])]])
    inferred_note = (["", f"origin 裡標「{INFERRED}」的專案，是從那筆第一版寫入時所在的資料夾推出來的，請確認。"]
                     if any(inferred for _, inferred in origins.values()) else [])
    existing = sorted((m for m in members if (generals or {}).get(m[0])), key=lambda m: (-(generals or {})[m[0]], m[0]))
    if existing:
        return _join_group(members, existing, places, origins, inferred_note, date, links or {})
    kinds = Counter(str(metadata.get("kind")) for _, metadata, _ in members)
    kind = "failure" if kinds.get("failure") else kinds.most_common(1)[0][0]
    topics = Counter(t for _, metadata, _ in members if (t := record_topic(metadata, routing)))
    topic = topics.most_common(1)[0][0] if topics else "general"
    lead = members[0][0].split(".")[-1]
    general_id = f"{kind}.{topic}.{_slug(lead)}_general"
    general: dict[str, Any] = {
        "id": general_id, "scope": "global", "source": "locally-verified", "priority": "high",
        "applies": "global", "topic": topic, "origin": places,
        "tags": sorted({str(tag) for _, metadata, _ in members for tag in metadata.get("tags", [])}),
        "evidence": "合併自 " + "、".join(record_id for record_id, _, _ in members) + "。",
    }
    for key, section in FIELDS[kind]:
        originals = [f"〔{record_id}〕{_extract(body, section)}" for record_id, metadata, body in members
                     if str(metadata.get("kind")) == kind and _extract(body, section)]
        general[key] = REWRITE + " ".join(originals) if originals else REWRITE
    if kind == "experience":
        general["decision_links"] = []

    files: dict[str, str] = {"general.json": json.dumps(general, ensure_ascii=False, indent=1) + "\n"}
    files["members.md"] = _side_by_side(f"# 合併草稿：{len(members)} 筆可能是同一個教訓（{date}）", members, kind)
    commands = ["memoryctl validate", f"memoryctl record --kind {kind} --input general.json"]
    for record_id, metadata, body in members:
        reason = f"{date} doctor 合併草稿：與 {len(members) - 1} 筆同根因，合併成通用教訓 {general_id}；這筆保留為原處的事件。"
        files[f"supersede-{record_id}.json"] = _member_supersede(metadata, body, general_id, reason)
        commands.append(f"memoryctl supersede --id {record_id} --input supersede-{record_id}.json")
    commands.append("memoryctl validate")
    dismiss_command = "memoryctl doctor dismiss " + " ".join(record_id for record_id, _, _ in members)
    files["README.md"] = "\n".join([
        f"# 合併草稿（{date}）：{general_id}",
        "",
        "doctor 只產生草稿，不會執行任何 record／supersede。審閱步驟：",
        "",
        f"1. 讀 members.md，確認這幾筆真的是同一個根本原因；不是的話執行 `{dismiss_command}`，之後 doctor 不會再為這幾筆產生草稿（只刪掉這個資料夾，下週會再產生）。",
        f"2. 改寫 general.json：id 建議用工具或技術命名（目前是 {general_id}），把每個「{REWRITE}」欄位寫成不分專案的通用版；origin 已列出每筆學到的地方（見下方）。",
        "3. 檢查每個 supersede-<id>.json：applies 改成 origin，修法開頭指向通用教訓；其餘內容沿用原紀錄。",
        "4. 確認後依序執行：",
        "",
        "```bash",
        *commands,
        "```",
        "",
        *_origin_lines(origins),
        *inferred_note,
    ]) + "\n"
    return {"name": f"{_slug(topic)}-{_slug(lead)}", "general_id": general_id, "join": False,
            "members": [m[0] for m in members], "files": files}


def _join_group(
    members: list[tuple[str, dict[str, Any], str]], existing: list[tuple[str, dict[str, Any], str]],
    places: list[str], origins: dict[str, Any], inferred_note: list[str], date: str,
    links: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Drafts for folding the other members into a general lesson that
    already exists: its own supersede adds evidence and origin. Each
    candidate is judged, and dismissed, on its own."""
    general_id, general_metadata, general_body = existing[0]
    kind = str(general_metadata.get("kind"))
    joining = [m for m in members if m[0] != general_id]
    names = "、".join(record_id for record_id, _, _ in joining)
    payload = _payload_from(general_metadata, general_body)
    payload["applies"] = general_metadata.get("applies") or "global"
    payload["origin"] = _origin_order(places)
    payload["evidence"] = f"{payload['evidence']} {date} 另併入 {names}。"
    payload["change_driver"] = "new-evidence"
    payload["change_reason"] = f"{date} doctor 合併草稿：{names} 與這筆通用教訓同根因，加入它；補上 evidence 與 origin。"
    files = {f"supersede-{general_id}.json": json.dumps(payload, ensure_ascii=False, indent=1) + "\n"}
    files["members.md"] = _side_by_side(f"# 加入既有通用教訓：{len(joining)} 筆可能和 {general_id} 是同一個教訓（{date}）",
                                        members, kind, general_id)
    commands = ["memoryctl validate", f"memoryctl supersede --id {general_id} --input supersede-{general_id}.json"]
    for record_id, metadata, body in joining:
        reason = f"{date} doctor 合併草稿：與通用教訓 {general_id} 同根因，併入它；這筆保留為原處的事件。"
        files[f"supersede-{record_id}.json"] = _member_supersede(metadata, body, general_id, reason)
        commands.append(f"memoryctl supersede --id {record_id} --input supersede-{record_id}.json")
    commands.append("memoryctl validate")
    others = [record_id for record_id, _, _ in existing[1:]]

    def how(record_id: str) -> str:
        pair = links.get(record_id)
        if pair is None:
            return ""
        text = "明文互指" if pair.get("named") else f"相似度 {pair.get('overlap')}"
        return f"（{text}" + ("，經由已併入的 " + "、".join(pair["via"]) if pair.get("via") else "") + "）"

    files["README.md"] = "\n".join([
        f"# 加入既有通用教訓（{date}）：{general_id}",
        "",
        f"這組裡已經有通用教訓 {general_id}（已有其他紀錄併入它），所以不產生新的通用教訓，而是把 {names} 併入它。",
        "doctor 只產生草稿，不會執行任何 supersede。審閱步驟：",
        "",
        f"1. 讀 members.md，一筆一筆確認候選和 {general_id} 是不是同一個根本原因。不是的那筆，執行它那一行的 dismiss：之後只有那筆不再出現在這組，其他候選照常（只刪掉這個資料夾，下週會再產生）。",
        *[f"   - {record_id}{how(record_id)}：`memoryctl doctor dismiss {general_id} {record_id}`"
          for record_id, _, _ in joining],
        f"2. 檢查 supersede-{general_id}.json：只補了 evidence 與 origin，文字沿用原本的；新成員帶來原本沒寫到的情況時，順手改寫成通用的說法。",
        "3. 檢查每個新成員的 supersede-<id>.json：applies 改成 origin，修法開頭指向通用教訓；其餘內容沿用原紀錄。",
        *([f"   這組還有別的通用教訓（{'、'.join(others)}），草稿把它當成一般成員併入 {general_id}；請確認要這樣做。"] if others else []),
        "4. 確認後依序執行（沒有要併入的候選，跳過它那一行 supersede，並從通用教訓的 evidence 與 origin 拿掉它）：",
        "",
        "```bash",
        *commands,
        "```",
        "",
        *_origin_lines(origins),
        *inferred_note,
    ]) + "\n"
    return {"name": f"join-{_slug(general_id.split('.')[-1])}", "general_id": general_id, "join": True,
            "members": [record_id for record_id, _, _ in joining], "files": files}


def write_drafts(
    root: Path, pairs: list[dict[str, Any]], report_dir: Path, date: str,
    config_root: Path | None = None, transcripts: tuple[Path, Path] | None = None,
) -> dict[str, Any]:
    """Write every group's drafts under <report_dir>/<date>-merge-drafts/,
    leaving out groups whose members are all in one dismissed set.
    `transcripts` (Claude and Codex folders) lets a member that names no
    project get one from where it was learned; None reads none."""
    from .lexical_index import compute_routing

    routing = compute_routing()
    generals = merged_targets(root)
    dismissed = load_dismissed(config_root) if config_root is not None else []
    planned: list[tuple[list[str], dict[str, dict[str, Any]] | None]] = []
    used: set[str] = set()
    excluded = left_out = 0
    # Around each general lesson first, the one most folded into first: every
    # record paired with it and not yet taken, less those dismissed with it.
    for hub in sorted({i for pair in pairs for i in pair["ids"] if generals.get(i)}, key=lambda g: (-generals[g], g)):
        if hub in used:
            continue
        links: dict[str, dict[str, Any]] = {}
        for pair in pairs:
            if hub in pair["ids"]:
                other = next(record_id for record_id in pair["ids"] if record_id != hub)
                if other not in used:
                    links.setdefault(other, pair)
        used.add(hub)
        kept = {other: pair for other, pair in links.items() if not any({hub, other} <= known for known in dismissed)}
        used.update(kept)
        if links and not kept:
            excluded += 1
        else:
            left_out += len(links) - len(kept)
        if kept:
            planned.append(([hub, *sorted(kept)], kept))
    for ids in groups([pair for pair in pairs if not used & set(pair["ids"])]):
        if any(set(ids) <= known for known in dismissed):
            excluded += 1
            continue
        planned.append((ids, None))
    base = report_dir / f"{date}-merge-drafts"
    written = []
    for ids, links in planned:
        group = build_group(root, ids, date, routing, generals, transcripts, links)
        if group is None:
            continue
        folder = base / f"{len(written) + 1:02d}-{group['name']}"
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in group["files"].items():
            atomic_write_text(folder / name, text)
        written.append({"folder": str(folder), "general_id": group["general_id"], "join": group["join"],
                        "members": group["members"]})
    return {"dir": str(base) if written else "", "groups": written, "dismissed": excluded,
            "dismissed_candidates": left_out}


# --- Records that only point at a federated source (spec 實際使用回饋改善 F5) ------
#
# A decision whose main section only says it moved to a project's ADRs is
# no longer injected; the weekly doctor lists it with two supersede
# skeletons a person picks from: rewrite it as the ADR's decision sentence,
# or keep it as is but scope it to the project.

_ADR_RE = re.compile(r"docs/adr/[\w.-]+\.md")
_REPO_RE = re.compile(r"in the ([\w.-]+) repo")
_DECISION_TEXT = (("problem", "Problem"), ("decision", "Decision"), ("rationale", "Rationale"))
_DECISION_LISTS = (("constraints", "Constraints"), ("alternatives_rejected", "Alternatives Rejected"),
                   ("consequences", "Consequences"), ("revisit_when", "Revisit When"))


def _decision_payload(metadata: dict[str, Any], body: str) -> dict[str, Any]:
    from .core import _extract_section

    payload: dict[str, Any] = {
        "scope": metadata.get("scope"), "source": metadata.get("source"),
        "priority": metadata.get("priority", "normal"), "tags": metadata.get("tags", []),
        "evidence": _extract_section(body, "Evidence") or "(none)",
    }
    for key, section in _DECISION_TEXT:
        payload[key] = _extract_section(body, section) or "(none)"
    for key, section in _DECISION_LISTS:
        items = [line[2:].strip() for line in _extract_section(body, section).splitlines() if line.startswith("- ")]
        payload[key] = [item for item in items if item] or ["(none)"]
    return payload


def pointer_drafts(root: Path, report_dir: Path, date: str) -> dict[str, Any]:
    """Every active record that only points at a federated source, with
    supersede skeletons under <report_dir>/<date>-pointer-drafts/<id>/."""
    from .core import _extract_section, decode_record, pointer_only
    from .lexical_index import _token_owner, compute_routing

    routing = compute_routing()
    base = report_dir / f"{date}-pointer-drafts"
    found = []
    for path in sorted((root / "records").glob("*/*.md")):
        try:
            metadata, body = decode_record(path)
        except Exception:
            continue
        if metadata.get("status") != "active" or not pointer_only(metadata, body):
            continue
        record_id, kind = str(metadata["id"]), str(metadata.get("kind"))
        main = _extract_section(body, "Decision" if kind == "decision" else "Value")
        adrs = sorted(set(_ADR_RE.findall(main)))
        repo = _REPO_RE.search(main)
        parts = record_id.split(".")
        project = (routing.get("sources", {}).get(repo.group(1)) if repo else None) or (
            _token_owner(routing).get(parts[1]) if len(parts) > 2 else None)
        if project and metadata.get("scope") == f"project:{project}":
            # Already scoped to the project of its source: a pointer that
            # was kept on purpose is not listed again (F5 acceptance).
            continue
        entry = {"id": record_id, "scope": metadata.get("scope"), "adrs": adrs, "project": project, "folder": ""}
        if kind == "decision":
            folder = base / record_id
            folder.mkdir(parents=True, exist_ok=True)
            payload = _decision_payload(metadata, body)
            reason = f"{date} doctor：這筆只剩指向聯邦來源的指標，不再注入。"
            rewrite = {**payload, "decision": f"{REWRITE}寫成 {('、'.join(adrs) or '那份 ADR')} 的決定句。原文：{main}",
                       "change_driver": "new-evidence", "change_reason": reason + "改寫成 ADR 的決定句，讓它自己說得出決定。"}
            files = {"supersede-rewrite.json": rewrite}
            if project:
                files["supersede-scope.json"] = {**payload, "scope": f"project:{project}", "change_driver": "new-evidence",
                                                  "change_reason": reason + f"保留原文，範圍改成 project:{project}。"}
            for name, data in files.items():
                atomic_write_text(folder / name, json.dumps(data, ensure_ascii=False, indent=1) + "\n")
            atomic_write_text(folder / "README.md", "\n".join([
                f"# {record_id}：只剩指標的紀錄（{date}）",
                "",
                f"它的決定段只寫著已移到聯邦來源（{('、'.join(adrs) or '見原文')}），沒有決定本身，所以不再注入；search 照樣找得到。",
                "doctor 只產生草稿，不會執行 supersede。二選一：",
                "",
                f"1. 改寫成 ADR 的決定句：編輯 supersede-rewrite.json 的 decision（目前是「{REWRITE}」加原文），再執行",
                f"   `memoryctl supersede --id {record_id} --input supersede-rewrite.json`",
                *([f"2. 保留原文、範圍改成 project:{project}：`memoryctl supersede --id {record_id} --input supersede-scope.json`"]
                  if project else ["2. 推不出它屬於哪個專案，只能用第 1 種，或自己填 scope。"]),
                "",
                "前後各跑一次 `memoryctl validate`。",
                "",
            ]))
            entry["folder"] = str(folder)
        found.append(entry)
    return {"dir": str(base) if any(e["folder"] for e in found) else "", "records": found}
