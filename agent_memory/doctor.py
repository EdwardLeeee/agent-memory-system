"""Weekly health check and effect signals for the memory system.

Read-only toward memory and user data: it inspects the store, the hooks'
machine-local health, the transcripts, and the ratings, and writes a report
under ~/Documents plus a small machine-local state file of its own. Nothing
it produces is injected into any model's context.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable

from . import rating
from .storage import atomic_write_text
from .storage import git_environment

# Where weekly reports go: AGENT_MEMORY_REPORT_DIR, else `report_dir` in
# <config>/doctor/doctor.toml, else this default.
DEFAULT_REPORT_DIR = Path.home() / ".local" / "state" / "agent-memory" / "doctor"
REPORT_DIR_ENV = "AGENT_MEMORY_REPORT_DIR"


def resolve_report_dir(config_root: Path) -> Path:
    value = os.environ.get(REPORT_DIR_ENV)
    if value:
        return Path(value).expanduser()
    try:
        from .federation import parse_minimal_toml

        text = (config_root / "doctor" / "doctor.toml").read_text(encoding="utf-8")
        configured = parse_minimal_toml(text).get("report_dir")
    except Exception:
        configured = None
    return Path(str(configured)).expanduser() if configured else DEFAULT_REPORT_DIR
GIT_AHEAD_LIMIT = 50
STALE_DAYS = 7
BUDGET_WARN_RATIO = 0.8
PREFERENCE_GROWTH_WARN = 0.2
OVERLAP_WARN = 0.5
TEST_MEMORY_FLOOR_MB = 2000
# Reading a week of transcripts streams, but still needs headroom; below this
# the transcript-based signals are skipped and shown as n/a.
TRANSCRIPT_MEMORY_FLOOR_MB = 1500
UNRATED_LINE = "本週尚未評分：memoryctl rate"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], env=git_environment(), text=True, capture_output=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _days_since(value: str, now: datetime) -> float | None:
    moment = _parse_time(value)
    return None if moment is None else round((now - moment).total_seconds() / 86400, 1)


def _check(item_id: str, title: str, warn: bool, value: Any, detail: str = "", skipped: bool = False) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": title,
        "status": "SKIP" if skipped else ("WARN" if warn else "OK"),
        "value": value,
        "detail": detail,
    }


# --- Part one: operational checks ------------------------------------------


def check_git(root: Path, now: datetime) -> dict[str, Any]:
    ahead_text = _git(root, "rev-list", "--count", "origin/master..master")
    ahead = int(ahead_text) if ahead_text.isdigit() else None
    pushed = _git(root, "reflog", "show", "--date=iso-strict", "-n1", "--format=%gd", "refs/remotes/origin/master")
    match = re.search(r"\{(.+)\}", pushed)
    last_push = match.group(1) if match else _git(root, "log", "-1", "--format=%cI", "origin/master")
    days = _days_since(last_push, now)
    dirty = [line for line in _git(root, "status", "--porcelain", "--untracked-files=no").splitlines() if line.strip()]
    warn = (
        (ahead is not None and ahead > GIT_AHEAD_LIMIT)
        or (days is not None and days > STALE_DAYS)
        or bool(dirty)
    )
    return _check("git", "git 與 origin", warn,
                  {"ahead": ahead, "days_since_push": days, "uncommitted": len(dirty)},
                  f"領先 origin {ahead} 個 commit；上次 push {days} 天前；未提交的已追蹤檔 {len(dirty)} 個")


def check_last_write(root: Path, now: datetime) -> dict[str, Any]:
    last = _git(root, "log", "-1", "--format=%cI", "-E", "--grep=^memory: (record|supersede)")
    days = _days_since(last, now)
    return _check("last_write", "最近一次 record／supersede", days is None or days > STALE_DAYS,
                  {"days_since_write": days}, f"{days} 天前")


def check_hooks(config_root: Path) -> dict[str, Any]:
    try:
        hooks = json.loads((config_root / "runtime" / "health.json").read_text(encoding="utf-8"))["hooks"]
    except (OSError, json.JSONDecodeError, KeyError):
        hooks = {}
    # A hook that writes its own runtime/health/<name>.json overrides health.json.
    for path in sorted((config_root / "runtime" / "health").glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(item, dict):
            hooks[path.stem] = item
    statuses = {name: (hooks.get(name) or {}).get("status", "never-run")
                for name in ("session_start", "user_prompt_submit", "pre_compact", "pre_tool_use", "router")}
    bad = {name: status for name, status in statuses.items() if status != "ok"}
    # router.toml loads report ok with a code when the file was unusable;
    # only "disabled" is a deliberate state (spec 記憶路由樹 8.0 ruling 14).
    router_code = (hooks.get("router") or {}).get("code")
    if statuses["router"] == "ok" and router_code and router_code != "disabled":
        statuses["router"] = f"ok/{router_code}"
        bad["router"] = statuses["router"]
    return _check("hooks", "hook 狀態", bool(bad), statuses,
                  "全部 ok" if not bad else "非 ok：" + "、".join(f"{k}={v}" for k, v in bad.items()))


HOOK_CONFIGS = {
    "claude": Path.home() / ".claude" / "settings.json",
    "codex": Path.home() / ".codex" / "hooks.json",
}


def check_hook_agents(configs: dict[str, Path] = HOOK_CONFIGS) -> dict[str, Any]:
    """Each agent's installed memory hooks name that agent, so the budget fits it."""
    rows = {}
    for agent, path in configs.items():
        try:
            hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks", {})
        except (OSError, json.JSONDecodeError, AttributeError):
            rows[agent] = "設定檔讀不到"
            continue
        commands = [
            str(handler.get("command", ""))
            for groups in hooks.values() if isinstance(groups, list)
            for group in groups if isinstance(group, dict)
            for handler in group.get("hooks", []) if isinstance(handler, dict)
            if "memoryctl hook" in str(handler.get("command", ""))
        ]
        missing = [c for c in commands if not c.rstrip().endswith(f"--agent {agent}")]
        rows[agent] = "沒有記憶 hook" if not commands else (f"{len(missing)} 條沒帶 --agent {agent}" if missing else "ok")
    return _check("hook_agents", "hook 帶 --agent", any(v != "ok" for v in rows.values()), rows,
                  "；".join(f"{k}：{v}" for k, v in rows.items()))


PREFERENCE_SHARE_WARN = 0.85


def check_session_packs(root: Path, cwds: list[Path], lesson_cwds: set[Path] = frozenset()) -> dict[str, Any]:
    from .core import AGENT_HOOK_BUDGETS, CLAUDE_HOOK_CHAR_LIMIT, _compose_session_context

    rows = []
    warn = False
    for cwd in cwds:
        for agent in ("claude", "codex"):
            budget = AGENT_HOOK_BUDGETS[agent][0]
            try:
                # As the hook builds it: Claude's pack under its character limit too (review 2, item 17).
                _, stats = _compose_session_context(
                    root, cwd, None, source="startup", budget_tokens=budget, allow_reindex=False,
                    char_limit=CLAUDE_HOOK_CHAR_LIMIT if agent == "claude" else None,
                )
            except Exception as exc:  # a broken pack is exactly what this reports
                rows.append({"cwd": str(cwd), "agent": agent, "error": type(exc).__name__})
                warn = True
                continue
            tokens = int(stats["estimated_tokens"])
            # A registered source's own pack must carry its PROJECT LESSONS;
            # that is the warning. Trimming or a pack near its budget is
            # listed as information: Codex's smaller budget makes it routine.
            lessons_missing = cwd in lesson_cwds and not stats.get("project_lessons_included")
            flagged = bool(stats["trimmed"]) or tokens > BUDGET_WARN_RATIO * budget
            # Preferences taking most of the budget crowd out the rest; the
            # longest are named so the user can be asked (spec F11 ruling).
            share = int(stats.get("preference_tokens") or 0) / budget
            crowded = share > PREFERENCE_SHARE_WARN
            warn = warn or lessons_missing or crowded
            rows.append({"cwd": str(cwd), "agent": agent, "tokens": tokens, "budget": budget,
                         "trimmed": bool(stats["trimmed"]), "lessons_missing": lessons_missing,
                         "flagged": flagged, "preference_share": round(share, 3), "crowded": crowded,
                         "longest_preferences": stats.get("longest_preferences") or []})
    detail = "；".join(
        f"{Path(r['cwd']).name or '~'}/{r['agent']} " + (r.get("error") or f"{r['tokens']}/{r['budget']}"
        + ("（裁切）" if r["trimmed"] else "") + ("（缺 PROJECT LESSONS）" if r.get("lessons_missing") else "")
        + (f"（偏好占 {r['preference_share']:.0%}，最長："
           + "、".join(f"{p['id']} {p['tokens']}" for p in r["longest_preferences"]) + "）" if r.get("crowded") else ""))
        for r in rows
    )
    return _check("session_packs", "模擬開場包", warn, rows, detail)


def check_preferences(root: Path, previous: dict[str, Any] | None) -> dict[str, Any]:
    from .core import _startup_preferences, estimate_tokens

    preferences = _startup_preferences(root, cwd=Path.home(), allow_reindex=False)
    count = len(preferences)
    tokens = sum(estimate_tokens(f"- {m['id']}: {v}") for m, v in preferences)
    warn = False
    if previous:
        before = previous.get("value") or {}
        for key, now_value in (("count", count), ("tokens", tokens)):
            old = before.get(key)
            if old and now_value >= old * (1 + PREFERENCE_GROWTH_WARN):
                warn = True
    return _check("preferences", "開場偏好", warn, {"count": count, "tokens": tokens}, f"{count} 條、{tokens} tokens")


CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
_TRAILING_UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")


def known_session_ids(projects_dir: Path, codex_dir: Path = CODEX_SESSIONS_DIR) -> set[str]:
    """Session ids of every Claude Code and Codex transcript, from file names only."""
    known = {path.stem for path in projects_dir.glob("**/*.jsonl") if _UUID_RE.match(path.stem)}
    for path in codex_dir.glob("**/rollout-*.jsonl"):
        match = _TRAILING_UUID_RE.search(path.stem)
        if match:
            known.add(match.group(1))
    return known


def check_capsules(root: Path, now: datetime, projects_dir: Path) -> dict[str, Any]:
    from .core import CAPSULE_IDLE_AFTER_DAYS, _capsule_candidates

    known = known_session_ids(projects_dir)
    modern_root = root / "sessions" / "by-session"
    stale, idle_soon, unmatched, legacy = [], [], [], []
    for path, _, fields in _capsule_candidates(root):
        folder = path.parent.name
        # Only an active capsule can be resumed or shown as another task; an
        # idle one is finished and never warrants a warning.
        if fields.get("state", "active") != "active":
            continue
        # A capsule outside sessions/by-session is the pre-v3 layout, which
        # capsule selection already ignores once modern capsules exist.
        if path.parent.parent != modern_root:
            legacy.append(str(path.relative_to(root)))
            continue
        days = _days_since(fields.get("updated_at", ""), now)
        # Same threshold as the capsule idle rule; a session paused for a
        # week or two is normal and is listed, not warned about.
        if days is not None and days > CAPSULE_IDLE_AFTER_DAYS:
            stale.append({"folder": folder, "days": days})
        elif days is not None and days > STALE_DAYS:
            idle_soon.append({"folder": folder, "days": days})
        if folder in known:
            continue
        prefixes = [h for h in re.findall(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])", folder.casefold())]
        if not folder.startswith("manual-") and any(
            sum(1 for k in known if k.startswith(p)) == 1 for p in prefixes
        ):
            continue
        unmatched.append(folder)
    return _check("capsules", "進度存檔", bool(stale or unmatched),
                  {"stale_active": stale, "paused": idle_soon, "unmatched": sorted(unmatched),
                   "legacy": sorted(legacy)},
                  f"active 但超過 {CAPSULE_IDLE_AFTER_DAYS} 天未更新 {len(stale)} 份；"
                  f"{STALE_DAYS}–{CAPSULE_IDLE_AFTER_DAYS} 天沒動 {len(idle_soon)} 份（資訊）；"
                  f"對不上任何 session {len(unmatched)} 份")


