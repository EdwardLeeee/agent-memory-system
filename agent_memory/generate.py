"""Markdown views of the ledger, kept where another tool reads them (spec 實際使用回饋改善 F14).

`generate.toml` in the config home names the ledger the files are generated
from (`store`) and each target: a file and the records it shows (by kind, the
project a record belongs to, scope, id prefix, tag). Run from another ledger
(a copy of this one runs with a copy of this configuration), nothing is
written, not even a file the real ledger has not written yet (F14 re-check 3).
The ledger is the record and the file a view of it: the file is regenerated
after record or supersede writes a record it shows or showed, and by the
weekly doctor, and written only when its text changes. A file the memory
system did not write (no marker line) is never replaced unless adopted, and
then its original is backed up first; a written file that has since changed
is backed up before it is written again. Backups and what was last written
stay in the config home, never beside the target, where the other tool would
read them; the target's folder is never created or changed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from typing import Any

from .storage import (
    MemoryError, _is_expired, atomic_write_text, config_home, decode_record, memory_home, utc_now, write_lock,
)

GENERATE_TOML_NAME = "generate.toml"
MARKER = "由記憶系統自動產生，不要手改；新增或修改請用 memoryctl record／supersede"
FILTER_KEYS = ("kind", "origin", "scope", "id_prefix", "tag")
TARGET_KEYS = frozenset({"name", "path", "intro", "frontmatter", "group", *FILTER_KEYS})
FRONTMATTER_KEYS = ("name", "description", "type")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# What a run did to each target; the doctor warns on the last five.
STATUSES = ("created", "updated", "unchanged", "adopted", "hand-written", "other-store", "not-this-store",
            "missing-folder", "error")


def _strings(value: Any) -> list[str] | None:
    if isinstance(value, str) and value.strip():
        return [value]
    if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
        return list(value)
    return None


def _inside_git_tree(path: Path) -> bool:
    """Whether `path` is in a git work tree, as written or through a link on
    the way: a folder or file linked into a project is the project's (F14
    review 2)."""
    return any((parent / ".git").exists() for parent in (*path.parents, *path.resolve().parents))


def _memory_folder(path: Path) -> bool:
    """Whether `path` is in a memory store (manifest.json beside records/)
    or a memory configuration (machine.json): this ledger's or another's,
    since a copy's configuration names the real ones (F14 review 3)."""
    return any((parent / "machine.json").is_file() or ((parent / "manifest.json").is_file() and (parent / "records").is_dir())
               for parent in path.resolve().parents)


