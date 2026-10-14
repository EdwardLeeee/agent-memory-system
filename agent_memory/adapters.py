from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

from .core import (
    MemoryError,
    SESSION_CONTEXT_SPILL_THRESHOLD,
    atomic_write_text,
    config_home,
)


ADAPTER_START = "<!-- agent-memory-adapter:start -->"
ADAPTER_END = "<!-- agent-memory-adapter:end -->"


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def adapter_text(agent: str) -> str:
    paths = {
        "codex": project_root() / "adapters" / "codex" / "AGENTS.snippet.md",
        "claude": project_root() / "adapters" / "claude" / "CLAUDE.snippet.md",
        "gemini": project_root() / "adapters" / "gemini" / "GEMINI.snippet.md",
        "generic": project_root() / "adapters" / "generic" / "MEMORY_BOOTSTRAP.md",
    }
    try:
        return paths[agent].read_text(encoding="utf-8")
    except KeyError as exc:
        raise MemoryError(f"unsupported adapter: {agent}") from exc


def _backup(path: Path) -> None:
    if not path.exists():
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = (
        config_home()
        / "adapter-backups"
        / f"{stamp}-{os.getpid()}"
        / path.name
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    shutil.copy2(path, target)
    target.chmod(0o600)


def _merge_guidance(existing: str, block: str) -> str:
    pattern = re.compile(
        re.escape(ADAPTER_START) + r".*?" + re.escape(ADAPTER_END), re.DOTALL
    )
    if pattern.search(existing):
        merged = pattern.sub(block.strip(), existing)
    else:
        merged = existing.rstrip() + ("\n\n" if existing.strip() else "") + block.strip()
    return merged.rstrip() + "\n"


def _memory_hook_group(
    event: str, command: str, *, spill_threshold: int | None = None
) -> dict[str, Any]:
    handler: dict[str, Any] = {
        "type": "command",
        "command": command,
        "timeout": 30,
    }
    group: dict[str, Any] = {"hooks": [handler]}
    if event == "SessionStart":
        group["matcher"] = "startup|resume|clear|compact"
        if spill_threshold is not None:
            handler["additionalContextLimit"] = spill_threshold
    elif event == "PreCompact":
        group["matcher"] = "manual|auto"
    elif event == "PreToolUse":
        # Runs before every shell command and blocks it while running; the
        # hook gives up after two seconds itself, this is the outer bound.
        group["matcher"] = "Bash"
        handler["timeout"] = 5
    return group


# SessionStart, UserPromptSubmit, PreCompact, and PreToolUse (shell commands
# only) are the only events this project installs. Stop is deliberately absent: lifecycle completion is not a
# reliable reflection boundary, and Stop continuations create new prompts.
_MEMORY_HOOK_EVENTS = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "user-prompt-submit",
    "PreCompact": "pre-compact",
    "PreToolUse": "pre-tool-use",
}


def _apply_memory_hooks(
    hook_map: dict[str, Any],
    executable: Path,
    *,
    spill_threshold: int | None,
    agent: str,
) -> None:
    """Replace this project's handlers in place, leaving foreign hooks alone."""
    for event in (*_MEMORY_HOOK_EVENTS, "Stop"):
        # Only clean up Stop when the config already mentions it; creating an
        # empty Stop array would add noise to a shared settings file.
        if event == "Stop" and event not in hook_map:
            continue
        cli_event = _MEMORY_HOOK_EVENTS.get(event, "stop")
        current = hook_map.get(event, [])
        if not isinstance(current, list):
            raise MemoryError(f"existing hook event {event} must be an array")
        hook_map[event] = _remove_existing_memory_handlers(current, cli_event)

    for event, cli_event in _MEMORY_HOOK_EVENTS.items():
        hook_map[event].append(
            _memory_hook_group(
                event,
                f"{executable} hook {cli_event} --agent {agent}",
                spill_threshold=spill_threshold,
            )
        )


