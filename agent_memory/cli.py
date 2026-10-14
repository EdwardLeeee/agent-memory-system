from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
import sys

from .adapters import adapter_text, install_claude_adapter, install_codex_adapter
from .core import (
    AGENT_HOOK_BUDGETS,
    ALLOWED_KINDS,
    MemoryError,
    audit_memory,
    check_bilingual_tag_coverage,
    check_origin_projects,
    build_context,
    checkpoint,
    config_home,
    current_session_id,
    export_bundle,
    handle_hook,
    health_status,
    initialize,
    initialize_git,
    import_bundle,
    list_sessions,
    memory_home,
    migrate_v2,
    migrate_v1,
    lesson_label,
    list_open_lessons,
    promote_lesson,
    record,
    reindex,
    router_toml_problems,
    semantic_documents,
    show_session,
    supersede,
    validate,
)
from .federation import (
    FederationConfigError,
    collect_source_entries,
    fetch_source_ref,
    install_source_hook,
    list_source_configs,
    load_source_config,
)
from .lexical_index import routing_report
from .semantic import SemanticUnavailable
from .semantic import verify_model_artifacts
from .semantic import reindex as reindex_semantic
from .semantic import set_enabled as set_semantic_enabled
from .semantic import setup as setup_semantic
from .semantic import status as semantic_status


