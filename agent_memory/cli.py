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
    resume.add_argument("--input")
    resume.add_argument("--session-id")
    # Change the saved capsule in place, no file needed (F10, T5).
    resume.add_argument("--set", action="append", metavar="FIELD=VALUE",
                        help="a capsule field, tasks.<id>.<title|status|waiting_on>, or tasks.<id>= to remove one")
    resume.add_argument("--show", action="store_true", help="print this session's capsule")

    skeleton = commands.add_parser("template", help="print the JSON skeleton of a write")
    skeleton.add_argument("kind")

    todo = commands.add_parser("task", help="tasks with a due time")
    todo_commands = todo.add_subparsers(dest="task_command", required=True)
    todo_done = todo_commands.add_parser("done", help="mark a task done (a new version, as a supersede)")
    todo_done.add_argument("id")
    todo_done.add_argument("--reason")

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
    search.add_argument("--as-of", help="facts only: those that held on this date (YYYY-MM-DD), by valid_from/valid_until and older versions")
    search.add_argument(
        "--context", action="store_true",
        help="with --id: the conversation the record was learned in (the user's message before it was written and the AI's reply after)",
    )
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
    doctor.add_argument("action", nargs="?", choices=("run", "install-timer", "dismiss"), default="run")
    doctor.add_argument("ids", nargs="*", help="dismiss: records that are not one lesson; no merge draft for them again")
    doctor.add_argument("--notify", action="store_true")
    doctor.add_argument("--no-tests", action="store_true")
    doctor.add_argument("--report-dir")
    doctor.add_argument("--projects-dir")
    doctor.add_argument("--unit-dir", help=argparse.SUPPRESS)
    doctor.add_argument("--enable", action="store_true", help="also enable the timer (needs the user's approval)")
    doctor.add_argument("--now", help=argparse.SUPPRESS)

    learned_in = commands.add_parser(
        "learned-in", help="find, in the agents' transcripts, the session that wrote each existing record"
    )
    learned_in.add_argument("action", choices=("backfill",))
    learned_in.add_argument("--claude-dir", help="Claude Code transcripts (default ~/.claude/projects)")
    learned_in.add_argument("--codex-dir", help="Codex transcripts (default ~/.codex/sessions)")
    learned_in.add_argument("--days", type=int, help="only transcripts changed in the last N days")
    replay = commands.add_parser(
        "replay", help="measure what the hooks inject over real sessions: as recorded, or replayed by code trees on a copy"
    )
    replay.add_argument("--days", type=int, default=7)
    replay.add_argument("--claude-dir", help="Claude Code transcripts (default ~/.claude/projects and ~/.claude-profiles/*/projects)")
    replay.add_argument("--codex-dir", help="Codex transcripts (default ~/.codex/sessions)")
    replay.add_argument("--session", action="append", default=[], help="only sessions whose id starts with this")
    replay.add_argument("--code", action="append", default=[],
                        help="a code tree to replay on a copy of the ledger and configuration (repeatable)")
    replay.add_argument("--json", help="also write the numbers to this file (record ids and counts, no message text)")
    replay.add_argument("--watch", action="append", default=[], metavar="ID",
                        help="also count where this record was injected, by kind of turn and session")
    replay.add_argument("--usage-samples", type=int, metavar="N",
                        help="print N random content matches of the usage rule, to check them by eye")
    replay.add_argument("--fidelity", type=int, metavar="N",
                        help="replay N sampled events on the program and records live had then, and explain every difference")
    replay.add_argument("--now", help=argparse.SUPPRESS)
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

    # Markdown views of the ledger where another tool reads them (F14).
    generate = commands.add_parser("generate")
    generate_commands = generate.add_subparsers(dest="generate_command", required=True)
    generate_commands.add_parser("list")
    generate_run = generate_commands.add_parser("run")
    generate_run.add_argument("--name", action="append")
    generate_run.add_argument("--adopt", action="store_true",
                              help="replace a file the memory system did not write, after backing it up")
    generate_run.add_argument("--dry-run", action="store_true")

    commands.add_parser("validate")
    commands.add_parser("reindex")
    return result