def check_speed_bump_failures(root: Path, config_root: Path) -> dict[str, Any]:
    """The records router.toml's [failures.speed_bump_failures] shows when a
    speed bump holds a command, in every project: one gone, or folded into a
    general lesson (applies = origin, or its text names one), should give
    way to that one (kill-by-name still named one of four shell lessons
    after they were merged into one: designer 2026-10-27)."""
    from .core import pointer_only
    from .merge_drafts import GENERAL_RE, _load
    from .storage import load_router_settings

    links = load_router_settings(config_root / "router.toml").get("speed_bump_failures") or {}
    named = [(bump, failure_id) for bump, failure_ids in sorted(links.items()) for failure_id in failure_ids]
    problems = []
    for bump, failure_id in named:
        found = _load(root, failure_id)
        if found is None:
            problems.append({"bump": bump, "id": failure_id, "why": "不存在（或已不是 active）", "suggest": None})
            continue
        metadata, body = found
        general = GENERAL_RE.search(body)
        suggest = general.group(1) if general and general.group(1) != failure_id else None
        if metadata.get("applies") == "origin":
            problems.append({"bump": bump, "id": failure_id, "why": "已併入通用教訓（applies=origin），只該在學到的專案提醒",
                             "suggest": suggest})
        elif suggest:
            problems.append({"bump": bump, "id": failure_id, "why": f"內容指向通用教訓 {suggest}", "suggest": suggest})
        elif pointer_only(metadata, body):
            problems.append({"bump": bump, "id": failure_id, "why": "內容只指向別處的文件", "suggest": None})
    detail = "；".join(f"{item['bump']} 指名的 {item['id']} {item['why']}"
                      + (f"，建議改成 {item['suggest']}" if item["suggest"] else "，建議換成現有的紀錄或拿掉")
                      for item in problems)
    return _check("speed_bump_failures", "減速帶指名的紀錄（router.toml）", bool(problems), problems,
                  detail or (f"{len(named)} 筆都在，沒有併入別筆" if named else "沒有指名紀錄"))


def check_sources(config_root: Path) -> dict[str, Any]:
    from .federation import FederationConfigError, list_source_configs, load_source_config

    rows, warn = [], False
    for name in list_source_configs(config_root):
        try:
            config = load_source_config(config_root, name)
        except FederationConfigError as exc:
            rows.append({"source": name, "error": str(exc)})
            warn = True
            continue
        try:
            synced = json.loads((config_root / "runtime" / "sources" / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            synced = {}
        ref = config.get("ref")
        if ref:
            # A source read from a ref is behind when the ref it reads (the
            # private ref sync fetched into, else the configured one) has
            # moved past the commit last synced (rulings 43, 55), and broken
            # when the ref cannot be resolved (ruling 51 d).
            from .federation import read_ref_commit

            current = read_ref_commit(config["repo"], ref) or ""
            last = synced.get("ref_commit")
            missing = not current or bool(synced.get("ref_problem"))
            commits = _git(config["repo"], "rev-list", "--count", f"{last}..{current}") if last and current else ""
            behind = not synced or missing or current != last
            rows.append({"source": name, "ref": ref, "synced_at": synced.get("synced_at"), "behind": behind,
                         "missing_ref": missing, "commits_behind": int(commits) if commits.isdigit() else None})
        else:
            head = _git(config["repo"], "log", "-1", "--format=%cI")
            behind = not synced or (
                _parse_time(head) is not None and _parse_time(synced.get("synced_at", "")) is not None
                and _parse_time(head) > _parse_time(synced["synced_at"])
            )
            rows.append({"source": name, "head_committed_at": head, "synced_at": synced.get("synced_at"), "behind": behind})
        warn = warn or behind

    def detail(row: dict[str, Any]) -> str:
        if not row.get("synced_at"):
            return "尚未記錄同步時間"
        if not row.get("behind"):
            return "已同步"
        if row.get("missing_ref"):
            return f"ref {row['ref']} 不存在，讀的是工作區"
        if row.get("ref"):
            count = row.get("commits_behind")
            return f"{row['ref']} 比同步新" + (f" {count} 個 commit" if count else "")
        return "HEAD 比同步新"

    return _check("sources", "來源同步", warn, rows, "；".join(f"{r['source']}：{detail(r)}" for r in rows))


def check_due_tasks(root: Path) -> dict[str, Any]:
    """Open tasks more than a week past due: a session start no longer lists
    them (spec 騰訊借鏡改善 T4)."""
    from .core import DUE_BEHIND, _due_now, due_tasks

    now = _due_now()
    try:
        late = [task for task in due_tasks(root, None, everywhere=True) if task["due"] < now - DUE_BEHIND]
    except Exception as exc:  # never stops the report
        return _check("due_tasks", "過期的待辦", True, None, f"讀不到：{str(exc)[:200]}")
    detail = "；".join(f"{task['id']}（過期 {(now - task['due']).days} 天）" for task in late[:10]) or "沒有過期超過 7 天的"
    return _check("due_tasks", "過期的待辦", bool(late), [task["id"] for task in late],
                  detail + ("→ 做完就 memoryctl task done <id>，不做了就 supersede 改掉" if late else ""))


_GENERATE_WORDS = {
    "created": "新建", "updated": "已更新", "unchanged": "沒有變", "adopted": "已接手（原檔已備份）",
    "hand-written": "是手寫的檔案，沒有覆蓋（確認後用 memoryctl generate run --adopt 接手）",
    "other-store": "是另一份帳本寫的（例如這是帳本副本），沒有覆蓋（這份是正本的話，用 memoryctl generate run --adopt 接手）",
    "not-this-store": "generate.toml 寫的是另一份帳本（例如這是帳本副本），沒有寫",
    "missing-folder": "資料夾不存在", "error": "寫入失敗",
}


def check_generated(root: Path, config_root: Path) -> dict[str, Any]:
    """Regenerates the Markdown files generate.toml names (spec 實際使用回饋
    改善 F14) and warns on a target left alone, a backup of a file changed by
    hand, or a problem in generate.toml."""
    from . import generate

    if not (config_root / generate.GENERATE_TOML_NAME).exists():
        return _check("generate", "產生的檔案", False, None, "沒有 generate.toml", skipped=True)
    try:
        result = generate.run(root, config_root=config_root)
    except Exception as exc:  # never stops the report
        return _check("generate", "產生的檔案", True, None, f"產生失敗：{str(exc)[:200]}")
    rows = result["targets"]
    warn = bool(result["problems"]) or any(
        row["status"] in ("hand-written", "other-store", "not-this-store", "missing-folder", "error") or row.get("backup")
        for row in rows)
    details = [
        f"{row['name']}：{_GENERATE_WORDS.get(row['status'], row['status'])}，{row['records']} 筆"
        + (f"；有人改過，已備份到 {row['backup']}" if row.get("backup") and row["status"] != "adopted" else "")
        for row in rows
    ]
    return _check("generate", "產生的檔案", warn, [{key: row.get(key) for key in ("name", "status", "records")} for row in rows],
                  "；".join(details + result["problems"]))


def _record_blocks(context: str) -> list[str]:
    """Each record block of a pack (### <id> up to the next heading)."""
    found = []
    for block in re.split(r"(?=^### )", context, flags=re.M):
        if block.startswith("### "):
            found.append(re.split(r"^## ", block, maxsplit=1, flags=re.M)[0])
    return found


def _ids_and_scores(context: str) -> tuple[list[tuple[str, int]], list[str]]:
    scored = []
    for block in re.split(r"(?=^### )", context, flags=re.M):
        if not block.startswith("### "):
            continue
        record_id = block.splitlines()[0][4:].strip()
        match = re.search(r"score=(\d+)", block)
        scored.append((record_id, int(match.group(1)) if match else 0))
    lessons = re.findall(r"^- ((?:lesson|federation)\.\S+?)(?: \(|:)", context, re.M)
    return scored, lessons


def _block_nodes(context: str) -> dict[str, str]:
    """id -> the node= its selected_because line names (routed explain only)."""
    nodes = {}
    for block in re.split(r"(?=^### )", context, flags=re.M):
        if block.startswith("### "):
            match = re.search(r"; node=(\S+)$", block, re.M)
            if match:
                nodes[block.splitlines()[0][4:].strip()] = match.group(1)
    return nodes


def _top_problem(top: str, scored: list[tuple[str, int]], nodes: dict[str, str]) -> bool:
    """Whether `top` fails to be the highest score: in its own node when routed."""
    if not scored:
        return True
    if not nodes:
        return max(scored, key=lambda item: item[1])[0] != top
    if top not in nodes:
        return True
    peers = [(record_id, score) for record_id, score in scored if nodes.get(record_id) == nodes[top]]
    return max(peers, key=lambda item: item[1])[0] != top


def run_golden(root: Path, config_root: Path) -> dict[str, Any]:
    from .federation import FederationConfigError, parse_minimal_toml

    path = config_root / "doctor" / "golden.toml"
    try:
        queries = parse_minimal_toml(path.read_text(encoding="utf-8")).get("query", [])
    except (OSError, FederationConfigError) as exc:
        return _check("golden", "黃金查詢", True, [], f"讀不到 {path}：{exc}")
    # An entry with level = "info" is for reference: failing, it is shown as
    # INFO, not a WARN (the Codex start pack: the user works in Claude Code;
    # designer 2026-10-22).
    results = [{**_query_check(root, query), **({"reference": True} if query.get("level") == "info" else {})}
               for query in queries]
    failed = sum(not result["ok"] and not result.get("reference") for result in results)
    reference = [result["text"] for result in results if not result["ok"] and result.get("reference")]
    passed = sum(result["ok"] for result in results)
    detail = f"{passed}/{len(results)} 通過" + (f"（只供參考、沒過：{'、'.join(reference)}）" if reference else "")
    check = _check("golden", "黃金查詢", failed > 0, results, detail)
    if not failed and reference:
        check["status"] = "INFO"
    return check


def run_probes(root: Path, config_root: Path) -> dict[str, Any]:
    """Fixed probe prompts beside golden (spec 實際使用回饋改善 F3 ruling):
    for each project a lone code name, a word common in that project, and a
    phrase as people talk, each with the ids that must or must not appear
    and a one-line reason. Checked like golden, on the pack as the hook
    gives it: without the explain lines, which take room in the budget."""
    from .federation import FederationConfigError, parse_minimal_toml

    path = config_root / "doctor" / "probes.toml"
    if not path.exists():
        return _check("probes", "探測問句", False, [], f"沒有 {path}")
    try:
        probes = parse_minimal_toml(path.read_text(encoding="utf-8")).get("probe", [])
    except (OSError, FederationConfigError) as exc:
        return _check("probes", "探測問句", True, [], f"讀不到 {path}：{exc}")
    results = [{**_query_check(root, probe, explain=False), "reason": str(probe.get("reason", ""))} for probe in probes]
    failed = sum(not result["ok"] for result in results)
    return _check("probes", "探測問句", failed > 0, results, f"{len(results) - failed}/{len(results)} 通過")


def _query_check(root: Path, query: dict[str, Any], explain: bool = True) -> dict[str, Any]:
    """One golden query or probe: the pack a prompt gets, held against what
    the entry expects. A top_id needs the scores `explain` adds."""
    from .core import AGENT_HOOK_BUDGETS, _render_context

    cwd = Path(str(query.get("cwd", "~"))).expanduser()
    text = str(query.get("text", ""))
    if query.get("kind") == "session":
        return _golden_session(root, cwd, str(query.get("agent", "codex")), query)
    agent = str(query.get("agent", "claude"))
    if agent not in AGENT_HOOK_BUDGETS:
        return {"text": text, "cwd": str(cwd), "ok": False, "why": f"未知 agent {agent}"}
    try:
        # Without the rating feedback: golden results never move with it (A1).
        context = _render_context(
            root, cwd, text, AGENT_HOOK_BUDGETS[agent][1], explain=explain, allow_reindex=False, agent=agent,
            feedback=False,
        )
    except Exception as exc:
        return {"text": text, "ok": False, "why": type(exc).__name__}
    scored, lessons = _ids_and_scores(context)
    ids = [record_id for record_id, _ in scored]
    every = ids + lessons
    problems = []
    # What answers a query is what the prompt brought: a PROJECT LESSONS
    # line comes every turn in its project, whatever was asked (retrieval
    # review). A forbidden id is wrong anywhere.
    for expected in query.get("expected_ids", []):
        if expected not in ids:
            problems.append(f"缺 {expected}")
    problems += [f"含不該出現的 {i}" for i in query.get("forbidden_ids", []) if i in every]
    # Any one of several right answers (designer 2026-10-21): a probe whose
    # ranking moves with the ledger names them all.
    any_of = query.get("expected_any_ids", [])
    if any_of and not any(i in ids for i in any_of):
        problems.append("一筆都沒有：" + "、".join(any_of))
    # A word one of the records brought holds, in any case: a lone code name
    # is not blocked whole by the relevance floor (designer 2026-10-21).
    blocks = [block.casefold() for block in _record_blocks(context)]
    problems += [f"沒有一筆提到 {word}" for word in query.get("expected_mentions", [])
                 if not any(str(word).casefold() in block for block in blocks)]
    prefixes = query.get("expected_prefixes", [])
    if prefixes and not any(i.startswith(tuple(prefixes)) for i in every):
        problems.append("沒有任何 " + "／".join(prefixes))
    forbidden = tuple(query.get("forbidden_prefixes", []))
    problems += [f"含禁止前綴 {i}" for i in every if forbidden and i.startswith(forbidden)]
    allowed = tuple(query.get("allowed_prefixes", []))
    problems += [f"不在允許前綴 {i}" for i in ids if allowed and not i.startswith(allowed)]
    problems += [f"缺 {section}" for section in query.get("expected_sections", []) if f"## {section}" not in context]
    if str(query.get("max_results", "")).isdigit() and len(ids) > int(query["max_results"]):
        problems.append(f"{len(ids)} 筆 > {query['max_results']}")
    top = str(query.get("top_id", ""))
    if top and _top_problem(top, scored, _block_nodes(context)):
        problems.append(f"最高分不是 {top}")
    return {"text": text, "cwd": str(cwd), "count": len(ids), "ok": not problems, "why": "；".join(problems)}


def _golden_session(root: Path, cwd: Path, agent: str, query: dict[str, Any]) -> dict[str, Any]:
    """A SessionStart golden: the pack for this cwd and agent must hold a section."""
    from .core import AGENT_HOOK_BUDGETS, _compose_session_context

    label = f"開場包 {cwd.name or '~'}/{agent}"
    try:
        context, _ = _compose_session_context(
            root, cwd, None, source="startup",
            budget_tokens=AGENT_HOOK_BUDGETS.get(agent, AGENT_HOOK_BUDGETS["codex"])[0],
            allow_reindex=False,
        )
    except Exception as exc:
        return {"text": label, "cwd": str(cwd), "ok": False, "why": type(exc).__name__}
    missing = [section for section in query.get("expected_sections", []) if f"## {section}" not in context]
    return {"text": label, "cwd": str(cwd), "ok": not missing, "why": "；".join(f"缺 {m}" for m in missing)}


def check_audit(root: Path) -> dict[str, Any]:
    from .core import audit_memory

    report = audit_memory(root)
    summary = report.get("summary", {})
    value = {
        "decisions": summary.get("decisions"),
        "experiences": summary.get("experiences"),
        "issues": len(report.get("issues", [])),
    }
    return _check("audit", "audit 摘要", False, value,
                  f"{value['decisions']} 決策、{value['experiences']} 經驗、{value['issues']} 項待處理")


PEAK_MEMORY_WARN_MB = 600


def check_peak_memory(child_mb: float | None = None) -> dict[str, Any]:
    """This run's own peak memory: a regression guard for streaming reads.
    The embedding child's peak is shown beside it; both count against the
    run's cap."""
    import resource

    peak_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    detail = f"{peak_mb} MB（上限警示 {PEAK_MEMORY_WARN_MB} MB）"
    if child_mb:
        detail += f"；向量子行程 {child_mb} MB，兩者合計 {round(peak_mb + child_mb, 1)} MB（上限 {CAP_MEMORY}）"
    return _check("peak_memory", "本次檢查峰值記憶體", peak_mb > PEAK_MEMORY_WARN_MB,
                  {"peak_mb": peak_mb, "semantic_child_mb": child_mb}, detail)


# Every manual run caps itself, not only the timer's unit: re-exec under a
# systemd scope with a hard memory limit, or fall back to an address-space
# limit where systemd-run is unavailable.
CAP_ENV = "MEMORYCTL_DOCTOR_CAPPED"
CAP_MEMORY = "1200M"
# RLIMIT_AS caps virtual memory, not RSS: the crashing run had 2.9 GB of
# virtual memory for 1.98 GB resident, so the fallback leaves room above that.
FALLBACK_ADDRESS_LIMIT = 3 * 1024 ** 3


def capped_command(argv: list[str], env: dict[str, str], systemd_run: str | None) -> list[str] | None:
    """The command to re-run this process under a memory cap, or None."""
    if env.get(CAP_ENV) == "1" or not systemd_run:
        return None
    runtime = env.get("XDG_RUNTIME_DIR", "")
    if not runtime or not Path(runtime, "systemd", "private").exists():
        return None
    return [systemd_run, "--user", "--scope", "--quiet", "-p", f"MemoryMax={CAP_MEMORY}",
            "-p", "MemorySwapMax=0", sys.executable, *argv]


def apply_memory_cap(argv: list[str]) -> None:
    command = capped_command(argv, dict(os.environ), shutil.which("systemd-run"))
    if command is not None:
        os.environ[CAP_ENV] = "1"
        os.execvp(command[0], command)
    if os.environ.get(CAP_ENV) != "1":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (FALLBACK_ADDRESS_LIMIT, FALLBACK_ADDRESS_LIMIT))
        os.environ[CAP_ENV] = "1"


def _available_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        return None
    return None


def check_tests(root: Path, run_tests: bool) -> dict[str, Any]:
    cache = root / ".cache"
    size_mb = round(sum(p.stat().st_size for p in cache.rglob("*") if p.is_file()) / 1e6, 1) if cache.exists() else 0
    available = _available_mb()
    if not run_tests or available is None or available < TEST_MEMORY_FLOOR_MB:
        why = "未要求" if not run_tests else f"可用記憶體 {available} MB < {TEST_MEMORY_FLOOR_MB}"
        return _check("tests", "測試與 .cache", False, {"tests": "skipped", "cache_mb": size_mb},
                      f"測試略過（{why}）；.cache {size_mb} MB", skipped=True)
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=root, text=True, capture_output=True, check=False, timeout=900,
    )
    tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
    ran = next((line for line in tail if line.startswith("Ran ")), "")
    ok = result.returncode == 0
    return _check("tests", "測試與 .cache", not ok, {"tests": "ok" if ok else "failed", "summary": ran, "cache_mb": size_mb},
                  f"{ran}，{'全過' if ok else '有失敗'}；.cache {size_mb} MB")