def load_targets(config_root: Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """The targets generate.toml names, and what is wrong with it; a target
    with a problem is left out, never guessed at. No file: no targets."""
    import tomllib

    source = (config_root or config_home()) / GENERATE_TOML_NAME
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return [], [f"{source}: {exc}"]
    problems = [f"{source}: unknown key {key!r}" for key in data if key not in ("store", "target")]
    entries = data.get("target", [])
    if not isinstance(entries, list):
        return [], [*problems, f"{source}: target must be an array of tables ([[target]])"]
    declared = data.get("store")
    owner = Path(declared).expanduser() if isinstance(declared, str) and declared.strip() else None
    if owner is None or not owner.is_absolute():
        return [], [*problems, f'{source}: store must name the ledger the files are generated from (store = "~/agent-memory")']
    owner = owner.resolve()
    store = memory_home().resolve()
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, entry in enumerate(entries, 1):
        label = f"{source}: target {number}"
        if not isinstance(entry, dict):
            problems.append(f"{label} must be a table")
            continue
        found: list[str] = []
        name = entry.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            found.append(f"{label}: name must be letters, digits, '.', '_' or '-'")
        elif name in seen:
            found.append(f"{label}: name {name!r} is used twice")
        else:
            label = f"{source}: target {name}"
            seen.add(name)
        found.extend(f"{label}: unknown key {key!r}" for key in entry if key not in TARGET_KEYS)
        raw_path = entry.get("path")
        path = Path(raw_path).expanduser() if isinstance(raw_path, str) and raw_path.strip() else None
        if path is None or not path.is_absolute() or path.suffix != ".md":
            found.append(f"{label}: path must be an absolute path to a .md file")
        elif (path.resolve().is_relative_to(store) or path.resolve().is_relative_to((config_root or config_home()).resolve())
              or _memory_folder(path)):
            found.append(f"{label}: path must not be inside a memory store or a memory configuration")
        elif _inside_git_tree(path):
            # The memory system never changes a project's own files.
            found.append(f"{label}: path is inside a git repository; generated files never go into a project")
        elif path.is_symlink():
            # Writing would put a file where the link was (F14 review 11).
            found.append(f"{label}: path is a link; a generated file never replaces a link")
        filters: dict[str, list[str]] = {}
        for key in FILTER_KEYS:
            if key in entry:
                values = _strings(entry[key])
                if values is None:
                    found.append(f"{label}: {key} must be a non-empty string or array of strings")
                else:
                    filters[key] = values
        if not filters:
            found.append(f"{label}: needs at least one of {', '.join(FILTER_KEYS)}")
        frontmatter = entry.get("frontmatter")
        if frontmatter is not None and not (
            isinstance(frontmatter, dict) and frontmatter
            and all(key in FRONTMATTER_KEYS and isinstance(value, str) and value.strip() for key, value in frontmatter.items())
        ):
            found.append(f"{label}: frontmatter takes name, description and type, each a string")
        intro = entry.get("intro", "")
        if not isinstance(intro, str):
            found.append(f"{label}: intro must be a string")
        groups = entry.get("group", [])
        if not (isinstance(groups, list) and all(
            isinstance(group, dict) and set(group) == {"tag", "title"}
            and all(isinstance(value, str) and value.strip() for value in group.values()) for group in groups
        )):
            found.append(f"{label}: each [[target.group]] takes a tag and a title")
        if found:
            problems.extend(found)
            continue
        targets.append({
            "name": name, "path": path, "filters": filters, "frontmatter": frontmatter, "intro": intro.strip(),
            "groups": [(group["tag"], group["title"]) for group in groups], "store": str(owner),
        })
    return targets, problems


# --- Which records a target shows ---------------------------------------------


def _visible_here(metadata: dict[str, Any], machine_id: str | None) -> bool:
    if metadata.get("status", "active") != "active" or _is_expired(metadata.get("expires_at")):
        return False
    scope = str(metadata.get("scope", ""))
    return not scope.startswith("machine:") or (machine_id is not None and scope == f"machine:{machine_id}")


def _active_records(root: Path, kinds: set[str] | None, config_root: Path | None) -> list[tuple[dict[str, Any], str]]:
    try:
        machine = json.loads(((config_root or config_home()) / "machine.json").read_text(encoding="utf-8"))
        machine_id = str(machine.get("machine_id") or "") or None
    except (OSError, ValueError, AttributeError):
        machine_id = None
    folders = [root / "records" / kind for kind in sorted(kinds)] if kinds else sorted((root / "records").glob("*"))
    found = []
    for folder in folders:
        for path in sorted(folder.glob("*.md")):
            metadata, body = decode_record(path)
            if _visible_here(metadata, machine_id):
                found.append((metadata, body))
    return found


def _origins(root: Path) -> Any:
    from .lexical_index import _routing_from_snapshot, origin_projects

    routing = _routing_from_snapshot(root)
    return lambda metadata: set(origin_projects(metadata, routing))


def matches(target: dict[str, Any], metadata: dict[str, Any], origins: set[str]) -> bool:
    """Every filter the target gives holds; any one value of a filter will do.
    `origin` is the project the record belongs to: an origin field, a
    project scope, or the project its id names."""
    filters = target["filters"]
    record_id = str(metadata.get("id", ""))
    checks = {
        "kind": lambda values: str(metadata.get("kind")) in values,
        "id_prefix": lambda values: any(record_id.startswith(prefix) for prefix in values),
        "scope": lambda values: str(metadata.get("scope")) in values,
        "tag": lambda values: bool({str(tag) for tag in metadata.get("tags") or []} & set(values)),
        "origin": lambda values: bool((origins | {f"project:{origin}" for origin in origins}) & set(values)),
    }
    return all(checks[key](values) for key, values in filters.items())


# --- The file -------------------------------------------------------------------


def _record_lines(metadata: dict[str, Any], body: str, level: int) -> list[str]:
    """The record under its id heading: its own title line left out (the id
    heading is it), its headings moved under the id's, and a fenced block
    kept as it is, # lines included (F14 review 8)."""
    updated = str(metadata.get("updated_at") or metadata.get("created_at") or "")[:10]
    lines = [f"{'#' * level} {metadata['id']}", "", f"第 {metadata.get('version', 1)} 版，{updated} 更新。", ""]
    fenced = titled = False
    for line in body.strip().splitlines():
        if re.match(r"^ {0,3}(```|~~~)", line):
            fenced = not fenced
        elif fenced:
            pass
        elif heading := re.match(r"^(#+) ", line):
            if len(heading.group(1)) == 1 and not titled:
                titled = True
                continue
            line = "#" * (level - 1) + line
        elif not line.strip() and not lines[-1].strip():
            continue
        lines.append(line)
    return [*lines, ""] if lines[-1].strip() else lines


def render(target: dict[str, Any], records: list[tuple[dict[str, Any], str]]) -> str:
    """The target's file: the frontmatter another tool needs, the marker,
    the intro, then each record, oldest first, under its group's title when
    the target groups by tag. No time of writing, so a run that changes no
    record changes no byte."""
    lines: list[str] = []
    frontmatter = target.get("frontmatter")
    if frontmatter:
        lines.append("---")
        if "name" in frontmatter:
            lines.append(f"name: {frontmatter['name']}")
        if "description" in frontmatter:
            lines.append(f"description: {json.dumps(frontmatter['description'], ensure_ascii=False)}")
        if "type" in frontmatter:
            lines.extend(["metadata:", f"  type: {frontmatter['type']}"])
        lines.extend(["---", ""])
    lines.extend([f"> {MARKER}", ""])
    if target.get("intro"):
        lines.extend([target["intro"], ""])
    ordered = sorted(records, key=lambda record: (str(record[0].get("created_at") or ""), str(record[0]["id"])))
    if not target.get("groups"):
        for metadata, body in ordered:
            lines.extend(_record_lines(metadata, body, 2))
        return "\n".join(lines).rstrip() + "\n"
    placed: set[str] = set()
    sections = [*target["groups"], ("", "其他")]
    for tag, title in sections:
        members = [(m, b) for m, b in ordered if m["id"] not in placed and (not tag or tag in (m.get("tags") or []))]
        if not members:
            continue
        lines.extend([f"## {title}", ""])
        for metadata, body in members:
            placed.add(str(metadata["id"]))
            lines.extend(_record_lines(metadata, body, 3))
    return "\n".join(lines).rstrip() + "\n"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _state_path(config_root: Path, name: str) -> Path:
    return config_root / "runtime" / "generate" / f"{name}.json"


def _state(config_root: Path, name: str) -> dict[str, Any] | None:
    """What was last written to the target, and by which ledger."""
    try:
        value = json.loads(_state_path(config_root, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _keep(path: Path, data: bytes) -> None:
    """A backup, byte for byte: a CRLF or Big5 file stays as it was (F14
    review 9)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _replace(path: Path, text: str) -> None:
    """Write through a temporary file beside the target, keeping the file's
    mode; the folder is another tool's, so it is neither created nor changed."""
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write(
    target: dict[str, Any], text: str, config_root: Path, *, store: str, adopt: bool = False, dry_run: bool = False,
) -> dict[str, Any]:
    """Put `text` in the target's file if it differs, after backing up what
    would be lost: a file the memory system did not write, or another
    ledger wrote (only when adopted), or one that changed since it was last
    written. `store` is the ledger writing it, resolved."""
    path, name = target["path"], target["name"]
    result: dict[str, Any] = {"name": name, "path": str(path)}
    if not path.parent.is_dir():
        return {**result, "status": "missing-folder"}
    try:
        raw: bytes | None = path.read_bytes()
    except FileNotFoundError:
        raw = None
    if raw == text.encode("utf-8"):
        return {**result, "status": "unchanged"}
    # Read as bytes: a file in another encoding is somebody's, not an error.
    current = raw.decode("utf-8", errors="replace") if raw is not None else None
    state = _state(config_root, name)
    writer = str(state.get("store") or "") if state else ""
    reason = None
    if current is not None and MARKER not in current:
        if not adopt:
            return {**result, "status": "hand-written"}
        status, reason = "adopted", "original"
    elif (state is not None and writer != store) or (state is None and current is not None):
        # Another ledger wrote it, or one this configuration kept no record
        # of: a copy of the ledger runs with a copy of this configuration,
        # whose targets are the real files (F14 review 3).
        if not adopt:
            return {**result, "status": "other-store", **({"writer": writer} if writer else {})}
        status, reason = ("adopted", "other-store") if current is not None else ("created", None)
    elif current is None:
        status = "created"
    else:
        status = "updated"
        if str(state.get("sha256")) != hashlib.sha256(raw or b"").hexdigest():
            reason = "edited"  # changed by hand since it was written
    if dry_run:
        return {**result, "status": status, "dry_run": True, **({"backup": reason} if reason else {})}
    if reason and raw is not None:
        stamp = utc_now().replace(":", "").replace("+0000", "Z")
        backup = config_root / "generate-backups" / name / f"{stamp}-{reason}.md"
        _keep(backup, raw)
        result["backup"] = str(backup)
    _replace(path, text)
    atomic_write_text(_state_path(config_root, name), json.dumps(
        {"sha256": _digest(text), "written_at": utc_now(), "path": str(path), "store": store}) + "\n")
    return {**result, "status": status}


def run(
    root: Path, names: list[str] | None = None, *, adopt: bool = False, dry_run: bool = False,
    config_root: Path | None = None,
) -> dict[str, Any]:
    """Regenerate the targets (all, or those named): what each run did, and
    what is wrong with generate.toml."""
    config_root = config_root or config_home()
    targets, problems = load_targets(config_root)
    if names is not None:
        known = {target["name"] for target in targets}
        problems.extend(f"no target named {name!r}" for name in names if name not in known)
        targets = [target for target in targets if target["name"] in names]
    results: list[dict[str, Any]] = []
    if not targets:
        return {"targets": results, "problems": problems}
    lock_root = config_root / "runtime" / "generate"
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        # One run at a time reads the ledger and writes: two sessions' writes
        # regenerate after each other, so the later one shows both (F14 review 10).
        with write_lock(lock_root):
            kinds = None if any("kind" not in target["filters"] for target in targets) else {
                kind for target in targets for kind in target["filters"]["kind"]}
            records = _active_records(root, kinds, config_root)
            origins_of = _origins(root)
            origins = {str(metadata["id"]): origins_of(metadata) for metadata, _ in records}
            for target in targets:
                shown = [(m, b) for m, b in records if matches(target, m, origins[str(m["id"])])]
                if target["store"] != str(root.resolve()):
                    # generate.toml is another ledger's: --adopt does not change that.
                    results.append({"name": target["name"], "path": str(target["path"]), "status": "not-this-store",
                                    "store": target["store"], "records": len(shown)})
                    continue
                try:
                    outcome = write(target, render(target, shown), config_root, store=str(root.resolve()), adopt=adopt,
                                    dry_run=dry_run)
                except Exception as exc:  # one target's trouble never stops the next (F14 review 9)
                    outcome = {"name": target["name"], "path": str(target["path"]), "status": "error",
                               "error": f"{type(exc).__name__}: {exc}"}
                results.append({**outcome, "records": len(shown)})
    except MemoryError as exc:
        problems.append(f"files not regenerated: {exc}")
    return {"targets": results, "problems": problems}


def _shows(path: Path, record_id: str) -> bool:
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return False
    return re.search(rf"^#+ {re.escape(record_id)}$", text, re.M) is not None


def refresh_for(root: Path, record_id: str, config_root: Path | None = None) -> dict[str, Any] | None:
    """After record or supersede wrote `record_id`: regenerate the targets
    that show it now, or showed it (a supersede can move a record out of a
    target's filters). None when no target is concerned."""
    config_root = config_root or config_home()
    targets, _ = load_targets(config_root)
    if not targets:
        return None
    metadata = None
    for path in (root / "records").glob(f"*/{record_id}.md"):
        candidate, _ = decode_record(path)
        if candidate.get("status") == "active":
            metadata = candidate
    origins = _origins(root)(metadata) if metadata is not None else set()
    names = [
        target["name"] for target in targets
        if (metadata is not None and matches(target, metadata, origins)) or _shows(target["path"], record_id)
    ]
    return run(root, names, config_root=config_root) if names else None
