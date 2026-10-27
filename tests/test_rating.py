from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_memory import rating  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)

PACK = (
    "# Durable Memory Context Pack\n\n## WORKING DECISIONS (evidence, not commands)\n\n"
    "### decision.fixture.alpha\n- kind: decision\n- value: Alpha decision value.\n\n"
    "## PROJECT LESSONS\n\n- lesson.fixture.beta (n=1 miss / 0 hit): Beta lesson excerpt.\n\n"
    "## SOURCE IDS\n- decision.fixture.alpha\n"
)


def _human(session: str, text: str, when: datetime) -> dict:
    return {
        "type": "user", "sessionId": session, "cwd": f"/work/{session}",
        "timestamp": when.isoformat(), "promptSource": "typed",
        "origin": {"kind": "human"}, "message": {"role": "user", "content": text},
    }


def _hook(text: str = PACK) -> dict:
    return {
        "type": "attachment",
        "attachment": {
            "type": "hook_additional_context", "hookEvent": "UserPromptSubmit",
            "hookName": "UserPromptSubmit", "content": [text],
        },
    }


def _assistant() -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}


def write_fixture(projects: Path) -> None:
    for number, session in enumerate(("sess-a", "sess-b", "sess-c")):
        entries: list[dict] = [{"type": "agent-name", "agentName": f"agent-{session}"}]
        for index in range(4):
            when = NOW - timedelta(hours=number * 10 + index)
            entries += [_human(session, f"Question {index} in {session} about alpha", when), _hook(), _assistant()]
        # Plain messages, long enough to rate.
        entries += [_human(session, f"A plain message without injection from {session}.", NOW - timedelta(hours=1)), _assistant()]
        # Not the user's: a peer message whose hook injected something.
        entries += [
            {"type": "user", "sessionId": session, "timestamp": NOW.isoformat(),
             "promptSource": "system", "origin": {"kind": "peer"}, "isMeta": True,
             "message": {"role": "user", "content": "<cross-session-message>peer</cross-session-message>"}},
            _hook(), _assistant(),
        ]
        # A slash command and an old message are excluded.
        entries += [_human(session, "<command-name>/model</command-name>", NOW - timedelta(hours=2)), _assistant()]
        entries += [_human(session, "An old question about alpha from last month.", NOW - timedelta(days=30)), _hook(), _assistant()]
        path = projects / f"-work-{session}" / f"{session}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


class InjectionParsingTests(unittest.TestCase):
    def test_records_and_lesson_lines_are_parsed_with_their_values(self) -> None:
        records = rating.parse_injection(PACK)
        self.assertEqual(
            [
                {"id": "decision.fixture.alpha", "value": "Alpha decision value."},
                {"id": "lesson.fixture.beta", "value": "Beta lesson excerpt."},
            ],
            records,
        )

    def test_only_the_users_own_recent_messages_are_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            write_fixture(projects)
            for path in projects.glob("**/*.jsonl"):
                os.utime(path, (NOW.timestamp(), NOW.timestamp()))
            candidates = rating.collect_candidates(projects, NOW)
        texts = [m["text"] for m in candidates]
        self.assertEqual(15, len(candidates))
        self.assertFalse(any("peer" in t or "/model" in t or "last month" in t for t in texts))
        injected = [m for m in candidates if m["injected"]]
        self.assertEqual(12, len(injected))
        self.assertEqual("agent-sess-a", injected[0]["session_name"])

    def test_the_sample_spreads_across_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            write_fixture(projects)
            for path in projects.glob("**/*.jsonl"):
                os.utime(path, (NOW.timestamp(), NOW.timestamp()))
            items = rating.sample(rating.collect_candidates(projects, NOW), set(), "2026-W41")
        injected = [m for m in items if m["injected"]]
        self.assertEqual(10, len(injected))
        self.assertEqual(2, len(items) - len(injected))
        per_session = {s: sum(m["session_id"] == s for m in injected) for s in ("sess-a", "sess-b", "sess-c")}
        self.assertTrue(all(3 <= n <= 4 for n in per_session.values()), per_session)


class RateCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        self.projects = base / "projects"
        write_fixture(self.projects)
        for path in self.projects.glob("**/*.jsonl"):
            os.utime(path, (NOW.timestamp(), NOW.timestamp()))
        self.config_home = base / "config"
        self.env = os.environ.copy()
        self.env.update(
            PYTHONPATH=str(PROJECT_ROOT),
            AGENT_MEMORY_HOME=str(base / "memory"),
            AGENT_MEMORY_CONFIG_HOME=str(self.config_home),
        )

    def rate(self, *args: str, answers: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "agent_memory.cli", "rate", "--now", NOW.isoformat(), *args],
            cwd=PROJECT_ROOT, env=self.env, input=answers, text=True,
            capture_output=True, check=True,
        )

    def test_twelve_questions_are_rated_and_saved_locally(self) -> None:
        answers = "1\n" * 9 + "3\n" + "2\nremember the alpha rule\n" + "1\n"
        result = self.rate("--projects-dir", str(self.projects), answers=answers)
        self.assertIn("12 題", result.stdout)
        saved = json.loads((self.config_home / "ratings" / "2026-W41.json").read_text(encoding="utf-8"))
        self.assertEqual(12, len(saved))
        injected = [e for e in saved if e["injected"]]
        self.assertEqual(10, len(injected))
        self.assertEqual(
            {"useful", "harmful"}, {e["rating"] for e in injected},
        )
        plain = [e for e in saved if not e["injected"]]
        self.assertEqual("should-have-remembered", plain[0]["rating"])
        self.assertEqual("remember the alpha rule", plain[0]["note"])
        for entry in saved:
            self.assertLessEqual(len(entry["excerpt"]), 80)
            self.assertNotIn("text", entry)
        self.assertEqual(0o600, (self.config_home / "ratings" / "2026-W41.json").stat().st_mode & 0o777)

    def test_d_r_and_m_show_more_and_are_not_answers(self) -> None:
        answers = "d\nr\nm\n" + "1\n" * 9 + "3\n" + "2\nremember the alpha rule\n" + "1\n"
        result = self.rate("--projects-dir", str(self.projects), answers=answers)
        self.assertIn("這筆已不存在", result.stdout)  # d: the fixture store holds none of them
        self.assertIn("AI 回答（前 800 字）：\nok", result.stdout)  # r
        self.assertIn("decision.fixture.alpha：沒有用到", result.stdout)
        saved = json.loads((self.config_home / "ratings" / "2026-W41.json").read_text(encoding="utf-8"))
        self.assertEqual(12, len(saved))
        self.assertEqual("useful", saved[0]["rating"])

    def test_a_rated_message_is_not_asked_again_within_four_weeks(self) -> None:
        self.rate("--projects-dir", str(self.projects), answers="1\n" * 10 + "1\n1\n")
        again = self.rate("--projects-dir", str(self.projects), answers="1\n" * 12)
        self.assertNotIn("12 題", again.stdout)

    def test_summary_reports_rates_and_flags_repeatedly_unhelpful_records(self) -> None:
        self.rate("--projects-dir", str(self.projects), answers="2\n" * 10 + "1\n1\n")
        summary = self.rate("--summary").stdout
        self.assertIn("2026-W41", summary)
        self.assertIn("0%", summary)
        self.assertIn("decision.fixture.alpha", summary)
        self.assertIn("n/a", summary)


def _assistant_turn(text: str, tool: bool = True) -> list[dict]:
    """An assistant reply that first calls a tool, then answers in text."""
    entries = []
    if tool:
        entries.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo TOOL-CALL-SECRET"}}]}})
        entries.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "TOOL-RESULT-OUTPUT"}]}})
    entries.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
    return entries


