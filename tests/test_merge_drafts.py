"""Merge drafts for lessons learned in several places (spec 論文借鏡改善 A4)."""

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
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from agent_memory import core, doctor  # noqa: E402

GATE = "The memory gate printed free memory and the heavy run went ahead anyway; the check never blocked the run."


def failure(record_id: str, scope: str, symptom: str) -> dict:
    return {"id": record_id, "scope": scope, "source": "locally-verified", "evidence": "Fixture.", "priority": "normal",
            "symptom": symptom, "attempt": "Printed the number first.", "root_cause": "The check only printed.",
            "verified_fix": "Make the gate exit non-zero and chain the run with &&.", "retry_when": "Before any check-then-run."}


GENERAL = "failure.shell.gate_must_block"
NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def folded(record_id: str, scope: str) -> dict:
    """A record already folded into GENERAL, as the 10-14 cleanup wrote them."""
    return {**failure(record_id, scope, GATE), "applies": "origin",
            "verified_fix": f"通用教訓見 {GENERAL}。Make the gate exit non-zero and chain the run with &&."}


def store(base: Path) -> tuple[Path, Path, Path, dict]:
    root, config, reports = base / "memory", base / "config", base / "reports"
    config.mkdir()
    (config / "router.toml").write_text('[project_tokens]\n"the-zoo" = ["thezoo"]\n"my-app" = ["myapp"]\n'
                                        '"kiln-lab" = ["kilnlab"]\n', encoding="utf-8")
    env = {"AGENT_MEMORY_HOME": str(root), "AGENT_MEMORY_CONFIG_HOME": str(config), "MEMORYCTL_DOCTOR_CAPPED": "1"}
    return root, config, reports, env


def record_general(root: Path) -> None:
    core.record(root, "failure", {**failure(GENERAL, "global", GATE), "applies": "global",
                                  "origin": ["project:the-zoo", "project:my-app"]})


