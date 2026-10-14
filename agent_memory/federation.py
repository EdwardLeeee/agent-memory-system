"""Source federation: index a project's own native knowledge (ADRs,
settlements, research notes, commit messages) instead of asking anyone to
duplicate it into a second ledger.

Knowledge that lives beside the work and rides along with a commit gets
written; knowledge that has to be copied into a separate memory system does
not. So instead of asking a project to also write into `memoryctl`, this
module makes `memoryctl` read the project's own sources.

Every entry produced here is derived and rebuildable: it lives only in the
derived SQLite index (never the canonical Markdown ledger), is regenerated in
full on every sync, and disappears if the source repo or file it came from
disappears. No file in the source repo is ever written by this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import subprocess

from .storage import git_environment, HOOK_GIT_VARIABLES
import unicodedata
from pathlib import Path
from typing import Any

FEDERATION_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
ALLOWED_PARSER_KINDS = {"adr", "settlement", "markdown-note", "git-log"}

# A settlement file's shape belongs to the project that writes it, so every
# field name the settlement parser reads comes from that project's source
# config. Each option defaults to unset, and a section whose keys are unset
# produces nothing: no project's schema is assumed here.
SETTLEMENT_OPTION_KEYS: dict[str, type] = {
    "subject_key": str,
    "version_key": str,
    "consequences_key": str,
    "errata_key": str,
    "errata_reason_key": str,
    "errata_field_key": str,
    "errata_claim_flag_key": str,
    "predictions_key": str,
    "prediction_outcome_key": str,
    "prediction_miss_value": str,
    "prediction_hit_value": str,
    "prediction_claim_key": str,
    "prediction_provenance_key": str,
    "prediction_house_key": str,
    "prediction_actual_key": str,
    "prediction_error_keys": list,
    "group_key": str,
    # Group values that mean "none of the above": grouping by them would merge
    # unrelated errata, so those errata fall back to the literal method.
    "group_key_fallback_values": list,
}
PARSER_OPTION_KEYS: dict[str, dict[str, type]] = {
    "settlement": SETTLEMENT_OPTION_KEYS,
    # heading_level "3" splits a note at ### headings: a decision log whose
    # ## headings are dates and whose ### headings are the decisions.
    "markdown-note": {"reference_headings": list, "heading_level": str},
}
MARKDOWN_HEADING_LEVELS = ("2", "3")


class FederationConfigError(Exception):
    """A sources.d/<name>.toml config is missing or malformed."""


# --- A minimal TOML reader -------------------------------------------------
#
# Deliberately not a general TOML implementation: agent-memory's runtime has
# no third-party Python dependency, and a source config only ever needs flat
# string/list-of-string values under `[section]` and `[[array.of.tables]]`
# headers. Anything outside that shape is rejected rather than silently
# misread.


def _parse_toml_scalar(raw: str, *, line_no: int) -> Any:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        items = []
        for part in inner.split(","):
            part = part.strip()
            if not part:
                continue
            if not (part.startswith('"') and part.endswith('"')):
                raise FederationConfigError(
                    f"line {line_no}: list items must be double-quoted strings"
                )
            items.append(part[1:-1])
        return items
    raise FederationConfigError(
        f"line {line_no}: only quoted strings and string lists are supported"
    )


def parse_minimal_toml(text: str) -> dict[str, Any]:
    """Parse the narrow TOML subset a source config needs.

    Supports `[section]` tables, `[[array.of.tables]]` (each becomes one dict
    appended to a list), `key = "string"`, `key = ["a", "b"]`, and `#`
    comments. Nothing else.
    """
    root: dict[str, Any] = {}
    current: dict[str, Any] = root
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[[") and line.endswith("]]"):
            name = line[2:-2].strip()
            if not name:
                raise FederationConfigError(f"line {line_no}: empty table array name")
            table: dict[str, Any] = {}
            root.setdefault(name, [])
            if not isinstance(root[name], list):
                raise FederationConfigError(
                    f"line {line_no}: {name!r} is already a table, not an array"
                )
            root[name].append(table)
            current = table
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if not name:
                raise FederationConfigError(f"line {line_no}: empty table name")
            root.setdefault(name, {})
            if not isinstance(root[name], dict):
                raise FederationConfigError(
                    f"line {line_no}: {name!r} is already an array of tables"
                )
            current = root[name]
            continue
        if "=" not in line:
            raise FederationConfigError(f"line {line_no}: expected key = value")
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            raise FederationConfigError(f"line {line_no}: empty key")
        current[key] = _parse_toml_scalar(value, line_no=line_no)
    return root


# --- Source config -----------------------------------------------------


def sources_dir(config_root: Path) -> Path:
    return config_root / "sources.d"


def source_config_path(config_root: Path, name: str) -> Path:
    return sources_dir(config_root) / f"{name}.toml"


def load_source_config(config_root: Path, name: str) -> dict[str, Any]:
    path = source_config_path(config_root, name)
    if not path.is_file():
        raise FederationConfigError(f"no such source config: {name} ({path})")
    try:
        parsed = parse_minimal_toml(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FederationConfigError(f"cannot read {path}: {exc}") from exc
    source = parsed.get("source")
    if not isinstance(source, dict):
        raise FederationConfigError(f"{path}: missing [source] table")
    repo = source.get("repo")
    if not isinstance(repo, str) or not repo:
        raise FederationConfigError(f"{path}: source.repo must be a non-empty string")
    if not Path(repo).expanduser().is_dir():
        raise FederationConfigError(f"{path}: source.repo does not exist: {repo}")
    affinity = source.get("affinity", [])
    if not isinstance(affinity, list) or not all(
        isinstance(item, str) and item for item in affinity
    ):
        raise FederationConfigError(f"{path}: source.affinity must be a string array")
    aliases = source.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
        raise FederationConfigError(f"{path}: source.aliases must be a string array")
    orientation = source.get("orientation", [])
    if not isinstance(orientation, list) or not all(
        isinstance(item, str) for item in orientation
    ):
        raise FederationConfigError(f"{path}: source.orientation must be a string array")
    ref = source.get("ref")
    if ref is not None and (not isinstance(ref, str) or not ref.strip() or ref.startswith("-")):
        raise FederationConfigError(f"{path}: source.ref must be a git ref such as \"origin/main\"")
    parsers = parsed.get("parser", [])
    if not isinstance(parsers, list) or not parsers:
        raise FederationConfigError(f"{path}: at least one [[parser]] is required")
    for parser in parsers:
        kind = parser.get("kind")
        if kind not in ALLOWED_PARSER_KINDS:
            raise FederationConfigError(
                f"{path}: unsupported parser.kind {kind!r}; choose one of "
                + ", ".join(sorted(ALLOWED_PARSER_KINDS))
            )
        if kind != "git-log" and not isinstance(parser.get("glob"), str):
            raise FederationConfigError(
                f"{path}: parser kind {kind!r} requires a string glob"
            )
        for key, value in parser.items():
            if key in ("kind", "glob", "since"):
                continue
            allowed = PARSER_OPTION_KEYS.get(kind, {})
            if key not in allowed:
                raise FederationConfigError(
                    f"{path}: parser kind {kind!r} has no option {key!r}"
                )
            expected = allowed[key]
            if expected is list and not (
                isinstance(value, list) and all(isinstance(item, str) for item in value)
            ):
                raise FederationConfigError(f"{path}: {key} must be a string array")
            if key == "heading_level" and value not in MARKDOWN_HEADING_LEVELS:
                raise FederationConfigError(f"{path}: heading_level must be \"2\" or \"3\"")
            if expected is str and not isinstance(value, str):
                raise FederationConfigError(f"{path}: {key} must be a string")
    return {
        "name": name,
        "repo": Path(repo).expanduser().resolve(),
        "affinity": list(affinity),
        # Names a query may use to name this project from outside it.
        "aliases": [item for item in aliases if item.strip()],
        "orientation": list(orientation),
        # With a ref, files are read from that commit, not the working tree.
        "ref": ref.strip() if isinstance(ref, str) else None,
        "parsers": parsers,
    }


def list_source_configs(config_root: Path) -> list[str]:
    directory = sources_dir(config_root)
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.toml"))


# --- Provenance --------------------------------------------------------


def _git_blob_sha(repo: Path, relative_path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"HEAD:{relative_path}"], env=git_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# Where `sources sync` fetches a remote ref: refs/agent-memory/<remote>/<branch>.
# refs/remotes/* belong to the project, and every worktree shares them; a
# sync that moved origin/main would move the base another session is about
# to reset or rebase onto (spec 記憶路由樹 v1.1, ruling 55).
PRIVATE_REF_PREFIX = "refs/agent-memory/"


def private_ref(ref: str) -> str:
    return PRIVATE_REF_PREFIX + ref


def read_ref_commit(repo: Path, ref: str) -> str | None:
    """The commit a source with `ref` is read from: the private ref once a
    sync has fetched it, else the configured ref (ruling 55)."""
    return (_resolve_commit(repo, private_ref(ref)) if "/" in ref else None) or _resolve_commit(repo, ref)


class SourceTree:
    """A source's files: its working tree, or with `ref`, that git ref.

    A project whose sessions work on worktree branches while GitHub merges
    main leaves the local checkout behind; with `ref = "origin/main"` the
    source is read from what sync fetched into the private ref, else from
    the configured ref until a fetch has succeeded (spec 記憶路由樹 v1.1,
    rulings 43, 55). `commit` is the commit actually read; a ref git cannot
    resolve falls back to the working tree with `missing_ref` set, which
    sync reports and doctor warns about (ruling 51 d, g).
    """

    def __init__(self, repo: Path, ref: str | None = None) -> None:
        self.repo = repo
        self.ref = ref
        self.missing_ref = False
        self._blobs: dict[str, str] | None = None
        if ref:
            commit = read_ref_commit(repo, ref)
            listing = self._list(commit) if commit else None
            if listing is None:
                self.missing_ref = True
            else:
                self._blobs = listing
            self.commit = commit if listing is not None else _resolve_commit(repo, "HEAD")
        else:
            self.commit = _resolve_commit(repo, "HEAD")

    def _list(self, commit: str) -> dict[str, str] | None:
        result = subprocess.run(
            ["git", "-C", str(self.repo), "ls-tree", "-r", "-z", commit, "--"], env=git_environment(),
            capture_output=True, check=False,
        )
        if result.returncode != 0:
            return None
        blobs = {}
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            header, _, name = record.partition(b"\t")
            parts = header.split()
            if len(parts) == 3 and parts[1] == b"blob":
                blobs[os.fsdecode(name)] = parts[2].decode()
        return blobs

    @property
    def from_ref(self) -> bool:
        return self._blobs is not None

    def files(self, pattern: str) -> list[str]:
        if self._blobs is None:
            return sorted(
                str(path.relative_to(self.repo)) for path in self.repo.glob(pattern) if path.is_file()
            )
        return sorted(name for name in self._blobs if _glob_match(name, pattern))

    def read(self, relative_path: str) -> str:
        if self._blobs is None:
            return (self.repo / relative_path).read_text(encoding="utf-8", errors="replace")
        result = subprocess.run(
            ["git", "-C", str(self.repo), "cat-file", "blob", self._blobs[relative_path]], env=git_environment(),
            capture_output=True, check=False,
        )
        if result.returncode != 0:
            raise OSError(f"cannot read {relative_path} at {self.ref}")
        return result.stdout.decode("utf-8", errors="replace")

    def blob(self, relative_path: str) -> str | None:
        if self._blobs is None:
            return _git_blob_sha(self.repo, relative_path)
        return self._blobs.get(relative_path)


def _resolve_commit(repo: Path, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], env=git_environment(),
        text=True, capture_output=True, check=False,
    )
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def _glob_match(name: str, pattern: str) -> bool:
    """pathlib-style glob over posix paths: `*` within one segment, `**` any."""
    import fnmatch

    def match(parts: list[str], pats: list[str]) -> bool:
        if not pats:
            return not parts
        if pats[0] == "**":
            return any(match(parts[i:], pats[1:]) for i in range(len(parts) + 1))
        return bool(parts) and fnmatch.fnmatchcase(parts[0], pats[0]) and match(parts[1:], pats[1:])

    return match(name.split("/"), [part for part in pattern.split("/") if part])


def _as_tree(repo: "Path | SourceTree") -> SourceTree:
    return repo if isinstance(repo, SourceTree) else SourceTree(repo)


def _remotes(repo: "Path | str") -> set[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "remote"], capture_output=True, timeout=5,
            stdin=subprocess.DEVNULL, env=git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in os.fsdecode(result.stdout).splitlines() if line.strip()}


def ref_remote(ref: str | None, repo: "Path | str") -> str | None:
    """The remote a ref names: the longest remote `git remote` lists that the
    ref starts with plus "/" (origin/main, team/up/main for a remote named
    team/up). Any other ref (main, release/1.0) is local and never fetched
    (rulings 51 d, 52 f, 53 e)."""
    if not ref or "/" not in str(ref):
        return None
    names = [name for name in _remotes(repo) if str(ref).startswith(name + "/") and len(str(ref)) > len(name) + 1]
    return max(names, key=len) if names else None


def ref_is_remote(ref: str | None, repo: "Path | str") -> bool:
    return ref_remote(ref, repo) is not None


def fetch_source_ref(config: dict[str, Any], timeout: float = 10.0) -> bool:
    """`git fetch` the remote branch a source's ref names into its private
    ref, within `timeout`.

    Only `sources sync` calls this (reindex and the hooks never touch the
    network). The fetch writes refs/agent-memory/<remote>/<branch> and
    nothing else: no remote-tracking ref (`--refmap=` turns off git's
    opportunistic update of origin/<branch>), no FETCH_HEAD, no tags
    (ruling 55). git never asks for credentials (GIT_TERMINAL_PROMPT=0,
    stdin from /dev/null), runs in its own process group, and the whole
    group is killed on timeout, so no credential helper outlives it (ruling
    51 c). Any failure leaves the private ref as it was.
    """
    import signal

    ref = config.get("ref")
    remote = ref_remote(ref, config["repo"])
    if remote is None:
        return False
    branch = str(ref)[len(remote) + 1:]
    env = git_environment({"GIT_TERMINAL_PROMPT": "0"})
    try:
        process = subprocess.Popen(
            [
                "git", "-C", str(config["repo"]), "fetch", "--quiet", "--no-write-fetch-head", "--no-tags",
                "--refmap=", remote, f"+refs/heads/{branch}:{private_ref(str(ref))}",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, start_new_session=True,
        )
    except OSError:
        return False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.wait()
        return False
    return process.returncode == 0


def source_ref_commit(config: dict[str, Any]) -> str | None:
    """The commit a source would be read from now: its ref, else HEAD."""
    return SourceTree(config["repo"], config.get("ref")).commit if config.get("ref") else _resolve_commit(config["repo"], "HEAD")


ENTRY_SLUG_LIMIT = 72


def _entry_id(source_name: str, discriminator: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "-", discriminator.casefold()).strip("-")
    safe = re.sub(r"-+", "-", safe) or "entry"
    if len(safe) > ENTRY_SLUG_LIMIT:
        # A heading can carry a whole digest; an id that long costs more
        # tokens than the excerpt it labels. Keep it unique with a hash.
        digest = hashlib.sha256(discriminator.encode("utf-8")).hexdigest()[:8]
        safe = safe[: ENTRY_SLUG_LIMIT - 9].rstrip("-") + "-" + digest
    candidate = f"federation.{source_name}.{safe}"
    if FEDERATION_ID_RE.fullmatch(candidate):
        return candidate
    digest = hashlib.sha256(discriminator.encode("utf-8")).hexdigest()[:12]
    return f"federation.{source_name}.entry-{digest}"


EXCERPT_LIMIT = 400


def _bounded_excerpt(text: str, limit: int = EXCERPT_LIMIT) -> str:
    """A pointer-shaped excerpt, not the full source text.

    Federation content is indicative, not authoritative: the repo is the
    current truth, this is a bounded pointer into it. An untruncated ADR or
    settlement dump would also crowd a query's token budget on its own,
    pushing out every other candidate regardless of relevance.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…（以 repo 現況為準，完整內容見 Evidence 路徑）"


