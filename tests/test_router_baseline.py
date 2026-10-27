"""router.enabled = false must reproduce the flat path byte for byte (spec 8.15 c-3).

The baseline under tests/fixtures/router-baseline/baseline/ was recorded once,
with the program at BASELINE_COMMIT, before the router existed. Every later
commit must keep this test passing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import router_baseline  # noqa: E402

ROUTER_DISABLED = "enabled = false\n"


class RouterBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with router_baseline.environment(router_toml=ROUTER_DISABLED) as paths:
            cls.outputs = router_baseline.render_all(paths)
            cls.problems = router_baseline.check_outputs(cls.outputs, paths)

    def test_the_baseline_records_the_program_it_came_from(self) -> None:
        lines = (router_baseline.FIXTURE / "BASELINE_COMMIT").read_text(encoding="utf-8").splitlines()
        commits = [line for line in lines if line.strip() and not line.startswith("#")]
        self.assertEqual(1, len(commits))
        self.assertRegex(commits[0], r"^[0-9a-f]{40}$")

    def test_every_baseline_file_is_reproduced_byte_for_byte(self) -> None:
        recorded = sorted(path.name for path in router_baseline.BASELINE.glob("*.txt"))
        self.assertEqual(recorded, sorted(self.outputs))
        for name in recorded:
            with self.subTest(name=name):
                expected = (router_baseline.BASELINE / name).read_text(encoding="utf-8")
                self.assertEqual(expected, self.outputs[name])

    def test_the_replayed_outputs_still_meet_the_baseline_conditions(self) -> None:
        self.assertEqual([], self.problems)

    def test_session_packs_carry_the_task_blocks_and_project_lessons(self) -> None:
        for agent in router_baseline.AGENTS:
            for label in ("alpha", "neutral"):
                context = json.loads(self.outputs[f"session.{agent}.{label}.txt"])[
                    "hookSpecificOutput"
                ]["additionalContext"]
                self.assertIn("## CURRENT TASK", context)
                self.assertIn("## OTHER ACTIVE TASKS", context)
                if label == "alpha":
                    self.assertIn("## PROJECT LESSONS", context)



class IsolationTests(unittest.TestCase):
    def test_records_added_to_the_fixture_are_indexed_under_its_own_configuration(self) -> None:
        # A record written with core.record before the fixture's config home was set built the
        # index from this machine's configuration: its projects reached the tests.
        from agent_memory import core
        from agent_memory.lexical_index import load_index_routing
        import sqlite3

        def records(root: Path) -> None:
            core.record(root, "fact", {"id": "fact.fixture.kiln_note", "scope": "global", "source": "locally-verified",
                                       "evidence": "Fixture.", "value": "The kiln note is kept."})

        with tempfile.TemporaryDirectory() as tmp:
            machine_config = Path(tmp)
            (machine_config / "machine.json").write_text(json.dumps({
                "machine_id": "00000000-0000-4000-8000-000000000000", "scope_paths": {"project:leaked": [tmp]}}),
                encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_MEMORY_CONFIG_HOME": str(machine_config)}), \
                    router_baseline.environment(extra_records=records) as paths:
                connection = sqlite3.connect(f"file:{paths['memory'] / '.cache' / 'index.sqlite3'}?mode=ro", uri=True)
                try:
                    projects = sorted((load_index_routing(connection) or {}).get("projects", {}))
                finally:
                    connection.close()
        self.assertNotIn("leaked", projects)
        self.assertIn("alpha", projects)

if __name__ == "__main__":
    unittest.main()