# --- Part two: effect signals ----------------------------------------------


def record_nodes(root: Path) -> Callable[[str], str]:
    """id -> routing node for the ledger's records, live and archived (spec 8.11).

    Uses the index's own node_for on each record's metadata, so an id the
    index no longer holds still lands where it was served from; the routing
    is computed from today's configuration and active scopes, as reindex
    does. A federation id is placed in its source's project/<p>/sources
    node: its kind is not in the ledger, so a federation lesson is not told
    apart from its sources/lessons child. Any other id found in neither
    records/ nor archive/ is "unknown".
    """
    from .core import decode_record
    from .lexical_index import compute_routing, node_for

    active: dict[str, dict[str, Any]] = {}
    archived: dict[str, dict[str, Any]] = {}
    for folder, target in (("archive", archived), ("records", active)):
        for path in sorted((root / folder).glob("**/*.md")):
            try:
                metadata, _ = decode_record(path)
            except Exception:
                continue
            if metadata.get("id"):
                target[str(metadata["id"])] = metadata
    routing = compute_routing(str(metadata.get("scope", "")) for metadata in active.values())
    nodes = {record_id: node_for(metadata, routing) for record_id, metadata in {**archived, **active}.items()}

    def lookup(record_id: str) -> str:
        if record_id in nodes:
            return nodes[record_id]
        if record_id.startswith("federation."):
            return node_for({"id": record_id, "kind": "note"}, routing)
        return "unknown"

    return lookup


def usage_signal(sessions: dict[str, Any], node_of: Callable[[str], str]) -> dict[str, Any]:
    """This week's use of what the hooks gave, from the whole-session
    measure (spec 實際使用回饋改善 F12): the share used, named by id and by
    content apart, by routing node, the records given most and never used,
    and the turns in which the agent searched the memory itself."""
    usage = sessions.get("usage") or {}
    if sessions.get("skipped") or sessions.get("error") or not usage:
        return {"skipped": sessions.get("skipped") or sessions.get("error") or "沒有本週的注入數字"}
    per_record = usage.get("per_record") or {}
    by_node: dict[str, dict[str, Any]] = {}
    for record_id, (injected, explicit, content) in sorted(per_record.items()):
        entry = by_node.setdefault(node_of(record_id), {"injected": 0, "explicit": 0, "content": 0})
        entry["injected"] += injected
        entry["explicit"] += explicit
        entry["content"] += content
    for entry in by_node.values():
        entry["rate"] = round((entry["explicit"] + entry["content"]) / entry["injected"], 3) if entry["injected"] else None
    never = sorted(((record_id, counts[0]) for record_id, counts in per_record.items() if not counts[1] and not counts[2]),
                   key=lambda item: (-item[1], item[0]))[:10]
    injected = int(usage.get("injected") or 0)

    def share(part: int) -> float | None:
        return round(part / injected, 3) if injected else None

    search = sessions.get("search") or {}
    apart = sessions.get("usage_memory_repo") or {}
    return {
        "injected": injected, "explicit": int(usage.get("explicit") or 0), "content": int(usage.get("content") or 0),
        # Turns in the memory system's own folders, left out of the numbers above (A1 ruling).
        "memory_repo": {key: int(apart.get(key) or 0) for key in ("injected", "explicit", "content")},
        "rate": usage.get("rate"), "explicit_rate": share(int(usage.get("explicit") or 0)),
        "content_rate": share(int(usage.get("content") or 0)), "detector": usage.get("detector"),
        "by_node": dict(sorted(by_node.items())),
        "never_used_top": [{"id": record_id, "injections": n} for record_id, n in never],
        "search_calls": int(search.get("calls") or 0),
        "search_after": dict(search.get("after") or {}),
    }


