"""Tasks with a due time (spec 騰訊借鏡改善 T4)."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from agent_memory import cli, core, doctor  # noqa: E402
from agent_memory.storage import MemoryError, decode_record  # noqa: E402
import router_baseline  # noqa: E402

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
TASK = "task.alpha.fire_the_obsidian_batch"


def task(root: Path, due: datetime | None, scope: str = "project:alpha", record_id: str = TASK) -> None:
    payload = {"id": record_id, "scope": scope, "source": "user-stated", "evidence": "Asked for it.",
               "value": "Fire the obsidian batch before the gallery opens. Cone six."}
    if due is not None:
        payload["due"] = due.astimezone(timezone(timedelta(hours=8))).isoformat()
    core.record(root, "task", payload)


def start(paths: dict, session: str = "s1", cwd: Path | None = None) -> str:
    output = core.handle_hook(paths["memory"], "session-start", {
        "session_id": session, "cwd": str(cwd or paths["alpha"]), "hook_event_name": "SessionStart", "source": "startup"},
        agent="claude")
    return output.get("hookSpecificOutput", {}).get("additionalContext", "")


def prompt(paths: dict, text: str, session: str = "s1") -> str:
    output = core.handle_hook(paths["memory"], "user-prompt-submit", {
        "session_id": session, "cwd": str(paths["alpha"]), "hook_event_name": "UserPromptSubmit", "prompt": text},
        agent="claude")
    return output.get("hookSpecificOutput", {}).get("additionalContext", "")


def at(moment: datetime):
    return mock.patch.object(core, "_due_now", lambda: moment, create=True)


class DueTaskTests(unittest.TestCase):
    def test_a_version_number_does_not_end_the_summary_and_under_an_hour_shows_minutes(self) -> None:
        with router_baseline.environment() as paths:
            core.record(paths["memory"], "task", {
                "id": TASK, "scope": "project:alpha", "source": "user-stated", "evidence": "Asked for it.",
                "value": "Ship v1.2 of the kiln app before the gallery opens. Then rest.",
                "due": (NOW + timedelta(minutes=30)).isoformat()})
            (listed,) = core.due_tasks(paths["memory"], paths["alpha"])
            soon = core._due_line(listed, NOW)
            late = core._due_line(listed, NOW + timedelta(minutes=50))
        self.assertEqual("Ship v1.2 of the kiln app before the gallery opens.", listed["summary"])
        self.assertTrue(soon.endswith("還有 30 分鐘）"), soon)
        self.assertTrue(late.endswith("已過 20 分鐘）"), late)  # not "an hour late"

    def test_an_abbreviation_does_not_end_the_summary(self) -> None:
        with router_baseline.environment() as paths:
            core.record(paths["memory"], "task", {
                "id": TASK, "scope": "project:alpha", "source": "user-stated", "evidence": "Asked for it.",
                "value": "Use e.g. the kiln log and ask Mr. Lee first. Then rest.",
                "due": (NOW + timedelta(minutes=30)).isoformat()})
            (listed,) = core.due_tasks(paths["memory"], paths["alpha"])
        self.assertEqual("Use e.g. the kiln log and ask Mr. Lee first.", listed["summary"])

    def test_a_session_start_lists_a_task_due_within_a_day_and_not_one_done(self) -> None:
        with router_baseline.environment() as paths:
            task(paths["memory"], NOW + timedelta(hours=24))
            with at(NOW - timedelta(hours=1)):
                early = start(paths)  # 25 hours before
            with at(NOW + timedelta(hours=1)):
                within = start(paths, "s2")  # 23 hours before
                self.assertIn("## DUE TASKS\n\n- task.alpha.fire_the_obsidian_batch：Fire the obsidian batch before the gallery "
                              "opens.（到期 ", within)
                core.task_done(paths["memory"], TASK)
                done = start(paths, "s3")
        self.assertNotIn("## DUE TASKS", early)
        self.assertIn("，還有 23 小時）", within)
        self.assertNotIn("## DUE TASKS", done)

    def test_another_projects_task_is_not_listed_and_a_global_one_is(self) -> None:
        with router_baseline.environment() as paths:
            task(paths["memory"], NOW + timedelta(hours=2), scope="project:beta", record_id="task.beta.glaze_order")
            task(paths["memory"], NOW + timedelta(hours=3), scope="global", record_id="task.any.renew_kiln_permit")
            with at(NOW):
                pack = start(paths)
        self.assertNotIn("task.beta.glaze_order", pack)
        self.assertIn("- task.any.renew_kiln_permit：", pack)

    def test_a_task_due_since_the_session_started_is_named_once_at_a_typed_prompt_and_only_routed(self) -> None:
        with router_baseline.environment() as paths:
            task(paths["memory"], NOW + timedelta(hours=30))  # beyond the start's day ahead
            with at(NOW):
                start(paths)
            with at(NOW + timedelta(hours=31)):
                peer = prompt(paths, "Another Claude session sent a message:\n<cross-session-message from=\"x\">kiln?</cross-session-message>")
                first = prompt(paths, "qwertyuiop zxcvbnm")
                again = prompt(paths, "qwertyuiop zxcvbnm")
        with router_baseline.environment(router_toml="enabled = false\n") as paths:
            task(paths["memory"], NOW + timedelta(hours=30))
            with at(NOW):
                start(paths)
            with at(NOW + timedelta(hours=31)):
                flat = prompt(paths, "fire the obsidian batch")
        self.assertNotIn("到期：", peer)  # not a turn the user typed
        self.assertEqual(f"到期：{TASK} Fire the obsidian batch before the gallery opens.", first.split("\n\n", 1)[0])
        self.assertNotIn("到期：", again)
        self.assertNotIn("到期：", flat)  # with the router off, a prompt is what it was

    def test_without_a_task_due_soon_the_packs_are_what_they_were(self) -> None:
        with router_baseline.environment() as paths:
            with at(NOW):
                before_start, before_prompt = start(paths), prompt(paths, "granite smoke suite timing")
                task(paths["memory"], NOW + timedelta(days=5))  # due, but not within a day
                task(paths["memory"], None, record_id="task.alpha.sweep_the_kiln_room")  # no due time
                after_start, after_prompt = start(paths, "s2"), prompt(paths, "granite smoke suite timing", "s2")
                task(paths["memory"], NOW + timedelta(hours=5), record_id="task.alpha.cool_the_kiln")
                soon = start(paths, "s3")
        self.assertEqual(before_start, after_start)
        self.assertEqual(before_prompt, after_prompt)
        self.assertNotIn("DUE TASKS", after_start)
        self.assertIn("## DUE TASKS\n\n- task.alpha.cool_the_kiln：", soon)  # and one due within the day is listed

    def test_task_done_writes_a_new_version_and_the_doctor_lists_a_week_overdue(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            task(root, datetime.now(timezone.utc) - timedelta(days=9))
            report, _ = doctor.build_report(root, paths["config"], datetime.now(timezone.utc), report_dir=paths["base"] / "reports",
                                            projects_dir=paths["base"] / "projects", run_tests=False)
            late = next((check for check in report["checks"] if check["id"] == "due_tasks"), None)
            self.assertIsNotNone(late)  # the weekly report lists tasks a week overdue
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["task", "done", TASK])
            metadata, _ = decode_record(root / "records" / "task" / f"{TASK}.md")
            archived = list((root / "archive" / "task").rglob("*.md"))
            cleared = doctor.check_due_tasks(root)
        self.assertEqual(("WARN", [TASK]), (late["status"], late["value"]))
        self.assertIn("過期 9 天", late["detail"])
        self.assertEqual((0, "done", 2, 1), (code, metadata["task_status"], metadata["version"], len(archived)))
        self.assertEqual("OK", cleared["status"])

    def test_a_due_time_needs_its_time_zone_and_belongs_to_a_task(self) -> None:
        with router_baseline.environment() as paths:
            with self.assertRaisesRegex(MemoryError, "time zone"):
                core.record(paths["memory"], "task", {"id": "task.alpha.naive", "scope": "global", "source": "user-stated",
                                                      "evidence": "x", "value": "v", "due": "2026-10-21T20:03"})
            with self.assertRaisesRegex(MemoryError, "open or done"):
                core.record(paths["memory"], "task", {"id": "task.alpha.odd", "scope": "global", "source": "user-stated",
                                                      "evidence": "x", "value": "v", "status": "later"})
            with self.assertRaisesRegex(MemoryError, "due is for tasks"):
                core.record(paths["memory"], "fact", {"id": "fact.alpha.odd", "scope": "global", "source": "user-stated",
                                                      "evidence": "x", "value": "v", "due": "2026-10-21T20:03+08:00"})


if __name__ == "__main__":
    unittest.main()
