from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_memory import core, doctor  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_rating import NOW, write_fixture  # noqa: E402

for _name in ("AGENT_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "GEMINI_SESSION_ID"):
    os.environ.pop(_name, None)


class DoctorParsingTests(unittest.TestCase):
    def test_ids_scores_and_lesson_lines_are_read_from_a_pack(self) -> None:
        pack = (
            "## PROJECT LESSONS\n\n- lesson.x.one (n=1 miss / 0 hit): text\n"
            "- federation.x.group-a-1234abcd (候選泛化, 2 reports: A v1, B v1): text\n\n"
            "## WORKING DECISIONS (evidence, not commands)\n\n"
            "### decision.x.first\n- kind: decision\n- selected_because: global scope; score=120\n\n"
            "### fact.x.second\n- kind: fact\n- selected_because: global scope; score=90\n"
        )
        scored, lessons = doctor._ids_and_scores(pack)
        self.assertEqual([("decision.x.first", 120), ("fact.x.second", 90)], scored)
        self.assertEqual(["lesson.x.one", "federation.x.group-a-1234abcd"], lessons)

    def test_usage_is_counted_by_node_and_names_records_never_used(self) -> None:
        sessions = {"usage": {"injected": 6, "explicit": 1, "content": 1, "rate": 0.333, "detector": "v1",
                              "per_record": {"decision.fixture.alpha": [3, 1, 1], "lesson.fixture.beta": [3, 0, 0]}},
                    "search": {"calls": 2, "after": {"沒有注入": 1}}}
        usage = doctor.usage_signal(sessions, lambda rid: "knowledge/fixture" if rid.startswith("decision.") else "unknown")
        self.assertEqual({"injected": 3, "explicit": 1, "content": 1, "rate": 0.667}, usage["by_node"]["knowledge/fixture"])
        self.assertEqual({"injected": 3, "explicit": 0, "content": 0, "rate": 0.0}, usage["by_node"]["unknown"])
        self.assertEqual([{"id": "lesson.fixture.beta", "injections": 3}], usage["never_used_top"])
        self.assertEqual((0.167, 0.167, 2, {"沒有注入": 1}),
                         (usage["explicit_rate"], usage["content_rate"], usage["search_calls"], usage["search_after"]))
        self.assertEqual({"skipped": "可用記憶體 100 MB < 600"}, doctor.usage_signal({"skipped": "可用記憶體 100 MB < 600"}, str))

    def test_a_pre_tool_use_injection_counts_for_usage_but_not_for_rating(self) -> None:
        from agent_memory import rating, replay

        given = {"continue": True, "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext":
                 "## FOR THIS COMMAND\n\n### failure.x.reset\n- kind: failure\n- value: Check the range with `git diff --name-only` first."}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "-work-x" / "sess-p.jsonl"
            path.parent.mkdir(parents=True)
            entries = [
                {"type": "user", "sessionId": "sess-p", "timestamp": NOW.isoformat(), "promptSource": "typed",
                 "origin": {"kind": "human"}, "message": {"content": "please reset the branch"}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                                               "input": {"command": "git reset --soft HEAD~1"}}]}},
                # The shape a shell hook's output has in the transcript.
                {"type": "attachment", "attachment": {"type": "hook_success", "hookEvent": "PreToolUse", "toolUseID": "toolu_1",
                                                      "content": "", "stdout": json.dumps(given)}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "先跑 `git diff --name-only` 檢查範圍。"}]}},
            ]
            path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
            measure = replay.Measure({"failure.x.reset": "failures"}, lambda cwd: set(), None)
            measure.timeline(replay.timeline(path, "claude"))
            messages = rating.read_transcript(path)
        self.assertEqual({"failure.x.reset": [1, 0, 1]}, measure.result()["usage"]["per_record"])
        self.assertEqual(1, len(messages))
        self.assertEqual([], messages[0]["records"])

    def test_search_calls_are_counted_outside_the_memory_repo(self) -> None:
        from agent_memory import replay

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "-work-y" / "sess-s.jsonl"
            path.parent.mkdir(parents=True)

            def call(command: str, cwd: str) -> dict:
                return {"type": "assistant", "cwd": cwd, "timestamp": NOW.isoformat(), "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}

            entries = [
                {"type": "user", "sessionId": "sess-s", "timestamp": NOW.isoformat(), "promptSource": "typed",
                 "origin": {"kind": "human"}, "message": {"content": "find the old rule"}},
                call("memoryctl search --query old rule", "/work/y"),
                call("~/agent-memory/bin/memoryctl search --id decision.x.y", "/work/y"),
                call("memoryctl search --query dev", str(Path.home() / "agent-memory")),
                call("memoryctl search --query dev", str(Path.home() / "agent-memory-dev" / "tests")),
                call("echo memoryctl search", "/work/y"),
            ]
            path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
            measure = replay.Measure({}, lambda cwd: set(), None)
            measure.timeline(replay.timeline(path, "claude"))
        self.assertEqual(2, measure.result()["search"]["calls"])

    def test_top_id_is_judged_inside_its_node_when_routed(self) -> None:
        scored = [("decision.a.x", 120), ("experience.a.y", 80), ("experience.k.z", 200)]
        nodes = {"decision.a.x": "project/a", "experience.a.y": "project/a", "experience.k.z": "knowledge/k"}
        self.assertFalse(doctor._top_problem("decision.a.x", scored, nodes))
        self.assertTrue(doctor._top_problem("experience.a.y", scored, nodes))
        self.assertTrue(doctor._top_problem("decision.a.x", scored, {}))
        self.assertFalse(doctor._top_problem("experience.k.z", scored, {}))
        self.assertTrue(doctor._top_problem("missing.id", scored, nodes))
        pack = "### decision.a.x\n- selected_because: global scope; score=120; node=project/a\n"
        self.assertEqual({"decision.a.x": "project/a"}, doctor._block_nodes(pack))


class RecordNodeTests(unittest.TestCase):
    """Spec 8.11: doctor maps ids to nodes from records/ and archive/ itself."""

    def test_live_archived_and_unknown_ids(self) -> None:
        from test_router import put
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root, config = Path(tmp) / "memory", Path(tmp) / "config"
            config.mkdir()
            put(root, "decision", "decision.ledger.scoped_rule", scope="project:ledger", decision="Scoped.")
            put(root, "experience", "experience.ledger.margin_call", outcome="Global ledger experience.")
            put(root, "decision", "decision.ledger.group_rule", scope="workspace:desktop", decision="Workspace.")
            put(root, "failure", "failure.ledger.stale_quote", symptom="Stale quote.")
            put(root, "fact", "fact.ledger.broker", value="Broker fact.")
            put(root, "experience", "experience.web.gone", outcome="Archived later.")
            old = root / "records" / "experience" / "experience.web.gone.md"
            archived = root / "archive" / "experience" / "experience.web.gone" / "v1-2026-10-01T000000-0000.md"
            archived.parent.mkdir(parents=True)
            old.rename(archived)
            with mock.patch.dict(os.environ, {"AGENT_MEMORY_CONFIG_HOME": str(config)}):
                node_of = doctor.record_nodes(root)
        self.assertEqual("project/ledger", node_of("experience.ledger.margin_call"))
        self.assertEqual("project/ledger", node_of("decision.ledger.group_rule"))
        self.assertEqual("project/ledger/failures", node_of("failure.ledger.stale_quote"))
        self.assertEqual("facts", node_of("fact.ledger.broker"))
        self.assertEqual("knowledge/web", node_of("experience.web.gone"))
        self.assertEqual("unknown", node_of("experience.never.existed"))

    def test_the_weekly_timer_runs_monday_morning_under_a_memory_cap(self) -> None:
        service, timer = doctor.timer_units(Path("/opt/memoryctl"))
        self.assertIn("ExecStart=/opt/memoryctl doctor --notify", service)
        self.assertIn("MemoryMax=1200M", service)
        self.assertIn("MemorySwapMax=0", service)
        self.assertIn("OnCalendar=Mon *-*-* 09:00:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("RandomizedDelaySec=600", timer)


class MemoryCapTests(unittest.TestCase):
    def test_a_manual_run_re_executes_under_a_systemd_memory_cap(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            (Path(runtime) / "systemd").mkdir()
            (Path(runtime) / "systemd" / "private").write_text("", encoding="utf-8")
            command = doctor.capped_command(["memoryctl", "doctor"], {"XDG_RUNTIME_DIR": runtime}, "/usr/bin/systemd-run")
        self.assertEqual(["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "-p", "MemoryMax=1200M",
                          "-p", "MemorySwapMax=0", sys.executable, "memoryctl", "doctor"], command)

    def test_an_already_capped_run_or_no_systemd_does_not_re_execute(self) -> None:
        self.assertIsNone(doctor.capped_command(["memoryctl"], {doctor.CAP_ENV: "1"}, "/usr/bin/systemd-run"))
        self.assertIsNone(doctor.capped_command(["memoryctl"], {}, None))
        self.assertIsNone(doctor.capped_command(["memoryctl"], {"XDG_RUNTIME_DIR": "/nonexistent"}, "/usr/bin/systemd-run"))

    def test_the_timer_service_marks_its_run_as_already_capped(self) -> None:
        service, _ = doctor.timer_units(Path("/opt/memoryctl"))
        self.assertIn(f"Environment={doctor.CAP_ENV}=1", service)

    def test_the_report_carries_its_own_peak_memory(self) -> None:
        check = doctor.check_peak_memory()
        self.assertEqual("peak_memory", check["id"])
        self.assertEqual("OK", check["status"])


class CrossProjectCandidateTests(unittest.TestCase):
    """Spec 學到的地方與適用範圍 v1, section 5."""

    def test_named_and_overlapping_pairs_across_origins(self) -> None:
        from test_router import put
        from unittest import mock

        gate = "The memory gate printed free memory and the heavy test run went ahead anyway on the shared box."
        with tempfile.TemporaryDirectory() as tmp:
            root, config = Path(tmp) / "memory", Path(tmp) / "config"
            config.mkdir()
            (config / "router.toml").write_text('[project_tokens]\n"the-zoo" = ["thezoo"]\n"my-app" = ["myapp"]\n', encoding="utf-8")
            put(root, "failure", "failure.machine.gate_one", scope="machine:old-box", symptom=gate)
            put(root, "failure", "failure.machine.gate_two", scope="machine:old-box",
                symptom="Same as failure.machine.gate_one on another project.")
            put(root, "failure", "failure.thezoo.gate_three", scope="project:the-zoo", symptom=gate)
            put(root, "failure", "failure.thezoo.unrelated", scope="project:the-zoo", symptom=gate)
            put(root, "experience", "experience.myapp.other", outcome="A completely different topic about fonts.")
            with mock.patch.dict(os.environ, {"AGENT_MEMORY_CONFIG_HOME": str(config)}):
                found = doctor.cross_project_candidates(root, "new-box")
        pairs = {tuple(pair["ids"]): pair for pair in found["pairs"]}
        self.assertTrue(pairs[("failure.machine.gate_one", "failure.machine.gate_two")]["named"])
        self.assertIn(("failure.machine.gate_one", "failure.thezoo.gate_three"), pairs)
        # Same project: never a cross-project pair.
        self.assertNotIn(("failure.thezoo.gate_three", "failure.thezoo.unrelated"), pairs)
        self.assertFalse([ids for ids in pairs if "experience.myapp.other" in ids])
        self.assertEqual({"failure.machine.gate_one", "failure.machine.gate_two"}, {item["id"] for item in found["other_machine"]})


class ReportDirTests(unittest.TestCase):
    def test_env_then_config_then_default(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(doctor.REPORT_DIR_ENV, None)
                self.assertEqual(doctor.DEFAULT_REPORT_DIR, doctor.resolve_report_dir(config))
                (config / "doctor").mkdir()
                (config / "doctor" / "doctor.toml").write_text('report_dir = "~/reports/memory"\n', encoding="utf-8")
                self.assertEqual(Path.home() / "reports" / "memory", doctor.resolve_report_dir(config))
                os.environ[doctor.REPORT_DIR_ENV] = str(config / "env-reports")
                self.assertEqual(config / "env-reports", doctor.resolve_report_dir(config))


class HookHealthTests(unittest.TestCase):
    def check(self, router: dict | None, pre_tool_use: dict | None = None) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            hooks = {name: {"status": "ok"} for name in ("session_start", "user_prompt_submit", "pre_compact")}
            if router is not None:
                hooks["router"] = router
            (Path(tmp) / "runtime" / "health").mkdir(parents=True)
            (Path(tmp) / "runtime" / "health.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
            (Path(tmp) / "runtime" / "health" / "pre_tool_use.json").write_text(
                json.dumps(pre_tool_use or {"status": "ok"}), encoding="utf-8"
            )
            return doctor.check_hooks(Path(tmp))

    def test_pre_tool_use_reports_from_its_own_file(self) -> None:
        check = self.check({"status": "ok"}, {"status": "error", "code": "state-write-failed"})
        self.assertEqual("WARN", check["status"])
        self.assertEqual("error", check["value"]["pre_tool_use"])
        self.assertEqual("ok", check["value"]["session_start"])

    def test_router_health_is_part_of_the_hook_check(self) -> None:
        self.assertEqual("OK", self.check({"status": "ok"})["status"])
        self.assertEqual("OK", self.check({"status": "ok", "code": "disabled"})["status"])
        invalid = self.check({"status": "ok", "code": "router-toml-invalid"})
        self.assertEqual("WARN", invalid["status"])
        self.assertIn("router=ok/router-toml-invalid", invalid["detail"])
        self.assertEqual("WARN", self.check({"status": "error", "code": "router-budget-invalid"})["status"])
        self.assertEqual("WARN", self.check(None)["status"])


class HookAgentCheckTests(unittest.TestCase):
    def test_a_hook_without_its_agent_flag_is_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "claude.json"
            bad = Path(tmp) / "codex.json"
            good.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
                {"command": "/x/memoryctl hook session-start --agent claude"}]}]}}), encoding="utf-8")
            bad.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
                {"command": "/x/memoryctl hook session-start"}]}]}}), encoding="utf-8")
            check = doctor.check_hook_agents({"claude": good, "codex": bad})
        self.assertEqual("WARN", check["status"])
        self.assertEqual("ok", check["value"]["claude"])
        self.assertIn("沒帶 --agent codex", check["value"]["codex"])


