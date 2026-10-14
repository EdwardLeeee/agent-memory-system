"""Weekly health check and effect signals for the memory system.

Read-only toward memory and user data: it inspects the store, the hooks'
machine-local health, the transcripts, and the ratings, and writes a report
under ~/Documents plus a small machine-local state file of its own. Nothing
it produces is injected into any model's context.
"""

from __future__ import annotations

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


def check_session_packs(root: Path, cwds: list[Path], lesson_cwds: set[Path] = frozenset()) -> dict[str, Any]:
    from .core import AGENT_HOOK_BUDGETS, _compose_session_context

    rows = []
    warn = False
    for cwd in cwds:
        for agent in ("claude", "codex"):
            budget = AGENT_HOOK_BUDGETS[agent][0]
            try:
                _, stats = _compose_session_context(
                    root, cwd, None, source="startup", budget_tokens=budget, allow_reindex=False
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
            warn = warn or lessons_missing
            rows.append({"cwd": str(cwd), "agent": agent, "tokens": tokens, "budget": budget,
                         "trimmed": bool(stats["trimmed"]), "lessons_missing": lessons_missing,
                         "flagged": flagged})
    detail = "；".join(
        f"{Path(r['cwd']).name or '~'}/{r['agent']} " + (r.get("error") or f"{r['tokens']}/{r['budget']}"
        + ("（裁切）" if r["trimmed"] else "") + ("（缺 PROJECT LESSONS）" if r.get("lessons_missing") else ""))
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
    from .core import AGENT_HOOK_BUDGETS, _render_context
    from .federation import FederationConfigError, parse_minimal_toml

    path = config_root / "doctor" / "golden.toml"
    try:
        queries = parse_minimal_toml(path.read_text(encoding="utf-8")).get("query", [])
    except (OSError, FederationConfigError) as exc:
        return _check("golden", "黃金查詢", True, [], f"讀不到 {path}：{exc}")
    results, failed = [], 0
    for query in queries:
        cwd = Path(str(query.get("cwd", "~"))).expanduser()
        text = str(query.get("text", ""))
        if query.get("kind") == "session":
            result = _golden_session(root, cwd, str(query.get("agent", "codex")), query)
            results.append(result)
            failed += not result["ok"]
            continue
        agent = str(query.get("agent", "claude"))
        if agent not in AGENT_HOOK_BUDGETS:
            results.append({"text": text, "cwd": str(cwd), "ok": False, "why": f"未知 agent {agent}"})
            failed += 1
            continue
        try:
            context = _render_context(
                root, cwd, text, AGENT_HOOK_BUDGETS[agent][1], explain=True, allow_reindex=False, agent=agent
            )
        except Exception as exc:
            results.append({"text": text, "ok": False, "why": type(exc).__name__})
            failed += 1
            continue
        scored, lessons = _ids_and_scores(context)
        ids = [record_id for record_id, _ in scored]
        every = ids + lessons
        problems = []
        for expected in query.get("expected_ids", []):
            if expected not in every:
                problems.append(f"缺 {expected}")
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
        results.append({"text": text, "cwd": str(cwd), "count": len(ids), "ok": not problems, "why": "；".join(problems)})
        failed += bool(problems)
    return _check("golden", "黃金查詢", failed > 0, results, f"{len(results) - failed}/{len(results)} 通過")


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


def check_peak_memory() -> dict[str, Any]:
    """This run's own peak memory: a regression guard for streaming reads."""
    import resource

    peak_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    return _check("peak_memory", "本次檢查峰值記憶體", peak_mb > PEAK_MEMORY_WARN_MB,
                  {"peak_mb": peak_mb}, f"{peak_mb} MB（上限警示 {PEAK_MEMORY_WARN_MB} MB）")


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


_SEARCH_COMMAND_RE = re.compile(r"^\s*(?:\S*/)?memoryctl\s+search\b")
_MEMORY_REPO = Path.home() / "agent-memory"


def _search_calls(raw_line: str, cutoff: datetime) -> int:
    """`memoryctl search` runs in one assistant line (spec 8.16 (c)): Bash
    tool calls only, recent, and not from a session working in the memory
    repo itself, where the command is development, not recall."""
    try:
        entry = json.loads(raw_line)
    except json.JSONDecodeError:
        return 0
    if not isinstance(entry, dict):
        return 0
    cwd = str(entry.get("cwd") or "")
    if cwd == str(_MEMORY_REPO) or cwd.startswith(str(_MEMORY_REPO) + os.sep):
        return 0
    moment = _parse_time(str(entry.get("timestamp") or ""))
    if moment is not None and moment < cutoff:
        return 0
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return 0
    return sum(
        1
        for item in content
        if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") == "Bash"
        and _SEARCH_COMMAND_RE.search(str((item.get("input") or {}).get("command") or ""))
    )


def citation_signal(
    projects_dir: Path, now: datetime, days: int = 7, node_of: Callable[[str], str] | None = None
) -> dict[str, Any]:
    """Share of injected records later named in the same session's own work.

    Streams each transcript line by line through rating's prefiltered reader;
    assistant lines are searched as raw text, never parsed. With `node_of`,
    the pairs are also counted per routing node.
    """
    cutoff = now - timedelta(days=days)
    search_calls = 0
    injected_pairs: set[tuple[str, str]] = set()
    cited_pairs: set[tuple[str, str]] = set()
    counts: dict[str, int] = {}
    cited_ids: set[str] = set()
    for path in projects_dir.glob("**/*.jsonl"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                continue
        except OSError:
            continue
        session = path.stem
        pending: set[str] = set()
        in_recent_human_turn = False
        for kind, payload in rating.iter_transcript_events(path):
            if kind == "human":
                entry, _ = payload
                moment = rating._parse_time(str(entry.get("timestamp") or ""))
                in_recent_human_turn = moment is not None and moment >= cutoff
                session = str(entry.get("sessionId") or session)
            elif kind == "prompt":
                in_recent_human_turn = False
            elif kind == "injection" and in_recent_human_turn:
                for record in rating.parse_injection(payload):
                    pair = (session, record["id"])
                    if pair not in injected_pairs:
                        injected_pairs.add(pair)
                        counts[record["id"]] = counts.get(record["id"], 0) + 1
                    if pair not in cited_pairs:
                        pending.add(record["id"])
            elif kind == "assistant" and "memoryctl search" in payload:
                search_calls += _search_calls(payload, cutoff)
            if kind == "assistant" and pending:
                for record_id in [rid for rid in pending if rid in payload]:
                    cited_pairs.add((session, record_id))
                    cited_ids.add(record_id)
                    pending.discard(record_id)
    total = len(injected_pairs)
    never = sorted(((rid, n) for rid, n in counts.items() if rid not in cited_ids), key=lambda x: (-x[1], x[0]))[:10]
    signal = {
        "search_calls": search_calls,
        "injected_pairs": total,
        "cited_pairs": len(cited_pairs),
        "rate": round(len(cited_pairs) / total, 3) if total else None,
        "never_cited_top": [{"id": rid, "injections": n} for rid, n in never],
    }
    if node_of is not None:
        by_node: dict[str, dict[str, Any]] = {}
        for pairs, key in ((injected_pairs, "injected"), (cited_pairs, "cited")):
            for _, record_id in pairs:
                entry = by_node.setdefault(node_of(record_id), {"injected": 0, "cited": 0})
                entry[key] += 1
        for entry in by_node.values():
            entry["rate"] = round(entry["cited"] / entry["injected"], 3) if entry["injected"] else None
        signal["by_node"] = dict(sorted(by_node.items()))
    return signal


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
CROSS_PROJECT_SECTIONS = {
    "failure": ("Root Cause", "Symptom", "Verified Fix", "Retry When"),
    "experience": ("Situation", "Expectation", "Observed Outcome", "Applicability"),
}


def cross_project_candidates(root: Path, local_machine: str | None = None) -> dict[str, Any]:
    """Lessons that may be one lesson learned in more than one place (spec
    學到的地方與適用範圍 v1, section 5). Information only, never a WARN.

    Reads the ledger files directly, so records bound to another machine,
    which this machine's index never serves, are compared too. Two records
    of different origin pair up when one names the other's id, or when the
    Jaccard similarity of their words reaches CROSS_PROJECT_SIMILARITY. A
    shared machine is not a shared origin: sessions of several projects
    wrote the records bound to one machine.
    """
    from .core import _extract_section
    from .lexical_index import compute_routing, record_origins

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
            })
    pairs = []
    for index, first in enumerate(items):
        for second in items[index + 1:]:
            places = [{o for o in item["origins"] if not o.startswith("machine:")} for item in (first, second)]
            if places[0] & places[1]:
                continue
            named = second["id"] in first["body"] or first["id"] in second["body"]
            union = first["terms"] | second["terms"]
            score = len(first["terms"] & second["terms"]) / len(union) if union else 0.0
            if named or score >= CROSS_PROJECT_SIMILARITY:
                pairs.append({
                    "ids": [first["id"], second["id"]],
                    "origins": [first["origins"], second["origins"]],
                    "named": named, "overlap": round(score, 3),
                })
    pairs.sort(key=lambda pair: (not pair["named"], -pair["overlap"], pair["ids"]))
    other_machine = [
        {"id": item["id"], "machine": item["scope"][len("machine:"):], "created": item["created"]}
        for item in items
        if item["scope"].startswith("machine:") and item["scope"] != f"machine:{local_machine}"
    ]
    return {"pairs": pairs, "other_machine": other_machine, "threshold": CROSS_PROJECT_SIMILARITY}


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
        run_golden(root, config_root),
        check_audit(root),
        check_tests(root, run_tests),
    ]
    summary = rating.summarize(config_root, now)
    available = _available_mb()
    transcripts_ok = available is not None and available >= TRANSCRIPT_MEMORY_FLOOR_MB
    this_week = summary["weeks"][0]
    rated_this_week = (rating.ratings_dir(config_root) / f"{rating.iso_week(now)}.json").exists()
    skipped = {"search_calls": 0, "injected_pairs": 0, "cited_pairs": 0, "rate": None, "never_cited_top": []}
    if not transcripts_ok:
        citation = {**skipped, "skipped": f"可用記憶體 {available} MB < {TRANSCRIPT_MEMORY_FLOOR_MB}"}
    else:
        try:
            citation = citation_signal(projects_dir, now, node_of=record_nodes(root))
        except MemoryError:
            citation = {**skipped, "skipped": "讀對話紀錄時碰到記憶體上限"}
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
    cross_project = cross_project_candidates(root, local_machine)
    failure_layer = failure_layer_signal(config_root, now)
    failure_layer["cited"] = sum(
        int(item.get("cited", 0))
        for node, item in (citation.get("by_node") or {}).items()
        if node == "failures" or node.endswith("/failures")
    )
    checks.append(check_peak_memory())
    scoreboard = {
        "useful_rate": this_week["useful_rate"],
        "harmful_rate": this_week["harmful_rate"],
        "citation_rate": citation["rate"],
        "repeated_mistakes": repeats["repeated_mistakes"],
    }
    report = {
        "date": now.date().isoformat(),
        "rated_this_week": rated_this_week,
        "scoreboard": scoreboard,
        "checks": checks,
        "signals": {"citation": citation, "repeats": repeats, "recovery": recovery, "failure_layer": failure_layer,
                    "cross_project": cross_project},
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
        ("citation_rate", "引用率", True), ("repeated_mistakes", "重複犯錯次數", False),
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
    lines.append(f"- 引用率：{citation['cited_pairs']}/{citation['injected_pairs']}（{_fmt(citation['rate'], True)}）"
                 + (f"（略過：{citation['skipped']}）" if citation.get("skipped") else ""))
    if citation.get("by_node"):
        lines.append("- 節點（注入／引用／引用率）：")
        lines += [f"  - {node}：{item['injected']}／{item['cited']}／{_fmt(item['rate'], True)}"
                  for node, item in citation["by_node"].items()]
    if citation["never_cited_top"]:
        lines.append("- 本週注入最多次但從未被引用：")
        lines += [f"  - {item['id']}（{item['injections']} 次）" for item in citation["never_cited_top"]]
    lines.append(f"- 重複犯錯：本週分組新增觀測 {repeats['new_observations_this_week']} 筆（自首次記錄起共 {repeats['new_observations']} 筆）；"
                 f"可能重複的 failure {len(repeats['possible_duplicate_failures'])} 筆")
    lines += [f"  - {d['new']} ≈ {d['existing']}（{d['overlap']:.0%}）" for d in repeats["possible_duplicate_failures"]]
    lines.append(f"- 重複交代的偏好：{len(repeats['possible_repeated_preferences'])} 筆")
    lines += [f"  - {d['new']} ≈ {d['existing']}（{d['overlap']:.0%}）" for d in repeats["possible_repeated_preferences"]]
    lines.append(f"- 失敗層：本週 PreToolUse 注入 {failure_layer['injected']} 次；failures 節點被引用 {failure_layer['cited']} 次")
    lines.append(f"- memoryctl search 呼叫：本週 {citation.get('search_calls', 0)} 次（不含在 ~/agent-memory 裡工作的 session）")
    lines += [f"  - {bump}：擋 {item['denied']}／放行 {item['released']}／改指令 {item['changed']}/{item['released']}"
              for bump, item in failure_layer["speed_bumps"].items()]
    lines.append(f"- 恢復成功率（compact／resume 有 CURRENT TASK）：{recovery['restored']}/{recovery['restored'] + recovery['missed']}（{_fmt(recovery['rate'], True)}）")
    lines += ["", "## 三、可能跨專案的教訓（資訊）", ""]
    lines += cross_project_lines(cross_project)
    return report, "\n".join(lines) + "\n"


CROSS_PROJECT_LIST_LIMIT = 20


def cross_project_lines(cross_project: dict[str, Any]) -> list[str]:
    """The report's cross-project section: at most CROSS_PROJECT_LIST_LIMIT
    pairs, named ones and the most similar first, then the total (ruling 52
    f); the JSON report keeps every pair."""
    pairs = cross_project["pairs"]
    lines = []
    if not pairs:
        lines.append(f"- 沒有找到（明文互指，或相似度 ≥ {cross_project['threshold']}）")
    for pair in pairs[:CROSS_PROJECT_LIST_LIMIT]:
        how = "明文互指" if pair["named"] else f"相似度 {pair['overlap']}"
        origins = "／".join("、".join(o) for o in pair["origins"])
        lines.append(f"- {pair['ids'][0]} ↔ {pair['ids'][1]}（{how}；學到的地方 {origins}）")
        lines.append("  - 建議：若是同一個教訓，合併成一筆 applies=global，或把其中一筆 supersede 成 applies=global")
    if len(pairs) > CROSS_PROJECT_LIST_LIMIT:
        lines.append(f"- 共 {len(pairs)} 對，只列前 {CROSS_PROJECT_LIST_LIMIT} 對（明文互指優先，再依相似度）；完整清單在同日的 JSON")
    elif pairs:
        lines.append(f"- 共 {len(pairs)} 對")
    if cross_project["other_machine"]:
        lines.append("- 綁在其他機器的紀錄（若根本原因與機器無關，考慮改成全域）：")
        lines += [f"  - {item['id']}（{item['machine'][:8]}…，{item['created']}）" for item in cross_project["other_machine"]]
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