def _remove_existing_memory_handlers(groups: list[Any], event: str) -> list[Any]:
    kept: list[Any] = []
    needle = f"memoryctl hook {event}"
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        handlers = [
            handler
            for handler in group["hooks"]
            if not (
                isinstance(handler, dict)
                and needle in str(handler.get("command", ""))
            )
        ]
        if handlers:
            copy = dict(group)
            copy["hooks"] = handlers
            kept.append(copy)
    return kept


def install_codex_adapter(agent_home: Path) -> tuple[Path, Path]:
    agent_home = agent_home.expanduser().resolve()
    if not agent_home.exists():
        agent_home.mkdir(parents=True)
        agent_home.chmod(0o700)
    guidance_path = agent_home / "AGENTS.md"
    hooks_path = agent_home / "hooks.json"
    existing_guidance = (
        guidance_path.read_text(encoding="utf-8") if guidance_path.exists() else ""
    )
    merged_guidance = _merge_guidance(existing_guidance, adapter_text("codex"))

    hooks: dict[str, Any]
    if hooks_path.exists():
        try:
            hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MemoryError(f"refusing to replace invalid hooks JSON: {hooks_path}") from exc
        if not isinstance(hooks, dict):
            raise MemoryError("existing hooks.json must contain a JSON object")
        # Codex CLI 0.142 rejects any top-level key besides `hooks` ("unknown
        # field `description`") and then loads no hook at all; earlier
        # installs of this adapter wrote that key.
        hooks.pop("description", None)
    else:
        hooks = {}
    hook_map = hooks.setdefault("hooks", {})
    if not isinstance(hook_map, dict):
        raise MemoryError("existing hooks.json field `hooks` must be an object")

    executable = project_root() / "bin" / "memoryctl"
    _apply_memory_hooks(
        hook_map,
        executable,
        spill_threshold=SESSION_CONTEXT_SPILL_THRESHOLD,
        agent="codex",
    )

    new_hooks = json.dumps(hooks, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if merged_guidance != existing_guidance:
        _backup(guidance_path)
        atomic_write_text(guidance_path, merged_guidance)
    if not hooks_path.exists() or new_hooks != hooks_path.read_text(encoding="utf-8"):
        _backup(hooks_path)
        atomic_write_text(hooks_path, new_hooks)
    return guidance_path, hooks_path


def install_claude_adapter(agent_home: Path) -> tuple[Path, Path]:
    """Install the same silent transport into Claude Code.

    Claude Code keeps hooks inside the shared `settings.json` rather than a
    dedicated hooks file, so this merges into the existing object and must
    preserve unrelated keys such as `model` and `theme`. It also omits Codex's
    `additionalContextLimit`, which Claude Code does not define.
    """
    agent_home = agent_home.expanduser().resolve()
    if not agent_home.exists():
        agent_home.mkdir(parents=True)
        agent_home.chmod(0o700)
    guidance_path = agent_home / "CLAUDE.md"
    settings_path = agent_home / "settings.json"
    existing_guidance = (
        guidance_path.read_text(encoding="utf-8") if guidance_path.exists() else ""
    )
    merged_guidance = _merge_guidance(existing_guidance, adapter_text("claude"))

    settings: dict[str, Any]
    existing_settings = ""
    if settings_path.exists():
        existing_settings = settings_path.read_text(encoding="utf-8")
        try:
            settings = json.loads(existing_settings)
        except json.JSONDecodeError as exc:
            raise MemoryError(
                f"refusing to replace invalid settings JSON: {settings_path}"
            ) from exc
        if not isinstance(settings, dict):
            raise MemoryError("existing settings.json must contain a JSON object")
    else:
        settings = {}
    hook_map = settings.setdefault("hooks", {})
    if not isinstance(hook_map, dict):
        raise MemoryError("existing settings.json field `hooks` must be an object")

    _apply_memory_hooks(
        hook_map, project_root() / "bin" / "memoryctl", spill_threshold=None, agent="claude"
    )

    new_settings = json.dumps(settings, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if merged_guidance != existing_guidance:
        _backup(guidance_path)
        atomic_write_text(guidance_path, merged_guidance)
    if new_settings != existing_settings:
        _backup(settings_path)
        atomic_write_text(settings_path, new_settings)
    return guidance_path, settings_path
