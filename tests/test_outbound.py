"""Reminders about what the agent sends, never holds (spec 實際使用回饋改善 F13)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from agent_memory import adapters, core, pretool  # noqa: E402
from agent_memory.storage import MemoryError, decode_record, encode_record, hook_chars  # noqa: E402
import router_baseline  # noqa: E402

FAILURE = "failure.alpha.unchecked_claims"
# Splits a run of non-space characters every way it can: seconds for 23 of
# them, four times that for two more (F13 review 1).
BACKTRACKS = r"(?:\S+\s*)+一定會"
HEADER = "送出的內容可能需要再查："
CORRECTION = f"上一則回覆可能需要更正：先拿到同事評估過的天數再說（{FAILURE}）"
SCHEDULE = r"(?:大概|大約)要?\s*[一二兩三\d]+\s*(?:到[一二兩三\d]+)?\s*(?:個)?天"


def claims(root: Path) -> None:
    core.record(root, "failure", {
        "id": FAILURE, "scope": "global", "source": "locally-verified", "evidence": "Fixture.", "applies": "origin",
        "symptom": "Told the user a release date nobody had estimated.", "attempt": "Gave a number.",
        "root_cause": "Said it before checking.", "verified_fix": "先拿到同事評估過的天數再說；沒有就說要先查。",
        "retry_when": "Before telling the user a schedule.", "outbound_triggers": [SCHEDULE, "最早"],
    })


def send(paths: dict, tool: str, tool_input: dict, session: str, cwd: Path | None = None) -> dict:
    output, _, _ = pretool.decide({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
                                   "session_id": session, "cwd": str(cwd or paths["alpha"])}, "claude")
    return output


def context_of(output: dict) -> str:
    return output.get("hookSpecificOutput", {}).get("additionalContext", "")


def typed(text: str) -> dict:
    """A prompt line as Claude Code writes one the user typed."""
    return {"type": "user", "origin": {"kind": "human"}, "promptSource": "typed", "message": {"role": "user", "content": text}}


def reply_transcript(folder: Path) -> Path:
    """A turn whose reply says when a story first ran, with a tool's output
    in between that says the same word."""
    transcript = folder / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in [
        typed("那則新聞什麼時候出來的？"),
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "最早 in a tool's output"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "這則報導最早見於美東 8:40。"}]}},
    ]) + "\n", encoding="utf-8")
    return transcript


def prompt_context(paths: dict, text: str, session: str, transcript: Path | None) -> str:
    """All a prompt's hook gave: the reply's correction line, then the pack."""
    output = core.handle_hook(paths["memory"], "user-prompt-submit", {
        "session_id": session, "cwd": str(paths["alpha"]), "hook_event_name": "UserPromptSubmit",
        "prompt": text, "transcript_path": str(transcript) if transcript else None}, agent="claude")
    return context_of(output)


def prompt(paths: dict, text: str, session: str, transcript: Path | None) -> str:
    """The correction line a prompt's hook gave, without the pack after it:
    every prompt in alpha routes alpha's lessons."""
    from agent_memory.turns import PACK_HEADER

    context = prompt_context(paths, text, session, transcript)
    return "" if context.startswith(PACK_HEADER) else context.split(f"\n\n{PACK_HEADER}", 1)[0]