def failure_layer_signal(config_root: Path, now: datetime, days: int = 7) -> dict[str, Any]:
    """PreToolUse over the last week, from its own state files (spec 8.12).

    Injections are the injected.<id>.json markers PreToolUse wrote; per speed
    bump, denials are the deny counts, releases the markers with a second
    run, and a changed command is a release whose command hash differs from
    the first run's. Commands themselves are never stored.
    """
    cutoff = now - timedelta(days=days)
    injected = 0
    bumps: dict[str, dict[str, int]] = {}
    for path in sorted((config_root / "runtime" / "pretooluse").glob("*/*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if path.name.startswith("injected."):
            moment = _parse_time(str(data.get("at") or ""))
            injected += data.get("event") == "PreToolUse" and moment is not None and moment >= cutoff
        elif path.name.startswith("bump."):
            moment = _parse_time(str(data.get("first_at") or ""))
            if moment is None or moment < cutoff:
                continue
            entry = bumps.setdefault(path.name[len("bump."):-len(".json")], {"denied": 0, "released": 0, "changed": 0})
            entry["denied"] += int(data.get("deny_count") or 0)
            if data.get("released_at"):
                entry["released"] += 1
                entry["changed"] += data.get("released_command_sha256") != data.get("first_command_sha256")
    return {"injected": injected, "speed_bumps": dict(sorted(bumps.items()))}


def _terms(text: str) -> set[str]:
    from .core import _query_terms

    return _query_terms(text)


def _overlap(new: set[str], old: set[str]) -> float:
    return len(new & old) / len(new) if new else 0.0


def _records(root: Path, kind: str) -> list[tuple[dict[str, Any], str]]:
    from .core import decode_record

    items = []
    for path in sorted((root / "records" / kind).glob("*.md")):
        try:
            metadata, body = decode_record(path)
        except Exception:
            continue
        if metadata.get("status") == "active":
            items.append((metadata, body))
    return items


def _created_within(metadata: dict[str, Any], now: datetime, days: int) -> bool:
    moment = _parse_time(str(metadata.get("created_at") or ""))
    return moment is not None and moment >= now - timedelta(days=days)


# Jaccard similarity over what happened and what to do about it. Measured
# 2026-10-14 over the live ledger's cross-origin pairs: the four known
# targets (the memory-gate trio's three pairs and the CI pair) score 0.07 to
# 0.20 and 13 pairs reach 0.07, nine of them the same family of machine
# memory, OOM, and disk-full lessons. Root Cause + Symptom alone ranked the
# CI pair (0.04) below 2,400 others: its two records share the fix and the
# moment to apply it, not the story.
CROSS_PROJECT_SIMILARITY = 0.07
# Two records of one place this alike may be one written twice; listed
# with no draft, a supersede into one being the usual fix (designer 2026-10-27).
SAME_PLACE_SIMILARITY = 0.3
SAME_PLACE_LIST_LIMIT = 10
CROSS_PROJECT_SECTIONS = {
    "failure": ("Root Cause", "Symptom", "Verified Fix", "Retry When"),
    "experience": ("Situation", "Expectation", "Observed Outcome", "Applicability"),
}


# The weekly embedding step (spec 論文借鏡改善 B1). It runs as a child process
# under the interpreter that has the semantic extra, inside the doctor's own
# memory cap; without it, or without the model, the report says so and goes on.
SEMANTIC_PYTHON_ENV = "AGENT_MEMORY_SEMANTIC_PYTHON"
SEMANTIC_PYTHON = Path.home() / ".local" / "share" / "agent-memory" / "semantic-venv" / "bin" / "python"
# The child peaked at 524 MB on the live ledger (299 records, 1196 sections).
SEMANTIC_MEMORY_FLOOR_MB = 1200
SEMANTIC_TIMEOUT = 900
# On the live ledger the pairs word overlap finds sit at 0.71 to 0.91 and its
# noise pairs at 0.46 to 0.57; above 0.75 the model adds about ten a week.
SEMANTIC_PAIR_THRESHOLD = 0.75
SEMANTIC_PAIR_LIMIT = 10
TOPIC_CLUSTER_THRESHOLD = 0.6
TOPIC_MIN_SIZE = 2
TOPIC_WORD_LIMIT = 5
TOPIC_LIST_LIMIT = 5
_SKIPPED_SECTIONS = frozenset({"evidence", "decision links"})


def _main_sections(body: str) -> list[str]:
    """A record's sections, without Evidence and Decision Links."""
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in body.splitlines():
        heading = re.fullmatch(r"##\s+(.+?)\s*", line.strip())
        if heading:
            current = None if heading.group(1).casefold() in _SKIPPED_SECTIONS else []
            if current is not None:
                sections.append(current)
        elif current is not None:
            current.append(line)
    return [text for text in ("\n".join(lines).strip() for lines in sections) if text]


def _trigger_in(word: str, text: str) -> bool:
    """Whether a topic trigger would open on this text, by the router's rule:
    CJK or spaced words as substrings, an underscored name when every part
    is a term, any other word only as a whole term."""
    from .router import _CJK_RE

    word = word.casefold()
    if _CJK_RE.search(word) or " " in word:
        return word in text.casefold()
    terms = _terms(text)
    if "_" in word:
        parts = [part for part in word.split("_") if part]
        return bool(parts) and all(part in terms for part in parts)
    return word in terms


def topic_candidates(
    root: Path, routing: dict[str, Any], settings: dict[str, Any], frequency: "Counter[str] | None" = None,
) -> dict[str, dict[str, Any]]:
    """Knowledge records no topic word reaches: in knowledge/misc, or in a
    topic whose name and words never appear in the record's own text. Fills
    `frequency` with how many active records hold each term."""
    from .core import decode_record
    from .lexical_index import node_for

    words = {str(item["name"]).casefold(): [str(w).casefold() for w in item["words"]] for item in settings["topics"]}
    found = {}
    for path in sorted((root / "records").glob("*/*.md")):
        try:
            metadata, body = decode_record(path)
        except Exception:
            continue
        if metadata.get("status") != "active":
            continue
        if frequency is not None:
            frequency.update(_terms(body))
        node = node_for(metadata, routing)
        if not node.startswith("knowledge/"):
            continue
        topic = node[len("knowledge/"):]
        if topic != "misc" and any(_trigger_in(trigger, body) for trigger in {topic, *words.get(topic, [])}):
            continue
        found[str(metadata["id"])] = {"topic": topic, "sections": _main_sections(body)}
    return found


def semantic_python() -> Path:
    value = os.environ.get(SEMANTIC_PYTHON_ENV)
    return Path(value).expanduser() if value else SEMANTIC_PYTHON


def semantic_unavailable(root: Path) -> str | None:
    """Why the embedding step cannot run now, or None."""
    from .semantic import _model_manifest

    python = semantic_python()
    if not os.access(python, os.X_OK):
        return f"沒有向量環境（{python}）"
    if not _model_manifest(root).is_file():
        return "沒有本機模型（memoryctl semantic setup）"
    available = _available_mb()
    if available is not None and available < SEMANTIC_MEMORY_FLOOR_MB:
        return f"可用記憶體 {available} MB < {SEMANTIC_MEMORY_FLOOR_MB}"
    return None


def semantic_similarities(root: Path, texts: dict[str, list[str]], floor: float) -> dict[str, Any]:
    """Cosine of every pair of records at or above `floor`, from the child
    process, or why there is none. Never raises."""
    reason = semantic_unavailable(root)
    if reason:
        return {"skipped": reason}
    python = semantic_python()
    code = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(code), "HF_HUB_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"}
    try:
        done = subprocess.run(
            [str(python), "-m", "agent_memory.semantic_doctor"], cwd=code, env=env, text=True,
            input=json.dumps({"root": str(root), "texts": texts, "floor": floor}, ensure_ascii=False),
            capture_output=True, timeout=SEMANTIC_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)[:300]}
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines()
        return {"error": (lines[-1] if lines else f"exit {done.returncode}")[:300]}
    try:
        output = json.loads(done.stdout)
        if output.get("skipped"):
            return {"skipped": str(output["skipped"])}
        pairs = {frozenset((str(a), str(b))): float(c) for a, b, c in output["pairs"] if a != b}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {"error": "向量子行程的輸出讀不懂"}
    return {"pairs": pairs, "records": int(output.get("count", 0) or 0),
            "seconds": output.get("seconds"), "peak_mb": round(int(output.get("peak_rss_kb", 0) or 0) / 1024, 1)}


def semantic_signal(root: Path) -> dict[str, Any]:
    """One embedding run for both uses: pairs the word overlap misses, and
    clusters of records no topic word reaches."""
    from .core import _extract_section
    from .lexical_index import compute_routing
    from .storage import load_router_settings

    reason = semantic_unavailable(root)
    if reason:
        return {"skipped": reason}
    routing = compute_routing()
    try:
        settings = load_router_settings()
    except Exception:
        from .storage import router_defaults

        settings = router_defaults()
    texts = {}
    for kind, sections in CROSS_PROJECT_SECTIONS.items():
        for metadata, body in _records(root, kind):
            texts[str(metadata["id"])] = [text for section in sections if (text := _extract_section(body, section))]
    frequency: Counter[str] = Counter()
    candidates = topic_candidates(root, routing, settings, frequency)
    for record_id, item in candidates.items():
        texts.setdefault(record_id, item["sections"])
    result = semantic_similarities(root, texts, min(SEMANTIC_PAIR_THRESHOLD, TOPIC_CLUSTER_THRESHOLD))
    if "pairs" in result:
        result["topics"] = topic_suggestions(candidates, result["pairs"], texts, routing, settings, frequency)
    result["topic_candidates"] = len(candidates)
    return result


_GENERIC_WORDS = frozenset({"need", "needs", "single", "command", "using", "used", "make", "made", "any", "every",
                            "still", "same", "other", "least", "most", "each", "both", "case", "cases", "thing"})
# Everyday English and the words every software record uses: none names a
# topic, so none is offered as a trigger word (spec 實際使用回饋改善 F15).
_COMMON_ENGLISH = frozenset("""
able about above across action actions active actual added adding after again against allow allowed almost along
already also always among amount another answer anything apply area around asked available back based basic because
become before begin behind being below best better between bring build built call called came cannot care cause
certain change changed changes check checked checks choose clear close code come common complete content context
control copy correct cost could count course create created current data date days default define detail details
different direct does done down during early easy effect either else enough entry error errors even event events
ever example exist expected explain extra fact facts fail failed false fast field fields final find first fixed
follow following form found free from full function future general give given goal going good great group half
hand handle happen hard have help here high hold however idea important include including info information inside
instead into issue issues item items itself just keep kind know known large last later left less level like likely
limit line lines list little local long look lots main many mean means might missing mode more move much must name
names near never next none normal note nothing number numbers object often once only open option options order
others output over page part parts pass past path place plan point possible present problem problems process quite
rather read real reason recall record records reference rest result results return right rule rules running runtime
save seen send setting settings should show side simple since size small some something soon source space start
state step steps stop such support sure system take taken task tell term test tested tests text than that their
them then there these they things think this those though through time times today together total true turn type
types under until update useful user users value values variable version very view want well were what when where
whether which while whole will with within without word words work working works would write wrong year
""".split())
# A trigger word is held by at most this many records per cluster member.
TOPIC_WORD_SPREAD = 5
# What the doctor's own list held besides router.toml's default function
# characters: a Chinese bigram with one is never a trigger word (之後, 還要,
# 離之 came back without them; retrieval re-check R4). router.toml is not
# changed.
TOPIC_EXTRA_FUNCTION_CHARS = frozenset("個要會有不從到其之以為所再還才但更")