class MergeDraftTests(unittest.TestCase):
    def test_a_group_of_three_gets_drafts_and_nothing_is_written_to_the_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, config, reports = base / "memory", base / "config", base / "reports"
            config.mkdir()
            (config / "router.toml").write_text('[project_tokens]\n"the-zoo" = ["thezoo"]\n"my-app" = ["myapp"]\n'
                                                '"kiln-lab" = ["kilnlab"]\n', encoding="utf-8")
            env = {"AGENT_MEMORY_HOME": str(root), "AGENT_MEMORY_CONFIG_HOME": str(config)}
            with mock.patch.dict(os.environ, env):
                core.initialize(root, "fixture-machine")
                core.record(root, "failure", failure("failure.thezoo.gate_printed", "project:the-zoo", GATE))
                core.record(root, "failure", failure("failure.myapp.gate_printed_again", "project:my-app", GATE))
                # Joined to the group only through naming the second one.
                core.record(root, "failure", failure("failure.kilnlab.same_as_myapp", "project:kiln-lab",
                                                     "Same as failure.myapp.gate_printed_again, with a kiln firing."))
                core.record(root, "failure", {**failure("failure.thezoo.unrelated", "project:the-zoo",
                                                        "A totally different font rendering glitch."),
                                              "attempt": "Swapped typefaces.", "root_cause": "Hinting was disabled.",
                                              "verified_fix": "Enable font hinting.", "retry_when": "Before shipping fonts."})
                before = sorted(p.name for p in (root / "records" / "failure").glob("*.md"))
                pairs = doctor.cross_project_candidates(root)["pairs"]
                from agent_memory import merge_drafts

                drafts = merge_drafts.write_drafts(root, pairs, reports, "2026-10-18")
                self.assertEqual(before, sorted(p.name for p in (root / "records" / "failure").glob("*.md")))
                self.assertEqual(1, len(drafts["groups"]))
                group = drafts["groups"][0]
                self.assertEqual(["failure.kilnlab.same_as_myapp", "failure.myapp.gate_printed_again",
                                  "failure.thezoo.gate_printed"], group["members"])
                folder = Path(group["folder"])
                self.assertEqual(reports / "2026-10-18-merge-drafts", folder.parent)
                names = sorted(p.name for p in folder.iterdir())
                self.assertEqual(["README.md", "general.json", "members.md"] + sorted(
                    f"supersede-{m}.json" for m in group["members"]), names)
                members = (folder / "members.md").read_text(encoding="utf-8")
                for member in group["members"]:
                    self.assertIn(f"### {member}", members)
                self.assertIn("## Root Cause", members)
                general = json.loads((folder / "general.json").read_text(encoding="utf-8"))
                self.assertEqual(("global", "global"), (general["scope"], general["applies"]))
                self.assertEqual(["project:kiln-lab", "project:my-app", "project:the-zoo"], general["origin"])
                self.assertTrue(general["symptom"].startswith("請改寫："))
                self.assertIn("failure.thezoo.gate_printed", general["evidence"])
                # The skeletons pass the payload checks (their text is only to be rewritten).
                core._validate_payload("failure", dict(general))
                for member in group["members"]:
                    payload = json.loads((folder / f"supersede-{member}.json").read_text(encoding="utf-8"))
                    self.assertEqual("origin", payload["applies"])
                    self.assertTrue(payload["verified_fix"].startswith(f"通用教訓見 {general['id']}。"))
                    core._validate_payload("failure", {**payload, "id": member})
                readme = (folder / "README.md").read_text(encoding="utf-8")
                self.assertIn("memoryctl record --kind failure --input general.json", readme)
                self.assertIn("memoryctl supersede --id failure.thezoo.gate_printed --input supersede-failure.thezoo.gate_printed.json", readme)

    def test_a_group_holds_only_records_paired_with_every_member(self) -> None:
        from agent_memory import merge_drafts

        pairs = [
            {"ids": ["a", "b"], "named": True, "overlap": 0.2},
            {"ids": ["a", "c"], "named": False, "overlap": 0.1},
            {"ids": ["b", "c"], "named": False, "overlap": 0.1},
            # d shares only b with the group: a neighbour, not the same lesson.
            {"ids": ["b", "d"], "named": False, "overlap": 0.3},
            {"ids": ["e", "f"], "named": False, "overlap": 0.08},
        ]
        self.assertEqual([["a", "b", "c"], ["e", "f"]], merge_drafts.groups(pairs))

    def test_the_weekly_report_points_at_the_drafts(self) -> None:
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, config, reports = base / "memory", base / "config", base / "reports"
            config.mkdir()
            env = {"AGENT_MEMORY_HOME": str(root), "AGENT_MEMORY_CONFIG_HOME": str(config), "MEMORYCTL_DOCTOR_CAPPED": "1"}
            with mock.patch.dict(os.environ, env):
                core.initialize(root, "fixture-machine")
                core.record(root, "failure", failure("failure.alpha.gate_printed", "project:alpha", GATE))
                core.record(root, "failure", failure("failure.beta.gate_printed", "project:beta", GATE))
                report, markdown = doctor.build_report(root, config, datetime(2026, 10, 18, tzinfo=timezone.utc),
                                                       report_dir=reports, projects_dir=base / "projects", run_tests=False)
        section = markdown.split("## 三、可能跨專案的教訓（資訊）", 1)[1].split("## 四", 1)[0]
        self.assertIn("合併草稿：1 組", section)
        self.assertIn("只產生草稿，不會自動執行", section)
        self.assertEqual(1, len(report["signals"]["cross_project"]["drafts"]["groups"]))