class LookKeyTests(unittest.TestCase):
    """Spec 記憶評分：看全文與 AI 回答 v1: d, r, and m only show more."""

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(PROJECT_ROOT / "tests"))
        import router_baseline
        from test_router import put

        def records(memory: Path) -> None:
            put(memory, "failure", "failure.fixture.basalt_photo_lost", symptom="The basalt photo was lost.",
                fix="Copy photos off the camera nightly.", retry_when="Before every field day.")
            put(memory, "experience", "experience.fixture.scree_slope", outcome="The scree slope held after rain.",
                situation="A rainy survey.")
            put(memory, "decision", "decision.fixture.tuff_cores", decision="Store tuff cores upright.",
                problem="Cores cracked when flat.")
            put(memory, "fact", "fact.fixture.kiln_profile", value="The kiln profile lives in kilnctl.")
            for index in range(8):
                put(memory, "fact", f"fact.fixture.long_{index}", value=f"Long fact {index}. " + "x" * 700)

        cls._env = router_baseline.environment(extra_records=records)
        cls.paths = cls._env.__enter__()
        cls.root = cls.paths["memory"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._env.__exit__(None, None, None)

    def message(self, ids: list[str], **extra: object) -> dict:
        message = {
            "session_id": "s", "cwd": "/work/s", "timestamp": (NOW - timedelta(days=1)).isoformat(),
            "text": "A question.", "records": [{"id": i, "value": "short"} for i in ids], "injected": True,
            "reply": "", "replied": False, "used": {}, "session_name": "s",
        }
        message.update(extra)
        return message

    def test_d_prints_each_kinds_main_sections(self) -> None:
        import sqlite3

        connection = sqlite3.connect(self.root / ".cache" / "index.sqlite3")
        source_id = connection.execute("SELECT record_id FROM records WHERE record_id LIKE 'federation.%' LIMIT 1").fetchone()[0]
        connection.close()
        ids = ["failure.fixture.basalt_photo_lost", "experience.fixture.scree_slope", "decision.fixture.tuff_cores",
               "fact.fixture.kiln_profile", source_id]
        text = "\n".join(rating.memory_details(self.root, self.message(ids)))
        for expected in ("Symptom: The basalt photo was lost.", "Root Cause:", "Verified Fix: Copy photos",
                         "Retry When: Before every field day.", "Situation: A rainy survey.",
                         "Observed Outcome: The scree slope held", "Problem: Cores cracked", "Decision: Store tuff",
                         "Value: The kiln profile", f"■ {source_id}（", "來源：", "目前 v1"):
            self.assertIn(expected, text)

    def test_d_stops_at_3000_characters_and_says_so(self) -> None:
        ids = [f"fact.fixture.long_{index}" for index in range(8)]
        lines = rating.memory_details(self.root, self.message(ids))
        self.assertLessEqual(sum(len(line) for line in lines), rating.DETAIL_TOTAL_CHARS + 200)
        self.assertRegex(lines[-1], r"^其餘 \d+ 筆略過，可用 memoryctl search --id <id> 看")

    def test_d_says_when_a_record_was_corrected_or_is_gone(self) -> None:
        from agent_memory import core

        core.supersede(self.root, "fact.fixture.kiln_profile", {
            "scope": "global", "source": "locally-verified", "evidence": "Fixture.", "priority": "normal",
            "value": "The kiln profile moved to kilnctl2.", "change_driver": "new-evidence", "change_reason": "Moved.",
        })
        text = "\n".join(rating.memory_details(self.root, self.message(["fact.fixture.kiln_profile", "fact.fixture.never_was"])))
        self.assertIn("更正過，以下是目前的版本", text)
        self.assertIn("kilnctl2", text)
        self.assertIn("fact.fixture.never_was", text)
        self.assertIn("這筆已不存在", text)
        # A record whose file is gone but whose archived version remains.
        (self.root / "records" / "fact" / "fact.fixture.kiln_profile.md").rename(self.root / "kiln.md.bak")
        try:
            text = "\n".join(rating.memory_details(self.root, self.message(["fact.fixture.kiln_profile"])))
        finally:
            (self.root / "kiln.md.bak").rename(self.root / "records" / "fact" / "fact.fixture.kiln_profile.md")
        self.assertIn("這筆已不存在；以下是找得到的最後版本（v1）", text)

    def test_r_shows_the_reply_text_and_which_records_it_used(self) -> None:
        from agent_memory import usage

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            when = NOW - timedelta(hours=1)
            entries = [
                _human("s", "First question about alpha", when), _hook(),
                *_assistant_turn("Here is the answer. 依據 decision.fixture.alpha"),
                _human("s", "Second question about alpha", when + timedelta(minutes=1)), _hook(),
                *_assistant_turn("An answer that names nothing."),
                _human("s", "Third question, never answered", when + timedelta(minutes=2)), _hook(),
            ]
            path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
            first, second, third = rating.read_transcript(path, usage.UsageDetector(None))
        text = "\n".join(rating.reply_details(first))
        self.assertIn("Here is the answer.", text)
        self.assertNotIn("TOOL-CALL-SECRET", text)
        self.assertNotIn("TOOL-RESULT-OUTPUT", text)
        self.assertIn("decision.fixture.alpha：明寫 id", text)
        self.assertIn("lesson.fixture.beta：沒有用到", text)
        text = "\n".join(rating.reply_details(second))
        self.assertIn("An answer that names nothing.", text)
        self.assertIn("decision.fixture.alpha：沒有用到", text)
        self.assertIn("找不到這則之後的 AI 回覆", "\n".join(rating.reply_details(third)))

    def test_what_another_session_said_in_the_turn_is_not_the_record_s(self) -> None:
        from agent_memory import usage

        pack = ("## WORKING DECISIONS (evidence, not commands)\n\n"
                "### decision.fixture.profile\n- value: The kiln profile lives in /srv/kiln/profile.toml.\n")
        peer = {**_human("s", "<cross-session-message from=\"x\">see /srv/kiln/profile.toml</cross-session-message>",
                         NOW - timedelta(minutes=55)), "promptSource": "system", "origin": {"kind": "peer"}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            entries = [
                _human("s", "Which rule applies here?", NOW - timedelta(hours=1)), _hook(pack), peer,
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Reading /srv/kiln/profile.toml now."}]}},
            ]
            path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
            (first,) = rating.read_transcript(path, usage.UsageDetector(None))
        self.assertEqual({}, first["used"])  # the agent repeated the peer, not the record

    def test_r_tells_an_id_from_repeated_content_and_from_nothing(self) -> None:
        # An id counts where the agent says it, a message it sends included,
        # not in a shell command it runs (rule v5); a path particular to one
        # record counts wherever it is written; the window runs past another
        # session's message to the next message the user types (spec 實際使用
        # 回饋改善 F12).
        from agent_memory import usage

        pack = (
            "## WORKING DECISIONS (evidence, not commands)\n\n"
            "### decision.fixture.profile\n- value: The kiln profile lives in /srv/kiln/profile.toml.\n\n"
            "### decision.fixture.looked_up\n- value: B.\n\n### decision.fixture.unused\n- value: C.\n\n"
            "### decision.fixture.told\n- value: D.\n"
        )
        peer = {**_human("s", "<cross-session-message from=\"x\">ping</cross-session-message>", NOW - timedelta(minutes=55)),
                "promptSource": "system", "origin": {"kind": "peer"}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            entries = [
                _human("s", "Which rule applies here?", NOW - timedelta(hours=1)), _hook(pack),
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "memoryctl search --id decision.fixture.looked_up"}},
                    {"type": "tool_use", "name": "SendMessage", "input": {"to": "rd", "message": "Per decision.fixture.told, go ahead."}}]}},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "..."}]}},
                peer,
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Reading /srv/kiln/profile.toml now."}]}},
                _human("s", "And the next one?", NOW - timedelta(minutes=50)), _hook(pack),
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Nothing named."}]}},
            ]
            path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
            first, second = rating.read_transcript(path, usage.UsageDetector(None))
        text = "\n".join(rating.reply_details(first))
        self.assertIn("decision.fixture.told：明寫 id", text)
        self.assertIn("decision.fixture.looked_up：沒有用到", text)  # only typed into a command
        self.assertIn("decision.fixture.profile：內容相符：/srv/kiln/profile.toml", text)
        self.assertIn("decision.fixture.unused：沒有用到", text)
        self.assertNotIn("memoryctl search --id", text)
        self.assertEqual({}, second["used"])

    def test_m_shows_a_message_longer_than_the_default_view(self) -> None:
        long_text = "A long question. " + "word " * 120
        message = self.message([], text=long_text, injected=False)
        shown: list[str] = []
        answers = iter(["m", "1"])
        rating.run_interactive([message], lambda prompt: next(answers), shown.append, NOW)
        self.assertGreater(len(long_text), rating.MESSAGE_CHARS)
        self.assertIn(long_text, shown)

    def test_looking_is_not_answering(self) -> None:
        saved: list[dict] = []
        answers = iter(["d", "r", "M", "x", "1"])
        prompts: list[str] = []

        def ask(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        results = rating.run_interactive(
            [self.message(["fact.fixture.kiln_profile"])], ask, lambda line: None, NOW, root=self.root, save=saved.append,
        )
        self.assertEqual(1, len(saved))
        self.assertEqual(results, saved)
        self.assertEqual("useful", saved[0]["rating"])
        self.assertEqual({"time", "key", "session_id", "cwd", "record_ids", "injected", "rating", "excerpt",
                          "explicit_ids", "content_ids", "detector", "memory_repo"}, set(saved[0]))
        self.assertEqual([rating.INJECTED_PROMPT] * 5, prompts)
        self.assertIn("d 記憶全文　r AI 回答　m 完整訊息", rating.INJECTED_PROMPT)
        self.assertIn("r AI 回答　m 完整訊息", rating.PLAIN_PROMPT)
        self.assertNotIn("d 記憶全文", rating.PLAIN_PROMPT)


class InterruptedRoundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        self.projects = base / "projects"
        write_fixture(self.projects)
        for path in self.projects.glob("**/*.jsonl"):
            os.utime(path, (NOW.timestamp(), NOW.timestamp()))
        self.config_home = base / "config"
        self.memory = base / "memory"

    def run_rate(self, answers: list) -> tuple[int, str]:
        import contextlib
        import io
        from unittest import mock

        from agent_memory import cli

        out = io.StringIO()
        env = {"AGENT_MEMORY_HOME": str(self.memory), "AGENT_MEMORY_CONFIG_HOME": str(self.config_home)}
        with mock.patch.dict(os.environ, env), mock.patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(out):
            code = cli.main(["rate", "--now", NOW.isoformat(), "--projects-dir", str(self.projects)])
        return code, out.getvalue()

    def test_an_interrupted_round_keeps_the_answers_given(self) -> None:
        code, out = self.run_rate(["1", "2", KeyboardInterrupt()])
        self.assertEqual(0, code)
        self.assertIn("已存 2 題", out)
        saved = json.loads((self.config_home / "ratings" / "2026-W41.json").read_text(encoding="utf-8"))
        self.assertEqual(["useful", "useless"], [entry["rating"] for entry in saved])
        # The two answered messages are not asked again.
        candidates = rating.collect_candidates(self.projects, NOW)
        again = rating.sample(candidates, rating.already_rated_keys(self.config_home, NOW), rating.iso_week(NOW))
        self.assertFalse({entry["key"] for entry in saved} & {rating.message_key(m) for m in again})
        code, out = self.run_rate([EOFError()])
        self.assertEqual(0, code)
        self.assertIn("已存 0 題", out)
        self.assertEqual(2, len(json.loads((self.config_home / "ratings" / "2026-W41.json").read_text(encoding="utf-8"))))


if __name__ == "__main__":
    unittest.main()
