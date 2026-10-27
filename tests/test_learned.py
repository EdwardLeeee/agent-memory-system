"""Where a record was learned (spec 論文借鏡改善 A2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from agent_memory import cli, core, rating  # noqa: E402

SESSION_ENV = ("AGENT_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "GEMINI_SESSION_ID")
WHEN = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)


def _fact(record_id: str, value: str) -> dict:
    return {"id": record_id, "scope": "global", "source": "locally-verified", "evidence": "Fixture.",
            "priority": "normal", "value": value}


def _no_session() -> dict:
    return {name: "" for name in SESSION_ENV}


def _claude_lines(session: str, record_id: str, version_line: str) -> list[dict]:
    at = WHEN.isoformat().replace("+00:00", "Z")
    return [
        {"type": "agent-name", "agentName": "fixture-agent"},
        {"type": "user", "sessionId": session, "timestamp": at, "promptSource": "typed", "origin": {"kind": "human"},
         "message": {"role": "user", "content": "Please remember that the kiln runs at 1240 degrees."}},
        {"type": "assistant", "sessionId": session, "timestamp": at, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "memoryctl record --kind fact --input x.json"}}]}},
        {"type": "user", "sessionId": session, "timestamp": (WHEN + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
         "message": {"role": "user", "content": [{"type": "tool_result", "content": version_line}]}},
        {"type": "assistant", "sessionId": session, "timestamp": at, "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Saved the kiln temperature."}]}},
        {"type": "user", "sessionId": session, "timestamp": (WHEN + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
         "promptSource": "typed", "origin": {"kind": "human"},
         "message": {"role": "user", "content": "Next topic entirely."}},
        {"type": "assistant", "sessionId": session, "timestamp": at, "message": {"role": "assistant", "content": [
            {"type": "text", "text": "AFTER-THE-NEXT-QUESTION"}]}},
    ]


class LearnedInTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        self.root = base / "memory"
        self.config = base / "config"
        self.claude = base / "claude"
        self.codex = base / "codex"
        patcher = mock.patch.dict(os.environ, {"AGENT_MEMORY_HOME": str(self.root), "AGENT_MEMORY_CONFIG_HOME": str(self.config),
                                               **_no_session()})
        patcher.start()
        self.addCleanup(patcher.stop)
        core.initialize(self.root, "fixture-machine")

    def write(self, path: Path, entries: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")

    def metadata(self, record_id: str) -> dict:
        return core.decode_record(self.root / "records" / "fact" / f"{record_id}.md")[0]

    def test_record_and_supersede_note_the_writing_session(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-one"}):
            core.record(self.root, "fact", _fact("fact.fixture.kiln", "The kiln runs at 1240 degrees."))
        metadata = self.metadata("fact.fixture.kiln")
        self.assertEqual({"session_id": "sess-one", "at": metadata["created_at"]}, metadata["learned_in"])
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-two"}):
            core.supersede(self.root, "fact.fixture.kiln", {**_fact("fact.fixture.kiln", "1250 degrees now."),
                                                            "change_driver": "new-evidence", "change_reason": "Moved."})
        self.assertEqual("sess-two", self.metadata("fact.fixture.kiln")["learned_in"]["session_id"])
        # A writer with no session id claims none, and drops the old one.
        core.supersede(self.root, "fact.fixture.kiln", {**_fact("fact.fixture.kiln", "1260 degrees."),
                                                        "change_driver": "new-evidence", "change_reason": "Again."})
        self.assertNotIn("learned_in", self.metadata("fact.fixture.kiln"))
        core.record(self.root, "fact", _fact("fact.fixture.plain", "No session."))
        self.assertNotIn("learned_in", self.metadata("fact.fixture.plain"))
        self.assertEqual(0, len(core.validate(self.root)[1]))

    def test_backfill_finds_old_writes_in_both_agents_transcripts(self) -> None:
        from agent_memory import learned

        def after(record_id: str, seconds: int = 5, archived: bool = False) -> str:
            path = (next((self.root / "archive" / "fact" / record_id).glob("*.md")) if archived
                    else self.root / "records" / "fact" / f"{record_id}.md")
            written = datetime.fromisoformat(core.decode_record(path)[0]["updated_at"].replace("Z", "+00:00"))
            return (written + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

        def tool_result(session: str, at: str, text: str) -> dict:
            return {"type": "user", "sessionId": session, "timestamp": at,
                    "message": {"role": "user", "content": [{"type": "tool_result", "content": text}]}}

        core.record(self.root, "fact", _fact("fact.fixture.kiln", "The kiln runs at 1240 degrees."))
        first_at = after("fact.fixture.kiln")
        core.supersede(self.root, "fact.fixture.kiln", {**_fact("fact.fixture.kiln", "1250."),
                                                        "change_driver": "new-evidence", "change_reason": "Moved."})
        core.record(self.root, "fact", _fact("fact.fixture.codex_note", "Written from Codex."))
        self.write(self.claude / "-work" / "sess-claude.jsonl", [
            # The same id written into a copy of the ledger in a trial, an
            # hour before the real write: not the real one.
            tool_result("sess-trial", (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "Recorded fact.fixture.kiln"),
            tool_result("sess-claude", first_at, "Recorded fact.fixture.kiln\n"),
            tool_result("sess-claude", after("fact.fixture.kiln"), "Superseded fact.fixture.kiln version 1; event e"),
            tool_result("sess-claude", after("fact.fixture.kiln"), "Recorded fact.demo.not_in_ledger"),
        ])
        self.write(self.codex / "2026" / "10" / "rollout-x-019f0000-0000-7000-8000-000000000001.jsonl", [
            {"type": "session_meta", "payload": {"id": "019f0000-0000-7000-8000-000000000001"}},
            {"type": "response_item", "timestamp": after("fact.fixture.codex_note"),
             "payload": {"type": "function_call_output", "output": "Recorded fact.fixture.codex_note"}},
        ])
        summary = learned.backfill(self.root, self.claude, self.codex)
        self.assertEqual(2, summary["files_scanned"])
        cache = learned.load_cache(self.root)["records"]
        self.assertEqual([1, 2], [e["version"] for e in cache["fact.fixture.kiln"]])
        self.assertEqual(["sess-claude", "sess-claude"], [e["session_id"] for e in cache["fact.fixture.kiln"]])
        self.assertEqual(first_at, cache["fact.fixture.kiln"][0]["at"])
        self.assertEqual("019f0000-0000-7000-8000-000000000001", cache["fact.fixture.codex_note"][0]["session_id"])
        self.assertNotIn("fact.demo.not_in_ledger", cache)
        found = learned.learned_in_for(self.root, "fact.fixture.kiln")
        self.assertEqual((2, "transcript", "sess-claude"), (found["version"], found["source"], found["session_id"]))
        # Unchanged transcripts are not read again.
        self.assertEqual(0, learned.backfill(self.root, self.claude, self.codex)["files_scanned"])
        # The ledger itself is untouched.
        self.assertNotIn("learned_in", self.metadata("fact.fixture.kiln"))

    def test_context_shows_the_conversation_or_says_why_not(self) -> None:
        from agent_memory import learned

        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-claude"}):
            core.record(self.root, "fact", _fact("fact.fixture.kiln", "The kiln runs at 1240 degrees."))
        transcript = self.claude / "-work" / "sess-claude.jsonl"
        self.write(transcript, _claude_lines("sess-claude", "fact.fixture.kiln", "Recorded fact.fixture.kiln\n"))
        text = learned.context_text(self.root, "fact.fixture.kiln", self.claude, self.codex)
        self.assertIn("session sess-claude", text)
        self.assertIn("fixture-agent", text)
        self.assertIn("Please remember that the kiln runs at 1240 degrees.", text)
        self.assertIn("Saved the kiln temperature.", text)
        self.assertNotIn("memoryctl record --kind fact", text)
        self.assertNotIn("AFTER-THE-NEXT-QUESTION", text)
        # The transcript was cleaned away.
        transcript.unlink()
        self.assertIn("找不到這個 session 的對話紀錄（可能已被清掉）", learned.context_text(self.root, "fact.fixture.kiln", self.claude, self.codex))
        # Nothing records where it was learned.
        core.record(self.root, "fact", _fact("fact.fixture.plain", "No session."))
        self.assertIn("沒有記下學到時的對話", learned.context_text(self.root, "fact.fixture.plain", self.claude, self.codex))

    def test_context_names_the_source_of_the_message_before_the_write(self) -> None:
        # A record written after a subagent's report, or another session's
        # message, must not be shown beside an unrelated old user message.
        from agent_memory import learned

        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-claude"}):
            core.record(self.root, "fact", _fact("fact.fixture.kiln", "The kiln runs at 1240 degrees."))
            core.record(self.root, "fact", _fact("fact.fixture.glaze", "The glaze is matte."))
        write = datetime.fromisoformat(self.metadata("fact.fixture.kiln")["created_at"].replace("Z", "+00:00"))

        def at(delta: timedelta) -> str:
            return (write + delta).isoformat().replace("+00:00", "Z")

        def system(kind: str, text: str, delta: timedelta) -> dict:
            return {"type": "user", "sessionId": "sess-claude", "timestamp": at(delta), "promptSource": "system",
                    "origin": {"kind": kind}, "isMeta": kind == "peer", "message": {"role": "user", "content": text}}

        def result(text: str, delta: timedelta) -> dict:
            return {"type": "user", "sessionId": "sess-claude", "timestamp": at(delta),
                    "message": {"role": "user", "content": [{"type": "tool_result", "content": text}]}}

        self.write(self.claude / "-work" / "sess-claude.jsonl", [
            {"type": "user", "sessionId": "sess-claude", "timestamp": at(-timedelta(hours=3)), "promptSource": "typed",
             "origin": {"kind": "human"}, "message": {"role": "user", "content": "An unrelated question from hours ago."}},
            system("task-notification", "<task-notification>The subagent measured the kiln at 1240.</task-notification>",
                   -timedelta(minutes=4)),
            result("Recorded fact.fixture.kiln", timedelta(seconds=2)),
            {"type": "assistant", "sessionId": "sess-claude", "timestamp": at(timedelta(seconds=3)),
             "message": {"role": "assistant", "content": [{"type": "text", "text": "Noted from the report."}]}},
            system("peer", '<cross-session-message from="uds:x" from-name="designer">Record the glaze finish.</cross-session-message>',
                   timedelta(minutes=10)),
            result("Recorded fact.fixture.glaze", timedelta(minutes=10, seconds=30)),
        ])
        text = learned.context_text(self.root, "fact.fixture.kiln", self.claude, self.codex)
        self.assertIn("寫入前最近的一則訊息（子代理回報，寫入前 4 分鐘）：\n<task-notification>The subagent measured", text)
        self.assertIn("使用者最後一則訊息（寫入前 3 小時 0 分）：\nAn unrelated question from hours ago.", text)
        self.assertIn("Noted from the report.", text)
        # The glaze record was written right after another session's message;
        # its own timestamp is when the ledger wrote it, so the peer message
        # is shown by source, not as the user's.
        text = learned.conversation_at(self.claude / "-work" / "sess-claude.jsonl", "claude", "fact.fixture.glaze", 1, "")
        self.assertEqual("其他 session 的訊息（designer）", text["last"][0])
        self.assertEqual("使用者", text["last_user"][0])
        # A subagent's report sent back as a session message is a subagent's.
        transcript = self.claude / "-work" / "sess-claude.jsonl"
        lines = transcript.read_text(encoding="utf-8").splitlines()
        lines[-2] = json.dumps(system("peer", '<cross-session-message from="uds:x" from-name="fixture-rd">[Subagent hand-back] '
                                      'The review found the glaze is matte.</cross-session-message>', timedelta(minutes=10)))
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
        text = learned.conversation_at(transcript, "claude", "fact.fixture.glaze", 1, "")
        self.assertEqual("子代理回報", text["last"][0])

    def test_search_context_and_learned_in_commands(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-claude"}):
            core.record(self.root, "fact", _fact("fact.fixture.kiln", "The kiln runs at 1240 degrees."))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.main(["search", "--id", "fact.fixture.kiln", "--context"]))
            self.assertEqual(0, cli.main(["learned-in", "backfill", "--claude-dir", str(self.claude), "--codex-dir", str(self.codex)]))
            self.assertEqual(0, cli.main(["search", "--id", "fact.fixture.kiln"]))
        text = out.getvalue()
        self.assertIn("學到時的 session sess-claude", text)
        self.assertIn("讀了 0 份對話紀錄", text)
        self.assertIn("- learned_in: session sess-claude", text)

    def test_the_rating_d_view_points_at_the_conversation(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-claude"}):
            core.record(self.root, "fact", _fact("fact.fixture.kiln", "The kiln runs at 1240 degrees."))
        core.record(self.root, "fact", _fact("fact.fixture.plain", "No session."))
        message = {"session_id": "s", "cwd": "/w", "timestamp": WHEN.isoformat(), "text": "q",
                   "records": [{"id": "fact.fixture.kiln", "value": "v"}, {"id": "fact.fixture.plain", "value": "v"}],
                   "injected": True, "reply": "", "replied": False, "cited": [], "mentioned": [], "session_name": "s"}
        text = "\n".join(rating.memory_details(self.root, message, {"sess-claude": "kiln-session"}))
        today = self.metadata("fact.fixture.kiln")["created_at"][:10]
        self.assertIn(f"學到時的對話：memoryctl search --id fact.fixture.kiln --context（kiln-session，{today}）", text)
        self.assertIn("學到時的對話：memoryctl search --id fact.fixture.plain --context", text)


if __name__ == "__main__":
    unittest.main()