def _extract_markdown_section(body: str, heading: str) -> str:
    marker = f"## {heading}\n"
    start = body.find(marker)
    if start < 0:
        return ""
    content_start = start + len(marker)
    next_heading = body.find("\n## ", content_start)
    return body[content_start : next_heading if next_heading >= 0 else None].strip()


def _extract_markdown_sections(text: str, level: int = 2) -> tuple[str, list[tuple[str, str]]]:
    """Return (H1 title, [(heading, body), ...]) for each heading of `level`.

    A heading of a higher level (fewer #) ends a section without starting
    one, so the text between a date heading and its first decision belongs
    to no section.
    """
    if level == 2:
        return _extract_markdown_h2_sections(text)
    marker = "#" * level + " "
    lines = text.splitlines()
    title = next((line.strip()[2:].strip() for line in lines if line.strip().startswith("# ")), "")
    sections: list[tuple[str, str]] = []
    heading: str | None = None
    buffer: list[str] = []
    for line in lines:
        is_marker = line.startswith(marker)
        is_higher = any(line.startswith("#" * n + " ") for n in range(1, level))
        if is_marker or is_higher:
            if heading is not None:
                sections.append((heading, "\n".join(buffer).strip()))
            heading = line[len(marker):].strip() if is_marker else None
            buffer = []
        elif heading is not None:
            buffer.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(buffer).strip()))
    return title, sections


