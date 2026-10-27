"""Preferences matched by words are for reference, and records that only
point at a federated source are not injected (spec 實際使用回饋改善 F4, F5)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from agent_memory import core, doctor, history  # noqa: E402
import router_baseline  # noqa: E402

POINTER = "decision.alpha.kestrel_tag_policy"
SETTLED = "decision.alpha.granite_tag_policy"


def ups(paths: dict, cwd: Path, text: str, session: str) -> str:
    output = core.handle_hook(paths["memory"], "user-prompt-submit", {
        "session_id": session, "cwd": str(cwd), "hook_event_name": "UserPromptSubmit", "prompt": text,
    }, agent="claude")
    return output.get("hookSpecificOutput", {}).get("additionalContext", "")


def record_pointer(root: Path, decision: str, scope: str = "project:alpha", record_id: str = POINTER) -> None:
    core.record(root, "decision", {
        "id": record_id, "scope": scope, "source": "user-stated", "evidence": "Fixture.",
        "problem": "Which kestrel tag policy alpha releases follow.", "decision": decision,
        "rationale": "The repo keeps the kestrel tag policy now.", "constraints": ["Fixture constraint."],
        "alternatives_rejected": ["Keep it here."], "consequences": ["Read the ADR."],
        "revisit_when": ["The ADR moves."],
    })
    core.reindex(root)


class PackSectionTests(unittest.TestCase):
    def test_a_preference_a_prompt_matched_is_for_reference(self) -> None:
        with router_baseline.environment() as paths:
            context = ups(paths, paths["neutral"], "release notes zephyr checklist migration", "s1")
        self.assertIn("## RELATED PREFERENCES（參考）\n\n### pref.fixture.release_notes", context)
        self.assertNotIn("## NON-NEGOTIABLE CONSTRAINTS", context)

    def test_a_record_that_only_points_at_a_federated_source_is_not_injected(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            record_pointer(root, "Superseded by federation: see docs/adr/0002-granite-smoke-before-tag.md in the alpha "
                                 "repo, which is now the maintained source.", scope="global")
            # The same kind of pointer, already scoped to its source's project.
            record_pointer(root, "Superseded by federation: see docs/adr/0001-granite-tag.md in the alpha repo, which is "
                                 "now the maintained source.", record_id=SETTLED)
            context = ups(paths, paths["alpha"], "kestrel tag policy for alpha releases", "s1")
            self.assertNotIn(POINTER, context)
            found = [hit["id"] for hit in history.search(root, "kestrel tag policy")]
            reports = paths["base"] / "reports"
            report, markdown = doctor.build_report(root, paths["config"], datetime(2026, 10, 21, tzinfo=timezone.utc),
                                                   report_dir=reports, projects_dir=paths["base"] / "projects",
                                                   run_tests=False)
            folder = Path(report["signals"]["harmful"]["pointer_only"]["dir"]) / POINTER
            drafts = {name: json.loads((folder / name).read_text(encoding="utf-8"))
                      for name in ("supersede-rewrite.json", "supersede-scope.json")}
        self.assertIn(POINTER, found)
        section = markdown.split("## 四、", 1)[1].split("## 五、", 1)[0]
        self.assertIn("- 只剩指標、不再注入的紀錄：1 筆", section)
        self.assertIn(f"  - {POINTER}（global；docs/adr/0002-granite-smoke-before-tag.md）", section)
        self.assertNotIn(SETTLED, section)
        self.assertTrue(drafts["supersede-rewrite.json"]["decision"].startswith("請改寫："))
        self.assertEqual("project:alpha", drafts["supersede-scope.json"]["scope"])
        for draft in drafts.values():
            core._validate_payload("decision", {**draft, "id": POINTER})
        # The same record saying its decision is injected.
        with router_baseline.environment() as paths:
            record_pointer(paths["memory"], "Alpha releases tag only after the kestrel tag policy check passes.")
            self.assertIn(f"### {POINTER}", ups(paths, paths["alpha"], "kestrel tag policy for alpha releases", "s1"))


ADR = """# ADR-0003: Rotate the quartz kiln weekly

- Status: Accepted

## Context

The quartz kiln wore unevenly.

## Decision

The quartz kiln is rotated a quarter turn every Monday. The rotation is logged in the kiln book. Skipping a week needs the lead's approval.

## Consequences

