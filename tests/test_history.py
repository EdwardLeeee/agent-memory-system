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


if __name__ == "__main__":
    unittest.main()
