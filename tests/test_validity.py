"""When a fact holds in the world (spec 論文借鏡改善 A3)."""

from __future__ import annotations

from datetime import date
import contextlib
import io
import os
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import router_baseline  # noqa: E402
from agent_memory import cli, core, history  # noqa: E402
from agent_memory.storage import router_defaults  # noqa: E402


def fact(record_id: str, value: str, **extra: str) -> dict:
    return {"id": record_id, "scope": "global", "source": "locally-verified", "evidence": "Fixture.",
            "priority": "normal", "value": value, "tags": ["basaltkiln"], **extra}


class FactValidityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._env = router_baseline.environment()
        cls.paths = cls._env.__enter__()
        cls.root = cls.paths["memory"]
        core.record(cls.root, "fact", fact("fact.basaltkiln.firing_now", "The basaltkiln fires at 1240 degrees.",
                                           valid_from="2026-09-01"))
        core.record(cls.root, "fact", fact("fact.basaltkiln.firing_old", "The basaltkiln fired at 1100 degrees.",
                                           valid_until="2020-01-01"))
        core.record(cls.root, "fact", fact("fact.basaltkiln.shelf", "The basaltkiln shelf is silicon carbide.",
                                           valid_until="2026-09-30"))
        core.supersede(cls.root, "fact.basaltkiln.shelf", {**fact("fact.basaltkiln.shelf", "The basaltkiln shelf is cordierite.",
                                                                  valid_from="2026-10-01"),
                                                           "change_driver": "new-evidence", "change_reason": "Replaced."})
        core.reindex(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._env.__exit__(None, None, None)

    def render(self, enabled: bool = True) -> str:
        settings = {**router_defaults(), "enabled": enabled}
        text, _ = core._render_context_status(self.root, self.paths["neutral"], "basaltkiln firing degrees", 3000, True,
                                              allow_reindex=False, agent="claude", settings=settings)
        return text

    def test_bad_dates_are_refused(self) -> None:
        for extra, message in (
            ({"valid_from": "2026-13-01"}, "valid_from must be a date"),
            ({"valid_from": "2026-10-02", "valid_until": "2026-10-01"}, "valid_from must not be after valid_until"),
        ):
            with self.subTest(extra=extra), self.assertRaisesRegex(core.MemoryError, message):
                core.record(self.root, "fact", fact("fact.basaltkiln.bad", "Bad.", **extra))
        with self.assertRaisesRegex(core.MemoryError, "only for fact records"):
            core.record(self.root, "experience", {
                "id": "experience.basaltkiln.bad", "scope": "global", "source": "locally-verified", "evidence": "E.",
                "situation": "s", "expectation": "e", "action": "a", "observed_outcome": "o", "applicability": "a",
                "decision_links": [], "valid_from": "2026-01-01"})
        self.assertEqual(0, len(core.validate(self.root)[1]))

    def test_a_fact_says_since_when_and_one_no_longer_true_is_not_injected(self) -> None:
        for enabled in (True, False):
            with self.subTest(router=enabled):
                rendered = self.render(enabled)
                self.assertRegex(rendered, r"- value: The basaltkiln fires at 1240 degrees\.（自 2026-09-01 起）\n")
                self.assertNotIn("fact.basaltkiln.firing_old", rendered)
        hits = history.search(self.root, "basaltkiln fired 1100")
        old = next(hit for hit in hits if hit["id"] == "fact.basaltkiln.firing_old")
        self.assertEqual("已於 2020-01-01 不再成立", old["note"])
        self.assertIn("(已於 2020-01-01 不再成立)", history.format_hits(hits))

    def test_as_of_lists_the_facts_that_held_that_day(self) -> None:
        def ids(day: str) -> dict[str, int]:
            return {hit["id"]: hit["version"] for hit in history.as_of(self.root, date.fromisoformat(day), "basaltkiln")}

        # Only valid_until given: true from an unknown start until that day.
        self.assertEqual({"fact.basaltkiln.firing_old": 1, "fact.basaltkiln.shelf": 1}, ids("2019-12-01"))
        self.assertEqual({"fact.basaltkiln.shelf": 1, "fact.basaltkiln.firing_now": 1}, ids("2026-09-15"))
        self.assertEqual({"fact.basaltkiln.shelf": 2, "fact.basaltkiln.firing_now": 1}, ids("2026-10-05"))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENT_MEMORY_HOME": str(self.root)}), contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.main(["search", "--as-of", "2026-09-15", "--query", "basaltkiln shelf"]))
        self.assertTrue(out.getvalue().startswith("2026-09-15 成立的事實：\n"))
        self.assertIn("- fact.basaltkiln.shelf v1（… ～ 2026-09-30）\n   The basaltkiln shelf is silicon carbide.", out.getvalue())


if __name__ == "__main__":
    unittest.main()