class SessionPackShareTests(unittest.TestCase):
    def test_preferences_filling_most_of_a_session_pack_are_a_warning_with_the_longest_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config = Path(tmp) / "memory", Path(tmp) / "config"
            with mock.patch.dict(os.environ, {"AGENT_MEMORY_HOME": str(root), "AGENT_MEMORY_CONFIG_HOME": str(config)}):
                core.initialize(root, "fixture-machine")
                for number, size in enumerate((1200, 900, 600, 300, 150)):
                    core.record(root, "preference", {
                        "id": f"pref.fixture.long_{number}", "scope": "global", "source": "user-stated", "priority": "high",
                        "evidence": "Fixture.", "value": "偏好" * (size // 2)})
                core.reindex(root)
                check = doctor.check_session_packs(root, [Path(tmp)])
        codex = next(row for row in check["value"] if row["agent"] == "codex")
        claude = next(row for row in check["value"] if row["agent"] == "claude")
        self.assertEqual("WARN", check["status"])
        self.assertTrue(codex["crowded"])
        self.assertFalse(claude["crowded"])  # the same preferences fit a larger budget
        self.assertEqual(["pref.fixture.long_0", "pref.fixture.long_1", "pref.fixture.long_2"],
                         [item["id"] for item in codex["longest_preferences"]])
        self.assertIn("（偏好占", check["detail"])


class DoctorCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        self.memory_home = base / "memory"
        self.config_home = base / "config"
        self.reports = base / "reports"
        self.projects = base / "projects"
        self.projects.mkdir()
        self.env = os.environ.copy()
        self.env.update(PYTHONPATH=str(PROJECT_ROOT), AGENT_MEMORY_HOME=str(self.memory_home),
                        AGENT_MEMORY_CONFIG_HOME=str(self.config_home), MEMORYCTL_DOCTOR_CAPPED="1")
        self.cli("init", "--machine-id", "machine-test")
        self.cli(
            "record", "--kind", "fact", "--input", "-",
            stdin={"id": "fact.golden.target", "scope": "global",
                   "value": "zephyrine lattice calibration is the golden answer.",
                   "source": "locally-verified", "evidence": "Fixture.", "priority": "normal",
                   "tags": ["zephyrine", "lattice"]},
        )
        golden = self.config_home / "doctor" / "golden.toml"
        golden.parent.mkdir(parents=True)
        golden.write_text(
            '[[query]]\ntext = "zephyrine lattice"\ncwd = "/work/x"\n'
            'expected_ids = ["fact.golden.target"]\nmax_results = "1"\n'
            '\n[[query]]\ntext = "zephyrine lattice"\ncwd = "/work/x"\n'
            'expected_ids = ["fact.golden.missing"]\n',
            encoding="utf-8",
        )

    def cli(self, *args: str, stdin=None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "agent_memory.cli", *args], cwd=PROJECT_ROOT, env=self.env,
            input=None if stdin is None else json.dumps(stdin), text=True, capture_output=True, check=True,
        )

    def doctor(self) -> tuple[str, dict]:
        self.cli("doctor", "--no-tests", "--report-dir", str(self.reports),
                 "--projects-dir", str(self.projects), "--now", NOW.isoformat())
        markdown = (self.reports / "2026-10-07.md").read_text(encoding="utf-8")
        report = json.loads((self.reports / "2026-10-07.json").read_text(encoding="utf-8"))
        return markdown, report

    def test_the_report_lists_at_most_twenty_cross_project_pairs_and_the_total(self) -> None:
        # Ruling 52 f: the section stops at 20 pairs and says how many there are.
        from unittest import mock

        pairs = [
            {"ids": [f"failure.a.x{n:02d}", f"failure.b.y{n:02d}"], "origins": [["project:a"], ["project:b"]],
             "named": False, "overlap": round(0.5 - n / 100, 3)}
            for n in range(25)
        ]
        found = {"pairs": pairs, "other_machine": [], "threshold": 0.07}
        with mock.patch.dict(os.environ, self.env), mock.patch.object(doctor, "cross_project_candidates", return_value=found):
            report, markdown = doctor.build_report(
                self.memory_home, self.config_home, NOW, report_dir=self.reports, projects_dir=self.projects, run_tests=False,
            )
        section = markdown.split("## 三、可能跨專案的教訓（資訊）", 1)[1]
        listed = [line for line in section.splitlines() if " ↔ " in line]
        self.assertEqual(20, len(listed))
        self.assertIn("failure.a.x00", listed[0])
        self.assertIn("共 25 對，只列前 20 對", section)
        self.assertEqual(25, len(report["signals"]["cross_project"]["pairs"]))

    def test_the_report_leads_with_the_rating_reminder_and_scoreboard(self) -> None:
        markdown, report = self.doctor()
        lines = markdown.splitlines()
        self.assertEqual(doctor.UNRATED_LINE, lines[0])
        self.assertLess(markdown.index("## 計分板"), markdown.index("## 一、運作檢查"))
        self.assertIn("| 有用率 | n/a | n/a | n/a |", markdown)
        self.assertEqual({"useful_rate", "harmful_rate", "usage_rate", "repeated_mistakes"},
                         set(report["scoreboard"]))

    def test_golden_queries_report_which_one_failed(self) -> None:
        markdown, report = self.doctor()
        golden = next(c for c in report["checks"] if c["id"] == "golden")
        self.assertEqual("WARN", golden["status"])
        self.assertEqual([True, False], [item["ok"] for item in golden["value"]])
        self.assertIn("缺 fact.golden.missing", markdown)

    def test_probes_name_what_must_and_must_not_appear_and_why(self) -> None:
        (self.config_home / "doctor" / "probes.toml").write_text(
            '[[probe]]\ntext = "zephyrine lattice"\ncwd = "/work/x"\nexpected_ids = ["fact.golden.target"]\n'
            'reason = "The fixture fact answers it."\n'
            '\n[[probe]]\ntext = "zephyrine lattice"\ncwd = "/work/x"\nforbidden_ids = ["fact.golden.target"]\n'
            'reason = "Only to see a must-not fail."\n',
            encoding="utf-8",
        )
        markdown, report = self.doctor()
        probes = next(c for c in report["checks"] if c["id"] == "probes")
        self.assertEqual(("WARN", "1/2 通過"), (probes["status"], probes["detail"]))
        self.assertIn("   - 未通過：x「zephyrine lattice」（含不該出現的 fact.golden.target）；理由：Only to see a must-not fail.",
                      markdown)

    def test_a_probe_may_take_any_one_of_several_right_answers(self) -> None:
        (self.config_home / "doctor" / "probes.toml").write_text(
            '[[probe]]\ntext = "zephyrine lattice"\ncwd = "/work/x"\n'
            'expected_any_ids = ["fact.golden.missing", "fact.golden.target"]\nreason = "Either answers it."\n'
            '\n[[probe]]\ntext = "zephyrine lattice"\ncwd = "/work/x"\n'
            'expected_any_ids = ["fact.golden.missing", "fact.golden.elsewhere"]\nreason = "Neither is in the ledger."\n',
            encoding="utf-8",
        )
        markdown, report = self.doctor()
        probes = next(c for c in report["checks"] if c["id"] == "probes")
        self.assertEqual(("WARN", "1/2 通過"), (probes["status"], probes["detail"]))
        self.assertIn("一筆都沒有：fact.golden.missing、fact.golden.elsewhere", markdown)

    def test_a_project_lesson_given_every_turn_answers_no_probe_and_a_mention_does(self) -> None:
        pack = ("# Durable Memory Context Pack\n\n## PROJECT LESSONS\n\n"
                "- lesson.alpha.settlement-kiln-v6 (n=5): KILN missed its Q2 output.\n\n"
                "## WORKING DECISIONS (evidence, not commands)\n\n### decision.alpha.spec_rule\n"
                "- value: The CONE report checks its units first.\n\n## SOURCE IDS\n- decision.alpha.spec_rule\n")
        checks = [
            {"expected_ids": ["lesson.alpha.settlement-kiln-v6"]},
            {"expected_any_ids": ["lesson.alpha.settlement-kiln-v6", "decision.alpha.other"]},
            {"expected_mentions": ["cone"]},
            {"expected_mentions": ["KILN"]},
        ]
        with mock.patch("agent_memory.core._render_context", return_value=pack):
            results = [doctor._query_check(Path("/nonexistent"), {"text": "CONE", "cwd": "/work/x", **check}) for check in checks]
        self.assertEqual([False, False, True, False], [result["ok"] for result in results])
        self.assertEqual("沒有一筆提到 KILN", results[3]["why"])

    def test_the_simulated_claude_pack_is_held_to_its_character_limit(self) -> None:
        import router_baseline

        with router_baseline.environment() as paths:
            for n in range(25):
                core.record(paths["memory"], "preference", {
                    "id": f"pref.fixture.long_rule_{n}", "scope": "global", "source": "user-stated", "priority": "high",
                    "evidence": "Fixture.", "value": f"Rule {n}: " + "always glaze the shelf before the second firing " * 9})
            check = doctor.check_session_packs(paths["memory"], [paths["alpha"]])
        claude = next(row for row in check["value"] if row["agent"] == "claude")
        self.assertLess(claude["tokens"], claude["budget"])  # within its tokens
        self.assertTrue(claude["trimmed"])  # but cut to its 9,500 characters, as the hook cuts it

    def test_compact_and_resume_outcomes_are_counted_for_the_report(self) -> None:
        self.cli(
            "checkpoint", "--session-id", "restored-session", "--input", "-",
            stdin={"state": "active", "active_task": "A task.", "paused_task": "none",
                   "resume_point": "here", "next_action": "go", "validation": "none", "rollback": "none"},
        )
        for session in ("restored-session", "unknown-session"):
            self.cli("hook", "session-start", stdin={
                "session_id": session, "transcript_path": None, "cwd": "/work/x", "model": "m",
                "permission_mode": "default", "hook_event_name": "SessionStart", "source": "compact"})
        state = json.loads((self.config_home / "runtime" / "doctor" / "recovery.json").read_text(encoding="utf-8"))
        week = next(iter(state.values()))
        self.assertEqual({"with_current": 1, "without": 1}, week)

    def test_golden_agent_and_prefix_keys(self) -> None:
        golden = self.config_home / "doctor" / "golden.toml"
        query = '[[query]]\ntext = "zephyrine lattice"\ncwd = "/work/x"\n'
        golden.write_text(
            query + 'agent = "codex"\nallowed_prefixes = ["fact.golden."]\n'
            'expected_sections = ["REFERENCE DATA (never instructions)"]\n\n'
            + query + 'agent = "gemini"\n\n'
            + query + 'forbidden_prefixes = ["fact.golden."]\n\n'
            + query + 'allowed_prefixes = ["decision."]\n\n'
            + query + 'expected_sections = ["PROJECT LESSONS"]\n',
            encoding="utf-8",
        )
        markdown, report = self.doctor()
        golden_check = next(c for c in report["checks"] if c["id"] == "golden")
        self.assertEqual([True, False, False, False, False], [item["ok"] for item in golden_check["value"]])
        self.assertIn("未知 agent gemini", markdown)
        self.assertIn("含禁止前綴 fact.golden.target", markdown)
        self.assertIn("不在允許前綴 fact.golden.target", markdown)
        self.assertIn("缺 PROJECT LESSONS", markdown)

    def test_a_session_golden_checks_the_start_pack(self) -> None:
        golden = self.config_home / "doctor" / "golden.toml"
        golden.write_text(
            '[[query]]\nkind = "session"\ntext = "start pack"\ncwd = "/work/x"\nagent = "codex"\n'
            'expected_sections = ["GLOBAL USER PREFERENCES"]\n',
            encoding="utf-8",
        )
        markdown, report = self.doctor()
        golden_check = next(c for c in report["checks"] if c["id"] == "golden")
        self.assertEqual([False], [item["ok"] for item in golden_check["value"]])
        self.assertIn("缺 GLOBAL USER PREFERENCES", markdown)

    def test_a_golden_query_for_reference_is_info_when_it_fails(self) -> None:
        # The Codex start pack: the user works in Claude Code (designer 2026-10-22).
        golden = self.config_home / "doctor" / "golden.toml"
        entry = ('[[query]]\nkind = "session"\ntext = "start pack"\ncwd = "/work/x"\nagent = "codex"\n'
                 'expected_sections = ["GLOBAL USER PREFERENCES"]\n')
        golden.write_text(entry + 'level = "info"\n', encoding="utf-8")
        _, report = self.doctor()
        reference = next(c for c in report["checks"] if c["id"] == "golden")
        golden.write_text(entry, encoding="utf-8")
        _, report = self.doctor()
        counted = next(c for c in report["checks"] if c["id"] == "golden")
        self.assertEqual(("INFO", "0/1 通過（只供參考、沒過：開場包 x/codex）"), (reference["status"], reference["detail"]))
        self.assertEqual("WARN", counted["status"])

    def test_idle_or_legacy_capsules_never_warn(self) -> None:
        sessions = self.memory_home / "sessions"
        capsule = (
            "# Current Resume Capsule\n\n- updated_at: 2026-07-01T00:00:00+00:00\n- session_id: {sid}\n"
            "- cwd: /work/x\n- state: {state}\n- active_task: Old task.\n"
        )
        for relative, state in (("by-session/plain-name-idle", "idle"), ("legacy-machine", "active")):
            folder = sessions / relative
            folder.mkdir(parents=True)
            (folder / "HOT.md").write_text(capsule.format(sid=folder.name, state=state), encoding="utf-8")
        _, report = self.doctor()
        capsules = next(c for c in report["checks"] if c["id"] == "capsules")
        self.assertEqual("OK", capsules["status"], capsules)
        self.assertEqual(["sessions/legacy-machine/HOT.md"], capsules["value"]["legacy"])
        self.assertNotIn("plain-name-idle", capsules["value"]["unmatched"])

    def test_install_timer_writes_both_units(self) -> None:
        units = Path(self.tempdir.name) / "units"
        result = self.cli("doctor", "install-timer", "--unit-dir", str(units))
        self.assertIn("Not enabled", result.stdout)
        self.assertIn("systemctl --user enable --now memoryctl-doctor.timer", result.stdout)
        self.assertIn("MemoryMax=1200M", (units / "memoryctl-doctor.service").read_text(encoding="utf-8"))
        self.assertIn("Persistent=true", (units / "memoryctl-doctor.timer").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