def _extract_markdown_h2_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (H1 title, [(heading, body), ...]) for each `## ` section."""
    lines = text.splitlines()
    title = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break
    sections: list[tuple[str, str]] = []
    heading: str | None = None
    buffer: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if heading is not None:
                sections.append((heading, "\n".join(buffer).strip()))
            heading = line[3:].strip()
            buffer = []
        elif heading is not None:
            buffer.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(buffer).strip()))
    return title, sections


# --- Parsers -------------------------------------------------------------
#
# Each parser returns a list of {"metadata": {...}, "body": "..."} dicts
# shaped exactly like a canonical record's decoded form, so the existing
# ranking and rendering code needs no federation-specific branch. `metadata`
# always carries an `origin` object for provenance and never a `status` other
# than "active" (derived entries are never archived; a resync simply stops
# producing one that no longer exists).


def _base_metadata(
    source_name: str,
    affinity: list[str],
    entry_id: str,
    kind: str,
    tags: list[str],
    repo: "Path | SourceTree",
    relative_path: str,
    anchor: str,
) -> dict[str, Any]:
    tree = _as_tree(repo)
    metadata: dict[str, Any] = {
        "id": entry_id,
        "kind": kind,
        "status": "active",
        "scope": "global",
        "affinity": list(affinity),
        "source": "locally-verified",
        "priority": "normal",
        "tags": [*tags, f"federation:{source_name}"],
        "last_verified": "",
        "expires_at": None,
        "origin": {
            "source": source_name,
            "repo": str(tree.repo),
            "path": relative_path,
            "anchor": anchor,
            "blob_sha256": tree.blob(relative_path) if relative_path or not tree.from_ref else None,
        },
    }
    if kind == "lesson":
        # A derived lesson is a raw finding, not yet a generalized rule; that
        # generalization is exactly what `lesson promote` adds. It starts
        # open, the same as any newly authored canonical lesson would.
        metadata["lesson_status"] = "open"
    return metadata


def _lesson_body(
    finding: str,
    applies_to: str,
    origin_anchor: str,
    relative_path: str,
    observations: str = "none",
) -> str:
    """Match the canonical `lesson` record's section shape, minus the rule.

    A derived entry is a raw finding, not a rule, so it has no `## Rule`
    section; the renderers show its Finding instead. It carries no
    placeholder text either: an instruction-shaped placeholder would be
    indexed as content and match every prompt that mentions it.
    """
    return (
        f"## Finding\n\n{_bounded_excerpt(finding)}\n\n"
        f"## Applies To\n\n{applies_to}\n\n"
        f"## Status\n\nopen\n\n"
        f"## Origin\n\n{origin_anchor}\n\n"
        f"## Observations\n\n{observations}\n\n"
        f"## Evidence\n\n{relative_path}\n"
    )


def _note_body(value: str, evidence: str) -> str:
    """Reference material, not a lesson: rendered under REFERENCE DATA.

    The context renderer reads a non-lesson, non-decision entry's `## Value`
    section, so a note must carry one or it would render as an empty value.
    """
    return f"## Value\n\n{_bounded_excerpt(value)}\n\n## Evidence\n\n{evidence}\n"


# A research note's H2 section is reference material by default. Only a
# section whose own heading or first sentence says it records a mistake, a
# trap, or a correction is a lesson; an analyst deliverable's constants block
# or hand-off remarks are not, and treating every section as a lesson let
# them flood prompt-time retrieval under applies_to=all.
LESSON_SIGNAL_RE = re.compile(
    r"陷阱|教訓|錯誤|勘誤|更正|錯在|根因|errata|trap|lesson|pitfall|carried|copy-paste"
    r"|correction|root cause",
    re.IGNORECASE,
)
_FIRST_SENTENCE_RE = re.compile(r"[。！？]|(?<=[.!?])\s")


def _first_sentence(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return _FIRST_SENTENCE_RE.split(line, maxsplit=1)[0]
    return ""


def _is_lesson_section(
    heading: str, section_body: str, reference_headings: list[str] = ()
) -> bool:
    # A project can name hand-off sections (a constants block, remarks for
    # another role) that are never lessons even when they mention an error.
    folded = heading.casefold()
    if any(marker.casefold() in folded for marker in reference_headings if marker):
        return False
    return bool(
        LESSON_SIGNAL_RE.search(heading) or LESSON_SIGNAL_RE.search(_first_sentence(section_body))
    )


def parse_adr(source_name: str, affinity: list[str], repo: "Path | SourceTree", glob: str) -> list[dict[str, Any]]:
    tree = _as_tree(repo)
    entries = []
    for relative_path in tree.files(glob):
        path = Path(relative_path)
        try:
            text = tree.read(relative_path)
        except OSError:
            continue
        title, sections = _extract_markdown_h2_sections(text)
        by_heading = dict(sections)
        decision = by_heading.get("Decision", "")
        if not decision:
            continue
        first_line = next((l for l in text.splitlines() if l.strip()), "")
        status_line = next(
            (l.strip("- ").strip() for l in text.splitlines() if l.strip().startswith("- Status")),
            "",
        )
        metadata = _base_metadata(
            source_name,
            affinity,
            _entry_id(source_name, f"adr-{path.stem}"),
            "decision",
            ["adr"],
            tree,
            relative_path,
            title or path.stem,
        )
        body = (
            f"## Decision\n\n{_bounded_excerpt(decision)}\n\n"
            f"## Rationale\n\n{_bounded_excerpt(by_heading.get('Context', ''))}\n\n"
            f"## Constraints\n\n{status_line or 'See the ADR for status.'}\n\n"
            f"## Revisit When\n\n"
            f"{_bounded_excerpt(by_heading.get('未涵蓋', by_heading.get('Consequences', '')))}\n\n"
            f"## Evidence\n\n{relative_path}\n"
        )
        entries.append({"metadata": metadata, "body": body})
    return entries


def _option(parser: dict[str, Any], key: str) -> str:
    value = parser.get(key, "")
    return value if isinstance(value, str) else ""


def settlement_findings(repo: "Path | SourceTree", parser: dict[str, Any]) -> list[dict[str, Any]]:
    """Read one settlement glob into flat findings, using only configured keys.

    A finding is a method consequence, an erratum, or a missed prediction; a
    hit is kept too, but only so a miss group can report its counterevidence.
    Nothing is grouped here; `derive_candidates` does that across all files.
    """
    findings: list[dict[str, Any]] = []
    subject_key = _option(parser, "subject_key")
    version_key = _option(parser, "version_key")
    consequences_key = _option(parser, "consequences_key")
    errata_key = _option(parser, "errata_key")
    reason_key = _option(parser, "errata_reason_key")
    field_key = _option(parser, "errata_field_key")
    claim_flag_key = _option(parser, "errata_claim_flag_key")
    group_key = _option(parser, "group_key")
    fallback_values = {str(value) for value in parser.get("group_key_fallback_values", [])}
    predictions_key = _option(parser, "predictions_key")
    outcome_key = _option(parser, "prediction_outcome_key")
    miss_value = _option(parser, "prediction_miss_value")
    hit_value = _option(parser, "prediction_hit_value")
    claim_key = _option(parser, "prediction_claim_key")
    provenance_key = _option(parser, "prediction_provenance_key")
    house_key = _option(parser, "prediction_house_key")
    actual_key = _option(parser, "prediction_actual_key")
    error_keys = [key for key in parser.get("prediction_error_keys", []) if key]

    tree = _as_tree(repo)
    for relative_path in tree.files(parser["glob"]):
        path = Path(relative_path)
        try:
            settlement = json.loads(tree.read(relative_path))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(settlement, dict):
            continue
        subject = (str(settlement.get(subject_key, "")) if subject_key else "") or path.parent.name
        version = (str(settlement.get(version_key, "")) if version_key else "") or path.stem
        base = {"path": relative_path, "subject": subject, "version": version}

        consequences = settlement.get(consequences_key) if consequences_key else None
        if isinstance(consequences, dict):
            for key, value in consequences.items():
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                if text.strip():
                    findings.append({**base, "category": "method", "anchor": str(key), "text": text})
        elif isinstance(consequences, str) and consequences.strip():
            # A free-text consequence is as much a finding as a keyed one.
            findings.append({**base, "category": "method", "anchor": "method", "text": consequences})

        errata = settlement.get(errata_key) if errata_key else None
        for position, erratum in enumerate(errata if isinstance(errata, list) else []):
            if isinstance(erratum, dict):
                reason = str(erratum.get(reason_key, "")) if reason_key else ""
                field = str(erratum.get(field_key, "")) if field_key else ""
                text = reason or json.dumps(erratum, ensure_ascii=False)
                claim = bool(erratum.get(claim_flag_key)) if claim_flag_key else False
                group_value = erratum.get(group_key) if group_key else None
            else:
                reason = text = str(erratum)
                field, claim, group_value = "", False, None
            if not text.strip():
                continue
            findings.append(
                {
                    **base,
                    "category": "errata",
                    # The erratum's own field path identifies it; its position
                    # is only a fallback for an erratum that names no field.
                    "anchor": f"errata:{field}" if field else f"errata#{position}",
                    "text": text,
                    "reason": reason or text,
                    "claim_flag": claim,
                    "group_value": (
                        str(group_value)
                        if group_value not in (None, "") and str(group_value) not in fallback_values
                        else ""
                    ),
                }
            )

        predictions = settlement.get(predictions_key) if predictions_key else None
        if not outcome_key:
            continue
        for prediction in predictions if isinstance(predictions, list) else []:
            if not isinstance(prediction, dict):
                continue
            outcome = str(prediction.get(outcome_key, ""))
            if outcome not in {value for value in (miss_value, hit_value) if value}:
                continue
            claim = str(prediction.get(claim_key, "")) if claim_key else ""
            claim = claim or "prediction"
            provenance = (
                str(prediction.get(provenance_key) or "") if provenance_key else ""
            ) or "unspecified"
            parts = [f"claim: {claim}"]
            for label, key in (("house", house_key), ("actual", actual_key)):
                if key and prediction.get(key) is not None:
                    parts.append(f"{label}: {prediction[key]}")
            for key in error_keys:
                if prediction.get(key) is not None:
                    parts.append(f"{key}: {prediction[key]}")
            findings.append(
                {
                    **base,
                    "category": "miss" if outcome == miss_value else "hit",
                    "anchor": f"miss:{claim}",
                    "text": "; ".join(parts),
                    "provenance": provenance,
                    "claim_key": _normalize_claim(claim),
                }
            )
    return findings


# --- Candidate grouping ---------------------------------------------------
#
# Deterministic and literal on purpose: a finding joins a group only on an
# exact structural key, identical normalized text, or an explicit reference to
# another settlement by name. A paraphrase of the same root cause is not
# caught; a project that wants those grouped can supply `group_key`.

_PERIOD_RE = re.compile(r"\bQ[1-4]\s*FY\s*\d+\b|\bFY\s*\d+\b|\b[1-4]Q\s*\d+\b", re.IGNORECASE)
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
_SENTENCE_END_RE = re.compile(r"[。；;]|\.(?=\s+[A-Z])|\.$")
# An erratum that only points back at the one before it in the same file.
_ANAPHORA_RE = re.compile(r"^(?:the\s+)?same\b|^同上|^同一個根因|^同樣的", re.IGNORECASE)
_MIN_SHARED_LATIN = 40
_MIN_SHARED_CJK = 15


def _normalize_claim(claim: str) -> str:
    text = _PAREN_RE.sub(" ", unicodedata.normalize("NFKC", claim))
    text = _PERIOD_RE.sub(" ", text)
    return " ".join(text.casefold().split())


def _first_reason_sentence(reason: str) -> str:
    text = " ".join(unicodedata.normalize("NFKC", reason).split())
    return _SENTENCE_END_RE.split(text, maxsplit=1)[0].strip().casefold()


def _long_enough_to_share(text: str) -> bool:
    cjk = len(re.findall(r"[㐀-鿿]", text))
    latin = len(re.findall(r"[a-z0-9]", text))
    return cjk >= _MIN_SHARED_CJK or latin >= _MIN_SHARED_LATIN


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[Any, Any] = {}

    def find(self, key: Any) -> Any:
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def attach(self, child: Any, parent: Any) -> None:
        """Merge child's group into parent's; the parent's root stays root."""
        child_root, parent_root = self.find(child), self.find(parent)
        if child_root != parent_root:
            self.parent[child_root] = parent_root


