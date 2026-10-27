"""Moments a lesson was likely learned, and whether memory followed; measured
only (spec 騰訊借鏡改善 T1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from agent_memory import core, doctor  # noqa: E402
import router_baseline  # noqa: E402

SESSION = "7a1b2c3d-0000-4000-8000-000000000001"
MARK = "ZEBRA-PLUM-4417"  # must reach neither the report nor any cache


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def line(kind: str, at: datetime, cwd: str, **fields: object) -> str:
    return json.dumps({"type": kind, "sessionId": SESSION, "cwd": cwd, "timestamp": stamp(at), **fields},
                      ensure_ascii=False, separators=(",", ":"))


def call(at: datetime, cwd: str, tool_id: str, name: str, tool_input: dict) -> str:
    return line("assistant", at, cwd, message={"content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]})


def result(at: datetime, cwd: str, tool_id: str, error: bool, text: str = "done", code: int = 1) -> str:
    # A failed Bash call's output starts with the exit code, as Claude Code writes it.
    content = f"Exit code {code}\n{text}" if error else text
    return line("user", at, cwd, message={"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content,
                                                        "is_error": error}]})


def typed(at: datetime, cwd: str, text: str) -> str:
    return line("user", at, cwd, message={"role": "user", "content": text}, origin={"kind": "human"}, promptSource="typed")


def transcript(folder: Path, cwd: str, now: datetime) -> None:
    hours = lambda n: now - timedelta(hours=n)  # noqa: E731
    lines = [
        # A rule word, two hours before the write: not recorded after it.
        typed(hours(2), cwd, f"以後 {MARK} 都用 podman"),
        # A shell command failed, then the same command succeeded 3 minutes later.
        call(hours(3), cwd, "toolu_1", "Bash", {"command": f"cd /w && npm test -- {MARK}"}),
        result(hours(3), cwd, "toolu_1", True, f"{MARK} failed"),
        call(hours(3) + timedelta(minutes=3), cwd, "toolu_2", "Bash", {"command": "cd /w && npm test"}),
        result(hours(3) + timedelta(minutes=3), cwd, "toolu_2", False),
        # Another command that failed and only succeeded half an hour later: no signal.
        call(hours(4), cwd, "toolu_3", "Bash", {"command": "make kiln"}),
        result(hours(4), cwd, "toolu_3", True),
        call(hours(4) + timedelta(minutes=30), cwd, "toolu_4", "Bash", {"command": "make kiln"}),
        result(hours(4) + timedelta(minutes=30), cwd, "toolu_4", False),
        # A question answered ten minutes before the write: recorded.
        call(now - timedelta(minutes=10), cwd, "toolu_5", "AskUserQuestion", {"questions": [{"question": MARK}]}),
        result(now - timedelta(minutes=10), cwd, "toolu_5", False, f"User answered: {MARK}"),
    ]
    path = folder / "-w" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class MissedTests(unittest.TestCase):
    def report(self, paths: dict, now: datetime) -> tuple[dict, str]:
        return doctor.build_report(paths["memory"], paths["config"], now, report_dir=paths["base"] / "reports",
                                   projects_dir=paths["base"] / "projects", run_tests=False)

    def test_the_three_signals_and_whether_a_record_followed_within_half_an_hour(self) -> None:
        with router_baseline.environment() as paths:
            now = datetime.now(timezone.utc)
            transcript(paths["base"] / "projects", str(paths["alpha"]), now)
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION}):
                core.record(paths["memory"], "fact", {"id": "fact.alpha.kiln_runtime", "scope": "global", "source": "user-stated",
                                                      "evidence": "Asked.", "value": "The kiln runs on podman."})
            report, markdown = self.report(paths, now + timedelta(minutes=1))
        self.assertIn("## 六、可能漏記的時刻（只量測）", markdown)
        totals = report["signals"]["missed"]["totals"]
        self.assertEqual({"question": {"count": 1, "recorded": 1, "late": 0, "capsule": 0, "none": 0},
                          "rule-word": {"count": 1, "recorded": 0, "late": 0, "capsule": 0, "none": 1},
                          "retry": {"count": 1, "recorded": 0, "late": 0, "capsule": 0, "none": 1}}, totals)
        self.assertEqual({"alpha"}, set(report["signals"]["missed"]["by_project"]))
        section = markdown.split("## 六、可能漏記的時刻（只量測）", 1)[1]
        self.assertIn("AskUserQuestion 的回答 1（有記 1、晚記 0、只在膠囊 0、沒記 0）、使用者打的規則用語 1（有記 0、晚記 0、"
                      "只在膠囊 0、沒記 1）、指令失敗後改對 1（有記 0、晚記 0、只在膠囊 0、沒記 1）", section)
        self.assertIn(f"  - {SESSION[:8]} ", section)

    def test_nothing_said_or_run_reaches_the_report_or_a_cache(self) -> None:
        with router_baseline.environment() as paths:
            now = datetime.now(timezone.utc)
            transcript(paths["base"] / "projects", str(paths["alpha"]), now)
            report, markdown = self.report(paths, now + timedelta(minutes=1))
            stored = [path for folder in (paths["memory"] / ".cache", paths["config"], paths["base"] / "reports")
                      for path in folder.rglob("*") if path.is_file()]
            leaked = [str(path) for path in stored if MARK.encode() in path.read_bytes()]
        self.assertIn("指令失敗後改對 1（有記 0、晚記 0、只在膠囊 0、沒記 1）", markdown)  # the moments were read
        self.assertNotIn(MARK, markdown)
        self.assertNotIn(MARK, json.dumps(report, ensure_ascii=False))
        self.assertEqual([], leaked)

    def test_a_codex_command_fails_by_the_exit_code_its_wrapper_writes(self) -> None:
        from agent_memory import missed, replay

        with router_baseline.environment() as paths:
            now = datetime.now(timezone.utc)
            rollout = paths["base"] / "codex" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)

            def item(at: datetime, payload: dict) -> str:
                return json.dumps({"timestamp": stamp(at), "type": "response_item", "payload": payload})

            rollout.write_text("\n".join([
                json.dumps({"timestamp": stamp(now - timedelta(hours=1)), "type": "session_meta",
                            "payload": {"id": "codex-thread-1", "cwd": str(paths["alpha"])}}),
                item(now - timedelta(minutes=50), {"type": "function_call", "name": "exec_command", "call_id": "c1",
                                                   "arguments": json.dumps({"cmd": "pytest -q tests"})}),
                # The command's own output says exit 0; the wrapper's head says 1.
                item(now - timedelta(minutes=50), {"type": "function_call_output", "call_id": "c1",
                                                   "output": "Chunk ID: 1\nProcess exited with code 1\nOutput:\nExit code: 0"}),
                item(now - timedelta(minutes=48), {"type": "function_call", "name": "exec_command", "call_id": "c2",
                                                   "arguments": json.dumps({"cmd": "pytest -q tests"})}),
                item(now - timedelta(minutes=48), {"type": "function_call_output", "call_id": "c2",
                                                   "output": "Chunk ID: 2\nProcess exited with code 0\nOutput:\nok"}),
            ]) + "\n", encoding="utf-8")
            found = missed.measure([(rollout, "codex")], paths["memory"], lambda cwd: {"alpha"},
                                   replay.Window(now - timedelta(days=1)), paths["config"])
        self.assertEqual({"count": 1, "recorded": 0, "late": 0, "capsule": 0, "none": 1}, found["totals"]["retry"])


class MeasureRuleTests(unittest.TestCase):
    """How T1 counts after its 20 positions were judged (designer 2026-10-21)."""

    def measure(self, paths: dict, lines: list[str], now: datetime) -> dict:
        from agent_memory import missed, replay

        path = paths["base"] / "projects" / "-w" / f"{SESSION}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return missed.measure([(path, "claude")], paths["memory"], lambda cwd: {"alpha"},
                              replay.Window(now - timedelta(days=1)), paths["config"])

    def test_a_rule_word_counts_once_a_prompt_and_not_beside_an_error_reply_or_in_a_long_english_prompt(self) -> None:
        def reply(at: datetime, cwd: str, text: str) -> str:
            return line("assistant", at, cwd, message={"content": [{"type": "text", "text": text}]})

        with router_baseline.environment() as paths:
            now = datetime.now(timezone.utc)
            cwd = str(paths["alpha"])
            minutes = lambda n: now - timedelta(minutes=n)  # noqa: E731
            found = self.measure(paths, [
                typed(minutes(300), cwd, "以後都用 podman"), reply(minutes(299), cwd, "好，之後都用 podman。"),
                typed(minutes(290), cwd, "以後都用 podman"), reply(minutes(289), cwd, "好。"),  # the same prompt again
                typed(minutes(280), cwd, "記住這個設定"), reply(minutes(279), cwd, "API Error: Claude usage limit reached."),
                typed(minutes(270), cwd, "please " * 60 + "always tag the release"), reply(minutes(269), cwd, "Sure."),
            ], now)
        self.assertEqual(1, found["totals"]["rule-word"]["count"])

    def test_a_retry_needs_two_failures_or_two_minutes_and_found_nothing_is_no_failure(self) -> None:
        with router_baseline.environment() as paths:
            now = datetime.now(timezone.utc)
            cwd = str(paths["alpha"])
            minutes = lambda n: now - timedelta(minutes=n)  # noqa: E731
            ua = 'UA="Mozilla/5.0 (X11; Linux x86_64)" curl -s https://kiln.example/a'
            found = self.measure(paths, [
                # Failed once and worked a minute later: a slip, not a lesson.
                call(minutes(300), cwd, "t1", "Bash", {"command": "make kiln"}), result(minutes(300), cwd, "t1", True),
                call(minutes(299), cwd, "t2", "Bash", {"command": "make kiln"}), result(minutes(299), cwd, "t2", False),
                # Failed twice, then worked: counted, its key past the quoted VAR=value.
                call(minutes(200), cwd, "t3", "Bash", {"command": ua}), result(minutes(200), cwd, "t3", True),
                call(minutes(199), cwd, "t4", "Bash", {"command": ua}), result(minutes(199), cwd, "t4", True),
                call(minutes(198), cwd, "t5", "Bash", {"command": ua.replace("/a", "/b")}), result(minutes(198), cwd, "t5", False),
                # The tests passed; the grep counting failures at the end found none and returned 1.
                call(minutes(100), cwd, "t6", "Bash", {"command": "python3 -m unittest 2>&1 | grep -c FAIL"}),
                result(minutes(100), cwd, "t6", True, "0"),
                call(minutes(96), cwd, "t7", "Bash", {"command": "python3 -m unittest 2>&1 | grep -c FAIL"}),
                result(minutes(96), cwd, "t7", False),
                # Two scripts fed on stdin share only their head: not the same command.
                call(minutes(50), cwd, "t8", "Bash", {"command": "python3 - <<'EOF'\nprint(1)\nEOF"}),
                result(minutes(50), cwd, "t8", True), result(minutes(50), cwd, "t8", True),
                call(minutes(45), cwd, "t9", "Bash", {"command": "python3 - <<'EOF'\nprint(2)\nEOF"}),
                result(minutes(45), cwd, "t9", False),
            ], now)
        self.assertEqual(1, found["totals"]["retry"]["count"])
        self.assertEqual([], [item for item in found["unrecorded"] if item["signal"] == "retry"])  # only counted

    def test_a_project_commit_counts_as_recorded_a_late_write_as_late_and_a_capsule_apart(self) -> None:
        import subprocess

        with router_baseline.environment() as paths:
            now = datetime.now(timezone.utc)
            cwd = str(paths["alpha"])
            minutes = lambda n: now - timedelta(minutes=n)  # noqa: E731

            def question(at: datetime, tool_id: str) -> list[str]:
                return [call(at, cwd, tool_id, "AskUserQuestion", {"questions": [{"question": "Which?"}]}),
                        result(at, cwd, tool_id, False, "User answered: this one")]

            # 1: the session commits an ADR the source parses, 10 minutes later.
            (paths["alpha"] / "docs" / "adr" / "0009-kiln.md").write_text("# ADR-0009: Kiln\n\n## Decision\n\nThis one.\n",
                                                                         encoding="utf-8")
            subprocess.run(["git", "-C", cwd, "add", "-A"], check=True)
            subprocess.run(["git", "-C", cwd, "-c", "user.name=f", "-c", "user.email=f@example.invalid", "commit", "-q",
                            "-m", "adr"], check=True)
            head = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            notes = paths["config"] / "runtime" / "commits.jsonl"
            notes.parent.mkdir(parents=True, exist_ok=True)
            notes.write_text(json.dumps({"session_id": SESSION, "repo": "alpha", "commit": head,
                                         "at": (minutes(300) + timedelta(minutes=10)).isoformat()}) + "\n", encoding="utf-8")
            # 2: a record written 45 minutes after.
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION}), \
                    mock.patch.object(core, "utc_now", return_value=(minutes(200) + timedelta(minutes=45)).isoformat()):
                core.record(paths["memory"], "fact", {"id": "fact.alpha.kiln_late", "scope": "global", "source": "user-stated",
                                                      "evidence": "Asked.", "value": "Late."})
            found = self.measure(paths, [
                *question(minutes(300), "q1"),
                *question(minutes(200), "q2"),
                # 3: only the capsule, 20 minutes later.
                *question(minutes(100), "q3"),
                call(minutes(80), cwd, "c1", "Bash", {"command": "memoryctl checkpoint --input cp.json"}),
                result(minutes(80), cwd, "c1", False),
            ], now)
        self.assertEqual({"count": 3, "recorded": 1, "late": 1, "capsule": 1, "none": 0}, found["totals"]["question"])


if __name__ == "__main__":
    unittest.main()
