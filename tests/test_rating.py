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


if __name__ == "__main__":
    unittest.main()