class OutboundTests(unittest.TestCase):
    def test_outbound_triggers_must_compile_and_belong_to_a_failure(self) -> None:
        with router_baseline.environment() as paths:
            with self.assertRaisesRegex(MemoryError, "outbound trigger does not compile"):
                core.record(paths["memory"], "failure", {
                    "id": "failure.alpha.broken", "scope": "global", "source": "locally-verified", "evidence": "x",
                    "symptom": "s", "attempt": "a", "root_cause": "r", "verified_fix": "v", "retry_when": "w",
                    "outbound_triggers": ["(unclosed"]})
            with self.assertRaisesRegex(MemoryError, "outbound_triggers are only for failure records"):
                core.record(paths["memory"], "fact", {"id": "fact.alpha.x", "scope": "global", "source": "locally-verified",
                                                      "evidence": "x", "value": "v", "outbound_triggers": ["x"]})

    def test_what_is_about_to_be_sent_gets_the_lesson_and_is_never_held(self) -> None:
        with router_baseline.environment(extra_records=claims) as paths:
            message = send(paths, "SendMessage", {"to": "ceo", "message": "這份報告大概要一兩天完成。"}, "s1")
            question = send(paths, "AskUserQuestion", {"questions": [{"question": "要現在做嗎？", "options": [
                {"label": "現在做", "description": "大約一到兩天發布"}, {"label": "晚點", "description": "先評估"}]}]}, "s2")
            plain = send(paths, "SendMessage", {"to": "ceo", "message": "RD 評估後是 8 到 10 個日曆天。"}, "s3")
            elsewhere = send(paths, "SendMessage", {"to": "ceo", "message": "大概要一兩天。"}, "s4", paths["neutral"])
        for output in (message, question):
            self.assertTrue(context_of(output).startswith(HEADER + "\n\n### " + FAILURE))
            self.assertNotIn("permissionDecision", output.get("hookSpecificOutput", {}))
        self.assertEqual((pretool.SILENT, pretool.SILENT), (plain, elsewhere))  # elsewhere: it applies where it was learned
        with router_baseline.environment(extra_records=claims, router_toml="enabled = false\n") as paths:
            self.assertEqual(pretool.SILENT, send(paths, "SendMessage", {"to": "ceo", "message": "大概要一兩天。"}, "s5"))

    def test_one_record_reminds_a_session_once_every_five_turns(self) -> None:
        with router_baseline.environment(extra_records=claims) as paths:
            said = {"to": "ceo", "message": "這則報導最早見於美東 8:40。"}
            first = send(paths, "SendMessage", said, "s1")
            again = send(paths, "SendMessage", said, "s1")
            for _ in range(4):
                prompt(paths, "qwertyuiop zxcvbnm", "s1", None)
            fourth = send(paths, "SendMessage", said, "s1")
            prompt(paths, "qwertyuiop zxcvbnm", "s1", None)
            fifth = send(paths, "SendMessage", said, "s1")
        self.assertTrue(context_of(first))
        self.assertEqual(("", ""), (context_of(again), context_of(fourth)))
        self.assertTrue(context_of(fifth))

    def test_a_lesson_left_out_for_its_size_does_not_count_as_a_reminder(self) -> None:
        with router_baseline.environment(extra_records=claims) as paths:
            said = {"to": "ceo", "message": "大概要一兩天。"}
            with mock.patch.object(pretool, "CLAUDE_HOOK_CHAR_LIMIT", 40):
                cut = send(paths, "SendMessage", said, "s1")
            whole = send(paths, "SendMessage", said, "s1")
        self.assertEqual(pretool.SILENT, cut)
        self.assertTrue(context_of(whole))

    def test_the_reply_before_a_prompt_is_checked_at_that_prompt(self) -> None:
        with router_baseline.environment(extra_records=claims) as paths, tempfile.TemporaryDirectory() as tmp:
            transcript = reply_transcript(Path(tmp))
            alone = prompt(paths, "qwertyuiop zxcvbnm", "s1", transcript)
            with_pack = prompt_context(paths, "granite smoke suite timing", "s2", transcript)
            spaced = prompt_context(paths, "granite smoke suite timing", "s2", transcript)
            notice_turn = prompt(paths, "<task-notification>\n<task-id>b1</task-id>", "s3", transcript)
        self.assertEqual(CORRECTION, alone)
        self.assertTrue(with_pack.startswith(CORRECTION + "\n\n# Durable Memory Context Pack"))
        self.assertNotIn(CORRECTION, spaced)  # once every five turns
        self.assertEqual(CORRECTION, notice_turn)  # a subagent's report, its pack lean, and the reply still needs it

    def test_a_reply_before_a_tool_output_of_megabytes_is_still_read(self) -> None:
        with router_baseline.environment(extra_records=claims) as paths, tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in [
                typed("那則新聞什麼時候出來的？"),
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "這則報導最早見於美東 8:40。"},
                                                              {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x" * 600_000}]}},
            ]) + "\n", encoding="utf-8")
            notice = prompt(paths, "qwertyuiop zxcvbnm", "s1", transcript)
        self.assertEqual(CORRECTION, notice)

    def test_the_line_and_the_pack_stay_one_message_under_the_limit(self) -> None:
        with router_baseline.environment(extra_records=claims) as paths, tempfile.TemporaryDirectory() as tmp:
            transcript = reply_transcript(Path(tmp))
            full = prompt_context(paths, "granite smoke suite timing", "s1", None)
            with mock.patch.object(core, "CLAUDE_HOOK_CHAR_LIMIT", hook_chars(full)):
                capped = prompt_context(paths, "granite smoke suite timing", "s2", transcript)
        self.assertTrue(capped.startswith(CORRECTION))
        self.assertLessEqual(hook_chars(capped), hook_chars(full))

    def test_a_trigger_that_backtracks_badly_is_refused(self) -> None:
        with router_baseline.environment(extra_records=claims) as paths:
            with self.assertRaisesRegex(MemoryError, "outbound trigger repeats a repeat"):
                core.record(paths["memory"], "failure", {
                    "id": "failure.alpha.promises", "scope": "global", "source": "locally-verified", "evidence": "x",
                    "symptom": "s", "attempt": "a", "root_cause": "r", "verified_fix": "v", "retry_when": "w",
                    "outbound_triggers": [BACKTRACKS]})
            # One saved before the check is reported by validate.
            path = paths["memory"] / "records" / "failure" / f"{FAILURE}.md"
            metadata, body = decode_record(path)
            path.write_text(encode_record({**metadata, "outbound_triggers": [SCHEDULE, BACKTRACKS]}, body), encoding="utf-8")
            _, errors = core.validate(paths["memory"])
        self.assertEqual(1, sum("outbound trigger repeats a repeat" in error for error in errors), errors)

    def test_a_trigger_slow_on_text_it_does_not_match_is_refused_and_left_out_of_the_index(self) -> None:
        # Overlapping alternatives and repeats side by side, which the syntax check misses (F13 re-check 1).
        overlapping, adjacent = r"(?:\S|\S\S)+一定會", r"\S*\S*\S*一定會"
        with router_baseline.environment(extra_records=claims) as paths:
            for pattern in (overlapping, adjacent):
                with self.assertRaisesRegex(MemoryError, "outbound trigger is slow"):
                    core.record(paths["memory"], "failure", {
                        "id": "failure.alpha.promises", "scope": "global", "source": "locally-verified", "evidence": "x",
                        "symptom": "s", "attempt": "a", "root_cause": "r", "verified_fix": "v", "retry_when": "w",
                        "outbound_triggers": [pattern]})
            # One saved before the check: validate names it, and reindex leaves it out.
            path = paths["memory"] / "records" / "failure" / f"{FAILURE}.md"
            metadata, body = decode_record(path)
            path.write_text(encode_record({**metadata, "outbound_triggers": [SCHEDULE, adjacent]}, body), encoding="utf-8")
            _, errors = core.validate(paths["memory"])
            core.reindex(paths["memory"])
            cache = json.loads((paths["memory"] / ".cache" / "triggers.json").read_text(encoding="utf-8"))
        self.assertEqual(1, sum("outbound trigger is slow" in error for error in errors), errors)
        self.assertEqual([SCHEDULE], next(entry for entry in cache["entries"] if entry["id"] == FAILURE)["outbound"])

    def test_a_reply_check_out_of_time_still_answers_quickly(self) -> None:
        with router_baseline.environment(extra_records=claims) as paths, tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in [
                typed("會準時嗎？"),
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "這" * 40}]}},
            ]) + "\n", encoding="utf-8")
            # A trigger saved before writes refused it.
            cache = paths["memory"] / ".cache" / "triggers.json"
            triggers = json.loads(cache.read_text(encoding="utf-8"))
            triggers["entries"].append({"id": "failure.alpha.promises", "scope": "global", "verified_fix": "先查證。",
                                        "outbound": [BACKTRACKS]})
            cache.write_text(json.dumps(triggers, ensure_ascii=False), encoding="utf-8")
            event = {"session_id": "s1", "cwd": str(paths["alpha"]), "hook_event_name": "UserPromptSubmit",
                     "prompt": "granite smoke suite timing", "transcript_path": str(transcript)}
            started = time.monotonic()
            try:
                done = subprocess.run([sys.executable, str(PROJECT_ROOT / "bin" / "memoryctl"), "hook", "user-prompt-submit",
                                       "--agent", "claude"], input=json.dumps(event), capture_output=True, text=True,
                                      timeout=20, env=dict(os.environ))
            except subprocess.TimeoutExpired:
                self.fail("the prompt hook was still checking the reply after 20 seconds")
            took = time.monotonic() - started
            health = json.loads((paths["config"] / "runtime" / "health.json").read_text(encoding="utf-8"))["hooks"]
        self.assertLess(took, 10)
        context = context_of(json.loads(done.stdout))
        self.assertTrue(context.startswith("# Durable Memory Context Pack"), context[:200])  # the pack, without a line
        self.assertEqual(("error", "reply-time-limit"),
                         (health["user_prompt_submit"]["status"], health["user_prompt_submit"].get("code")))

    def test_the_spacing_comes_before_the_two_lines(self) -> None:
        def three(root: Path) -> None:
            claims(root)
            for name in ("vague_dates", "wishful_dates"):
                core.record(root, "failure", {
                    "id": f"failure.alpha.{name}", "scope": "global", "source": "locally-verified", "evidence": "Fixture.",
                    "symptom": "s", "attempt": "a", "root_cause": "r", "verified_fix": f"{name} 先查。", "retry_when": "w",
                    "outbound_triggers": ["最早"]})

        from agent_memory import turns

        with router_baseline.environment(extra_records=three) as paths, tempfile.TemporaryDirectory() as tmp:
            transcript = reply_transcript(Path(tmp))
            turns.due_reminders("s1", [FAILURE])  # the first of the three reminded this session just now
            notice = prompt(paths, "qwertyuiop zxcvbnm", "s1", transcript)
        self.assertEqual(["vague_dates 先查", "wishful_dates 先查"],
                         [line.split("：", 1)[1].split("（")[0] for line in notice.splitlines()])

    def test_only_what_was_written_since_the_prompt_before_is_checked(self) -> None:
        asked = typed("那則新聞什麼時候出來的？")
        claimed = {"type": "assistant", "message": {"content": [{"type": "text", "text": "這則報導最早見於美東 8:40。"}]}}
        now = typed("qwertyuiop zxcvbnm")

        def notice(paths: dict, session: str, lines: list[dict], folder: str) -> str:
            transcript = Path(folder) / f"{session}.jsonl"
            transcript.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n", encoding="utf-8")
            return prompt(paths, "qwertyuiop zxcvbnm", session, transcript)

        with router_baseline.environment(extra_records=claims) as paths, tempfile.TemporaryDirectory() as tmp:
            # A command queued while the agent worked is a prompt: the claim before it was the reply to the one before.
            queued = notice(paths, "s1", [asked, claimed, {"type": "attachment", "attachment": {
                "type": "queued_command", "prompt": "那還有別的嗎？", "commandMode": "prompt"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "沒有別的了。"}]}}], tmp)
            # Nothing written since the prompt before: the reply before that one was checked then.
            silent = notice(paths, "s2", [asked, claimed, typed("等一下"), now], tmp)
            # This turn's prompt already in the transcript is passed over.
            replied = notice(paths, "s3", [asked, claimed, now], tmp)
        self.assertEqual(("", ""), (queued, silent))
        self.assertEqual(CORRECTION, replied)

    def test_a_command_an_interrupt_or_a_summary_after_the_reply_is_not_the_prompt_before(self) -> None:
        # None of these lines ran a hook, so the reply before them was never checked (F13 re-check R1).
        claimed = {"type": "assistant", "message": {"content": [{"type": "text", "text": "這則報導最早見於美東 8:40。"}]}}
        after = {
            "interrupt": [{"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "[Request interrupted by user]"}]}}],
            "built-in command": [{"type": "user", "isMeta": True, "message": {"role": "user", "content": "<command-name>/model</command-name>"}},
                                 {"type": "user", "message": {"role": "user", "content": "<local-command-stdout>Set model</local-command-stdout>"}}],
            "summary": [{"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "This session is being continued."}}],
        }
        with router_baseline.environment(extra_records=claims) as paths, tempfile.TemporaryDirectory() as tmp:
            notices = {}
            for number, (name, lines) in enumerate(after.items()):
                transcript = Path(tmp) / f"{number}.jsonl"
                transcript.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in
                                                [typed("那則新聞什麼時候出來的？"), claimed, *lines]) + "\n", encoding="utf-8")
                notices[name] = prompt(paths, "qwertyuiop zxcvbnm", f"s{number}", transcript)
        self.assertEqual({name: CORRECTION for name in after}, notices)

    def test_what_was_written_while_a_queued_prompt_waited_is_checked_at_the_next(self) -> None:
        # A queued prompt's hook runs when it is queued; its line is written when it is delivered.
        def said(text: str, at: str) -> dict:
            return {"type": "assistant", "timestamp": at, "message": {"content": [{"type": "text", "text": text}]}}

        queued = {"type": "attachment", "timestamp": "2026-10-21T08:00:10.000Z", "attachment": {
            "type": "queued_command", "prompt": "那還有別的嗎？", "commandMode": "prompt"}}
        with router_baseline.environment(extra_records=claims) as paths, tempfile.TemporaryDirectory() as tmp:
            notices = []
            for number, at in enumerate(("2026-10-21T08:00:20.000Z", "2026-10-21T08:00:05.000Z")):
                transcript = Path(tmp) / f"{number}.jsonl"
                transcript.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in [
                    {**typed("那則新聞什麼時候出來的？"), "timestamp": "2026-10-21T08:00:00.000Z"},
                    said("這則報導最早見於美東 8:40。", at), queued, said("沒有別的了。", "2026-10-21T08:00:30.000Z"),
                ]) + "\n", encoding="utf-8")
                notices.append(prompt(paths, "qwertyuiop zxcvbnm", f"s{number}", transcript))
        # Written after it was queued: read now. Written before: its own hook read it then.
        self.assertEqual([CORRECTION, ""], notices)

    def test_a_subagent_s_reminder_is_its_own(self) -> None:
        with router_baseline.environment(extra_records=claims) as paths, tempfile.TemporaryDirectory() as tmp:
            transcript = reply_transcript(Path(tmp))
            prompt(paths, "qwertyuiop zxcvbnm", "s1", None)
            by_subagent, _, _ = pretool.decide({
                "hook_event_name": "PreToolUse", "tool_name": "SendMessage", "session_id": "s1", "agent_id": "a-sub-1",
                "cwd": str(paths["alpha"]), "tool_input": {"to": "main", "message": "查到了：這則報導最早見於美東 8:40。"}}, "claude")
            notice = prompt(paths, "qwertyuiop zxcvbnm", "s1", transcript)
        self.assertTrue(context_of(by_subagent))
        self.assertEqual(CORRECTION, notice)  # the main agent's own reply still gets its line

    def test_two_sends_in_one_message_remind_once(self) -> None:
        import threading

        from agent_memory import turns

        with router_baseline.environment(extra_records=claims) as paths:
            prompt(paths, "qwertyuiop zxcvbnm", "s1", None)
            read = turns._outbound_state
            first_read, second_read = threading.Event(), threading.Event()

            def interleaved(session_id: str) -> dict:
                state = read(session_id)
                if threading.current_thread().name == "first":
                    first_read.set()
                    second_read.wait(0.5)  # unlocked, the second hook reads the same state meanwhile
                else:
                    second_read.set()
                return state

            results: dict[str, list[str]] = {}

            def run() -> None:
                results[threading.current_thread().name] = turns.due_reminders("s1", [FAILURE])

            with mock.patch.object(turns, "_outbound_state", interleaved):
                first = threading.Thread(target=run, name="first")
                first.start()
                first_read.wait(2)
                second = threading.Thread(target=run, name="second")
                second.start()
                first.join(5)
                second.join(5)
        self.assertEqual({"first": [FAILURE], "second": []}, results)

    def test_a_reply_saying_again_what_a_send_was_just_reminded_of_gets_its_line(self) -> None:
        with router_baseline.environment(extra_records=claims) as paths, tempfile.TemporaryDirectory() as tmp:
            transcript = reply_transcript(Path(tmp))
            prompt(paths, "qwertyuiop zxcvbnm", "s1", None)
            sent = send(paths, "SendMessage", {"to": "ceo", "message": "這則報導最早見於美東 8:40。"}, "s1")
            next_turn = prompt(paths, "qwertyuiop zxcvbnm", "s1", transcript)
            after = prompt(paths, "qwertyuiop zxcvbnm", "s1", transcript)
        self.assertTrue(context_of(sent))
        self.assertEqual(CORRECTION, next_turn)  # the reply said it again in the same turn (F13 ruling 2)
        self.assertEqual("", after)  # and then the gap holds

    def test_claude_s_shell_hook_also_sees_what_is_sent(self) -> None:
        def matcher(agent: str) -> str:
            hooks: dict = {}
            adapters._apply_memory_hooks(hooks, Path("/opt/memoryctl"), spill_threshold=None, agent=agent)
            return hooks["PreToolUse"][-1]["matcher"]

        self.assertEqual(("Bash|SendMessage|AskUserQuestion", "Bash"), (matcher("claude"), matcher("codex")))


if __name__ == "__main__":
    unittest.main()