def derive_candidates(
    source_name: str,
    affinity: list[str],
    repo: "Path | SourceTree",
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group settlement findings by root cause and emit one lesson per group.

    A group that recurs in two or more reports is marked a candidate for
    generalization. All of this is derived and recomputed on every sync; no
    canonical record is written.
    """
    groups: dict[Any, list[dict[str, Any]]] = {}
    hits: dict[Any, list[dict[str, Any]]] = {}
    last_errata_key: dict[str, Any] = {}
    for finding in findings:
        category = finding["category"]
        if category == "hit":
            hits.setdefault(("miss", finding["provenance"], finding["claim_key"]), []).append(finding)
            continue
        if category == "miss":
            key = ("miss", finding["provenance"], finding["claim_key"])
        elif category == "method":
            key = ("method", finding["path"], finding["anchor"])
        elif finding["group_value"]:
            key = ("class", finding["group_value"])
        elif _ANAPHORA_RE.match(finding["reason"].strip()) and finding["path"] in last_errata_key:
            key = last_errata_key[finding["path"]]
        else:
            key = ("text", finding["path"], _first_reason_sentence(finding["reason"]))
        if category == "errata":
            last_errata_key[finding["path"]] = key
        groups.setdefault(key, []).append(finding)

    union = _UnionFind()
    for key in groups:
        union.find(key)
    # Identical, sufficiently specific first sentences in different reports
    # are one root cause; the earlier file's group stays the root.
    by_sentence: dict[str, Any] = {}
    for key in groups:
        if key[0] == "text" and _long_enough_to_share(key[2]):
            if key[2] in by_sentence:
                union.attach(key, by_sentence[key[2]])
            else:
                by_sentence[key[2]] = key
    # An erratum that names another report by subject and version inherits
    # that report's error, provided the named report has one errata group.
    errata_roots_by_report: dict[tuple[str, str], set[Any]] = {}
    for key, members in groups.items():
        if members[0]["category"] == "errata":
            errata_roots_by_report.setdefault(
                (members[0]["subject"], members[0]["version"]), set()
            ).add(key)
    for key, members in groups.items():
        if members[0]["category"] != "errata":
            continue
        own = (members[0]["subject"], members[0]["version"])
        reasons = " ".join(member["reason"] for member in members)
        for report, keys in sorted(errata_roots_by_report.items()):
            if report == own or len(keys) != 1:
                continue
            target = next(iter(keys))
            # A source's own classes are authoritative: two classified groups
            # stay apart even when one report names the other.
            if key[0] == "class" and target[0] == "class":
                continue
            if f"{report[0]} {report[1]}" in reasons:
                union.attach(key, target)

    # The root group's own members come first: its first finding supplies the
    # group's id, origin, and representative text.
    merged: dict[Any, list[dict[str, Any]]] = {}
    for key, members in groups.items():
        root = union.find(key)
        if key == root:
            merged[root] = [*members, *merged.get(root, [])]
        else:
            merged.setdefault(root, []).extend(members)

    known_reports = {(finding["subject"], finding["version"]) for finding in findings}
    entries = []
    for root_key, members in merged.items():
        entries.append(
            _group_entry(
                source_name, affinity, repo, root_key, members, hits.get(root_key, []), known_reports
            )
        )
    return entries


def _referenced_reports(
    members: list[dict[str, Any]], candidates: set[tuple[str, str]]
) -> set[tuple[str, str]]:
    """Reports among `candidates` that these members' reasons name explicitly."""
    text = " ".join(str(member.get("reason", "")) for member in members)
    own = {(member["subject"], member["version"]) for member in members}
    return {
        report
        for report in candidates
        if report not in own and f"{report[0]} {report[1]}" in text
    }


def _choose_representative(
    reports: list[tuple[str, str]],
    by_report: dict[tuple[str, str], list[dict[str, Any]]],
    known_reports: set[tuple[str, str]],
) -> tuple[tuple[str, str], dict[tuple[str, str], set[tuple[str, str]]]]:
    """The report that best shows what the group's error is, by weight.

    A report's weight is its own findings in the group plus the findings of
    every report that names it (its inheritors), so a source always outweighs
    each report inheriting from it, while a large unrelated report can still
    represent the group: its findings are where the error shows most. Ties go
    to a report that names no other report, then to file order.
    """
    in_group = set(reports)
    inherits = {report: _referenced_reports(by_report[report], in_group) for report in reports}
    weight = {
        report: len(by_report[report])
        + sum(len(by_report[other]) for other in reports if report in inherits[other])
        for report in reports
    }
    ranked = sorted(
        reports,
        key=lambda report: (
            -weight[report],
            1 if _referenced_reports(by_report[report], known_reports) else 0,
            reports.index(report),
        ),
    )
    return ranked[0], inherits


def _group_entry(
    source_name: str,
    affinity: list[str],
    repo: Path,
    root_key: Any,
    members: list[dict[str, Any]],
    hits: list[dict[str, Any]],
    known_reports: set[tuple[str, str]] = frozenset(),
) -> dict[str, Any]:
    # The root group's first finding fixes the id, so a change in which report
    # represents the group never renames it.
    first = members[0]
    category = first["category"]
    by_report: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for member in members:
        by_report.setdefault((member["subject"], member["version"]), []).append(member)
    representative_report, inherits = _choose_representative(
        list(by_report), by_report, known_reports
    )
    representative = by_report[representative_report][0]
    # List the source report first, then the rest in file order.
    ordered = [*by_report[representative_report]] + [
        member for member in members if (member["subject"], member["version"]) != representative_report
    ]
    reports: list[tuple[str, str]] = []
    observations: list[dict[str, Any]] = []
    for member in ordered:
        report = (member["subject"], member["version"])
        if report not in reports:
            reports.append(report)
            observations.append(
                {
                    "subject": report[0],
                    "version": report[1],
                    "anchors": [],
                    "n_fields": 0,
                    "n_claim_flagged": 0,
                }
            )
        observation = observations[reports.index(report)]
        observation["anchors"].append(member["anchor"])
        observation["n_fields"] += 1
        observation["n_claim_flagged"] += int(bool(member.get("claim_flag")))
    for observation, report in zip(observations, reports):
        if inherits[report]:
            observation["inherits_from"] = [f"{s} {v}" for s, v in sorted(inherits[report])]
    subjects = sorted({subject for subject, _ in reports})
    applies_to = ",".join(subjects)

    if root_key[0] == "miss":
        slug = f"miss-{root_key[1]}-{root_key[2]}"
    elif root_key[0] == "class":
        slug = f"errata-class-{root_key[1]}"
    else:
        slug = f"{category}-{first['subject']}-{first['version']}"
    digest = hashlib.sha256(repr(root_key).encode("utf-8")).hexdigest()[:8]
    metadata = _base_metadata(
        source_name,
        affinity,
        _entry_id(source_name, f"group-{slug}-{digest}"),
        "lesson",
        ["settlement", *[subject.casefold() for subject in subjects]],
        repo,
        representative["path"],
        representative["anchor"],
    )
    metadata["applies_to"] = applies_to
    metadata["lesson_category"] = category
    metadata["members"] = [{"path": m["path"], "anchor": m["anchor"]} for m in members]
    metadata["observations"] = observations
    # A report that names another in the group repeats that report's error;
    # it is counted, but shown as inherited rather than as independent.
    metadata["recurrence"] = {
        "reports": len(reports),
        "subjects": len(subjects),
        "inherited": sum(1 for report in reports if inherits[report]),
    }
    metadata["candidate"] = len(reports) >= 2
    if category == "miss":
        metadata["counter"] = {"misses": len(members), "hits": len(hits)}

    lines = []
    for observation in observations:
        line = f"- {observation['subject']} {observation['version']}: {observation['n_fields']}"
        if observation["n_claim_flagged"]:
            line += f" ({observation['n_claim_flagged']} flagged as changing a claim)"
        if observation.get("inherits_from"):
            line += f"; inherited from {', '.join(observation['inherits_from'])}"
        lines.append(line)
    if category == "miss":
        lines.append(f"- hits with the same input and claim: {len(hits)}")
    evidence = ", ".join(dict.fromkeys(member["path"] for member in ordered))
    finding = f"{representative['subject']} {representative['version']} — {representative['text']}"
    body = _lesson_body(finding, applies_to, representative["anchor"], evidence, "\n".join(lines))
    return {"metadata": metadata, "body": body}


def parse_markdown_note(
    source_name: str,
    affinity: list[str],
    repo: "Path | SourceTree",
    glob: str,
    reference_headings: list[str] = (),
    heading_level: str = "2",
) -> list[dict[str, Any]]:
    tree = _as_tree(repo)
    entries = []
    for relative_path in tree.files(glob):
        path = Path(relative_path)
        try:
            text = tree.read(relative_path)
        except OSError:
            continue
        title, sections = _extract_markdown_sections(text, int(heading_level))
        for index, (heading, section_body) in enumerate(sections):
            if not section_body.strip():
                continue
            kind = (
                "lesson"
                if _is_lesson_section(heading, section_body, reference_headings)
                else "note"
            )
            metadata = _base_metadata(
                source_name,
                affinity,
                # The slug strips non-ASCII characters, and headings here are
                # often entirely CJK, which would otherwise collapse every
                # section in a note to the same id. The index guarantees
                # uniqueness independent of what survives slugging.
                _entry_id(source_name, f"note-{path.stem}-{index}-{heading}"),
                kind,
                ["research-note"],
                tree,
                relative_path,
                heading,
            )
            text = f"{title} — {heading}: {section_body}"
            if kind == "lesson":
                metadata["applies_to"] = "all"
                metadata["lesson_category"] = "note"
                body = _lesson_body(text, "all", heading, f"{relative_path}#{heading}")
            else:
                body = _note_body(text, f"{relative_path}#{heading}")
            entries.append({"metadata": metadata, "body": body})
    return entries


def parse_git_log(
    source_name: str, affinity: list[str], repo: "Path | SourceTree", since: str
) -> list[dict[str, Any]]:
    tree = _as_tree(repo)
    repo = tree.repo
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "log",
            *([tree.ref] if tree.from_ref and tree.ref else []),
            f"--since={since}",
            "--format=%H%x1f%ad%x1f%s",
            "--date=short",
            "-i",
            "-E",
            "--grep",
            r"trap|lesson|陷阱|教訓|settle|結算",
        ], env=git_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    entries = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, date, subject = parts
        metadata = _base_metadata(
            source_name,
            affinity,
            _entry_id(source_name, f"commit-{sha[:12]}"),
            # A commit subject says what changed, not what was learned; it is
            # searchable reference material and never a lesson.
            "note",
            ["commit-message"],
            repo,
            "",
            sha[:12],
        )
        metadata["origin"]["blob_sha256"] = sha
        body = _note_body(f"{date} {subject}", f"commit {sha[:12]}")
        entries.append({"metadata": metadata, "body": body})
    return entries