def _word_tail(term: str, ledger: str, function_chars: set[str]) -> bool:
    """A Chinese bigram that is the end of a longer word, across the whole
    ledger: it never starts a run of Chinese (no punctuation, space, letter
    or function character before it anywhere), and four in five times the
    same character comes before it (割區 in 分割區, 分後 in 出分後). A word
    that mostly follows another word in its members' text is still a word:
    系統 after 記憶, 容器 before 化 (retrieval review)."""
    before: Counter[str] = Counter()
    for match in re.finditer(re.escape(term), ledger):
        char = ledger[match.start() - 1] if match.start() else ""
        if not re.fullmatch(r"[\u3400-\u9fff]", char) or char in function_chars:
            return False  # it stands alone somewhere
        before[char] += 1
    if sum(before.values()) < 2:
        return False
    return before.most_common(1)[0][1] * 5 >= sum(before.values()) * 4


def topic_suggestions(
    candidates: dict[str, dict[str, Any]], pairs: dict[frozenset[str], float], texts: dict[str, list[str]],
    routing: dict[str, Any], settings: dict[str, Any], frequency: "Counter[str] | None" = None,
) -> list[dict[str, Any]]:
    """A topic and trigger words for each cluster of candidates (every two
    members alike), as suggestions for router.toml. The topic is the one the
    members already sit in when they share one, whose words then miss them;
    the words are terms at least two members hold, rarest in the ledger
    first."""
    from .lexical_index import _token_owner
    from .merge_drafts import groups

    ids = set(candidates)
    clusters = [g for g in groups([{"ids": sorted(key), "named": False, "overlap": value}
                                   for key, value in pairs.items() if key <= ids and value >= TOPIC_CLUSTER_THRESHOLD])
                if len(g) >= TOPIC_MIN_SIZE]
    configured = {str(item["name"]).casefold(): [str(w).casefold() for w in item["words"]] for item in settings["topics"]}
    taken = set(configured) | {w for group in configured.values() for w in group} | set(_token_owner(routing))
    terms = {record_id: _terms(" ".join(sections)) for record_id, sections in texts.items()}
    if not frequency:
        frequency = Counter(term for found in terms.values() for term in found)
    projects = set(_token_owner(routing))
    # router.toml's function characters (retrieval review), and the
    # doctor's own on top: routing counts 之後 or 還要 as words, but a
    # trigger word holding one is no word (retrieval re-check R4).
    from .storage import ROUTER_DEFAULT_FUNCTION_CHARS

    function_chars = set(str((settings.get("relevance") or {}).get("function_chars") or ROUTER_DEFAULT_FUNCTION_CHARS))
    function_chars |= TOPIC_EXTRA_FUNCTION_CHARS
    ledger = "\n".join(text for values in texts.values() for text in values)

    def first(counter: Counter[str]) -> str | None:
        return min(counter.items(), key=lambda item: (-item[1], item[0]))[0] if counter else None

    def fragment(term: str, members: list[str]) -> bool:
        """Hashes, versions, device names, function-word bigrams, plain words,
        everyday English, and the tail of a longer Chinese word."""
        if term in _GENERIC_WORDS or term in _COMMON_ENGLISH or (term.isascii() and len(term) < 3):
            return True
        if sum(char.isdigit() for char in term) >= 2 or re.fullmatch(r"[\d.]+", term):
            return True
        if not term.isascii() and len(term) < 2:
            return True
        return not term.isascii() and (any(char in function_chars for char in term)
                                       or _word_tail(term, ledger, function_chars))

    suggestions = []
    for cluster in clusters:
        name = (first(Counter(candidates[i]["topic"] for i in cluster if candidates[i]["topic"] != "misc"))
                or first(Counter(parts[1] for i in cluster if len(parts := i.split(".")) > 2 and parts[1] not in projects))
                or "new_topic")
        counts = Counter(term for i in cluster for term in terms.get(i, set()))
        words = sorted(
            (t for t, n in counts.items() if n >= 2 and t not in taken and t != name and not fragment(t, cluster)
             and frequency.get(t, 0) <= TOPIC_WORD_SPREAD * len(cluster)),
            key=lambda t: (-counts[t] / max(frequency.get(t, 0), counts[t]), -counts[t], t),
        )[:TOPIC_WORD_LIMIT]
        status = ("configured" if name in configured
                  else "unworded" if name in {c["topic"] for c in candidates.values()} else "new")
        suggestions.append({"name": name, "status": status, "words": words, "members": cluster})
    return sorted(suggestions, key=lambda s: (-len(s["members"]), s["name"]))


def cross_project_candidates(
    root: Path, local_machine: str | None = None, semantic: dict[frozenset[str], float] | None = None,
    transcripts: tuple[Path, Path] | None = None,
) -> dict[str, Any]:
    """Lessons that may be one lesson learned in more than one place (spec
    學到的地方與適用範圍 v1, section 5). Information only, never a WARN.

    Reads the ledger files directly, so records bound to another machine,
    which this machine's index never serves, are compared too. Two records
    of different origin pair up when one names the other's id, or when the
    Jaccard similarity of their words reaches CROSS_PROJECT_SIMILARITY. A
    shared machine is not a shared origin: sessions of several projects
    wrote the records bound to one machine; nor is global. A record that
    names no project gets the projects of the folder its first version was
    written in (`transcripts`, as the drafts do), or that folder itself as
    folder:<path>; one whose session left no transcript is nowhere known
    and pairs with any (four global shell lessons learned in two projects
    were never paired: designer 2026-10-27). Two records of one
    place alike at SAME_PLACE_SIMILARITY are listed apart as maybe written
    twice (`same_place`). A record already folded into a
    general lesson (論文借鏡改善 A4) stands for it: its pairs go to the
    general one (`via` names it), and it is never paired with the general
    one or with another record folded into the same one. A record naming a
    general lesson the ledger does not hold is an ordinary record. With the
    embedding step's cosines, a pair the words miss joins at
    SEMANTIC_PAIR_THRESHOLD, the SEMANTIC_PAIR_LIMIT strongest only.
    """
    from . import merge_drafts
    from .core import _extract_section
    from .lexical_index import compute_routing, record_origins
    from .merge_drafts import merged_into

    routing = compute_routing()
    items = []
    for kind, sections in CROSS_PROJECT_SECTIONS.items():
        for metadata, body in _records(root, kind):
            items.append({
                "id": str(metadata["id"]), "kind": kind, "body": body,
                "origins": record_origins(metadata, routing),
                "scope": str(metadata.get("scope", "")),
                "created": str(metadata.get("created_at") or "")[:10],
                "terms": _terms(" ".join(_extract_section(body, section) for section in sections)),
                "merged": merged_into(metadata, body),
            })
    by_id = {item["id"]: item for item in items}
    unplaced = [item["id"] for item in items if not any(o.startswith("project:") for o in item["origins"])]
    learned = merge_drafts.learned_places(root, unplaced, transcripts) if transcripts else {}
    for item in items:
        if item["merged"] not in by_id:
            item["merged"] = None
        place = learned.get(item["id"])
        item["inferred"] = ([f"project:{project}" for project in place["projects"]] or [f"folder:{place['folder']}"]
                            if place else [])
        item["places"] = {o for o in item["origins"] if not o.startswith("machine:") and o != "global"} | set(item["inferred"])
    same_place = []
    best: dict[frozenset[str], dict[str, Any]] = {}
    for index, first in enumerate(items):
        for second in items[index + 1:]:
            ends = [by_id[item["merged"]] if item["merged"] else item for item in (first, second)]
            if ends[0]["id"] == ends[1]["id"]:
                continue
            union = first["terms"] | second["terms"]
            score = round(len(first["terms"] & second["terms"]) / len(union), 3) if union else 0.0
            # Learned in one place: no lesson of two places, but maybe one
            # written twice, listed apart with no draft (designer 2026-10-27).
            if shared := first["places"] & second["places"]:
                if score >= SAME_PLACE_SIMILARITY:
                    same_place.append({"ids": [first["id"], second["id"]], "overlap": score, "places": sorted(shared)})
                continue
            named = second["id"] in first["body"] or first["id"] in second["body"]
            cosine = (semantic or {}).get(frozenset((first["id"], second["id"])))
            if not (named or score >= CROSS_PROJECT_SIMILARITY or (cosine is not None and cosine >= SEMANTIC_PAIR_THRESHOLD)):
                continue
            pair = {"ids": [ends[0]["id"], ends[1]["id"]], "origins": [ends[0]["origins"], ends[1]["origins"]],
                    "named": named, "overlap": score}
            if ends[0]["inferred"] or ends[1]["inferred"]:
                pair["inferred"] = [ends[0]["inferred"], ends[1]["inferred"]]
            if cosine is not None:
                pair["semantic"] = cosine
            if via := [item["id"] for item in (first, second) if item["merged"]]:
                pair["via"] = via
            known = best.get(frozenset(pair["ids"]))
            if known is None or (named, score, cosine or 0.0) > (known["named"], known["overlap"], known.get("semantic", 0.0)):
                best[frozenset(pair["ids"])] = pair
    by_words = [pair for pair in best.values() if pair["named"] or pair["overlap"] >= CROSS_PROJECT_SIMILARITY]
    by_meaning = sorted((pair for pair in best.values() if pair not in by_words),
                        key=lambda pair: (-pair["semantic"], pair["ids"]))
    pairs = by_words + by_meaning[:SEMANTIC_PAIR_LIMIT]
    pairs.sort(key=lambda pair: (not pair["named"], -pair["overlap"], pair["ids"]))
    other_machine = [
        {"id": item["id"], "machine": item["scope"][len("machine:"):], "created": item["created"]}
        for item in items
        if item["scope"].startswith("machine:") and item["scope"] != f"machine:{local_machine}"
    ]
    merged = {item["id"]: item["merged"] for item in items if item["merged"]}
    same_place.sort(key=lambda pair: (-pair["overlap"], pair["ids"]))
    return {"pairs": pairs, "other_machine": other_machine, "threshold": CROSS_PROJECT_SIMILARITY, "merged": merged,
            "semantic_only": {"added": min(len(by_meaning), SEMANTIC_PAIR_LIMIT), "left_out": max(0, len(by_meaning) - SEMANTIC_PAIR_LIMIT)},
            "same_place": same_place, "same_place_threshold": SAME_PLACE_SIMILARITY}