def _read_input(path: str) -> dict[str, object]:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise MemoryError("input must be a JSON object")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="memoryctl")
    result.add_argument("--home", help="override AGENT_MEMORY_HOME")
    commands = result.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("--machine-id")
    init.add_argument("--git", action="store_true")

    remember = commands.add_parser("record")
    remember.add_argument("--kind", required=True)
    remember.add_argument("--input", required=True)

    resume = commands.add_parser("checkpoint")
    resume.add_argument("--input", required=True)
    resume.add_argument("--session-id")

    replace = commands.add_parser("supersede")
    replace.add_argument("--id", required=True)
    replace.add_argument("--input", required=True)

    hook = commands.add_parser("hook")
    hook.add_argument(
        "event",
        choices=("session-start", "user-prompt-submit", "pre-compact", "pre-tool-use", "stop"),
    )
    hook.add_argument("--agent", choices=("claude", "codex"))

    migrate = commands.add_parser("migrate-v1")
    migrate.add_argument("--source", required=True)
    commands.add_parser("migrate-v2")

    export = commands.add_parser("export")
    export.add_argument("--output", required=True)

    restore = commands.add_parser("import")
    restore.add_argument("--input", required=True)
    restore.add_argument("--new-machine-id")

    adapter = commands.add_parser("adapter")
    adapter_commands = adapter.add_subparsers(dest="adapter_command", required=True)
    adapter_install = adapter_commands.add_parser("install")
    adapter_install.add_argument("agent", choices=("codex", "claude"))
    adapter_install.add_argument("--agent-home")
    adapter_show = adapter_commands.add_parser("show")
    adapter_show.add_argument("agent", choices=("codex", "claude", "gemini", "generic"))

    context = commands.add_parser("context")
    context.add_argument("--cwd", required=True)
    context.add_argument("--query", default="")
    context.add_argument("--budget-tokens", type=int, default=None)
    context.add_argument("--agent", choices=("claude", "codex"))
    context.add_argument("--explain", action="store_true")
    context.add_argument("--session-id")
    context.add_argument(
        "--retrieval-mode",
        choices=("auto", "lexical", "hybrid"),
        default="auto",
    )

    search = commands.add_parser(
        "search", help="look up any record, idle capsule, archived version, or event"
    )
    search.add_argument("--query", help="words to look for")
    search.add_argument("--id", help="print one record in full")
    search.add_argument("--kind", choices=sorted(ALLOWED_KINDS | {"note"}))
    search.add_argument("--project", help="only records of project:<name>")
    search.add_argument("--since", help="only records updated on or after YYYY-MM-DD")
    search.add_argument("--include-archive", action="store_true", help="also superseded versions and events")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--json", action="store_true")

    audit = commands.add_parser("audit")
    audit.add_argument("--scope")
    audit.add_argument("--json", action="store_true")

    health = commands.add_parser("health")
    health.add_argument("--json", action="store_true")

    sessions = commands.add_parser("sessions")
    session_commands = sessions.add_subparsers(dest="sessions_command", required=True)
    sessions_list = session_commands.add_parser("list")
    sessions_list.add_argument("--json", action="store_true")
    sessions_show = session_commands.add_parser("show")
    sessions_show.add_argument("--session-id", required=True)
    sessions_show.add_argument("--json", action="store_true")

    semantic = commands.add_parser("semantic")
    semantic_commands = semantic.add_subparsers(
        dest="semantic_command", required=True
    )
    semantic_status_parser = semantic_commands.add_parser("status")
    semantic_status_parser.add_argument("--json", action="store_true")
    semantic_commands.add_parser("setup")
    semantic_commands.add_parser("verify")
    semantic_commands.add_parser("reindex")
    semantic_commands.add_parser("enable")
    semantic_commands.add_parser("disable")

    lesson = commands.add_parser("lesson")
    lesson_commands = lesson.add_subparsers(dest="lesson_command", required=True)
    lesson_promote = lesson_commands.add_parser("promote")
    lesson_promote.add_argument("derived_id")
    lesson_promote.add_argument("--applies-to", required=True)
    lesson_promote.add_argument("--rule", required=True)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("action", nargs="?", choices=("run", "install-timer"), default="run")
    doctor.add_argument("--notify", action="store_true")
    doctor.add_argument("--no-tests", action="store_true")
    doctor.add_argument("--report-dir")
    doctor.add_argument("--projects-dir")
    doctor.add_argument("--unit-dir", help=argparse.SUPPRESS)
    doctor.add_argument("--enable", action="store_true", help="also enable the timer (needs the user's approval)")
    doctor.add_argument("--now", help=argparse.SUPPRESS)

    rate = commands.add_parser("rate")
    rate.add_argument("--summary", action="store_true")
    rate.add_argument("--projects-dir")
    rate.add_argument("--days", type=int, default=7)
    rate.add_argument("--now", help=argparse.SUPPRESS)

    lessons = commands.add_parser("lessons")
    lessons.add_argument("--cwd")
    lessons.add_argument("--subject", help="only lessons that apply to this subject (or to all)")
    # The old name, kept so existing scripts keep working.
    lessons.add_argument("--ticker", dest="subject", help=argparse.SUPPRESS)
    lessons.add_argument("--candidates", action="store_true")
    lessons.add_argument("--json", action="store_true")

    sources = commands.add_parser("sources")
    sources_commands = sources.add_subparsers(dest="sources_command", required=True)
    sources_list = sources_commands.add_parser("list")
    sources_list.add_argument("--json", action="store_true")
    sources_sync = sources_commands.add_parser("sync")
    sources_sync.add_argument("name")
    sources_hook = sources_commands.add_parser("install-hook")
    sources_hook.add_argument("name")

    commands.add_parser("validate")
    commands.add_parser("reindex")
    return result


def _run_doctor(root: Path, args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from . import doctor

    if args.action == "install-timer":
        memoryctl = Path(__file__).resolve().parents[1] / "bin" / "memoryctl"
        service, timer = doctor.install_timer(
            memoryctl,
            Path(args.unit_dir) if args.unit_dir else None,
            enable=args.enable,
        )
        print(f"Wrote {service} and {timer}")
        if not args.enable:
            print(f"Not enabled. To enable after approval: {doctor.ENABLE_COMMAND}")
        return 0
    doctor.apply_memory_cap(sys.argv)
    now = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else datetime.now(timezone.utc).astimezone()
    )
    report_dir = Path(args.report_dir) if args.report_dir else doctor.resolve_report_dir(config_home())
    report, markdown = doctor.build_report(
        root,
        config_home(),
        now,
        report_dir=report_dir,
        projects_dir=Path(args.projects_dir) if args.projects_dir else None,
        run_tests=not args.no_tests,
    )
    md_path = doctor.write_report(report, markdown, report_dir)
    if args.notify:
        doctor.notify(report, markdown, md_path)
    warns = sum(check["status"] == "WARN" for check in report["checks"])
    print(f"Wrote {md_path} ({warns} WARN)")
    return 0


