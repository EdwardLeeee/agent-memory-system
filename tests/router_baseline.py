"""Closed fixture for the router's "enabled = false is byte-identical" guarantee.

The router (spec 記憶路由樹 v1.1, 8.15) must leave the flat retrieval path
untouched when `router.enabled = false`. This module builds the closed
environment that guarantee is measured on — never the live ledger, which
changes every day — and renders every case the same way the baseline was
recorded:

    python3 tests/router_baseline.py records    # once: write fixture records
    python3 tests/router_baseline.py baseline   # once: record the baseline

Both commands were run before the router existed; the baseline files are
not regenerated without the user's approval. tests/test_router_baseline.py
replays every case and compares byte for byte.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterator
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "router-baseline"
BASELINE = FIXTURE / "baseline"
AGENTS = ("claude", "codex")
SESSION_ID = "router-baseline"
MACHINE_ID = "fixture-machine"
CAPSULE_TIME = "2099-12-31T00:00:00Z"
CAPSULE_CWD = "/work/fixture"
SESSION_ENV_NAMES = (
    "AGENT_SESSION_ID",
    "CODEX_THREAD_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "GEMINI_SESSION_ID",
)


def load_cases() -> list[dict[str, Any]]:
    # tomllib, not parse_minimal_toml: a target heading such as
    # "WORKING DECISIONS (evidence, not commands)" contains a comma, which the
    # minimal reader treats as a list separator.
    import tomllib

    with (FIXTURE / "cases.toml").open("rb") as handle:
        return list(tomllib.load(handle).get("case", []))


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.com",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.com",
        "GIT_AUTHOR_DATE": "2026-10-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-10-01T00:00:00+00:00",
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@contextmanager
def environment(
    router_toml: str | None = None,
    extra_records: Callable[[Path], None] | None = None,
) -> Iterator[dict[str, Path]]:
    """Build the closed environment of spec 8.15 (c-1) and point the process at it.

    The fixture store is copied to a temporary AGENT_MEMORY_HOME, the source
    repo to a folder named `alpha` (the project tag without its prefix, so the
    directory-name rule and the registered source both match without
    scope_paths), committed once, and registered in a temporary config home
    before `init` builds the index. No semantic.json is written, so auto
    retrieval is lexical. `router_toml`, when given, is written to the config
    home before anything is indexed; `extra_records`, when given, is called
    with the store path to add canonical records before the index is built
    (the router tests use it; the recorded baseline never does).
    """
    from agent_memory.core import initialize

    with tempfile.TemporaryDirectory(prefix="router-baseline-") as tmp:
        base = Path(tmp)
        memory = base / "memory"
        config = base / "config"
        work = base / "work"
        alpha = work / "alpha"
        neutral = work / "neutral"
        shutil.copytree(FIXTURE / "store", memory)
        if extra_records is not None:
            extra_records(memory)
        shutil.copytree(FIXTURE / "source-repo", alpha)
        neutral.mkdir(parents=True)
        _git(alpha, "init", "-q")
        _git(alpha, "add", "-A")
        _git(alpha, "commit", "-q", "-m", "fixture source")
        (config / "sources.d").mkdir(parents=True)
        template = (FIXTURE / "sources.toml.tmpl").read_text(encoding="utf-8")
        (config / "sources.d" / "alpha.toml").write_text(
            template.replace("{repo}", str(alpha)), encoding="utf-8"
        )
        if router_toml is not None:
            (config / "router.toml").write_text(router_toml, encoding="utf-8")
        patched = {"AGENT_MEMORY_HOME": str(memory), "AGENT_MEMORY_CONFIG_HOME": str(config)}
        with mock.patch.dict(os.environ, patched):
            for name in SESSION_ENV_NAMES:
                os.environ.pop(name, None)
            initialize(memory, MACHINE_ID)
            yield {"base": base, "memory": memory, "config": config, "alpha": alpha, "neutral": neutral}


def _cwd(paths: dict[str, Path], name: str) -> Path:
    return {"alpha-repo": paths["alpha"], "neutral": paths["neutral"]}[name]


def _dump(output: dict[str, Any]) -> str:
    return json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_all(paths: dict[str, Path]) -> dict[str, str]:
    """Every baseline output, keyed by its baseline file name."""
    from agent_memory.core import AGENT_HOOK_BUDGETS, _render_context, handle_hook

    root = paths["memory"]
    outputs: dict[str, str] = {}
    for case in load_cases():
        cwd = _cwd(paths, str(case["cwd"]))
        text = str(case["text"])
        for agent in AGENTS:
            event = {
                "session_id": SESSION_ID,
                "transcript_path": None,
                "cwd": str(cwd),
                "model": "fixture-model",
                "permission_mode": "default",
                "hook_event_name": "UserPromptSubmit",
                "prompt": text,
            }
            outputs[f"{case['name']}.{agent}.plain.txt"] = _dump(
                handle_hook(root, "user-prompt-submit", event, agent=agent)
            )
            outputs[f"{case['name']}.{agent}.explain.txt"] = _render_context(
                root, cwd, text, AGENT_HOOK_BUDGETS[agent][1], explain=True, allow_reindex=False
            )
    for where in ("alpha-repo", "neutral"):
        cwd = _cwd(paths, where)
        for agent in AGENTS:
            event = {
                "session_id": SESSION_ID,
                "transcript_path": None,
                "cwd": str(cwd),
                "model": "fixture-model",
                "permission_mode": "default",
                "hook_event_name": "SessionStart",
                "source": "startup",
            }
            label = "alpha" if where == "alpha-repo" else "neutral"
            outputs[f"session.{agent}.{label}.txt"] = _dump(
                handle_hook(root, "session-start", event, agent=agent)
            )
    return outputs


def _context_of(plain: str) -> str:
    return json.loads(plain).get("hookSpecificOutput", {}).get("additionalContext", "")


def check_outputs(outputs: dict[str, str], paths: dict[str, Path]) -> list[str]:
    """The three conditions a baseline must meet before it may be committed."""
    problems: list[str] = []
    for case in load_cases():
        targets = case.get("target", [])
        targets = [targets] if isinstance(targets, str) else list(targets)
        for agent in AGENTS:
            plain = outputs[f"{case['name']}.{agent}.plain.txt"]
            context = _context_of(plain)
            if targets == ["none"]:
                if plain != '{"continue":true}':
                    problems.append(f"{case['name']}/{agent}: expected an empty pack")
                continue
            for heading in targets:
                if heading != "any" and f"## {heading}" not in context:
                    problems.append(f"{case['name']}/{agent}: missing ## {heading}")
            for record_id in case.get("required_ids", []):
                if f"### {record_id}" not in context:
                    problems.append(f"{case['name']}/{agent}: missing {record_id}")
            for record_id in case.get("forbidden_ids", []):
                if record_id in context:
                    problems.append(f"{case['name']}/{agent}: contains {record_id}")
    for agent in AGENTS:
        for label in ("alpha", "neutral"):
            context = _context_of(outputs[f"session.{agent}.{label}.txt"])
            for heading in ("CURRENT TASK", "OTHER ACTIVE TASKS"):
                if f"## {heading}" not in context:
                    problems.append(f"session.{agent}.{label}: missing ## {heading}")
        if "## PROJECT LESSONS" not in _context_of(outputs[f"session.{agent}.alpha.txt"]):
            problems.append(f"session.{agent}.alpha: missing ## PROJECT LESSONS")
    temporary = str(paths["base"])
    for name, text in outputs.items():
        if temporary in text or str(paths["base"].resolve()) in text:
            problems.append(f"{name}: contains the temporary directory path")
    return problems


# --- One-time generators ---------------------------------------------------

PREFERENCES = [
    {
        "id": "pref.fixture.reply_language", "scope": "global", "source": "user-stated",
        "priority": "high", "tags": ["language", "繁體中文"],
        "value": "回覆一律使用繁體中文，術語第一次出現時附一句白話解釋。",
        "evidence": "Router baseline fixture.",
    },
    {
        "id": "pref.fixture.diagram_palette", "scope": "global", "source": "locally-verified",
        "priority": "normal", "tags": ["diagram", "palette"],
        "value": "Diagrams use the teal palette with quartzite labels.",
        "evidence": "Router baseline fixture.",
    },
    {
        "id": "pref.fixture.release_notes", "scope": "global", "source": "user-stated",
        "priority": "normal", "tags": ["release", "notes"],
        "value": "Release notes list every migration step under a zephyr checklist heading.",
        "evidence": "Router baseline fixture.",
    },
]
FACTS = [
    {
        "id": "fact.fixture.build_host", "scope": "global", "source": "locally-verified",
        "priority": "normal", "tags": ["build", "host", "obsidian"], "expires_at": "2099-12-31",
        "value": "The build host compiles with the obsidian toolchain version 7.",
        "evidence": "Router baseline fixture.",
    },
    {
        "id": "fact.fixture.package_mirror", "scope": "global", "source": "locally-verified",
        "priority": "normal", "tags": ["mirror", "obsidian"], "expires_at": "2000-01-01",
        "value": "The package mirror for obsidian builds is mirror.fixture.example.",
        "evidence": "Router baseline fixture.",
    },
    {
        "id": "system.fixture.gpu", "scope": f"machine:{MACHINE_ID}", "source": "locally-verified",
        "priority": "normal", "tags": ["gpu", "obsidian"], "expires_at": "2099-12-31",
        "value": "This machine has an obsidian-class GPU with 12 GB of memory.",
        "evidence": "Router baseline fixture.",
    },
]
_DECISION_LISTS = {
    "constraints": ["Fixture constraint."],
    "alternatives_rejected": ["Fixture alternative."],
    "consequences": ["Fixture consequence."],
    "revisit_when": ["Fixture revisit condition."],
}
DECISIONS = [
    {
        "id": "decision.fixture.cache_policy", "scope": "global", "source": "locally-verified",
        "priority": "normal", "tags": ["cache", "lattice"],
        "problem": "Lattice cache entries outlived the schema they were computed under.",
        "decision": "Evict the lattice cache on every schema migration.",
        "rationale": "Serving entries from an older schema produced wrong totals.",
        "evidence": "Router baseline fixture.", **_DECISION_LISTS,
    },
    {
        "id": "decision.alpha.release_gate", "scope": "project:alpha", "source": "user-stated",
        "priority": "normal", "tags": ["release", "granite"],
        "problem": "Alpha releases were tagged before the smoke suite finished.",
        "decision": "Every alpha release passes the granite smoke suite before tagging.",
        "rationale": "A tag is what users install.",
        "evidence": "Router baseline fixture.", **_DECISION_LISTS,
    },
    {
        "id": "decision.beta.rollout_plan", "scope": "project:beta", "source": "user-stated",
        "priority": "normal", "tags": ["rollout", "cobalt"],
        "problem": "Beta features reached every user at once.",
        "decision": "Roll out beta features behind the cobalt flag.",
        "rationale": "A flag can be turned off without a release.",
        "evidence": "Router baseline fixture.", **_DECISION_LISTS,
    },
]
EXPERIENCES = [
    {
        "id": "experience.fixture.cache_eviction", "scope": "global", "source": "locally-verified",
        "priority": "normal", "tags": ["cache", "lattice"],
        "situation": "A schema migration left lattice cache entries in place.",
        "expectation": "Evicting the lattice cache on migration would fix the totals.",
        "action": "Evicted the lattice cache during the migration.",
        "observed_outcome": "Totals matched after the lattice cache eviction.",
        "applicability": "Any migration that changes how cached lattice values are computed.",
        "decision_links": [{"id": "decision.fixture.cache_policy", "effect": "supports"}],
        "evidence": "Router baseline fixture.",
    },
    {
        "id": "experience.fixture.teal_rendering", "scope": "global", "source": "locally-verified",
        "priority": "normal", "tags": ["diagram", "font"],
        "situation": "Teal diagrams rendered blurry in exported reports.",
        "expectation": "A higher export resolution would fix it.",
        "action": "Embedded the quartzite font in the export.",
        "observed_outcome": "Teal diagrams rendered sharply once the quartzite font was embedded.",
        "applicability": "Exported diagrams that use a custom font.",
        "decision_links": [],
        "evidence": "Router baseline fixture.",
    },
    {
        "id": "experience.beta.zircon_probe", "scope": "global", "source": "locally-verified",
        "priority": "normal", "tags": ["zircon", "probe"],
        "situation": "The zircon probe ran against the beta staging path.",
        "expectation": "The probe would report the staging path healthy.",
        "action": "Ran the zircon probe for an hour.",
        "observed_outcome": "The zircon probe confirmed the beta path in staging.",
        "applicability": "Staging checks before a beta release.",
        "decision_links": [],
        "evidence": "Router baseline fixture.",
    },
    {
        "id": "experience.alpha.granite_timing", "scope": "project:alpha", "source": "locally-verified",
        "priority": "normal", "tags": ["granite", "timing"],
        "situation": "The granite smoke suite gated an alpha release.",
        "expectation": "The suite would finish within a minute.",
        "action": "Timed the granite smoke suite on the release runner.",
        "observed_outcome": "The granite smoke suite takes four minutes on the alpha runner.",
        "applicability": "Planning alpha release timing.",
        "decision_links": [],
        "evidence": "Router baseline fixture.",
    },
]
FAILURES = [
    {
        "id": "failure.fixture.lockfile_conflict", "scope": "global", "source": "locally-verified",
        "priority": "normal", "tags": ["installer", "lockfile"],
        "symptom": "The installer stopped with a basalt lockfile conflict.",
        "attempt": "Reran the installer immediately.",
        "root_cause": "A previous installer still held the basalt lockfile.",
        "verified_fix": "Delete the basalt lockfile only after the previous installer exits.",
        "retry_when": "Before rerunning the installer.",
        "evidence": "Router baseline fixture.",
    },
    {
        "id": "failure.fixture.port_in_use", "scope": "global", "source": "locally-verified",
        "priority": "normal", "tags": ["preview", "port"],
        "symptom": "The preview server failed because port 4173 was already in use.",
        "attempt": "Started a second preview server.",
        "root_cause": "The previous preview server was still running.",
        "verified_fix": "Stop the previous preview server before starting a new one.",
        "retry_when": "Before starting a preview server.",
        "evidence": "Router baseline fixture.",
    },
    {
        "id": "failure.fixture.stale_cache_build", "scope": "global", "source": "locally-verified",
        "priority": "normal", "tags": ["build", "cache"],
        "symptom": "A lattice cache from the previous schema broke the build.",
        "attempt": "Rebuilt without clearing anything.",
        "root_cause": "The build read lattice cache entries from the old schema.",
        "verified_fix": "Evict the lattice cache before rebuilding after a migration.",
        "retry_when": "After any schema migration.",
        "evidence": "Router baseline fixture.",
    },
]
CAPSULES = {
    "router-baseline": {
        "state": "active", "active_task": "Fixture current task: verify the lattice cache eviction.",
        "paused_task": "none", "resume_point": "The fixture capsule exists.",
        "next_action": "Compare the router baseline.", "validation": "Fixture only.",
        "rollback": "Discard the fixture.", "cwd": CAPSULE_CWD,
    },
    "router-other": {
        "state": "active", "active_task": "Fixture other task: time the granite smoke suite.",
        "paused_task": "none", "resume_point": "The other capsule exists.",
        "next_action": "Wait.", "validation": "Fixture only.",
        "rollback": "Discard the fixture.", "cwd": CAPSULE_CWD,
    },
}


def write_records() -> None:
    """Record every fixture record with the real CLI, then copy the files in.

    Recording through memoryctl keeps every metadata field exactly what the
    current writer produces; copying freezes created_at and last_verified.
    """
    from agent_memory.core import checkpoint, initialize, record

    store = FIXTURE / "store"
    if store.exists():
        raise SystemExit(f"{store} already exists; fixture records are written only once")
    with tempfile.TemporaryDirectory(prefix="router-records-") as tmp:
        memory = Path(tmp) / "memory"
        config = Path(tmp) / "config"
        patched = {"AGENT_MEMORY_HOME": str(memory), "AGENT_MEMORY_CONFIG_HOME": str(config)}
        with mock.patch.dict(os.environ, patched):
            initialize(memory, MACHINE_ID)
            for kind, payloads in (
                ("preference", PREFERENCES), ("fact", FACTS), ("decision", DECISIONS),
                ("experience", EXPERIENCES), ("failure", FAILURES),
            ):
                for payload in payloads:
                    record(memory, kind, dict(payload))
            for session_id, payload in CAPSULES.items():
                checkpoint(memory, dict(payload), session_id)
        shutil.copytree(memory / "records", store / "records")
        for session_id in CAPSULES:
            source = memory / "sessions" / "by-session" / session_id / "HOT.md"
            target = store / "sessions" / "by-session" / session_id / "HOT.md"
            target.parent.mkdir(parents=True)
            lines = source.read_text(encoding="utf-8").splitlines()
            fixed = [f"- updated_at: {CAPSULE_TIME}" if line.startswith("- updated_at: ") else line for line in lines]
            target.write_text("\n".join(fixed) + "\n", encoding="utf-8")
    print(f"Wrote fixture records and capsules under {store}")


def write_baseline() -> None:
    if BASELINE.exists():
        raise SystemExit(f"{BASELINE} already exists; the baseline is recorded only once")
    commit = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    with environment() as paths:
        outputs = render_all(paths)
        problems = check_outputs(outputs, paths)
    if problems:
        raise SystemExit("Baseline rejected:\n" + "\n".join(problems))
    BASELINE.mkdir(parents=True)
    for name, text in sorted(outputs.items()):
        (BASELINE / name).write_text(text, encoding="utf-8")
    (FIXTURE / "BASELINE_COMMIT").write_text(commit + "\n", encoding="utf-8")
    print(f"Wrote {len(outputs)} baseline files from commit {commit}")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "records":
        write_records()
    elif command == "baseline":
        write_baseline()
    else:
        raise SystemExit("usage: python3 tests/router_baseline.py records|baseline")