Mondays start ten minutes later.
"""
NOTE = ("# Obsidian glaze notes\n\n## Obsidian glaze cooling\n\n"
        + " ".join(f"Obsidian glaze cooling step {n} holds the kiln at {600 - n * 20} degrees for {n + 2} minutes." for n in range(1, 8))
        + "\n")


LISTED = ADR.replace("0003", "0004").replace("Rotate the quartz kiln weekly", "Check the basalt shelf").replace(
    "The quartz kiln is rotated a quarter turn every Monday. The rotation is logged in the kiln book. Skipping a week "
    "needs the lead's approval.", "A basalt firing needs two conditions: 1. the basalt shelf is cool. 2. the vents are open.")


def project_sources(paths: dict) -> None:
    (paths["alpha"] / "docs" / "adr" / "0003-quartz-kiln-rotation.md").write_text(ADR, encoding="utf-8")
    (paths["alpha"] / "docs" / "adr" / "0004-basalt-shelf.md").write_text(LISTED, encoding="utf-8")
    (paths["alpha"] / "research" / "2026-10-02-obsidian-glaze.md").write_text(NOTE, encoding="utf-8")
    core.reindex(paths["memory"])


def value_of(context: str, needle: str) -> str:
    return next(line for line in context.splitlines() if line.startswith("- value: ") and needle in line)


class FederatedExcerptTests(unittest.TestCase):
    def test_a_project_adr_shows_the_first_sentence_of_its_decision_and_the_flat_pack_what_it_did(self) -> None:
        with router_baseline.environment() as paths:
            project_sources(paths)
            routed = ups(paths, paths["alpha"], "quartz kiln rotation every Monday", "s1")
            listed = ups(paths, paths["alpha"], "basalt firing shelf conditions", "s2")
        with router_baseline.environment(router_toml="enabled = false\n") as paths:
            project_sources(paths)
            flat = ups(paths, paths["alpha"], "quartz kiln rotation every Monday", "s1")
        self.assertEqual("- value: The quartz kiln is rotated a quarter turn every Monday.…", value_of(routed, "quartz kiln"))
        # A list item's number does not end a sentence.
        self.assertEqual("- value: A basalt firing needs two conditions: 1. the basalt shelf is cool.…", value_of(listed, "basalt"))
        self.assertEqual("- value: The quartz kiln is rotated a quarter turn every Monday. The rotation is logged in the "
                         "kiln book. Skipping a week needs the lead's approval.", value_of(flat, "quartz kiln"))

    def test_an_excerpt_says_enough_and_cuts_no_quote_number_or_e_g(self) -> None:
        # What the retrieval review found cut misleadingly (2026-10-21).
        def first(text: str) -> str:
            return core._sentence_excerpt(text, core.FEDERATED_VALUE_TOKENS, True, False)

        # Too short to say what was decided: the next sentence comes along, and the bold is closed.
        self.assertEqual("**Keep the kiln book.** Errata go into the settlement, never into the bundle.…",
                         first("**Keep the kiln book.** Errata go into the settlement, never into the bundle. The reason is the layout."))
        self.assertEqual("**不改。已發布報告的事實錯誤記錄成 errata，寫進 settlement。**…",
                         first("**不改。已發布報告的事實錯誤記錄成 errata，寫進 settlement。** 理由來自這個 repo 的結構。"))
        self.assertEqual("Add `vents.toml`. The vent schedule lives in that one file.…",
                         first("Add `vents.toml`. The vent schedule lives in that one file. Old files are removed."))
        # A number opening a quote or after a colon, and e.g., end no sentence.
        self.assertEqual("原話：「1. 先降溫到三百度 2. 再把窗戶打開」才可以燒下一批。…",
                         first("原話：「1. 先降溫到三百度 2. 再把窗戶打開」才可以燒下一批。後面是說明。"))
        self.assertEqual("Sample first, e.g. thirty firings, then change the schedule.…",
                         first("Sample first, e.g. thirty firings, then change the schedule. Measure again."))

    def test_a_title_or_abbreviation_ends_no_excerpt_sentence(self) -> None:
        def first(text: str) -> str:
            return core._sentence_excerpt(text, core.FEDERATED_VALUE_TOKENS, True, False)

        self.assertEqual("Check the firing notes with Dr. Chen before changing the schedule.…",
                         first("Check the firing notes with Dr. Chen before changing the schedule. Then fire."))
        self.assertEqual("Two vents open on each firing, vs. one in the old kiln.…",
                         first("Two vents open on each firing, vs. one in the old kiln. The ledger says so."))

    def test_code_and_paths_do_not_count_toward_the_first_sentence(self) -> None:
        # ADR-0016 showed only 「新增 `backend/…/macro.py`。」 (retrieval re-check 3).
        def first(text: str) -> str:
            return core._sentence_excerpt(text, core.FEDERATED_VALUE_TOKENS, True, False)

        self.assertEqual("新增 `backend/src/kiln/vents.py`。窯門先開三分鐘，再升溫到九百度，排氣扇全程開著。…",
                         first("新增 `backend/src/kiln/vents.py`。窯門先開三分鐘，再升溫到九百度，排氣扇全程開著。其餘不變。"))
        self.assertEqual("Add tools/kiln/vent_schedule.py. The vent schedule lives in that one file now.…",
                         first("Add tools/kiln/vent_schedule.py. The vent schedule lives in that one file now. Old files go."))

    def test_a_project_note_cut_short_ends_at_a_whole_sentence(self) -> None:
        with router_baseline.environment() as paths:
            project_sources(paths)
            context = ups(paths, paths["alpha"], "obsidian glaze cooling steps", "s1")
        value = value_of(context, "Obsidian glaze cooling")
        self.assertTrue(value.endswith(" minutes.…"), value)  # the 80-token limit, at a sentence


VENTS_ADR = """# ADR-0005: Obsidian vents