def _record_source_sync(name: str, config: dict, entries: int, tree: Any = None) -> None:
    """Machine-local note of when a source was last synced, and the commit
    actually read (ruling 51 g), for doctor."""
    import subprocess
    from datetime import datetime, timezone

    from .federation import SourceTree
    from .storage import atomic_write_text, git_environment

    tree = tree or SourceTree(config["repo"], config.get("ref"))
    ref_commit = None if tree.missing_ref else tree.commit
    head = subprocess.run(
        ["git", "-C", str(config["repo"]), "log", "-1", "--format=%cI", tree.commit or "HEAD"], env=git_environment(),
        text=True, capture_output=True, check=False,
    ).stdout.strip()
    try:
        path = config_home() / "runtime" / "sources" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path,
            json.dumps(
                {
                    "synced_at": datetime.now(timezone.utc).isoformat(), "head_committed_at": head,
                    "ref": config.get("ref") or "HEAD", "ref_commit": ref_commit, "entries": entries,
                    **({"ref_problem": f"ref {config.get('ref')} 不存在"} if tree.missing_ref else {}),
                },
                ensure_ascii=False,
            )
            + "\n",
        )
    except OSError:
        pass


def _run_rate(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from . import rating

    now = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else datetime.now(timezone.utc)
    )
    config_root = config_home()
    if args.summary:
        summary = rating.summarize(config_root, now)
        print("週別        評分數  有用率  礙事率")
        for week in summary["weeks"]:
            useful = "n/a" if week["useful_rate"] is None else f"{week['useful_rate']:.0%}"
            harmful = "n/a" if week["harmful_rate"] is None else f"{week['harmful_rate']:.0%}"
            print(f"{week['week']:10s}  {week['rated']:6d}  {useful:>6s}  {harmful:>6s}")
        if summary["flagged_records"]:
            print("被標「沒用／礙事」三次以上的紀錄（只列出，不會自動改）：")
            for item in summary["flagged_records"]:
                print(f"  {item['id']}  ×{item['count']}")
        return 0
    projects_dir = Path(args.projects_dir) if args.projects_dir else rating.default_projects_dir()
    candidates = rating.collect_candidates(projects_dir, now, args.days)
    items = rating.sample(
        candidates, rating.already_rated_keys(config_root, now), rating.iso_week(now)
    )
    if not items:
        print("最近沒有可以評分的訊息。")
        return 0
    print(f"本週評分：{len(items)} 題。回答只存在這台電腦，不會進 Git，也不會給 AI 看。")
    results = rating.run_interactive(items, input, print, now)
    path = rating.save_ratings(config_root, rating.iso_week(now), results)
    print(f"已存 {len(results)} 題到 {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = memory_home(args.home)
    try:
        if args.command == "init":
            machine_id = initialize(root, args.machine_id)
            if args.git:
                initialize_git(root)
            print(f"Initialized durable memory at {root} for machine {machine_id}")
            return 0
        if args.command == "record":
            record_id = record(root, args.kind, _read_input(args.input))
            print(f"Recorded {record_id}")
            return 0
        if args.command == "checkpoint":
            session_id = current_session_id(args.session_id)
            path = checkpoint(root, _read_input(args.input), session_id)
            print(f"Checkpointed current task at {path}")
            return 0
        if args.command == "supersede":
            old_version, event_id = supersede(root, args.id, _read_input(args.input))
            print(f"Superseded {args.id} version {old_version}; event {event_id}")
            return 0
        if args.command == "hook":
            if args.event == "pre-tool-use":
                # bin/memoryctl normally takes this before importing anything;
                # this path serves `--home X hook ...` and the console script.
                from .pretool import main as pretool_main

                os.environ["AGENT_MEMORY_HOME"] = str(root)
                return pretool_main(["--agent", args.agent] if args.agent else [])
            output = handle_hook(root, args.event, _read_input("-"), agent=args.agent)
            print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
            return 0
        if args.command == "migrate-v1":
            active, historical, hot = migrate_v1(root, Path(args.source))
            suffix = "HOT imported" if hot else "HOT missing"
            print(
                f"Migrated {active} active records and {historical} historical events; {suffix}"
            )
            return 0
        if args.command == "migrate-v2":
            changed = migrate_v2(root)
            if changed:
                print("Migrated store manifest from v2 to v3; v2 record bytes preserved")
            else:
                print("Store manifest is already v3")
            return 0
        if args.command == "export":
            output = Path(args.output)
            export_bundle(root, output)
            print(f"Encrypted memory export created at {output.expanduser().resolve()}")
            return 0
        if args.command == "import":
            mode, machine_id = import_bundle(root, Path(args.input), args.new_machine_id)
            if mode == "empty":
                print(f"Imported memory into empty store for machine {machine_id}")
            else:
                print(f"Merged encrypted memory export for machine {machine_id}")
            return 0
        if args.command == "adapter":
            if args.adapter_command == "show":
                print(adapter_text(args.agent), end="")
                return 0
            defaults = {
                "codex": ("CODEX_HOME", ".codex"),
                "claude": ("CLAUDE_CONFIG_DIR", ".claude"),
            }
            home_env, home_dir = defaults[args.agent]
            agent_home = (
                Path(args.agent_home)
                if args.agent_home
                else Path(os.environ.get(home_env, Path.home() / home_dir))
            )
            if args.agent == "claude":
                guidance, hooks = install_claude_adapter(agent_home)
                label = "Claude Code"
            else:
                guidance, hooks = install_codex_adapter(agent_home)
                label = "Codex"
            print(f"Installed {label} durable-memory adapter: {guidance}, {hooks}")
            return 0
        if args.command == "semantic":
            if args.semantic_command == "status":
                status = semantic_status(root, config_home(), semantic_documents(root))
                if args.json:
                    print(json.dumps(status, ensure_ascii=False, sort_keys=True, indent=2))
                else:
                    line = (
                        "Semantic retrieval: "
                        f"activation={status['activation_state']}, "
                        f"enabled={str(status['enabled']).lower()}, "
                        f"model_installed={str(status['model_installed']).lower()}, "
                        f"index={status['index_status']}"
                    )
                    if status["activation_code"]:
                        line += f" [{status['activation_code']}]"
                    if not status["threshold_calibrated"]:
                        line += (
                            "\nThreshold "
                            f"{status['min_similarity']} is an uncalibrated default, "
                            "not a reviewed production setting."
                        )
                    print(line)
                return 0
            if args.semantic_command == "setup":
                try:
                    status = setup_semantic(root, config_home())
                except SemanticUnavailable as exc:
                    raise MemoryError(str(exc)) from exc
                print(
                    "Verified local semantic model: "
                    f"{status['model_id']} at {status['revision']}"
                )
                return 0
            if args.semantic_command == "verify":
                code = verify_model_artifacts(root)
                if code is not None:
                    raise MemoryError(
                        f"semantic model verification failed [{code}]"
                    )
                print(
                    "Semantic model verified: every declared artifact matches "
                    "its recorded size and SHA-256"
                )
                return 0
            if args.semantic_command == "reindex":
                documents = semantic_documents(root)
                try:
                    count = reindex_semantic(root, config_home(), documents)
                except SemanticUnavailable as exc:
                    raise MemoryError(str(exc)) from exc
                print(f"Indexed {count} active records for semantic retrieval")
                return 0
            if args.semantic_command == "disable":
                set_semantic_enabled(config_home(), False)
                print("Semantic retrieval disabled; lexical retrieval remains available")
                return 0
            if args.semantic_command == "enable":
                status = semantic_status(root, config_home(), semantic_documents(root))
                if not status["model_installed"] or status["index_status"] != "ready":
                    raise MemoryError(
                        "run `memoryctl semantic setup` and "
                        "`memoryctl semantic reindex` before enabling semantic retrieval"
                    )
                # An installed model and a built index say nothing about whether
                # retrieval quality ever passed a gate. Activation requires a
                # reviewed evaluation artifact that still matches this store.
                # Status only stats the artifacts; enabling rehashes them.
                code = verify_model_artifacts(root)
                if code is not None:
                    raise MemoryError(
                        f"refusing to enable semantic retrieval: "
                        f"model verification failed [{code}]"
                    )
                if status["activation_state"] not in ("approved", "disabled"):
                    raise MemoryError(
                        "refusing to enable semantic retrieval: "
                        f"{status['activation_state']} "
                        f"[{status['activation_code']}]. Enabling requires a reviewed "
                        "activation artifact whose metrics met their gates and whose "
                        "profile, contracts, model, and corpus fingerprint still match "
                        "this store."
                    )
                set_semantic_enabled(config_home(), True)
                print(
                    "Semantic retrieval enabled at reviewed threshold "
                    f"{status['min_similarity']}"
                )
                return 0
            raise MemoryError(
                f"semantic {args.semantic_command} is not available until setup is implemented"
            )
        if args.command == "search":
            from datetime import date as _date

            from . import history

            if bool(args.query) == bool(args.id):
                raise MemoryError("search needs exactly one of --query or --id")
            if args.id:
                print(history.show(root, args.id, include_archive=args.include_archive), end="")
                return 0
            try:
                since = _date.fromisoformat(args.since) if args.since else None
            except ValueError as exc:
                raise MemoryError("--since must be YYYY-MM-DD") from exc
            hits = history.search(
                root, args.query, kind=args.kind, project=args.project, since=since,
                include_archive=args.include_archive, limit=args.limit,
            )
            if args.json:
                print(json.dumps({"count": len(hits), "hits": hits}, ensure_ascii=False, indent=2))
            else:
                print(history.format_hits(hits), end="")
            return 0
        if args.command == "context":
            if args.agent and args.budget_tokens is not None:
                raise MemoryError("--agent sets the budget; do not combine it with --budget-tokens")
            if args.agent:
                # The hook's own budget for this agent: prompt pack with a
                # query, start pack without one.
                budget = AGENT_HOOK_BUDGETS[args.agent][1 if args.query.strip() else 0]
            else:
                budget = 5000 if args.budget_tokens is None else args.budget_tokens
            print(
                build_context(
                    root,
                    Path(args.cwd),
                    args.query,
                    budget,
                    args.explain,
                    current_session_id(args.session_id),
                    args.retrieval_mode,
                    agent=args.agent,
                ),
                end="",
            )
            return 0
        if args.command == "audit":
            report = audit_memory(root, args.scope)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
            else:
                summary = report["summary"]
                capture = report["capture"]
                print(
                    f"Memory audit: {summary['decisions']} decisions, "
                    f"{summary['experiences']} experiences, {summary['issues']} issues"
                )
                days = capture["days_since_last_write"]
                print(
                    "Last durable write: "
                    + (f"{days} day(s) ago" if days is not None else "never")
                )
                for record_scope, counts in sorted(capture["by_scope"].items()):
                    print(
                        f"  capture[{record_scope}]: "
                        f"{counts['with_experience']}/{counts['decisions']} "
                        "decisions have a linked experience"
                    )
                stale_facts = report["stale_facts"]
                print(
                    f"Stale facts: {len(stale_facts)}/{report['summary']['facts']} "
                    "(missing expires_at or already expired)"
                )
                for issue in report["issues"]:
                    detail = issue.get("experience_id") or issue.get(
                        "latest_experience_id"
                    )
                    suffix = f" via {detail}" if detail else ""
                    print(
                        f"- {issue['code']}: {issue['decision_id']}{suffix}: "
                        f"{issue['message']}"
                    )
            return 0
        if args.command == "health":
            report = health_status(root)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
            else:
                print(f"Index: {report['index']['status']}")
                for name, item in report["hooks"].items():
                    detail = f" ({item['code']})" if item.get("code") else ""
                    print(f"- {name}: {item['status']}{detail}")
            return 0
        if args.command == "sessions":
            if args.sessions_command == "list":
                items = list_sessions(root)
                if args.json:
                    print(
                        json.dumps(
                            {"count": len(items), "sessions": items},
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        )
                    )
                else:
                    print(f"Sessions: {len(items)}")
                    for item in items:
                        print(
                            f"- {item['session_id']} [{item['state']}]: "
                            f"{item['active_task']} ({item['updated_at']})"
                        )
                return 0
            item = show_session(root, args.session_id)
            if args.json:
                print(json.dumps(item, ensure_ascii=False, sort_keys=True, indent=2))
            else:
                for key in (
                    "session_id",
                    "state",
                    "cwd",
                    "active_task",
                    "paused_task",
                    "resume_point",
                    "next_action",
                    "validation",
                    "rollback",
                    "updated_at",
                ):
                    if key in item:
                        print(f"{key}: {item[key]}")
            return 0
        if args.command == "lesson":
            if args.lesson_command == "promote":
                canonical_id = promote_lesson(
                    root, args.derived_id, args.applies_to, args.rule
                )
                print(f"Promoted {args.derived_id} to canonical lesson {canonical_id}")
                return 0
        if args.command == "rate":
            return _run_rate(args)
        if args.command == "doctor":
            return _run_doctor(root, args)
        if args.command == "lessons":
            lessons_found = list_open_lessons(
                root,
                cwd=Path(args.cwd) if args.cwd else None,
                subject=args.subject,
                candidates_only=args.candidates,
            )
            if args.json:
                print(json.dumps(lessons_found, ensure_ascii=False, indent=2))
            else:
                if not lessons_found:
                    print("No open lessons match.")
                for item in lessons_found:
                    marker = "promoted" if item["promoted"] else "derived"
                    label = lesson_label(item)
                    print(
                        f"- [{marker}] {item['id']} (applies_to={item['applies_to']})"
                        + (f" {label}" if label and label != "(derived)" else "")
                    )
                    print(f"    {item['value']}")
            return 0
        if args.command == "sources":
            if args.sources_command == "list":
                names = list_source_configs(config_home())
                if args.json:
                    print(json.dumps(names, ensure_ascii=False))
                else:
                    if not names:
                        print("No source configs registered.")
                    for name in names:
                        print(name)
                return 0
            if args.sources_command == "sync":
                try:
                    config = load_source_config(config_home(), args.name)
                except FederationConfigError as exc:
                    raise MemoryError(str(exc)) from exc
                from .federation import SourceTree, ref_is_remote

                ref = config.get("ref")
                fetched = fetch_source_ref(config) if ref_is_remote(ref, config["repo"]) else None
                tree = SourceTree(config["repo"], ref)
                entries = collect_source_entries(config, tree)
                reindex(root)
                _record_source_sync(args.name, config, len(entries), tree)
                if tree.missing_ref:
                    print(f"ref {ref} 不存在，改讀工作區", file=sys.stderr)
                    where = f"{config['repo']} (working tree)"
                elif ref:
                    where = f"{config['repo']} at {ref} ({(tree.commit or '')[:12]})"
                else:
                    where = str(config["repo"])
                note = " (fetch failed; read the ref as it was)" if fetched is False and not tree.missing_ref else ""
                print(f"Synced source {args.name!r}: {len(entries)} derived entries from {where}{note}")
                return 0
            if args.sources_command == "install-hook":
                try:
                    hook_path = install_source_hook(
                        config_home(),
                        root,
                        Path(__file__).resolve().parent.parent / "bin" / "memoryctl",
                        args.name,
                    )
                except FederationConfigError as exc:
                    raise MemoryError(str(exc)) from exc
                print(f"Installed post-commit sync hook at {hook_path}")
                return 0
        if args.command == "validate":
            count, errors = validate(root)
            errors += [f"router.toml: {item}" for item in router_toml_problems()]
            origin_warnings = check_origin_projects(root)
            if errors:
                print("Memory validation failed:", file=sys.stderr)
                for item in errors:
                    print(f"- {item}", file=sys.stderr)
                for warning in origin_warnings:
                    print(f"Warning: {warning}", file=sys.stderr)
                return 1
            print(f"Memory validation passed: {count} active records")
            for warning in [*origin_warnings, *check_bilingual_tag_coverage(root)]:
                print(f"Warning: {warning}")
            return 0
        if args.command == "reindex":
            count = reindex(root)
            print(f"Indexed {count} active records")
            for line in routing_report(root):
                print(line)
            return 0
    except SemanticUnavailable as exc:
        code = getattr(exc, "code", "semantic-unavailable")
        print(f"memoryctl: {exc} [{code}]", file=sys.stderr)
        return 2
    except (MemoryError, OSError, json.JSONDecodeError) as exc:
        print(f"memoryctl: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