def collect_source_entries(config: dict[str, Any], tree: "SourceTree | None" = None) -> list[dict[str, Any]]:
    tree = tree or SourceTree(config["repo"], config.get("ref"))
    entries: list[dict[str, Any]] = []
    # Settlement findings from every settlement parser are grouped together,
    # so a recurrence across two globs of one source is still one recurrence.
    findings: list[dict[str, Any]] = []
    for parser in config["parsers"]:
        kind = parser["kind"]
        if kind == "adr":
            entries.extend(
                parse_adr(config["name"], config["affinity"], tree, parser["glob"])
            )
        elif kind == "settlement":
            findings.extend(settlement_findings(tree, parser))
        elif kind == "markdown-note":
            entries.extend(
                parse_markdown_note(
                    config["name"],
                    config["affinity"],
                    tree,
                    parser["glob"],
                    parser.get("reference_headings", []),
                    parser.get("heading_level", "2"),
                )
            )
        elif kind == "git-log":
            since = parser.get("since", "1970-01-01")
            entries.extend(
                parse_git_log(config["name"], config["affinity"], tree, since)
            )
    if findings:
        entries.extend(
            derive_candidates(config["name"], config["affinity"], tree, findings)
        )
    return entries


def orientation_summary(config: dict[str, Any]) -> str:
    """A one-line hint pointing at a repo's own knowledge, not duplicating it."""
    repo = config["repo"]
    parts = []
    for hint in config["orientation"]:
        target = repo / hint
        if target.is_file():
            parts.append(hint)
        elif target.is_dir():
            count = sum(1 for _ in target.rglob("*") if _.is_file())
            parts.append(f"{count} {hint.rstrip('/')}")
        else:
            parts.append(hint)
    joined = "、".join(parts)
    return f"此 repo 有 {joined}；寫報告前先讀。"