class AlreadyMergedTests(unittest.TestCase):
    def test_records_folded_into_a_general_lesson_are_not_proposed_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config, reports, env = store(Path(tmp))
            with mock.patch.dict(os.environ, env):
                core.initialize(root, "fixture-machine")
                record_general(root)
                core.record(root, "failure", folded("failure.thezoo.gate_printed", "project:the-zoo"))
                core.record(root, "failure", folded("failure.myapp.gate_printed_again", "project:my-app"))
                found = doctor.cross_project_candidates(root)
                from agent_memory import merge_drafts

                drafts = merge_drafts.write_drafts(root, found["pairs"], reports, "2026-10-18")
        self.assertEqual([], found["pairs"])
        self.assertEqual([], drafts["groups"])
        self.assertEqual({"failure.thezoo.gate_printed": GENERAL, "failure.myapp.gate_printed_again": GENERAL},
                         found["merged"])

    def test_a_group_with_a_general_lesson_joins_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config, reports, env = store(Path(tmp))
            with mock.patch.dict(os.environ, env):
                core.initialize(root, "fixture-machine")
                record_general(root)
                core.record(root, "failure", folded("failure.thezoo.gate_printed", "project:the-zoo"))
                core.record(root, "failure", failure("failure.kilnlab.gate_printed_in_kiln", "project:kiln-lab", GATE))
                from agent_memory import merge_drafts

                drafts = merge_drafts.write_drafts(root, doctor.cross_project_candidates(root)["pairs"], reports,
                                                   "2026-10-18")
                self.assertEqual(1, len(drafts["groups"]))
                group = drafts["groups"][0]
                self.assertEqual((GENERAL, ["failure.kilnlab.gate_printed_in_kiln"], True),
                                 (group["general_id"], group["members"], group.get("join")))
                folder = Path(group["folder"])
                self.assertEqual(["README.md", "members.md", "supersede-failure.kilnlab.gate_printed_in_kiln.json",
                                  f"supersede-{GENERAL}.json"], sorted(p.name for p in folder.iterdir()))
                general = json.loads((folder / f"supersede-{GENERAL}.json").read_text(encoding="utf-8"))
                self.assertEqual("global", general["applies"])
                self.assertEqual(["project:kiln-lab", "project:my-app", "project:the-zoo"], general["origin"])
                self.assertIn("failure.kilnlab.gate_printed_in_kiln", general["evidence"])
                self.assertEqual(GATE, general["symptom"])
                core._validate_payload("failure", {**general, "id": GENERAL})
                member = json.loads((folder / "supersede-failure.kilnlab.gate_printed_in_kiln.json").read_text(encoding="utf-8"))
                self.assertEqual("origin", member["applies"])
                self.assertTrue(member["verified_fix"].startswith(f"通用教訓見 {GENERAL}。"))
                readme = (folder / "README.md").read_text(encoding="utf-8")
                self.assertIn(f"memoryctl supersede --id {GENERAL} --input supersede-{GENERAL}.json", readme)
                self.assertNotIn("memoryctl record", readme)


    def test_a_record_like_a_folded_one_joins_its_general_lesson(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, config, reports, env = store(Path(tmp))
            with mock.patch.dict(os.environ, env):
                core.initialize(root, "fixture-machine")
                # The general lesson is worded apart from the events it holds.
                core.record(root, "failure", {
                    **failure(GENERAL, "global", "Warnings appeared on screen while later steps kept going."),
                    "attempt": "Observed output manually.", "root_cause": "Detection lacked enforcement.",
                    "verified_fix": "Return failure status so dependent commands halt.",
                    "retry_when": "Whenever validation precedes heavy work.", "applies": "global",
                    "origin": ["project:the-zoo"]})
                core.record(root, "failure", folded("failure.thezoo.gate_printed", "project:the-zoo"))
                core.record(root, "failure", failure("failure.kilnlab.gate_printed_in_kiln", "project:kiln-lab", GATE))
                found = doctor.cross_project_candidates(root)
                from agent_memory import merge_drafts

                drafts = merge_drafts.write_drafts(root, found["pairs"], reports, "2026-10-18")
        self.assertEqual([(["failure.kilnlab.gate_printed_in_kiln", GENERAL], ["failure.thezoo.gate_printed"])],
                         [(sorted(p["ids"]), p.get("via")) for p in found["pairs"]])
        self.assertIn("經由已併入的 failure.thezoo.gate_printed", "\n".join(doctor.cross_project_lines(found)))
        self.assertEqual([(GENERAL, ["failure.kilnlab.gate_printed_in_kiln"], True)],
                         [(g["general_id"], g["members"], g.get("join")) for g in drafts["groups"]])


    def test_every_record_paired_with_a_general_lesson_joins_and_each_can_be_dismissed(self) -> None:
        from agent_memory import merge_drafts

        disk = "failure.zeta.backend_tests_on_nearly_full_disk"
        with tempfile.TemporaryDirectory() as tmp:
            root, config, reports, env = store(Path(tmp))
            with mock.patch.dict(os.environ, env):
                core.initialize(root, "fixture-machine")
                core.record(root, "failure", {
                    **failure(GENERAL, "global", "Warnings appeared on screen while later steps kept going."),
                    "attempt": "Observed output manually.", "root_cause": "Detection lacked enforcement.",
                    "verified_fix": "Return failure status so dependent commands halt.",
                    "retry_when": "Whenever validation precedes heavy work.", "applies": "global",
                    "origin": ["project:the-zoo"]})
                core.record(root, "failure", folded("failure.thezoo.gate_printed", "project:the-zoo"))
                core.record(root, "failure", failure("failure.kilnlab.gate_printed_in_kiln", "project:kiln-lab", GATE))
                core.record(root, "failure", failure("failure.myapp.gate_printed_again", "project:my-app", GATE))
                # Named after the folded record, worded apart from the other two:
                # paired with the general lesson only.
                core.record(root, "failure", {
                    **failure(disk, "project:zeta", "Same as failure.thezoo.gate_printed, on a nearly full disk."),
                    "attempt": "Ignored the quota.", "root_cause": "Storage exhaustion went unnoticed.",
                    "verified_fix": "Abort when free space drops.", "retry_when": "Prior to large builds."})

                def join_group() -> tuple[dict, dict]:
                    drafts = merge_drafts.write_drafts(root, doctor.cross_project_candidates(root)["pairs"], reports,
                                                       "2026-10-18", config)
                    return drafts, next(g for g in drafts["groups"] if g["general_id"] == GENERAL)

                drafts, group = join_group()
                self.assertEqual(["failure.kilnlab.gate_printed_in_kiln", "failure.myapp.gate_printed_again", disk],
                                 group["members"])
                self.assertEqual(1, len(drafts["groups"]))
                readme = (Path(group["folder"]) / "README.md").read_text(encoding="utf-8")
                for member in group["members"]:
                    self.assertIn(f"`memoryctl doctor dismiss {GENERAL} {member}`", readme)
                merge_drafts.dismiss(config, [GENERAL, disk], NOW)
                drafts, group = join_group()
        self.assertEqual(["failure.kilnlab.gate_printed_in_kiln", "failure.myapp.gate_printed_again"], group["members"])
        self.assertEqual((0, 1), (drafts["dismissed"], drafts["dismissed_candidates"]))
        self.assertIn("已排除 1 筆加入既有通用教訓的候選", "\n".join(doctor.cross_project_lines(
            {"pairs": [], "threshold": 0.07, "other_machine": [], "drafts": drafts})))


class DismissTests(unittest.TestCase):
    def test_a_dismissed_group_is_not_drafted_again_until_a_new_member_joins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, config, reports, env = store(base)

            def dismiss(*ids: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run([sys.executable, "-m", "agent_memory.cli", "doctor", "dismiss", *ids],
                                      cwd=PROJECT_ROOT, env={**os.environ, **env}, text=True, capture_output=True)

            def section() -> tuple[dict, str]:
                report, markdown = doctor.build_report(root, config, NOW, report_dir=reports,
                                                       projects_dir=base / "projects", run_tests=False)
                return (report["signals"]["cross_project"]["drafts"],
                        markdown.split("## 三、可能跨專案的教訓（資訊）", 1)[1].split("## 四", 1)[0])

            with mock.patch.dict(os.environ, env):
                core.initialize(root, "fixture-machine")
                core.record(root, "failure", failure("failure.alpha.gate_printed", "project:alpha", GATE))
                core.record(root, "failure", failure("failure.beta.gate_printed", "project:beta", GATE))
                self.assertEqual(1, len(section()[0]["groups"]))
                readme = next(Path(section()[0]["groups"][0]["folder"]).glob("README.md")).read_text(encoding="utf-8")
                self.assertIn("memoryctl doctor dismiss failure.alpha.gate_printed failure.beta.gate_printed", readme)
                self.assertEqual(2, dismiss("failure.alpha.gate_printed").returncode)
                result = dismiss("failure.alpha.gate_printed", "failure.beta.gate_printed")
                self.assertEqual(0, result.returncode, result.stderr)
                drafts, text = section()
                self.assertEqual(([], 1), (drafts["groups"], drafts["dismissed"]))
                self.assertIn("已排除 1 組", text)
                # A record that is new to the group brings it back.
                core.record(root, "failure", failure("failure.gamma.gate_printed", "project:gamma", GATE))
                drafts, _ = section()
                self.assertEqual([["failure.alpha.gate_printed", "failure.beta.gate_printed", "failure.gamma.gate_printed"]],
                                 [g["members"] for g in drafts["groups"]])


class InferredOriginTests(unittest.TestCase):
    def test_a_machine_record_gets_the_project_of_the_session_it_was_learned_in(self) -> None:
        from agent_memory.lexical_index import compute_routing, write_project_paths

        session = "11111111-2222-3333-4444-555555555555"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, config, reports, env = store(base)
            alpha, elsewhere = base / "work" / "alpha", base / "work" / "elsewhere"
            alpha.mkdir(parents=True)
            elsewhere.mkdir()
            transcript = base / "projects" / "-work-alpha" / f"{session}.jsonl"
            transcript.parent.mkdir(parents=True)
            # The session was in alpha when it wrote the record and moved on later.
            transcript.write_text("\n".join(json.dumps({"type": "user", "sessionId": session, "cwd": str(cwd),
                                                        "timestamp": when}) for cwd, when in (
                (alpha, "2000-01-01T00:00:00Z"), (elsewhere, "2999-01-01T00:00:00Z"))) + "\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {**env, "AGENT_SESSION_ID": session}):
                core.initialize(root, "fixture-machine")
                machine_path = config / "machine.json"
                machine = json.loads(machine_path.read_text(encoding="utf-8"))
                machine.setdefault("scope_paths", {})["project:alpha"] = [str(alpha)]
                machine_path.write_text(json.dumps(machine), encoding="utf-8")
                write_project_paths(root, compute_routing())
                core.record(root, "failure", failure("failure.machine.gate_below_memory", "machine:fixture-machine", GATE))
            with mock.patch.dict(os.environ, env):
                core.record(root, "failure", failure("failure.beta.gate_printed", "project:beta", GATE))
                report, _ = doctor.build_report(root, config, NOW, report_dir=reports, projects_dir=base / "projects",
                                                run_tests=False)
            folder = Path(report["signals"]["cross_project"]["drafts"]["groups"][0]["folder"])
            general = json.loads((folder / "general.json").read_text(encoding="utf-8"))
            readme = (folder / "README.md").read_text(encoding="utf-8")
        self.assertEqual(["project:alpha", "project:beta", "machine:fixture-machine"], general["origin"])
        self.assertIn("- failure.machine.gate_below_memory：machine:fixture-machine；project:alpha 由學到時的對話推得", readme)

    def places(self, base: Path, env: dict) -> tuple[Path, Path, dict[str, str]]:
        """Folders alpha and beta registered as projects, and a session for each
        of A and C in alpha, B in beta, E and F in notes, which no project
        claims; D's session left no transcript."""
        from agent_memory.lexical_index import compute_routing, write_project_paths

        root, config = Path(env["AGENT_MEMORY_HOME"]), Path(env["AGENT_MEMORY_CONFIG_HOME"])
        sessions = {name: f"{n}1111111-2222-3333-4444-555555555555" for n, name in enumerate("ABCDEF", start=1)}
        folders = {"A": base / "work" / "alpha", "B": base / "work" / "beta", "C": base / "work" / "alpha",
                   "E": base / "work" / "notes", "F": base / "work" / "notes"}
        for name, folder in folders.items():
            folder.mkdir(parents=True, exist_ok=True)
            transcript = base / "projects" / f"-work-{folder.name}" / f"{sessions[name]}.jsonl"
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(json.dumps({"type": "user", "sessionId": sessions[name], "cwd": str(folder),
                                              "timestamp": "2000-01-01T00:00:00Z"}) + "\n", encoding="utf-8")
        with mock.patch.dict(os.environ, env):
            core.initialize(root, "fixture-machine")
            machine_path = config / "machine.json"
            machine = json.loads(machine_path.read_text(encoding="utf-8"))
            machine.setdefault("scope_paths", {}).update({"project:alpha": [str(folders["A"])], "project:beta": [str(folders["B"])]})
            machine_path.write_text(json.dumps(machine), encoding="utf-8")
            write_project_paths(root, compute_routing())
        return root, config, sessions

    def test_global_records_pair_by_where_they_were_learned_and_an_unknown_place_pairs_with_any(self) -> None:
        # Four global shell lessons learned in two projects were never paired; records learned in folders
        # no project claims paired with each other (designer 2026-10-27).
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _, _, reports, env = store(base)
            root, config, sessions = self.places(base, env)
            for name, session in sessions.items():
                with mock.patch.dict(os.environ, {**env, "AGENT_SESSION_ID": session}):
                    core.record(root, "failure", failure(f"failure.shell.gate_printed_{name.lower()}", "global", GATE))
            with mock.patch.dict(os.environ, env):
                report, markdown = doctor.build_report(root, config, NOW, report_dir=reports, projects_dir=base / "projects",
                                                       run_tests=False)
        found = report["signals"]["cross_project"]
        ids = {name: f"failure.shell.gate_printed_{name}" for name in "abcdef"}
        # Every two but the two of alpha and the two of notes; D, nowhere known, with all.
        self.assertEqual({frozenset((ids[x], ids[y])) for x in "abcdef" for y in "abcdef" if x < y} - {
            frozenset((ids["a"], ids["c"])), frozenset((ids["e"], ids["f"]))}, {frozenset(pair["ids"]) for pair in found["pairs"]})
        inferred = {frozenset(pair["ids"]): pair.get("inferred") for pair in found["pairs"]}
        self.assertEqual([["project:alpha"], ["project:beta"]], inferred[frozenset((ids["a"], ids["b"]))])
        notes = f"folder:{(base / 'work' / 'notes').as_posix()}"
        self.assertEqual([([ids["a"], ids["c"]], ["project:alpha"]), ([ids["e"], ids["f"]], [notes])],
                         [(pair["ids"], pair["places"]) for pair in found["same_place"]])
        self.assertIn(f"- 同一個地方可能重複記了兩次（相似度 ≥ 0.3；不產生草稿，是同一個就 supersede 成一筆）：\n"
                      f"  - {ids['a']} ↔ {ids['c']}（相似度 1.0；地方 project:alpha）", markdown)
        self.assertIn(f"{notes}（由學到時的對話推得）", markdown)

    def test_a_speed_bump_naming_a_record_folded_into_a_general_lesson_is_warned_with_the_general_one(self) -> None:
        # kill-by-name kept naming one of the merged lessons after the merge (designer 2026-10-27).
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, config, reports, env = store(base)
            with (config / "router.toml").open("a", encoding="utf-8") as handle:
                handle.write('\n[failures.speed_bump_failures]\nkill-by-name = ["failure.alpha.gate_printed", "failure.gone.record"]\n'
                             'rm-rf = ["failure.beta.gate_printed"]\n')
            with mock.patch.dict(os.environ, env):
                core.initialize(root, "fixture-machine")
                record_general(root)
                core.record(root, "failure", folded("failure.alpha.gate_printed", "project:the-zoo"))
                core.record(root, "failure", failure("failure.beta.gate_printed", "project:my-app", GATE))
                report, markdown = doctor.build_report(root, config, NOW, report_dir=reports, projects_dir=base / "projects",
                                                       run_tests=False)
        check = next((c for c in report["checks"] if c["id"] == "speed_bump_failures"), {})
        self.assertEqual(("WARN", [("failure.alpha.gate_printed", GENERAL), ("failure.gone.record", None)]),
                         (check.get("status"), [(item["id"], item["suggest"]) for item in check.get("value") or []]))
        self.assertIn(f"kill-by-name 指名的 failure.alpha.gate_printed 已併入通用教訓（applies=origin），只該在學到的專案提醒，"
                      f"建議改成 {GENERAL}", markdown)


class SessionFolderTests(unittest.TestCase):
    def test_a_folder_no_project_claims_is_its_repository_s_main_folder_and_home_is_tilde(self) -> None:
        from agent_memory import merge_drafts

        with tempfile.TemporaryDirectory() as tmp:
            main, worktree = Path(tmp) / "agent-memory", Path(tmp) / "agent-memory-dev"
            (main / "tools").mkdir(parents=True)
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
            subprocess.run([*git, "-C", str(main), "init", "-q"], check=True)
            subprocess.run([*git, "-C", str(main), "commit", "-q", "--allow-empty", "-m", "base"], check=True)
            subprocess.run([*git, "-C", str(main), "worktree", "add", "-q", "-b", "dev", str(worktree)], check=True)
            places = [merge_drafts.folder_place(str(path)) for path in (worktree, main / "tools", Path(tmp))]
        self.assertEqual([main.as_posix(), main.as_posix(), Path(tmp).as_posix()], places)
        self.assertEqual("~", merge_drafts.folder_place(str(Path.home())))

    def test_each_moment_gets_the_folder_of_then_from_one_pass(self) -> None:
        from agent_memory import merge_drafts

        session = "99999999-2222-3333-4444-555555555555"
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "projects" / "-w" / f"{session}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("\n".join(json.dumps({"type": "user", "sessionId": session, "cwd": cwd, "timestamp": when})
                                            for cwd, when in (("/w/alpha", "2026-10-01T01:00:00Z"), ("/w/beta", "2026-10-02T01:00:00Z"),
                                                              ("/w/gamma", "2026-10-03T01:00:00Z"))) + "\n", encoding="utf-8")
            found = merge_drafts.session_cwds({session: ["2026-10-02T12:00:00Z", "2026-09-01T00:00:00Z", "2026-10-01T05:00:00Z", ""],
                                               "00000000-0000-0000-0000-000000000000": ["2026-10-02T12:00:00Z"]},
                                              Path(tmp) / "projects", None)
        # Before the first line, its folder; a time that does not parse, the last.
        self.assertEqual({(session, "2026-10-02T12:00:00Z"): "/w/beta", (session, "2026-09-01T00:00:00Z"): "/w/alpha",
                          (session, "2026-10-01T05:00:00Z"): "/w/alpha", (session, ""): "/w/gamma"}, found)


if __name__ == "__main__":
    unittest.main()
