"""Ranking feedback from ratings and the agents' use of what they were given
(spec 論文借鏡改善 A1, 實際使用回饋改善 F12)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import router_baseline  # noqa: E402
from test_router import put  # noqa: E402
from agent_memory import core  # noqa: E402

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def corpus(memory: Path) -> None:
    # Two equally relevant notes in one topic, and a weak one below the floor.
    put(memory, "experience", "experience.web.kiln_vent_alpha", outcome="The kiln vent fan was cleaned before firing.")
    put(memory, "experience", "experience.web.kiln_vent_beta", outcome="The kiln vent fan was cleaned before firing.")
    put(memory, "experience", "experience.web.kiln_shelf_weak", outcome="A kiln shelf note.")


class RoutedFeedbackTests(unittest.TestCase):
    QUERY = "web kiln vent fan cleaned before firing"

    @classmethod
    def setUpClass(cls) -> None:
        cls._env = router_baseline.environment(extra_records=corpus)
        cls.paths = cls._env.__enter__()
        cls.root = cls.paths["memory"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._env.__exit__(None, None, None)

    def tearDown(self) -> None:
        (self.paths["config"] / "runtime" / "feedback.json").unlink(missing_ok=True)

    def feedback(self, records: dict) -> None:
        path = self.paths["config"] / "runtime" / "feedback.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"records": records}), encoding="utf-8")

    def render(self, **kwargs: object) -> str:
        return core._render_context(self.root, self.paths["neutral"], self.QUERY, 3000, explain=True,
                                    allow_reindex=False, agent="claude", **kwargs)

    def order(self, rendered: str) -> list[str]:
        return [i for i in re.findall(r"^### (\S+)", rendered, re.M) if i.startswith("experience.web.kiln")]

    def test_a_record_rated_harmful_twice_goes_down_and_nothing_else_moves(self) -> None:
        # Without feedback the tie goes by id, and nothing is labelled.
        rendered = self.render()
        self.assertEqual(["experience.web.kiln_vent_alpha", "experience.web.kiln_vent_beta"], self.order(rendered))
        self.assertNotIn("回饋：", rendered)
        self.feedback({"experience.web.kiln_vent_alpha": {"harmful": 2, "useful": 0}})
        rendered = self.render()
        self.assertEqual(["experience.web.kiln_vent_beta", "experience.web.kiln_vent_alpha"], self.order(rendered))
        self.assertIn("回饋：降（礙事 2 次、有用 0 次）", rendered)
        self.assertIn("回饋：無", rendered)
        # Harmful no more often than useful: unchanged.
        self.feedback({"experience.web.kiln_vent_alpha": {"harmful": 2, "useful": 3}})
        rendered = self.render()
        self.assertEqual(["experience.web.kiln_vent_alpha", "experience.web.kiln_vent_beta"], self.order(rendered))
        self.assertNotIn("回饋：降", rendered)
        # A broken file is no feedback at all.
        (self.paths["config"] / "runtime" / "feedback.json").write_text("{not json", encoding="utf-8")
        rendered = self.render()
        self.assertEqual(["experience.web.kiln_vent_alpha", "experience.web.kiln_vent_beta"], self.order(rendered))
        self.assertNotIn("回饋：", rendered)

    def test_useful_ratings_or_use_go_up_but_never_past_the_floor(self) -> None:
        self.feedback({"experience.web.kiln_vent_beta": {"useful": 2, "harmful": 0}})
        rendered = self.render()
        self.assertEqual(["experience.web.kiln_vent_beta", "experience.web.kiln_vent_alpha"], self.order(rendered))
        self.assertIn("回饋：升（有用 2 次、沒有礙事）", rendered)
        self.feedback({"experience.web.kiln_vent_beta": {"used": 3}})
        rendered = self.render()
        self.assertIn("回饋：升（近 4 週被明寫用到 3 次）", rendered)
        # A file written before F12 counted citations; they count as use.
        self.feedback({"experience.web.kiln_vent_beta": {"cited": 3}})
        self.assertIn("回饋：升（近 4 週被明寫用到 3 次）", self.render())
        # A record that did not pass the floor stays out, however well rated.
        self.feedback({"experience.web.kiln_shelf_weak": {"useful": 9, "harmful": 0, "used": 9}})
        rendered = self.render()
        self.assertNotIn("experience.web.kiln_shelf_weak", rendered)
        self.assertIn("回饋：無", rendered)

    def test_golden_renders_without_feedback(self) -> None:
        self.feedback({"experience.web.kiln_vent_alpha": {"harmful": 5, "useful": 0}})
        rendered = self.render(feedback=False)
        self.assertEqual(["experience.web.kiln_vent_alpha", "experience.web.kiln_vent_beta"], self.order(rendered))
        self.assertNotIn("回饋：", rendered)


class FeedbackFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.config = Path(self.tempdir.name) / "config"
        self.reports = Path(self.tempdir.name) / "reports"
        self.projects = Path(self.tempdir.name) / "projects"
        (self.config / "ratings").mkdir(parents=True)
        self.reports.mkdir()
        self.projects.mkdir()

    def rate(self, week: str, entries: list[dict]) -> None:
        (self.config / "ratings" / f"{week}.json").write_text(json.dumps(entries), encoding="utf-8")

    def build(self, this_week: dict | None = None) -> dict:
        from agent_memory import feedback

        # A ledger with no index: every feature of a record counts as its own.
        feedback.rebuild(self.config, NOW, self.reports, this_week, self.projects, Path(self.tempdir.name) / "no-store")
        return feedback.load(self.config)

    def test_a_rating_is_credited_to_the_memory_the_agent_used(self) -> None:
        from agent_memory import feedback, usage

        def entry(verdict: str, ids: list[str], used: list[str], day: int = 17, content: list[str] | None = None,
                  cwd: str = "/w/alpha") -> dict:
            return {"time": f"2026-10-{day}T10:00:00+00:00", "injected": True, "rating": verdict, "record_ids": ids,
                    "explicit_ids": used, "content_ids": content or [], "detector": usage.DETECTOR,
                    "key": f"k-{verdict}-{len(ids)}-{day}-{cwd}", "session_id": "s", "cwd": cwd}

        self.rate("2026-W42", [
            # Four memories, the agent named one: only that one is useful.
            entry("useful", ["a", "b", "c", "d"], ["b"]),
            # One it only repeated the content of is not credited (A1 ruling, until content matching is trusted).
            entry("useful", ["c", "d", "e"], [], 14, content=["d"]),
            # A turn spent on the memory system itself counts for nothing: there ids are the subject.
            entry("useful", ["c", "d"], ["c"], 13, cwd=str(PROJECT_ROOT / "tests")),
            # Several memories, none used: harmful goes to none of them.
            entry("harmful", ["a", "c", "e"], []),
            # A pack of one: that one.
            entry("harmful", ["f"], [], 16),
            entry("harmful", ["f"], [], 15),
            # useless goes to every memory of the pack.
            entry("useless", ["a", "g"], []),
            {"time": "2026-10-17T12:00:00+00:00", "injected": True, "rating": "skipped", "record_ids": ["h"], "cited_ids": []},
        ])
        records = self.build()
        self.assertEqual({"b"}, {k for k, v in records.items() if v["useful"]})
        self.assertEqual({"f"}, {k for k, v in records.items() if v["harmful"]})
        self.assertEqual({"a", "g"}, {k for k, v in records.items() if v["useless"]})
        self.assertNotIn("h", records)
        self.assertEqual({"useful": 1, "harmful": 1}, feedback.unattributed(self.config))
        self.assertEqual(1, json.loads((self.config / "runtime" / "feedback.json").read_text(encoding="utf-8"))["memory_repo_ratings"])
        self.assertEqual([{"id": "f", "harmful": 2, "last_rated": "2026-10-16"}], feedback.flagged_harmful(self.config))

    def test_a_rating_of_a_turn_worked_in_the_memory_system_counts_for_nothing(self) -> None:
        from agent_memory import usage

        # Asked from ~, but the agent worked in the memory system's folders in that turn.
        self.rate("2026-W42", [{"time": "2026-10-17T10:00:00+00:00", "injected": True, "rating": "useful",
                                "record_ids": ["a", "b"], "explicit_ids": ["a"], "content_ids": [], "detector": usage.DETECTOR,
                                "key": "k-memory", "session_id": "s", "cwd": "/w/home", "memory_repo": True}])
        records = self.build()
        self.assertEqual(set(), {k for k, v in records.items() if v["useful"]})

    def test_older_ratings_get_their_use_from_the_transcript(self) -> None:
        from agent_memory import feedback, rating, usage

        when = NOW - timedelta(days=1)
        pack = "### fact.x.used\n- value: The kiln profile lives in /srv/kiln/profile.toml.\n\n### fact.x.other\n- value: B.\n"
        lines = [
            {"type": "user", "sessionId": "s1", "cwd": "/w", "timestamp": when.isoformat(), "promptSource": "typed",
             "origin": {"kind": "human"}, "message": {"role": "user", "content": "Which kiln rule applies?"}},
            {"type": "attachment", "attachment": {"type": "hook_additional_context", "hookEvent": "UserPromptSubmit",
                                                  "content": [pack]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Per fact.x.used, reading /srv/kiln/profile.toml first."}]}},
        ]
        path = self.projects / "-w" / "s1.jsonl"
        path.parent.mkdir()
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
        (message,) = rating.read_transcript(path)
        self.rate("2026-W42", [
            # Judged by an older rule (its used ids mixed named and content): worked out again from the
            # transcript by the rule in use, and what it stored is not read.
            {"time": when.isoformat(), "injected": True, "rating": "useful", "record_ids": ["fact.x.used", "fact.x.other"],
             "key": rating.message_key(message), "session_id": "s1", "cited_ids": [], "detector": "v4",
             "used_ids": ["fact.x.other"]},
            # Its transcript is gone: the citations it was saved with, then the pack-of-one rule.
            {"time": when.isoformat(), "injected": True, "rating": "useful", "record_ids": ["fact.x.p", "fact.x.q"],
             "key": "gone-1", "session_id": "gone", "cited_ids": ["fact.x.q"]},
            {"time": when.isoformat(), "injected": True, "rating": "useful", "record_ids": ["fact.x.lonely"],
             "key": "gone-2", "session_id": "gone"},
            {"time": when.isoformat(), "injected": True, "rating": "useful", "record_ids": ["fact.x.r", "fact.x.s"],
             "key": "gone-3", "session_id": "gone"},
        ])
        records = self.build()
        self.assertEqual({"fact.x.used", "fact.x.q", "fact.x.lonely"}, {k for k, v in records.items() if v["useful"]})
        self.assertEqual({"useful": 1, "harmful": 0}, feedback.unattributed(self.config))
        self.assertEqual({"stored": 0, "recomputed": 1, "from_citations": 3}, json.loads(
            (self.config / "runtime" / "feedback.json").read_text(encoding="utf-8"))["worked_out"])
        cache = json.loads((self.config / "runtime" / "rating_usage.json").read_text(encoding="utf-8"))
        self.assertEqual(usage.DETECTOR, cache["detector"])  # a new rule works them out again
        self.assertEqual((["fact.x.used"], []), (cache["messages"][rating.message_key(message)]["explicit"],
                                                 cache["messages"][rating.message_key(message)]["content"]))
        self.assertFalse(cache["messages"]["gone-1"]["found"])

    def test_weekly_use_is_summed_over_four_weeks(self) -> None:
        from agent_memory import feedback

        (self.reports / "2026-10-11.json").write_text(json.dumps(  # a report with the whole-session numbers (F12)
            {"signals": {"sessions": {"usage": {"per_record": {"b": {"injected": 4, "explicit": 1, "content": 1}}}}}}),
            encoding="utf-8")
        (self.reports / "2026-10-04.json").write_text(json.dumps(  # a report from before them counted citations
            {"signals": {"citation": {"per_record": {"b": {"injected": 3, "cited": 1}}}}}), encoding="utf-8")
        (self.reports / "2026-08-01.json").write_text(json.dumps(
            {"signals": {"citation": {"per_record": {"b": {"injected": 9, "cited": 9}}}}}), encoding="utf-8")
        records = self.build({"b": {"injected": 1, "used": 1}})
        # 1 + 1 named (the content match is not counted, A1 ruling) + 1 cited.
        self.assertEqual((8, 3), (records["b"]["injected"], records["b"]["used"]))
        self.assertEqual("升", feedback.judge(records["b"])[1])

    def test_a_report_s_per_record_lists_count_as_well(self) -> None:
        # What replay writes since F12: [injected, used by id, used by content].
        for day, counts in (("2026-10-11", [4, 1, 1]), ("2026-10-04", [3, 2, 0])):
            (self.reports / f"{day}.json").write_text(json.dumps(
                {"signals": {"sessions": {"usage": {"per_record": {"b": counts}}}}}), encoding="utf-8")
        records = self.build()  # as memoryctl rate rebuilds it: no week of its own
        # Named only (A1 ruling): 1 + 2.
        self.assertEqual((7, 3), (records.get("b", {}).get("injected"), records.get("b", {}).get("used")))


class DoctorHarmfulSectionTests(unittest.TestCase):
    def test_the_report_lists_records_rated_harmful_twice(self) -> None:
        from agent_memory import doctor

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, config, reports = base / "memory", base / "config", base / "reports"
            with mock.patch.dict(os.environ, {"AGENT_MEMORY_HOME": str(root), "AGENT_MEMORY_CONFIG_HOME": str(config),
                                              "MEMORYCTL_DOCTOR_CAPPED": "1"}):
                core.initialize(root, "fixture-machine")
                (config / "ratings").mkdir(parents=True)
                (config / "ratings" / "2026-W42.json").write_text(json.dumps([
                    {"time": "2026-10-16T10:00:00+00:00", "injected": True, "rating": "harmful", "record_ids": ["fact.x.stale"],
                     "cited_ids": []},
                    {"time": "2026-10-17T10:00:00+00:00", "injected": True, "rating": "harmful",
                     "record_ids": ["fact.x.stale", "fact.x.other"], "cited_ids": ["fact.x.stale"]},
                    {"time": "2026-10-17T11:00:00+00:00", "injected": True, "rating": "harmful",
                     "record_ids": ["fact.x.a", "fact.x.b"], "cited_ids": []},
                ]), encoding="utf-8")
                report, markdown = doctor.build_report(root, config, NOW, report_dir=reports,
                                                       projects_dir=base / "projects", run_tests=False)
        section = markdown.split("## 四、被標礙事的記憶（請檢查是否過時）", 1)[1]
        self.assertIn("fact.x.stale：礙事 2 次，最後一次評分 2026-10-17", section)
        self.assertIn("memoryctl supersede", section)
        self.assertIn("無法歸到單一記憶的評分：有用 0 題、礙事 1 題", section)
        self.assertEqual("fact.x.stale", report["signals"]["harmful"]["records"][0]["id"])


if __name__ == "__main__":
    unittest.main()