def repeat_signals(root: Path, config_root: Path, now: datetime) -> dict[str, Any]:
    from .core import _extract_section, _open_index, _index_row

    # Settlement groups: remember when each was first seen and how many
    # observations it had then; growth afterwards is the same mistake again.
    state_path = config_root / "runtime" / "doctor" / "groups.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    connection = _open_index(root, allow_reindex=False)
    try:
        rows = [_index_row(row) for row in connection.execute("SELECT * FROM records WHERE kind = 'lesson'")]
    finally:
        connection.close()
    grown = []
    for row in rows:
        metadata = row["metadata"]
        if "members" not in metadata:
            continue
        observed = sum(int(o.get("n_fields", 0)) for o in metadata.get("observations", []))
        entry = state.setdefault(str(metadata["id"]), {"first_seen": now.date().isoformat(), "observations": observed})
        if observed > entry["observations"]:
            grown.append({"id": metadata["id"], "first_seen": entry["first_seen"], "new_observations": observed - entry["observations"]})
    state_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(state_path, json.dumps(state, ensure_ascii=False, indent=1) + "\n")

    def duplicates(kind: str, sections: tuple[str, ...]) -> list[dict[str, Any]]:
        records = [
            (metadata, _created_within(metadata, now, 7),
             _terms(" ".join(_extract_section(body, s) for s in sections)))
            for metadata, body in _records(root, kind)
        ]
        found = []
        for metadata, is_new, new_terms in records:
            if not is_new:
                continue
            for other, other_is_new, other_terms in records:
                if other_is_new:
                    continue
                score = _overlap(new_terms, other_terms)
                if score >= OVERLAP_WARN:
                    found.append({"new": metadata["id"], "existing": other["id"], "overlap": round(score, 2)})
        return found

    failure_dupes = duplicates("failure", ("Symptom", "Root Cause"))
    preference_dupes = [
        d for d in duplicates("preference", ("Value",))
    ]
    return {
        "groups_grown": grown,
        "new_observations": sum(g["new_observations"] for g in grown),
        "possible_duplicate_failures": failure_dupes,
        "possible_repeated_preferences": preference_dupes,
        "repeated_mistakes": sum(g["new_observations"] for g in grown) + len(failure_dupes),
    }


