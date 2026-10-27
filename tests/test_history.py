"""memoryctl search and the routed pack's search hint (spec 記憶路由樹 v1.1, 4.4)."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import router_baseline  # noqa: E402
from test_router import corpus, put  # noqa: E402
from agent_memory import core, history, router  # noqa: E402


def records(memory: Path) -> None:
    corpus(memory)
    put(memory, "experience", "experience.web.pumice_shelf_old", outcome="The pumice shelf label faded on the web gallery.")
    folder = memory / "sessions" / "by-session" / "idle-worker"
    folder.mkdir(parents=True)
    (folder / "HOT.md").write_text(
        "# Current Resume Capsule\n\n- updated_at: 2026-01-01T00:00:00Z\n- session_id: idle-worker\n"
        "- cwd: /work/old\n- state: idle\n- active_task: Catalogue the basalt drawer labels.\n",
        encoding="utf-8",
    )


class SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._env = router_baseline.environment(extra_records=records)
        cls.paths = cls._env.__enter__()
        cls.root = cls.paths["memory"]
        core.supersede(cls.root, "experience.web.pumice_shelf_old", {
            "scope": "global", "source": "locally-verified", "evidence": "Test.", "situation": "s.",
            "expectation": "e.", "action": "a.", "observed_outcome": "The pumice shelf label was reprinted for the web gallery.",
            "applicability": "x.", "decision_links": [], "change_driver": "new-evidence", "change_reason": "Reprinted labels.",
        })

    @classmethod
    def tearDownClass(cls) -> None:
        cls._env.__exit__(None, None, None)

    def ids(self, query: str, **kwargs: object) -> list[tuple[str, str]]:
        return [(hit["id"], hit["where"]) for hit in history.search(self.root, query, **kwargs)]

    def test_any_active_record_is_found_whatever_its_node(self) -> None:
        found = self.ids("tephra ignimbrite deposit")
        self.assertIn(("experience.alpha.deposit_one", "active"), found)
        self.assertIn(("experience.vent.deposit_two", "active"), found)
        self.assertIn(("failure.git.squash_lost_work", "active"), self.ids("reset soft squash lost"))

    def test_filters(self) -> None:
        self.assertEqual([], [i for i in self.ids("tephra ignimbrite deposit", kind="decision")])
        project = self.ids("tephra ignimbrite deposit", project="alpha")
        self.assertIn(("experience.vent.deposit_two", "active"), project)
        self.assertEqual([], self.ids("tephra ignimbrite deposit", project="beta"))
        self.assertEqual([], self.ids("tephra ignimbrite deposit", since=date(2099, 1, 1)))
        self.assertEqual(1, len(history.search(self.root, "pumice", limit=1)))

    def test_history_needs_include_archive_but_idle_capsules_do_not(self) -> None:
        plain = self.ids("pumice shelf label")
        self.assertIn(("experience.web.pumice_shelf_old", "active"), plain)
        self.assertFalse([where for _, where in plain if where.startswith(("archive", "event"))])
        archived = self.ids("pumice shelf label faded", include_archive=True)
        self.assertIn(("experience.web.pumice_shelf_old", "archive v1"), archived)
        self.assertIn(("experience.web.pumice_shelf_old", "event superseded"), self.ids("reprinted labels pumice", include_archive=True))
        self.assertIn(("capsule:idle-worker", "capsule (idle)"), self.ids("basalt drawer labels"))

    def test_show_prints_a_record_in_full(self) -> None:
        text = history.show(self.root, "decision.alpha.lahar_policy")
        self.assertIn("# decision.alpha.lahar_policy  [active; node=project/alpha]", text)
        self.assertIn("## Rationale", text)
        older = history.show(self.root, "experience.web.pumice_shelf_old", include_archive=True)
        self.assertIn("--- archived: archive/experience/experience.web.pumice_shelf_old/", older)
        self.assertIn("--- events", older)
        self.assertIn("superseded", older)
        with self.assertRaises(core.MemoryError):
            history.show(self.root, "experience.never.existed")

    def test_the_routed_pack_ends_with_the_search_hint_only_when_it_holds_records(self) -> None:
        rendered = core._render_context(
            self.root, self.paths["alpha"], "tephra ignimbrite deposit", 3000, explain=False, allow_reindex=False, agent="claude"
        )
        self.assertTrue(rendered.rstrip().endswith(router.SEARCH_HINT))
        self.assertLessEqual(core.estimate_tokens(router.SEARCH_HINT), 15)
        empty = core.handle_hook(self.root, "user-prompt-submit", {
            "session_id": "s", "cwd": str(self.paths["neutral"]), "hook_event_name": "UserPromptSubmit",
            "prompt": "qwertyuiop zxcvbnm",
        }, agent="claude")
        self.assertEqual({"continue": True}, empty)

    def test_the_cli_needs_one_of_query_or_id(self) -> None:
        import os
        import subprocess

        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        both = subprocess.run([sys.executable, "-m", "agent_memory.cli", "search", "--query", "x", "--id", "y"],
                              capture_output=True, text=True, env=env)
        self.assertEqual(2, both.returncode)
        listed = subprocess.run([sys.executable, "-m", "agent_memory.cli", "search", "--query", "tephra ignimbrite", "--json"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertGreaterEqual(json.loads(listed.stdout)["count"], 2)


class SearchMatchingTests(unittest.TestCase):
    """English words match whole, side-by-side words count as a phrase, and a
    record of the project the search runs from counts for more (spec 實際使用回饋改善 F9)."""

    def test_an_english_word_matches_only_whole(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            put(root, "fact", "fact.alpha.kiln_store_listing", value="The kiln app is listed on the play store.")
            put(root, "fact", "fact.alpha.kiln_screenshots", value="Kiln screenshots run under playwright on a wide display.")
            core.reindex(root)
            found = [hit["id"] for hit in history.search(root, "kiln play")]
        self.assertEqual(["fact.alpha.kiln_store_listing", "fact.alpha.kiln_screenshots"], found)
        # Both hold "kiln"; only one holds "play" as a word, so it ranks first by a whole content hit.

    def test_an_ids_underscores_part_its_words_and_a_few_endings_still_match(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            put(root, "fact", "fact.alpha.kiln_idle_reaper", value="Stops idle kiln timers.")
            put(root, "fact", "fact.alpha.reaperbot_logs", value="Where the bot writes its logs.")
            put(root, "fact", "fact.alpha.glaze_steps", value="Glaze steps are chained one after another.")
            put(root, "fact", "fact.alpha.chainsaw_notes", value="Notes on the chainsaw.")
            core.reindex(root)
            reaper = [hit["id"] for hit in history.search(root, "reaper")]
            chain = [hit["id"] for hit in history.search(root, "chain")]
        self.assertEqual(["fact.alpha.kiln_idle_reaper"], reaper)  # reaperbot is another word
        self.assertEqual(["fact.alpha.glaze_steps"], chain)  # chained, not chainsaw

    def test_a_two_letter_word_takes_no_ending_and_a_version_number_is_one(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            put(root, "fact", "fact.alpha.kiln_user_list", value="Every user of the kiln signs in.")
            put(root, "fact", "fact.alpha.kiln_first_aid", value="The first aid box hangs by the kiln.")
            put(root, "fact", "fact.alpha.kiln_runtime", value="The kiln scripts run on python3 only.")
            core.reindex(root)
            us = [hit["id"] for hit in history.search(root, "us")]
            ai = [hit["id"] for hit in history.search(root, "ai")]
            python = [hit["id"] for hit in history.search(root, "python")]
        self.assertNotIn("fact.alpha.kiln_user_list", us)
        self.assertNotIn("fact.alpha.kiln_first_aid", ai)
        self.assertEqual(["fact.alpha.kiln_runtime"], python)

    def test_two_words_side_by_side_count_as_a_phrase(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            put(root, "fact", "fact.alpha.basalt_apart", value="The basalt shelf and the kiln mould are in the north room.")
            put(root, "fact", "fact.alpha.basalt_phrase", value="The north room keeps the basalt kiln shelf mould.")
            core.reindex(root)
            hits = history.search(root, "basalt kiln")
        scores = {hit["id"]: hit["score"] for hit in hits}
        self.assertEqual("fact.alpha.basalt_phrase", hits[0]["id"])
        self.assertEqual(20, scores["fact.alpha.basalt_phrase"] - scores["fact.alpha.basalt_apart"])

    def test_another_project_s_record_goes_behind_never_out_and_a_global_lesson_is_nobody_s(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            # One word in its text only, scoped to another project: 20 points, behind but kept.
            put(root, "fact", "fact.beta.ash_layers", scope="project:beta", value="Tephra layers hold the dates.")
            # Global, learned in beta: its origin is where it was learned, not where it applies.
            core.record(root, "failure", {
                "id": "failure.git.tephra_split", "scope": "global", "source": "locally-verified", "evidence": "Fixture.",
                "origin": "project:beta", "symptom": "Tephra paths split across repos.", "attempt": "a",
                "root_cause": "r", "verified_fix": "Keep one tephra repo.", "retry_when": "w"})
            core.reindex(root)
            found = {hit["id"]: hit["score"] for hit in history.search(root, "tephra", cwd=paths["alpha"])}
            named = [hit["id"] for hit in history.search(root, "tephra", cwd=paths["alpha"], project="beta")]
        self.assertIn("fact.beta.ash_layers", found)
        self.assertGreater(found["failure.git.tephra_split"], found["fact.beta.ash_layers"])
        self.assertIn("fact.beta.ash_layers", named)

    def test_only_a_record_scoped_to_the_cwd_s_project_gets_its_bonus(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            # Both global with the same words; one was learned in alpha.
            for name, origin in (("learned_here", {"origin": "project:alpha"}), ("learned_nowhere", {})):
                core.record(root, "failure", {
                    "id": f"failure.git.pumice_{name}", "scope": "global", "source": "locally-verified",
                    "evidence": "Fixture.", **origin, "symptom": "Pumice dust clogged the kiln vents.", "attempt": "a",
                    "root_cause": "r", "verified_fix": "Vacuum the vents first.", "retry_when": "w"})
            put(root, "fact", "fact.alpha.pumice_vents", scope="project:alpha", value="Pumice dust clogged the kiln vents.")
            core.reindex(root)
            found = {hit["id"]: hit["score"] for hit in history.search(root, "pumice vents", cwd=paths["alpha"])}
        self.assertEqual(found["failure.git.pumice_learned_nowhere"], found["failure.git.pumice_learned_here"])
        self.assertGreater(found["fact.alpha.pumice_vents"], found["failure.git.pumice_learned_here"])

    def test_the_project_the_search_runs_from_first_and_another_projects_record_last(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            # The same words in three records: of alpha, of no project, of another project.
            put(root, "fact", "fact.zz.obsidian_cooling_here", scope="project:alpha", value="Obsidian cooling takes four hours.")
            put(root, "fact", "fact.mm.obsidian_cooling_anywhere", value="Obsidian cooling takes four hours.")
            put(root, "fact", "fact.aa.obsidian_cooling_there", scope="project:beta", value="Obsidian cooling takes four hours.")
            core.reindex(root)

            def ids(cwd: Path) -> list[str]:
                # memoryctl search runs from where the agent works.
                import contextlib
                import io
                import os

                from agent_memory import cli

                out, before = io.StringIO(), os.getcwd()
                os.chdir(cwd)
                try:
                    with contextlib.redirect_stdout(out):
                        cli.main(["search", "--query", "obsidian cooling", "--json"])
                finally:
                    os.chdir(before)
                return [hit["id"] for hit in json.loads(out.getvalue())["hits"] if "obsidian_cooling" in hit["id"]]

            here, elsewhere = ids(paths["alpha"]), ids(paths["neutral"])
        self.assertEqual(["fact.zz.obsidian_cooling_here", "fact.mm.obsidian_cooling_anywhere", "fact.aa.obsidian_cooling_there"], here)
        self.assertEqual(["fact.aa.obsidian_cooling_there", "fact.mm.obsidian_cooling_anywhere", "fact.zz.obsidian_cooling_here"],
                         elsewhere)  # outside any project, equal scores keep their order by id


if __name__ == "__main__":
    unittest.main()