- Status: Accepted

## Context

Two obsidian firings ran with the vents shut.

## Decision

Before each obsidian firing the kiln vents are opened, and the shift lead signs the vent ledger.
"""


def commit_on(repo: Path, relative: str, text: str, day: str) -> None:
    import os
    import subprocess

    (repo / relative).write_text(text, encoding="utf-8")
    stamp = f"{day}T00:00:00+00:00"
    env = {**os.environ, "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid", "GIT_COMMITTER_NAME": "f",
           "GIT_COMMITTER_EMAIL": "f@example.invalid", "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", relative], check=True, env=env)


class NewerProjectDecisionTests(unittest.TestCase):
    """A ledger decision the project has since recorded itself (spec 實際使用回饋改善 F8)."""

    def setUp(self) -> None:
        from test_router import put

        self.put = put

    def record_vents(self, paths: dict) -> None:
        # Recorded 2026-10-09; the project's ADR on the same rule appears 2026-10-15.
        self.put(paths["memory"], "decision", "decision.alpha.obsidian_vent_ledger", scope="project:alpha",
                 problem="Whether the obsidian kiln vents get logged.",
                 decision="Open the obsidian kiln vents before each firing; the shift lead signs the vent ledger.")
        commit_on(paths["alpha"], "docs/adr/0005-obsidian-vents.md", VENTS_ADR, "2026-10-15")
        core.reindex(paths["memory"])

    def test_the_routed_pack_names_the_projects_newer_record_and_the_flat_one_does_not(self) -> None:
        line = "- 專案決策紀錄有較新的：federation.alpha.adr-0005-obsidian-vents（2026-10-15）"
        with router_baseline.environment() as paths:
            self.record_vents(paths)
            routed = ups(paths, paths["alpha"], "obsidian kiln vents ledger", "s1")
        with router_baseline.environment(router_toml="enabled = false\n") as paths:
            self.record_vents(paths)
            flat = ups(paths, paths["alpha"], "obsidian kiln vents ledger", "s1")
        block = routed.split("### decision.alpha.obsidian_vent_ledger", 1)[1].split("\n### ", 1)[0]
        self.assertIn(line, block)
        self.assertIn("### decision.alpha.obsidian_vent_ledger", flat)
        self.assertNotIn("專案決策紀錄有較新的", flat)

    def test_an_older_project_record_or_a_different_one_is_not_named(self) -> None:
        with router_baseline.environment() as paths:
            self.put(paths["memory"], "decision", "decision.alpha.obsidian_vent_ledger", scope="project:alpha",
                     problem="Whether the obsidian kiln vents get logged.",
                     decision="Open the obsidian kiln vents before each firing; the shift lead signs the vent ledger.")
            commit_on(paths["alpha"], "docs/adr/0005-obsidian-vents.md", VENTS_ADR, "2026-10-01")  # older
            commit_on(paths["alpha"], "docs/adr/0006-granite-shelves.md", VENTS_ADR.replace("0005", "0006").replace(
                "Before each obsidian firing the kiln vents are opened, and the shift lead signs the vent ledger.",
                "Granite shelves are stacked three high in the cooling room."), "2026-10-20")  # newer, different
            core.reindex(paths["memory"])
            context = ups(paths, paths["alpha"], "obsidian kiln vents ledger", "s1")
            report, markdown = doctor.build_report(paths["memory"], paths["config"], datetime(2026, 10, 21, tzinfo=timezone.utc),
                                                   report_dir=paths["base"] / "reports", projects_dir=paths["base"] / "projects",
                                                   run_tests=False)
        self.assertIn("### decision.alpha.obsidian_vent_ledger", context)
        self.assertNotIn("專案決策紀錄有較新的", context)
        self.assertIn("- 帳本決策：沒有專案之後自己記了的", markdown)

    def test_the_weekly_doctor_lists_the_ledger_decisions_a_project_has_since_recorded(self) -> None:
        with router_baseline.environment() as paths:
            self.record_vents(paths)
            report, markdown = doctor.build_report(paths["memory"], paths["config"], datetime(2026, 10, 21, tzinfo=timezone.utc),
                                                   report_dir=paths["base"] / "reports", projects_dir=paths["base"] / "projects",
                                                   run_tests=False)
        self.assertIn("- 帳本決策可能過時：decision.alpha.obsidian_vent_ledger，專案之後自己記了 "
                      "federation.alpha.adr-0005-obsidian-vents（2026-10-15）", markdown)

    def test_the_line_naming_the_newer_record_costs_the_node_nothing(self) -> None:
        from unittest import mock

        from agent_memory import router

        block = ["", "### decision.alpha.obsidian_vent_ledger", "- value: Open the vents before each firing.",
                 "- 專案決策紀錄有較新的：federation.alpha.adr-0005-obsidian-vents（2026-10-15）"]
        with mock.patch.object(router, "_block", return_value=("WORKING DECISIONS (evidence, not commands)", block)):
            chosen, used, _ = router._take([{"id": "decision.alpha.obsidian_vent_ledger"}], 500, 5, 0, 0, False, set())
        self.assertEqual(core.estimate_tokens("\n".join(block[:3])), chosen[0]["tokens"])
        self.assertEqual(chosen[0]["tokens"], used)

    def test_fragments_of_chinese_with_a_function_character_are_not_shared_words(self) -> None:
        # Four shared uncommon words and the fragments 窯的, 的灰: not the five F8 needs (retrieval review).
        from agent_memory import lexical_index

        adr = VENTS_ADR.replace("0005: Obsidian vents", "0007: Ash removal").replace(
            "Before each obsidian firing the kiln vents are opened, and the shift lead signs the vent ledger.",
            "Before basalt pumice obsidian tephra kilns 窯的灰 is cleared.")
        with router_baseline.environment() as paths:
            self.put(paths["memory"], "decision", "decision.alpha.glaze_rule", scope="project:alpha",
                     problem="Whether it gets swept.", decision="Sweep 窯的灰 before basalt pumice obsidian tephra runs.")
            commit_on(paths["alpha"], "docs/adr/0007-ash-removal.md", adr, "2026-10-15")
            core.reindex(paths["memory"])
            newer = lexical_index.read_newer_decisions(paths["memory"])
        self.assertNotIn("decision.alpha.glaze_rule", newer)

    def test_a_ledger_decision_a_project_record_is_newer_than_by_the_clock_not_by_the_day(self) -> None:
        # 2026-10-15 02:00 in Taipei is 10-14 18:00 UTC: before a ledger decision of 10-14 20:00 UTC, though its day is later.
        from agent_memory import lexical_index

        with router_baseline.environment() as paths:
            self.put(paths["memory"], "decision", "decision.alpha.obsidian_vent_ledger", scope="project:alpha",
                     problem="Whether the obsidian kiln vents get logged.",
                     decision="Open the obsidian kiln vents before each firing; the shift lead signs the vent ledger.")
            path = next((paths["memory"] / "records" / "decision").glob("decision.alpha.obsidian_vent_ledger.md"))
            text = path.read_text(encoding="utf-8")
            import re as _re

            path.write_text(_re.sub(r'"updated_at": "[^"]+"', '"updated_at": "2026-10-14T20:00:00+00:00"', text), encoding="utf-8")
            import os
            import subprocess

            (paths["alpha"] / "docs" / "adr" / "0005-obsidian-vents.md").write_text(VENTS_ADR, encoding="utf-8")
            stamp = "2026-10-15T02:00:00+08:00"
            env = {**os.environ, "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid", "GIT_COMMITTER_NAME": "f",
                   "GIT_COMMITTER_EMAIL": "f@example.invalid", "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
            subprocess.run(["git", "-C", str(paths["alpha"]), "add", "-A"], check=True, env=env)
            subprocess.run(["git", "-C", str(paths["alpha"]), "commit", "-q", "-m", "adr"], check=True, env=env)
            core.reindex(paths["memory"])
            newer = lexical_index.read_newer_decisions(paths["memory"])
        self.assertNotIn("decision.alpha.obsidian_vent_ledger", newer)


class DecisionLogDayTests(unittest.TestCase):
    """When each entry of a project's decision log was recorded (F8, retrieval review 6 and 7)."""

    def log_repo(self, folder: Path) -> Path:
        import subprocess

        repo = folder / "proj"
        (repo / "docs").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        commit_on(repo, "docs/decisions.md", "# Log\n\n## 2026-09-30\n\n### D-001 Kiln vents\n\nOpen them.\n", "2026-09-30")
        commit_on(repo, "docs/decisions.md", "# Log\n\n## 2026-09-30\n\n### D-001 Kiln vents\n\nOpen them.\n\n"
                  "### D-092 Shelf order\n\nStack three high.\n\n### D-093 (2026-10-21) Ash glaze\n\nUse ash glaze.\n", "2026-10-20")
        return repo

    def test_an_entry_is_dated_by_its_own_heading_or_the_commit_that_wrote_it(self) -> None:
        import tempfile

        from agent_memory import federation

        with tempfile.TemporaryDirectory() as tmp:
            days = federation._log_days(federation.SourceTree(self.log_repo(Path(tmp))), "docs/decisions.md")
        self.assertEqual("2026-09-30", days["D-001 Kiln vents"][:10])
        self.assertEqual("2026-10-20", days["D-092 Shelf order"][:10])  # under the 09-30 heading, written 10-20
        self.assertEqual("2026-10-21", days["D-093 (2026-10-21) Ash glaze"])

    def test_a_chinese_file_name_and_a_renamed_adr_get_their_times(self) -> None:
        import os
        import subprocess
        import tempfile

        from agent_memory import federation

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "proj"
            (repo / "docs" / "adr").mkdir(parents=True)
            (repo / "briefs").mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            commit_on(repo, "docs/adr/0001-舊名.md", VENTS_ADR, "2026-10-01")
            commit_on(repo, "briefs/週報.md", "# 週報\n\n第一週。\n", "2026-10-05")
            stamp = "2026-10-10T00:00:00+00:00"
            env = {**os.environ, "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid", "GIT_COMMITTER_NAME": "f",
                   "GIT_COMMITTER_EMAIL": "f@example.invalid", "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
            subprocess.run(["git", "-C", str(repo), "mv", "docs/adr/0001-舊名.md", "docs/adr/0001-新名.md"], check=True, env=env)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "rename"], check=True, env=env)
            commit_on(repo, "briefs/週報.md", "# 週報\n\n第二週。\n", "2026-10-12")
            tree = federation.SourceTree(repo)
            added = federation.first_commits(tree, "docs/adr/*.md")
            changed = federation.last_commits(tree, "briefs/*.md")
        self.assertEqual("2026-10-01", added.get("docs/adr/0001-新名.md", "")[:10])  # from its first name's add
        self.assertEqual("2026-10-12", changed.get("briefs/週報.md", "")[:10])

    def test_a_log_is_read_once_however_many_entries_it_holds(self) -> None:
        import tempfile
        from unittest import mock

        from agent_memory import federation

        with tempfile.TemporaryDirectory() as tmp:
            repo = self.log_repo(Path(tmp))
            config = {"name": "proj", "affinity": ["project:proj"], "repo": repo, "ref": None,
                      "parsers": [{"kind": "markdown-note", "glob": "docs/decisions.md", "heading_level": "3"}]}
            with mock.patch.object(federation, "_log_days", wraps=federation._log_days) as read:
                entries = federation.collect_source_entries(config)
        self.assertGreaterEqual(len(entries), 3)
        self.assertEqual(1, read.call_count)


if __name__ == "__main__":
    unittest.main()