# --- Write-side trigger --------------------------------------------------
#
# Sync is always explicit: a manual `sources sync <name>`, or a repo's own
# post-commit hook calling it. This module never installs anything inside a
# source repo except that one hook file, which git itself excludes from
# version control, so nothing here touches a tracked file.

_HOOK_MARKER_START = "# agent-memory-sources-sync:start"
_HOOK_MARKER_END = "# agent-memory-sources-sync:end"


def install_source_hook(
    config_root: Path, root: Path, memoryctl_path: Path, name: str
) -> Path:
    """Add a post-commit line that resyncs one source, without blocking commits.

    Idempotent: reinstalling replaces only the marked block. Any existing
    hook content outside the markers is preserved and backed up first.
    """
    config = load_source_config(config_root, name)
    repo = config["repo"]
    hooks_dir = repo / ".git" / "hooks"
    if not hooks_dir.is_dir():
        raise FederationConfigError(f"not a git repository: {repo}")
    hook_path = hooks_dir / "post-commit"
    existing = hook_path.read_text(encoding="utf-8") if hook_path.is_file() else ""

    block = (
        f"{_HOOK_MARKER_START}\n"
        # The hook's own repository is unset first (ruling 52 a); the program
        # also clears it before every git call, so an older line stays safe.
        f"( env {' '.join(f'-u {name_}' for name_ in HOOK_GIT_VARIABLES)} "
        f'"{memoryctl_path}" --home "{root}" sources sync "{name}" '
        f'>/dev/null 2>&1 & ) || true\n'
        f"{_HOOK_MARKER_END}\n"
    )
    pattern = re.compile(
        re.escape(_HOOK_MARKER_START) + r".*?" + re.escape(_HOOK_MARKER_END) + r"\n?",
        re.DOTALL,
    )
    if pattern.search(existing):
        merged = pattern.sub(block, existing)
    else:
        prefix = existing if existing.strip() else "#!/bin/sh\n"
        merged = prefix.rstrip("\n") + "\n\n" + block if prefix.strip() else prefix + block

    if hook_path.is_file():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = config_root / "adapter-backups" / f"{stamp}-{os.getpid()}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_dir.chmod(0o700)
        shutil.copy2(hook_path, backup_dir / "post-commit")

    hook_path.write_text(merged, encoding="utf-8")
    hook_path.chmod(0o700)
    return hook_path