def _run_doctor(root: Path, args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from . import doctor

    if args.ids and args.action != "dismiss":
        raise MemoryError("record ids are only for doctor dismiss")
    if args.action == "dismiss":
        from . import merge_drafts

        ids = list(dict.fromkeys(args.ids))
        if len(ids) < 2:
            raise MemoryError("doctor dismiss needs two or more record ids")
        missing = [record_id for record_id in ids if merge_drafts._load(root, record_id) is None]
        if missing:
            raise MemoryError(f"no active record: {', '.join(missing)}")
        if merge_drafts.dismiss(config_home(), ids, datetime.now(timezone.utc)):
            print(f"Dismissed {len(ids)} records as not one lesson; doctor drafts no merge for them again "
                  f"({merge_drafts.dismissed_path(config_home())})")
        else:
            print("Already dismissed together; nothing changed")
        return 0
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


def _run_replay(root: Path, args: argparse.Namespace) -> int:
    from datetime import datetime, timedelta, timezone

    from . import replay
    from .pretool import cwd_projects
    from .storage import atomic_write_text

    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)
    claude_dir = Path(args.claude_dir).expanduser() if args.claude_dir else replay.default_claude_dirs()
    codex_dir = Path(args.codex_dir).expanduser() if args.codex_dir else replay.default_codex_dir()
    files = replay.transcript_files(claude_dir, codex_dir, now, args.days)
    window = replay.Window(now - timedelta(days=args.days), set(args.session) or None)
    if args.fidelity:
        checked = replay.fidelity(files, root, config_home(), args.fidelity, now=now, window=window)
        print(f"## 重放吻合率：抽 {checked['sampled']} 個事件（紀錄裡被擋下的 {checked['holds']} 個）、"
              f"{checked['versions']} 個當時的版本；比帳本最新 commit 還晚的 {checked['after_ledger']} 個事件不抽")
        for result, count in sorted(checked["results"].items(), key=lambda kv: -kv[1]):
            how = replay.VERIFIED_BY.get(result)
            print(f"- {'相同' if result == 'same' else replay.CAUSES.get(result, result)}：{count}"
                  + (f"（驗證：{how}）" if how else ""))
        unknown = checked["results"].get("unknown", 0)
        print(f"- 合計：已解釋 {checked['sampled'] - checked['unverified'] - unknown}、未驗證 {checked['unverified']}、"
              f"原因不明 {unknown}")
        for item in checked["differing"]:
            only_actual = sorted(set(item["actual"] or ()) - set(item["replayed"] or ()))
            only_replay = sorted(set(item["replayed"] or ()) - set(item["actual"] or ()))
            pointed = sorted(set(item.get("pointers") or ()) ^ set(item.get("replayed_pointers") or ()))
            linked = (f"；指標只在一邊 {pointed}" if pointed else "") + "".join(
                f"；較早只有{'重放' if side == 'replay' else '實際'}給過 {record_id}（{at[:16]}）"
                for record_id, (side, at) in sorted((item.get("given_apart") or {}).items())) + "".join(
                f"；來源照當時的 commit 重放相同（{name} {commit[:8]}）" for name, commit in sorted((item.get("pinned") or {}).items()))
            print(f"  - [{item['result']}] {item['stratum']} {item['session'][:8]} {item['at'][:16]} v{item['version']}："
                  f"只在實際 {only_actual}；只在重放 {only_replay}{linked}")
        if args.json:
            atomic_write_text(Path(args.json).expanduser(), json.dumps(checked, ensure_ascii=False, indent=1) + "\n")
        return 0
    if args.usage_samples:
        measured = replay.measure_actual(files, root, lambda cwd: cwd_projects(cwd, root), window, None, args.usage_samples)
        samples = measured.get("evidence") or []
        print(f"## 判斷規則的「內容相符」抽樣：{len(samples)} 個（AI 輸出裡含祕密的不列；email 換成 [email]）")
        for number, item in enumerate(samples, start=1):
            print(f"{number}. {item['id']}｜{'原文片語' if item['kind'] == 'phrase' else '特有字串'}：{item['feature']}")
            print(f"   AI 輸出：…{item['agent']}…")
        return 0
    if args.code:
        result = replay.replay(files, root, config_home(), [Path(code).expanduser().resolve() for code in args.code], window,
                               set(args.watch))
    else:
        result = {"actual": replay.measure_actual(files, root, lambda cwd: cwd_projects(cwd, root), window, set(args.watch)),
                  "codes": {}}
    result["actual"]["missing"] = replay.missing_folders(claude_dir, codex_dir)
    print(f"## 紀錄裡實際的注入（近 {args.days} 天）")
    for line in replay.reading_lines(result["actual"]) + replay.summary_lines(result["actual"]) + replay.watch_lines(result["actual"]):
        print(line)
    for code, measured in result["codes"].items():
        fidelity = measured["fidelity"]
        print("")
        print(f"## 用 {code} 重放")
        print(f"- 送出 {measured['hook_events']:,} 個 hook 事件，出錯 {measured['hook_errors']:,} 個；"
              f"UPS 注入的紀錄和實際相同：{fidelity['same_records']:,}/{fidelity['ups_events']:,}")
        for line in replay.summary_lines(measured, result["actual"], "實際") + replay.watch_lines(measured):
            print(line)
    if args.json:
        atomic_write_text(Path(args.json).expanduser(), json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    return 0


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
    from . import usage

    detector = usage.UsageDetector(usage.FeatureFrequency.from_index(memory_home(args.home)))
    candidates = rating.collect_candidates(projects_dir, now, args.days, detector, memory_home(args.home))
    items = rating.sample(
        candidates, rating.already_rated_keys(config_root, now), rating.iso_week(now)
    )
    if not items:
        print("最近沒有可以評分的訊息。")
        return 0
    print(f"本週評分：{len(items)} 題。回答只存在這台電腦，不會進 Git，也不會給 AI 看。")
    print("d、r、m 只是多看一點，不算作答：d 看記憶全文、r 看 AI 當時的回答、m 看完整訊息。")
    week = rating.iso_week(now)
    path = rating.ratings_dir(config_root) / f"{week}.json"
    saved: list[dict[str, Any]] = []

    def save(result: dict[str, Any]) -> None:
        # Each answer is written at once, so stopping halfway keeps it.
        rating.save_ratings(config_root, week, [result])
        saved.append(result)

    try:
        names = {m["session_id"]: m["session_name"] for m in candidates if m.get("session_name")}
        rating.run_interactive(items, input, print, now, root=memory_home(args.home), save=save, names=names)
    except (KeyboardInterrupt, EOFError):
        print("")
    print(f"已存 {len(saved)} 題到 {path}")
    if saved:
        # The ranking feedback follows the new ratings at once (A1).
        from . import doctor as doctor_module
        from . import feedback

        try:
            feedback.rebuild(config_root, now, doctor_module.resolve_report_dir(config_root), projects_dir=projects_dir,
                             root=memory_home(args.home))
        except Exception:
            pass
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
        if args.command == "task":
            from .core import task_done

            old_version, event_id = task_done(root, args.id, args.reason)
            print(f"Marked {args.id} done; version {old_version} archived; event {event_id}")
            return 0
        if args.command == "template":
            from .forms import TEMPLATE_KINDS, template

            if args.kind not in TEMPLATE_KINDS:
                raise MemoryError(f"template takes one of: {', '.join(TEMPLATE_KINDS)}")
            print(json.dumps(template(args.kind), ensure_ascii=False, indent=2))
            return 0
        if args.command == "checkpoint":
            from .core import CAPSULE_LIMIT, CAPSULE_WARN, capsule_payload, checkpoint_report

            session_id = current_session_id(args.session_id)
            if args.show:
                path, _ = capsule_payload(root, session_id)
                print(path.read_text(encoding="utf-8"), end="")
                return 0
            if not args.input and not args.set:
                raise MemoryError("checkpoint needs --input, --set or --show")
            # Without --input, the saved capsule is read and changed under the
            # write lock, so two --set at once both land (review 2, item 8).
            payload = _read_input(args.input) if args.input else None
            path, report = checkpoint_report(root, payload, session_id, args.set or [])
            print(f"Checkpointed current task at {path}")
            if report["dropped_events"]:
                print(f"dropped the {report['dropped_events']} oldest resume_point events to stay under {CAPSULE_LIMIT} bytes "
                      "(git keeps the earlier versions):", file=sys.stderr)
                for head in report["dropped_heads"]:
                    print(f"  - {head}", file=sys.stderr)
            if report["dropped_done"]:
                print(f"dropped {report['dropped_done']} done tasks past the newest three", file=sys.stderr)
            if report["bytes"] > CAPSULE_WARN:
                print(f"warning: the capsule is {report['bytes']} of {CAPSULE_LIMIT} bytes; merge resume_point's events "
                      "into fewer, or move work items to tasks (checkpoint --set tasks.<id>.title=... "
                      "--set tasks.<id>.status=doing)", file=sys.stderr)
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

            if args.as_of:
                try:
                    day = _date.fromisoformat(args.as_of)
                except ValueError as exc:
                    raise MemoryError("--as-of must be YYYY-MM-DD") from exc
                if args.id:
                    raise MemoryError("--as-of lists facts; it does not take --id")
                print(history.format_as_of(history.as_of(root, day, args.query), day), end="")
                return 0
            if bool(args.query) == bool(args.id):
                raise MemoryError("search needs exactly one of --query or --id")
            if args.id:
                if args.context:
                    from . import learned

                    print(learned.context_text(root, args.id), end="")
                    return 0
                print(history.show(root, args.id, include_archive=args.include_archive), end="")
                return 0
            if args.context:
                raise MemoryError("--context needs --id")
            try:
                since = _date.fromisoformat(args.since) if args.since else None
            except ValueError as exc:
                raise MemoryError("--since must be YYYY-MM-DD") from exc
            hits = history.search(
                root, args.query, kind=args.kind, project=args.project, since=since,
                include_archive=args.include_archive, limit=args.limit, cwd=Path.cwd(),
            )
            if args.json:
                print(json.dumps({"count": len(hits), "hits": hits}, ensure_ascii=False, indent=2))
            else:
                print(history.format_hits(hits), end="")
            return 0
        if args.command == "learned-in":
            from . import learned

            summary = learned.backfill(
                root,
                Path(args.claude_dir).expanduser() if args.claude_dir else None,
                Path(args.codex_dir).expanduser() if args.codex_dir else None,
                args.days,
            )
            print(
                f"讀了 {summary['files_scanned']} 份對話紀錄，新找到 {summary['events']} 次寫入；"
                f"快取裡共有 {summary['records']} 筆紀錄知道是在哪個 session 學到的。"
            )
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
        if args.command == "replay":
            return _run_replay(root, args)
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
        if args.command == "generate":
            from .generate import load_targets, run as run_generate

            if args.generate_command == "list":
                targets, problems = load_targets()
                outcome = {row["name"]: row for row in run_generate(root, dry_run=True)["targets"]}
                if not targets and not problems:
                    print("No files configured (generate.toml).")
                for target in targets:
                    row = outcome.get(target["name"], {})
                    filters = " ".join(f"{key}={','.join(values)}" for key, values in target["filters"].items())
                    print(f"{target['name']}: {target['path']}\n  {filters}; {row.get('records', 0)} records; "
                          f"next run: {row.get('status', '?')}" + (f" (backs up: {row['backup']})" if row.get("backup") else ""))
                for problem in problems:
                    print(f"problem: {problem}", file=sys.stderr)
                return 1 if problems else 0
            result = run_generate(root, args.name, adopt=args.adopt, dry_run=args.dry_run)
            for row in result["targets"]:
                print(f"{row['name']}: {row['status']}{' (dry run)' if row.get('dry_run') else ''}, {row['records']} records"
                      + (f"; written by the ledger at {row['writer']}" if row.get("writer") else "")
                      + (f"; generate.toml is for the ledger at {row['store']}" if row.get("store") else "")
                      + (f"; backed up to {row['backup']}" if row.get("backup") and not row.get("dry_run") else "")
                      + (f"; would back up ({row['backup']})" if row.get("backup") and row.get("dry_run") else ""))
            for problem in result["problems"]:
                print(f"problem: {problem}", file=sys.stderr)
            return 1 if result["problems"] or any(row["status"] in ("hand-written", "other-store", "missing-folder", "error")
                                                  for row in result["targets"]) else 0
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

                # The commit a session just made, noted before any fetch can hang (T6).
                try:
                    from datetime import datetime as _datetime, timezone as _timezone

                    from .commits import note_commit

                    note_commit(config_home(), args.name, config["repo"], Path.cwd(), current_session_id(),
                                _datetime.now(_timezone.utc))
                except Exception:
                    pass
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