def recovery_signal(config_root: Path, now: datetime) -> dict[str, Any]:
    try:
        state = json.loads((config_root / "runtime" / "doctor" / "recovery.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    bucket = state.get(rating.iso_week(now), {})
    restored, missed = int(bucket.get("with_current", 0)), int(bucket.get("without", 0))
    total = restored + missed
    return {"restored": restored, "missed": missed, "rate": round(restored / total, 3) if total else None}


# --- Report ---------------------------------------------------------------


def _previous_report(report_dir: Path, now: datetime, days_back: int, tolerance: int) -> dict[str, Any] | None:
    target = (now - timedelta(days=days_back)).date()
    best = None
    for path in report_dir.glob("*.json"):
        try:
            when = datetime.fromisoformat(path.stem).date()
        except ValueError:
            continue
        gap = abs((when - target).days)
        if gap <= tolerance and (best is None or gap < best[0]):
            best = (gap, path)
    if best is None:
        return None
    try:
        return json.loads(best[1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _fmt(value: Any, percent: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{value:.0%}" if percent else str(value)


def build_report(
    root: Path,
    config_root: Path,
    now: datetime,
    *,
    report_dir: Path | None = None,
    projects_dir: Path | None = None,
    run_tests: bool = True,
) -> tuple[dict[str, Any], str]:
    from .federation import FederationConfigError, list_source_configs, load_source_config

    report_dir = report_dir or resolve_report_dir(config_root)

    projects_dir = projects_dir or rating.default_projects_dir()
    last_week = _previous_report(report_dir, now, 7, 3)
    four_weeks = _previous_report(report_dir, now, 28, 7)
    previous_checks = {c["id"]: c for c in (last_week or {}).get("checks", [])}

    cwds = [Path.home()]
    for name in list_source_configs(config_root):
        try:
            cwds.append(load_source_config(config_root, name)["repo"])
        except FederationConfigError:
            continue
    checks = [
        check_git(root, now),
        check_last_write(root, now),
        check_hooks(config_root),
        check_hook_agents(),
        check_session_packs(root, cwds, set(cwds[1:])),
        check_preferences(root, previous_checks.get("preferences")),
        check_capsules(root, now, projects_dir),
        check_sources(config_root),
        check_speed_bump_failures(root, config_root),
        check_generated(root, config_root),
        check_due_tasks(root),
        run_golden(root, config_root),
        run_probes(root, config_root),
        check_audit(root),
        check_tests(root, run_tests),
    ]
    summary = rating.summarize(config_root, now)
    available = _available_mb()
    transcripts_ok = available is not None and available >= TRANSCRIPT_MEMORY_FLOOR_MB
    this_week = summary["weeks"][0]
    rated_this_week = (rating.ratings_dir(config_root) / f"{rating.iso_week(now)}.json").exists()
    repeats = repeat_signals(root, config_root, now)
    previous_cumulative = ((last_week or {}).get("signals", {}).get("repeats", {}) or {}).get("new_observations")
    repeats["new_observations_this_week"] = (
        repeats["new_observations"] - previous_cumulative
        if isinstance(previous_cumulative, int)
        else repeats["new_observations"]
    )
    repeats["repeated_mistakes"] = max(0, repeats["new_observations_this_week"]) + len(
        repeats["possible_duplicate_failures"]
    )
    recovery = recovery_signal(config_root, now)
    try:
        local_machine = json.loads((config_root / "machine.json").read_text(encoding="utf-8")).get("machine_id")
    except (OSError, ValueError, AttributeError):
        local_machine = None
    # Pairs the words miss and topic suggestions, from the local model (B1).
    try:
        semantic = semantic_signal(root)
    except Exception as exc:  # the embedding step never stops the report
        semantic = {"error": str(exc)[:300]}
    # Where each record was learned: transcripts changed since last week are
    # read again for the writes they report (spec 論文借鏡改善 A2). Codex
    # transcripts are read only with the default Claude folder, so a run on
    # another folder (a test) never reads this machine's sessions.
    learned_in: dict[str, Any] = {"skipped": "可用記憶體不足"} if not transcripts_ok else {}
    from . import learned

    codex = learned.default_codex_dir() if projects_dir == rating.default_projects_dir() else projects_dir / ".no-codex"
    if transcripts_ok:
        try:
            learned_in = learned.backfill(root, projects_dir, codex, now=now)
        except Exception as exc:  # a broken cache or transcript never stops the report
            learned_in = {"error": str(exc)}
    # What the hooks injected over whole sessions this week, as the
    # transcripts recorded it: numbers and record ids only (spec 實際使用回饋改善 F16).
    if transcripts_ok:
        try:
            from . import replay as replay_module
            from .pretool import cwd_projects

            # With the default folder, each account profile's folder too.
            claude = replay_module.default_claude_dirs() if projects_dir == rating.default_projects_dir() else projects_dir
            week_files = list(replay_module.transcript_files(claude, codex, now))
            sessions = replay_module.measure_actual(week_files, root, lambda cwd: cwd_projects(cwd, root),
                                                    replay_module.Window(now - timedelta(days=7)))
            sessions["missing"] = replay_module.missing_folders(claude, codex)
        except Exception as exc:  # never stops the report
            sessions = {"error": str(exc)[:300]}
            week_files = None
        # Moments a lesson was likely learned, and whether memory followed
        # (spec 騰訊借鏡改善 T1): counts, session ids and times only.
        try:
            from . import missed as missed_module

            missed = (missed_module.measure(week_files, root, lambda cwd: cwd_projects(cwd, root),
                                            replay_module.Window(now - timedelta(days=7)), config_root)
                      if week_files is not None else {"error": "對話紀錄讀不到"})
        except Exception as exc:  # never stops the report
            missed = {"error": str(exc)[:300]}
    else:
        sessions = {"skipped": f"可用記憶體 {available} MB < {TRANSCRIPT_MEMORY_FLOOR_MB}"}
        missed = {"skipped": f"可用記憶體 {available} MB < {TRANSCRIPT_MEMORY_FLOOR_MB}"}
    sessions["live_versions"] = live_versions(root, now)
    # Whether the agent used what it was given, judged from what it wrote
    # (spec 實際使用回饋改善 F12); it was the share of records it cited.
    usage = usage_signal(sessions, record_nodes(root))
    # Lessons that may be one learned in several places, and drafts for
    # merging each group into one general lesson; never applied (A4). After
    # the backfill, so a record's origin can come from the session it was
    # learned in.
    from . import merge_drafts

    try:
        cross_project = cross_project_candidates(root, local_machine, semantic.get("pairs"),
                                                 (projects_dir, codex) if transcripts_ok else None)
    except Exception as exc:  # a transcript that cannot be read never stops the report
        cross_project = {**cross_project_candidates(root, local_machine, semantic.get("pairs")), "inferred_error": str(exc)[:300]}
    cross_project["semantic"] = {key: value for key, value in semantic.items() if key != "pairs"}
    try:
        cross_project["drafts"] = merge_drafts.write_drafts(
            root, cross_project["pairs"], report_dir, now.date().isoformat(), config_root,
            (projects_dir, codex) if transcripts_ok else None,
        )
    except Exception as exc:
        cross_project["drafts"] = {"dir": "", "groups": [], "dismissed": 0, "error": str(exc)}
    # Records that only point at a federated source are no longer injected
    # and get supersede skeletons (spec 實際使用回饋改善 F5).
    try:
        pointer_only = merge_drafts.pointer_drafts(root, report_dir, now.date().isoformat())
    except Exception as exc:
        pointer_only = {"dir": "", "records": [], "error": str(exc)}
    # The ranking feedback is rebuilt from the ratings and this week's
    # per-record use plus the last weeks' reports (A1, F12).
    from . import feedback as feedback_module

    per_record = ((sessions.get("usage") or {}).get("per_record") or {}) if not usage.get("skipped") else None
    week_use = ({record_id: {"injected": counts[0], "explicit": counts[1], "content": counts[2]}
                 for record_id, counts in per_record.items()} if per_record is not None else None)
    try:
        feedback_module.rebuild(config_root, now, report_dir, week_use, projects_dir, root)
    except Exception:
        pass
    harmful = {"records": feedback_module.flagged_harmful(config_root),
               "unattributed": feedback_module.unattributed(config_root), "pointer_only": pointer_only}
    failure_layer = failure_layer_signal(config_root, now)
    failure_layer["used"] = sum(
        int(item.get("explicit", 0)) + int(item.get("content", 0))
        for node, item in (usage.get("by_node") or {}).items()
        if node == "failures" or node.endswith("/failures")
    )
    checks.append(check_peak_memory(semantic.get("peak_mb")))
    scoreboard = {
        "useful_rate": this_week["useful_rate"],
        "harmful_rate": this_week["harmful_rate"],
        "usage_rate": usage.get("rate"),
        "repeated_mistakes": repeats["repeated_mistakes"],
    }
    # Commit notes are kept a year (T6).
    try:
        from .commits import prune as prune_commits

        prune_commits(config_root, now)
    except Exception:
        pass
    # Ledger decisions a project has since recorded itself (F8), from reindex.
    from .lexical_index import read_newer_decisions

    newer_decisions = read_newer_decisions(root)
    try:
        from .replay import wide_scope_records

        wide_scope = wide_scope_records(root)
    except Exception:  # never stops the report
        wide_scope = []
    report = {
        "date": now.date().isoformat(),
        "rated_this_week": rated_this_week,
        "scoreboard": scoreboard,
        "checks": checks,
        "signals": {"usage": usage, "repeats": repeats, "recovery": recovery, "failure_layer": failure_layer,
                    "cross_project": cross_project, "learned_in": learned_in, "harmful": harmful,
                    "sessions": sessions, "newer_decisions": newer_decisions, "missed": missed,
                    "wide_scope": wide_scope},
    }

    def past(key: str, which: dict[str, Any] | None) -> Any:
        return None if which is None else (which.get("scoreboard") or {}).get(key)

    lines = []
    if not rated_this_week:
        lines.append(UNRATED_LINE)
        lines.append("")
    lines += [f"# 記憶系統健康檢查 {report['date']}", "", "## 計分板", "",
              "| 指標 | 本週 | 上週 | 四週前 |", "|---|---|---|---|"]
    for key, label, percent in (
        ("useful_rate", "有用率", True), ("harmful_rate", "礙事率", True),
        ("usage_rate", "使用率", True), ("repeated_mistakes", "重複犯錯次數", False),
    ):
        lines.append(f"| {label} | {_fmt(scoreboard[key], percent)} | {_fmt(past(key, last_week), percent)} | {_fmt(past(key, four_weeks), percent)} |")
    warns = sum(c["status"] == "WARN" for c in checks)
    lines += ["", f"## 一、運作檢查（{warns} 項 WARN）", ""]
    for number, check in enumerate(checks, start=1):
        before = previous_checks.get(check["id"], {}).get("detail", "n/a")
        lines.append(f"{number}. [{check['status']}] {check['title']}：{check['detail']}（上週：{before}）")
        if check["id"] == "golden":
            for item in check["value"]:
                if not item.get("ok"):
                    lines.append(f"   - 未通過：{item['text'][:40]}（{item.get('why', '')}）")
        if check["id"] == "probes":
            for item in check["value"]:
                if not item.get("ok"):
                    lines.append(f"   - 未通過：{Path(item.get('cwd', '')).name or '~'}「{item['text'][:40]}」（{item.get('why', '')}）"
                                 + (f"；理由：{item['reason']}" if item.get("reason") else ""))
        if check["id"] == "capsules":
            for item in check["value"]["stale_active"]:
                lines.append(f"   - 久未更新：{item['folder']}（{item['days']} 天）")
            for item in check["value"].get("paused", []):
                lines.append(f"   - 暫停中（資訊）：{item['folder']}（{item['days']} 天）")
            for folder in check["value"]["unmatched"]:
                lines.append(f"   - 對不上 session：{folder}")
            for item in check["value"].get("legacy", []):
                lines.append(f"   - 舊版位置（可移除，資訊）：{item}")
    lines += ["", "## 二、效果訊號", ""]
    lines += session_lines(sessions, ((last_week or {}).get("signals") or {}).get("sessions"))
    if usage.get("skipped"):
        lines.append(f"- 使用率：略過（{usage['skipped']}）")
    else:
        lines.append(f"- 使用率（估計值，系統依 AI 寫出的內容判斷，規則 {usage.get('detector') or 'v1'}）：合計 {_fmt(usage['rate'], True)}"
                     f"（{usage['explicit'] + usage['content']:,}/{usage['injected']:,}）；明寫 id {_fmt(usage['explicit_rate'], True)}"
                     f"、內容相符 {_fmt(usage['content_rate'], True)}")
        apart = usage.get("memory_repo") or {}
        if apart.get("injected"):
            lines.append(f"  - 不含在記憶系統 repo 裡工作的回合，那些另外列：注入 {apart['injected']:,}、明寫 id {apart['explicit']:,}"
                         f"、內容相符 {apart['content']:,}")
        if usage.get("by_node"):
            lines.append("- 節點（注入／明寫 id／內容相符／使用率）：")
            lines += [f"  - {node}：{item['injected']}／{item['explicit']}／{item['content']}／{_fmt(item['rate'], True)}"
                      for node, item in usage["by_node"].items()]
        if usage.get("never_used_top"):
            lines.append("- 本週注入最多次但從沒被用到：")
            lines += [f"  - {item['id']}（{item['injections']} 次）" for item in usage["never_used_top"]]
    lines.append(f"- 重複犯錯：本週分組新增觀測 {repeats['new_observations_this_week']} 筆（自首次記錄起共 {repeats['new_observations']} 筆）；"
                 f"可能重複的 failure {len(repeats['possible_duplicate_failures'])} 筆")
    lines += [f"  - {d['new']} ≈ {d['existing']}（{d['overlap']:.0%}）" for d in repeats["possible_duplicate_failures"]]
    lines.append(f"- 重複交代的偏好：{len(repeats['possible_repeated_preferences'])} 筆")
    lines += [f"  - {d['new']} ≈ {d['existing']}（{d['overlap']:.0%}）" for d in repeats["possible_repeated_preferences"]]
    lines.append(f"- 失敗層：本週 PreToolUse 注入 {failure_layer['injected']} 次；failures 節點被用到 {failure_layer['used']} 次")
    outbound = sessions.get("outbound") if isinstance(sessions, dict) else None
    if outbound:
        # F13 ruling 3: every prompt is a turn for now; a high M means the spacing needs another look.
        lines.append(f"- 送出前提醒 {outbound.get('reminders', 0)} 次，其中同一筆 10 分鐘內重複 {outbound.get('repeats', 0)} 次")
    lines.append(f"- memoryctl search 呼叫：本週 {usage.get('search_calls', 0)} 次（不含在 ~/agent-memory 裡工作的 session）")
    lines += [f"  - {bump}：擋 {item['denied']}／放行 {item['released']}／改指令 {item['changed']}/{item['released']}"
              for bump, item in failure_layer["speed_bumps"].items()]
    lines.append(f"- 恢復成功率（compact／resume 有 CURRENT TASK）：{recovery['restored']}/{recovery['restored'] + recovery['missed']}（{_fmt(recovery['rate'], True)}）")
    lines += ["", "## 三、可能跨專案的教訓（資訊）", ""]
    lines += cross_project_lines(cross_project)
    lines += ["", "## 四、被標礙事的記憶（請檢查是否過時）", ""]
    lines += harmful_lines(harmful)
    lines += ["", "## 五、可能需要改程式的地方（資訊）", ""]
    lines += code_change_lines(checks, usage, failure_layer, harmful, cross_project.get("semantic"), newer_decisions,
                               wide_scope)
    from .missed import lines as missed_lines

    lines += ["", "## 六、可能漏記的時刻（只量測）", ""]
    lines += missed_lines(missed)
    return report, "\n".join(lines) + "\n"


def live_versions(root: Path, now: datetime, days: int = 7) -> list[dict[str, str]]:
    """The code versions live ran this week, oldest first: the one in use
    when the week began ("start"), then each commit on the main line that
    changed the program, with its local time."""
    since = (now - timedelta(days=days)).isoformat()

    def log(*limit: str) -> list[dict[str, str]]:
        try:
            done = subprocess.run(
                ["git", "-C", str(root), "log", "--first-parent", *limit, "--date=format-local:%Y-%m-%d %H:%M",
                 "--format=%h%x09%cd", "--", "agent_memory", "bin"],
                capture_output=True, text=True, timeout=10, env=git_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if done.returncode != 0:
            return []
        return [{"commit": commit, "time": time} for commit, _, time in (line.partition("\t") for line in done.stdout.splitlines()) if commit]

    start = log("-1", f"--until={since}")
    return [*({**item, "start": "1"} for item in start), *reversed(log(f"--since={since}"))]


def session_lines(sessions: dict[str, Any], last: dict[str, Any] | None) -> list[str]:
    """Section 二's whole-session numbers, beside last week's."""
    from . import replay as replay_module

    if sessions.get("skipped") or sessions.get("error"):
        lines = [f"- 每週注入指標：略過（{sessions.get('skipped') or sessions.get('error')}）"]
    else:
        lines = ["- 每週注入指標（這週紀錄裡實際的注入；只算數字與紀錄 id）："]
        lines += ["  " + line for line in replay_module.reading_lines(sessions) + replay_module.summary_lines(sessions, last)]
    versions = sessions.get("live_versions") or []
    start = [v for v in versions if v.get("start")]
    changed = [v for v in versions if not v.get("start")]
    lines.append("- 這週 live 的程式版本（本機時間）：期間開始時 " + (f"{start[0]['time']} {start[0]['commit']}" if start else "不明")
                 + "；期間內換過 " + ("、".join(f"{v['time']} {v['commit']}" for v in changed) if changed else "沒有"))
    return lines


def harmful_lines(harmful: dict[str, Any]) -> list[str]:
    """Records the user rated harmful twice or more, and the ratings that
    could not be credited to one memory (spec 論文借鏡改善 A1)."""
    rows = harmful.get("records", [])
    lines = [f"- {row['id']}：礙事 {row['harmful']} 次，最後一次評分 {row['last_rated'] or '不明'}" for row in rows]
    if rows:
        lines.append("- 確認過時就用 memoryctl supersede 更正；排序已經自動降低它的分數，但內容要人來改。")
    else:
        lines.append("- 沒有（評分裡被標「礙事」2 次以上、而且歸得到那一筆的記憶）")
    left = harmful.get("unattributed", {})
    if left.get("useful") or left.get("harmful"):
        lines.append(
            f"- 無法歸到單一記憶的評分：有用 {left.get('useful', 0)} 題、礙事 {left.get('harmful', 0)} 題"
            "（那題附了多筆記憶，系統判斷 AI 沒有用到其中任何一筆）"
        )
    pointers = (harmful.get("pointer_only") or {}).get("records") or []
    if pointers:
        folder = harmful["pointer_only"].get("dir")
        lines.append(f"- 只剩指標、不再注入的紀錄：{len(pointers)} 筆（決定段只寫已移到聯邦來源；search 照樣找得到）"
                     + (f"；supersede 草稿在 {folder}，照各筆的 README.md 二選一" if folder else ""))
        lines += [f"  - {item['id']}（{item['scope']}；" + ("、".join(item["adrs"]) or "沒寫 ADR") + "）" for item in pointers]
    return lines


# A speed bump the agent ran again unchanged this often after the hold is
# probably holding commands it should not (spec 論文借鏡改善 A5).
LOOSE_BUMP_MIN_DENIED = 3
LOOSE_BUMP_RATIO = 0.8
CODE_LIST_LIMIT = 5


def code_change_lines(
    checks: list[dict[str, Any]], usage: dict[str, Any], failure_layer: dict[str, Any], harmful: dict[str, Any],
    semantic: dict[str, Any] | None = None, newer_decisions: dict[str, list[dict[str, str]]] | None = None,
    wide_scope: list[dict[str, str]] | None = None,
) -> list[str]:
    """Where the program itself may need changing, one line each with what
    to do about it. Information for the designer, who writes the specs; the
    doctor never changes code."""
    lines = []
    golden = next((check for check in checks if check.get("id") == "golden"), None)
    rows = [item for item in (golden or {}).get("value") or [] if isinstance(item, dict)]
    failed = [item for item in rows if not item.get("ok") and not item.get("reference")]
    reference = [str(item.get("text", ""))[:40] for item in rows if not item.get("ok") and item.get("reference")]
    if failed:
        for item in failed[:CODE_LIST_LIMIT]:
            lines.append(f"- golden 失敗：「{str(item.get('text', ''))[:40]}」（{item.get('why') or '原因不明'}）"
                         "→ 用 memoryctl context --explain 跑這題，看是門檻、預算還是路由")
    elif rows:
        lines.append("- golden：全部通過" + (f"（只供參考的「{'」「'.join(reference)}」沒過）" if reference else ""))
    else:
        lines.append("- golden：沒有可跑的題目（doctor/golden.toml）")
    loose = []
    for bump, item in (failure_layer.get("speed_bumps") or {}).items():
        denied = int(item.get("denied", 0) or 0)
        same = int(item.get("released", 0) or 0) - int(item.get("changed", 0) or 0)
        if denied >= LOOSE_BUMP_MIN_DENIED and same / denied >= LOOSE_BUMP_RATIO:
            loose.append(f"- 危險清單可能誤擋：{bump} 擋 {denied} 次，其中 {same} 次照原指令放行"
                         "→ 考慮收窄 pattern，或移出清單")
    lines += loose or ["- 危險清單：沒有放行比例偏高的項目"]
    never = usage.get("never_used_top") or []
    if never:
        shown = "、".join(f"{item['id']}（{item['injections']} 次）" for item in never[:CODE_LIST_LIMIT])
        lines.append(f"- 被注入很多次卻從沒被用到：{shown}→ 檢查是否太常被帶出（門檻、主題詞），或內容需要改寫")
    search_after = usage.get("search_after") or {}
    if search_after:
        shown = "、".join(f"{node} {count} 輪" for node, count in list(search_after.items())[:CODE_LIST_LIMIT])
        lines.append(f"- 路由可能漏了：AI 自己跑 memoryctl search 的輪次（依那一輪的注入來源）：{shown}"
                     "→ 看那幾輪問了什麼，補主題詞或節點")
    else:
        lines.append("- 路由：本週沒有 AI 自己再去查記憶的輪次")
    lines += topic_lines(semantic)
    newer = sorted((newer_decisions or {}).items())
    for record_id, items in newer[:CODE_LIST_LIMIT]:
        lines.append(f"- 帳本決策可能過時：{record_id}，專案之後自己記了 "
                     + "、".join(f"{item['id']}（{item['recorded_on']}）" for item in items)
                     + "→ 確認後 supersede 成指向專案紀錄（F5 的做法），或退役")
    if len(newer) > CODE_LIST_LIMIT:
        lines.append(f"- 另外還有 {len(newer) - CODE_LIST_LIMIT} 筆帳本決策，專案之後自己記了（.cache/newer_decisions.json）")
    if not newer:
        lines.append("- 帳本決策：沒有專案之後自己記了的")
    # Global records whose id names a project: not another project's in the
    # weekly count, but maybe scoped wider than meant (F16 re-check).
    by_project: dict[str, list[str]] = {}
    for item in wide_scope or []:
        by_project.setdefault(item["project"], []).append(item["id"])
    for project, ids in sorted(by_project.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        shown = "、".join(ids[:CODE_LIST_LIMIT]) + (f" 等 {len(ids)} 筆" if len(ids) > CODE_LIST_LIMIT else "")
        lines.append(f"- scope 可能太寬：{project} 有 {len(ids)} 筆是 global，id 卻帶專案名（{shown}）"
                     f"→ 不算跨專案注入；要不要縮成 project:{project}，由那個專案決定，doctor 不會改")
    if not by_project:
        lines.append("- scope：沒有 global 卻帶專案名的紀錄")
    lines.append(f"- 被標礙事的記憶：{len(harmful.get('records') or [])} 筆（見第四節）")
    lines.append("- 這一節只列建議；要改的項目由設計者寫成規格，doctor 不會自己改程式。")
    return lines


CROSS_PROJECT_LIST_LIMIT = 20


def topic_lines(semantic: dict[str, Any] | None) -> list[str]:
    """Topic suggestions from the embedding step, for router.toml."""
    if semantic is None:
        return []
    if semantic.get("skipped") or semantic.get("error"):
        return [f"- 主題建議：略過（{semantic.get('skipped') or semantic.get('error')}）"]
    topics = semantic.get("topics") or []
    count = semantic.get("topic_candidates", 0)
    if not topics:
        return [f"- 主題建議：主題詞找不到的 {count} 筆記憶沒有分出相似的群"]
    lines = [f"- 建議主題（向量分群，資訊）：主題詞找不到的 {count} 筆記憶分出 {len(topics)} 群；"
             "審閱後加到 router.toml 的 [[knowledge.topic]]。注意：寫了這段就會取代內建的主題清單，"
             "要連同內建的 memory、web、ci、agents、ui、shell、git 一起列"]
    for topic in topics[:TOPIC_LIST_LIMIT]:
        kind = {"configured": "既有主題，現有的詞找不到這幾筆，補觸發詞",
                "unworded": "既有主題，目前沒有觸發詞"}.get(topic.get("status"), "新主題")
        words = "、".join(topic["words"]) or "（沒有共同的詞，請自己取）"
        lines.append(f"  - 「{topic['name']}」（{kind}，{len(topic['members'])} 筆）：觸發詞 {words}；成員 "
                     + "、".join(topic["members"]))
    if len(topics) > TOPIC_LIST_LIMIT:
        lines.append(f"  - 另有 {len(topics) - TOPIC_LIST_LIMIT} 群，完整清單在同日的 JSON")
    return lines


def cross_project_lines(cross_project: dict[str, Any]) -> list[str]:
    """The report's cross-project section: at most CROSS_PROJECT_LIST_LIMIT
    pairs, named ones and the most similar first, then the total (ruling 52
    f); the JSON report keeps every pair."""
    from .merge_drafts import INFERRED

    pairs = cross_project["pairs"]
    lines = []
    if not pairs:
        lines.append(f"- 沒有找到（明文互指，或相似度 ≥ {cross_project['threshold']}）")
    for pair in pairs[:CROSS_PROJECT_LIST_LIMIT]:
        if pair["named"] or pair["overlap"] >= cross_project["threshold"]:
            how = "明文互指" if pair["named"] else f"相似度 {pair['overlap']}"
            if pair.get("semantic") is not None:
                how += f"，語意 {pair['semantic']}"
        else:
            how = f"語意相似 {pair['semantic']}（用字 {pair['overlap']}）"
        if pair.get("via"):
            how += "，經由已併入的 " + "、".join(pair["via"])
        origins = "／".join("、".join([*places, *(f"{p}（{INFERRED}）" for p in inferred)])
                           for places, inferred in zip(pair["origins"], pair.get("inferred") or [[], []]))
        lines.append(f"- {pair['ids'][0]} ↔ {pair['ids'][1]}（{how}；學到的地方 {origins}）")
        lines.append("  - 建議：若是同一個教訓，合併成一筆 applies=global，或把其中一筆 supersede 成 applies=global")
    if len(pairs) > CROSS_PROJECT_LIST_LIMIT:
        lines.append(f"- 共 {len(pairs)} 對，只列前 {CROSS_PROJECT_LIST_LIMIT} 對（明文互指優先，再依相似度）；完整清單在同日的 JSON")
    elif pairs:
        lines.append(f"- 共 {len(pairs)} 對")
    semantic = cross_project.get("semantic")
    if semantic is not None:
        if semantic.get("skipped") or semantic.get("error"):
            lines.append(f"- 向量配對：略過（{semantic.get('skipped') or semantic.get('error')}），只用用字相似度")
        else:
            added = (cross_project.get("semantic_only") or {}).get("added", 0)
            left = (cross_project.get("semantic_only") or {}).get("left_out", 0)
            lines.append(f"- 向量配對：用字沒抓到、語意相似 ≥ {SEMANTIC_PAIR_THRESHOLD} 的多找到 {added} 對"
                         + (f"（每週最多 {SEMANTIC_PAIR_LIMIT} 對，另有 {left} 對沒列）" if left else ""))
    drafts = cross_project.get("drafts") or {}
    if drafts.get("groups"):
        lines.append(f"- 合併草稿：{len(drafts['groups'])} 組，在 {drafts['dir']}（只產生草稿，不會自動執行；照各組的 README.md 審閱）")
        lines += [f"  - {group['general_id']}" + ("（加入既有通用教訓）" if group.get("join") else "") + " ← "
                  + "、".join(group["members"]) for group in drafts["groups"][:CROSS_PROJECT_LIST_LIMIT]]
    if drafts.get("dismissed"):
        lines.append(f"- 已排除 {drafts['dismissed']} 組（memoryctl doctor dismiss 記下不是同一個教訓的）")
    if drafts.get("dismissed_candidates"):
        lines.append(f"- 已排除 {drafts['dismissed_candidates']} 筆加入既有通用教訓的候選（memoryctl doctor dismiss <通用教訓> <候選>）")
    if cross_project["other_machine"]:
        lines.append("- 綁在其他機器的紀錄（若根本原因與機器無關，考慮改成全域）：")
        lines += [f"  - {item['id']}（{item['machine'][:8]}…，{item['created']}）" for item in cross_project["other_machine"]]
    same_place = cross_project.get("same_place") or []
    threshold = cross_project.get("same_place_threshold", SAME_PLACE_SIMILARITY)
    if not same_place:
        lines.append(f"- 同一個地方可能重複記了兩次：沒有（相似度 ≥ {threshold}）")
    else:
        lines.append(f"- 同一個地方可能重複記了兩次（相似度 ≥ {threshold}；不產生草稿，是同一個就 supersede 成一筆）：")
        lines += [f"  - {pair['ids'][0]} ↔ {pair['ids'][1]}（相似度 {pair['overlap']}；地方 {'、'.join(pair['places'])}）"
                  for pair in same_place[:SAME_PLACE_LIST_LIMIT]]
        if len(same_place) > SAME_PLACE_LIST_LIMIT:
            lines.append(f"  - 共 {len(same_place)} 對，只列前 {SAME_PLACE_LIST_LIMIT} 對；完整清單在同日的 JSON")
    return lines


def write_report(report: dict[str, Any], markdown: str, report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / f"{report['date']}.md"
    atomic_write_text(md_path, markdown)
    atomic_write_text(report_dir / f"{report['date']}.json", json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    return md_path


def notify(report: dict[str, Any], markdown: str, md_path: Path) -> None:
    """Tell the user, never the model: a desktop notification and a herdr one."""
    warns = sum(c["status"] == "WARN" for c in report["checks"])
    unrated = not report["rated_this_week"]
    if not warns and not unrated:
        return
    parts = []
    if unrated:
        parts.append(UNRATED_LINE)
    if warns:
        parts.append(f"{warns} 項 WARN，報告：{md_path}")
    body = "\n".join(parts)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "記憶系統健康檢查", body], check=False)
    if shutil.which("herdr"):
        subprocess.run(["herdr", "notification", "show", "記憶系統健康檢查", "--body", body],
                       check=False, capture_output=True)


SERVICE_NAME = "memoryctl-doctor"


def timer_units(memoryctl: Path) -> tuple[str, str]:
    service = (
        "[Unit]\nDescription=Weekly memory system health check\n\n"
        "[Service]\nType=oneshot\n"
        # A hard memory cap: the 2026-10-07 crash came from this job reading
        # transcripts with no limit. Over the cap only this job is killed.
        "MemoryMax=1200M\nMemorySwapMax=0\nNice=10\nIOSchedulingClass=idle\n"
        f"Environment={CAP_ENV}=1\n"
        f"ExecStart={memoryctl} doctor --notify\n"
    )
    timer = (
        "[Unit]\nDescription=Run the memory system health check every Monday\n\n"
        "[Timer]\nOnCalendar=Mon *-*-* 09:00:00\nPersistent=true\nRandomizedDelaySec=600\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


ENABLE_COMMAND = f"systemctl --user daemon-reload && systemctl --user enable --now {SERVICE_NAME}.timer"


def install_timer(memoryctl: Path, unit_dir: Path | None = None, enable: bool = False) -> tuple[Path, Path]:
    """Write the user units. Enabling them is a separate, explicit step."""
    unit_dir = unit_dir or Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    service, timer = timer_units(memoryctl)
    service_path = unit_dir / f"{SERVICE_NAME}.service"
    timer_path = unit_dir / f"{SERVICE_NAME}.timer"
    atomic_write_text(service_path, service)
    atomic_write_text(timer_path, timer)
    if enable:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.timer"], check=True)
    return service_path, timer_path
