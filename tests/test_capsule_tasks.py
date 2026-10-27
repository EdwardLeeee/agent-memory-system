"""Writes that name every missing field, `memoryctl template`, and a capsule
changed in place with a task table (spec 實際使用回饋改善 F10, 騰訊借鏡改善 T5)."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from agent_memory import cli, core  # noqa: E402
from agent_memory.storage import MemoryError  # noqa: E402
import router_baseline  # noqa: E402

CAPSULE = {"state": "active", "active_task": "glaze the kiln shelves", "paused_task": "none",
           "resume_point": "fired batch one", "next_action": "fire batch two", "validation": "cone six bent",
           "rollback": "reglaze", "cwd": "/w/alpha"}


def run_cli(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(args))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


def capsule(paths: dict, session: str = "s1") -> str:
    return core.session_capsule_path(paths["memory"], session).read_text(encoding="utf-8")


def tasks_in(text: str) -> list[dict[str, str]]:
    """The task lines a capsule shows: [status] id：title ... · time."""
    import re

    found = []
    for line in text.splitlines():
        match = re.match(r"^  - \[(\w+)\] ([^：]+)：(.+?)(?:（等：.+）)? · (\S+)$", line)
        if match:
            found.append({"status": match.group(1), "id": match.group(2), "title": match.group(3), "updated_at": match.group(4)})
    return found


class MissingFieldTests(unittest.TestCase):
    def test_a_record_missing_several_fields_is_told_all_of_them_with_a_skeleton(self) -> None:
        with router_baseline.environment() as paths:
            with self.assertRaises(MemoryError) as caught:
                core.record(paths["memory"], "decision", {"id": "decision.alpha.kiln_shelves", "scope": "global"})
        message = str(caught.exception)
        self.assertIn("missing required decision fields: source, evidence, problem, decision, rationale, constraints, "
                      "alternatives_rejected, consequences, revisit_when", message)
        skeleton = json.loads(message.split("(memoryctl template decision):\n", 1)[1])  # JSON to copy from
        self.assertIn("revisit_when", skeleton)

    def test_a_supersede_and_a_checkpoint_list_theirs_too(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            core.record(root, "fact", {"id": "fact.alpha.kiln_count", "scope": "global", "source": "locally-verified",
                                       "evidence": "Counted.", "value": "Two kilns."})
            with self.assertRaises(MemoryError) as superseded:
                core.supersede(root, "fact.alpha.kiln_count", {"scope": "global", "value": "Three kilns."})
            with self.assertRaises(MemoryError) as checkpointed:
                core.checkpoint(root, {"state": "active", "active_task": "glaze"}, "s1")
        self.assertIn("missing required fact fields: source, evidence, change_driver, change_reason", str(superseded.exception))
        self.assertIn("missing required checkpoint fields: paused_task, resume_point, next_action, validation, rollback",
                      str(checkpointed.exception))

    def test_memoryctl_template_prints_the_skeleton_of_a_write(self) -> None:
        with router_baseline.environment():
            code, out, _ = run_cli("template", "failure")
            checkpoint_code, checkpoint_out, _ = run_cli("template", "checkpoint")
        self.assertEqual((0, 0), (code, checkpoint_code))
        self.assertIn("outbound_triggers", json.loads(out))
        self.assertEqual(["id", "title", "status", "waiting_on"], list(json.loads(checkpoint_out)["tasks"][0]))


class CapsuleTaskTests(unittest.TestCase):
    def test_tasks_are_one_line_each_and_a_capsule_without_them_is_as_before(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            core.checkpoint(root, dict(CAPSULE), "s1")
            plain = capsule(paths)
            core.checkpoint(root, {**CAPSULE, "tasks": [
                {"id": "PR-3", "title": "Fix the shelf parser", "status": "doing"},
                {"id": "PR-4", "title": "Kiln log", "status": "waiting", "waiting_on": "RD review"},
            ]}, "s1")
            with_tasks = capsule(paths)
        self.assertTrue(plain.endswith("- read_next: retrieve only the relevant canonical records and decisions\n"))
        self.assertNotIn("- tasks:", plain)
        self.assertIn("- tasks:\n", with_tasks)
        lines = with_tasks.split("- read_next: retrieve only the relevant canonical records and decisions\n", 1)[1].splitlines()
        self.assertEqual("- tasks:", lines[0])
        self.assertRegex(lines[1], r"^  - \[doing\] PR-3：Fix the shelf parser · 20\d\d-\d\d-\d\dT\d\d:\d\dZ$")
        self.assertRegex(lines[2], r"^  - \[waiting\] PR-4：Kiln log（等：RD review） · ")

    def test_set_changes_one_task_or_field_in_place_and_show_prints_the_capsule(self) -> None:
        with router_baseline.environment() as paths:
            core.checkpoint(paths["memory"], {**CAPSULE, "tasks": [
                {"id": "PR-3", "title": "Fix the shelf parser", "status": "doing"},
                {"id": "PR-4", "title": "Kiln log", "status": "todo"}]}, "s1")
            before = tasks_in(capsule(paths))
            code, _, _ = run_cli("checkpoint", "--session-id", "s1", "--set", "tasks.PR-3.status=done",
                                 "--set", "next_action=fire batch three", "--set", "tasks.PR-5.title=Order cones",
                                 "--set", "tasks.PR-5.status=todo")
            after = capsule(paths)
            shown_code, shown, _ = run_cli("checkpoint", "--session-id", "s1", "--show")
        tasks = {task["id"]: task for task in tasks_in(after)}
        self.assertEqual(0, code)
        self.assertEqual(("done", "todo", "todo"), (tasks["PR-3"]["status"], tasks["PR-4"]["status"], tasks["PR-5"]["status"]))
        self.assertEqual(before[1]["updated_at"], tasks["PR-4"]["updated_at"])  # unchanged, its time kept
        self.assertIn("- next_action: fire batch three\n", after)
        self.assertEqual((0, after), (shown_code, shown))

    def test_only_the_three_done_tasks_changed_last_stay(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            tasks = [{"id": f"PR-{n}", "title": f"Step {n}", "status": "done"} for n in range(1, 4)]
            core.checkpoint(root, {**CAPSULE, "tasks": tasks}, "s1")
            core.checkpoint(root, {**CAPSULE, "tasks": [*tasks, {"id": "PR-9", "title": "Step 9", "status": "done"}]}, "s1")
            left = [task["id"] for task in tasks_in(capsule(paths))]
        self.assertEqual(3, len(left))
        self.assertIn("PR-9", left)  # the one changed last stays

    def test_past_the_limit_the_oldest_events_go_and_this_writes_stay(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            old = [f"event {n} " + "x" * 180 for n in range(25)]  # about 4,800 bytes of resume_point
            core.checkpoint(root, {**CAPSULE, "resume_point": "；".join(old)}, "s1")
            new = ["event new " + "y" * 900, "event newer " + "z" * 900]
            code, _, err = run_cli("checkpoint", "--session-id", "s1", "--set", "resume_point=" + "；".join(old + new))
            text = capsule(paths)
        self.assertEqual(0, code)
        self.assertLessEqual(len(text.encode("utf-8")), 6000)
        self.assertIn("event newer", text)
        self.assertIn("event new y", text)
        self.assertNotIn("event 0 x", text)  # the oldest one an earlier write had
        self.assertIn("event 24 x", text)
        dropped = [line[4:] for line in err.splitlines() if line.startswith("  - ")]
        self.assertIn(f"dropped the {len(dropped)} oldest resume_point events", err)
        self.assertEqual([event[:40] for event in old[:len(dropped)]], dropped)  # each by its first 40 characters
        self.assertIn("warning: the capsule is", err)
        self.assertIn("or move work items to tasks", err)

    def test_a_task_line_holds_no_line_break_of_any_kind_and_its_id_no_equals_sign(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            for task in ({"id": "PR-3", "title": "Fix the shelf\u2028parser", "status": "doing"},
                         {"id": "PR-3", "title": "Fix the shelf\rparser", "status": "doing"},
                         {"id": "PR-3", "title": "Fix the shelf parser", "status": "waiting", "waiting_on": "RD\x0breview"},
                         {"id": "PR=3", "title": "Fix the shelf parser", "status": "doing"}):
                with self.subTest(task=task), self.assertRaises(MemoryError):
                    core.checkpoint(root, {**CAPSULE, "tasks": [task]}, "s1")

    def test_a_semicolon_in_quotes_backticks_or_a_path_splits_no_event(self) -> None:
        events = core._resume_events("跑了 `a；b` 的指令；看「先停；再跑」那句；檔案 /tmp/kiln；glaze/notes.md 留著；寫進 ~/agent-memory；收工")
        self.assertEqual(["跑了 `a；b` 的指令", "看「先停；再跑」那句", "檔案 /tmp/kiln；glaze/notes.md 留著", "寫進 ~/agent-memory", "收工"],
                         events)
        self.assertEqual(["a 「沒關上", "b"], core._resume_events("a 「沒關上；b"))  # an open quote: split as before

    def test_two_sets_at_once_both_land(self) -> None:
        import threading

        with router_baseline.environment() as paths:
            root = paths["memory"]
            core.checkpoint(root, dict(CAPSULE), "s1")
            read, first_read = core.capsule_payload, threading.Event()

            def slow(*args: object, **kwargs: object) -> tuple:
                found = read(*args, **kwargs)
                if threading.current_thread().name == "first":
                    first_read.set()
                    threading.Event().wait(0.5)  # the second --set reads the same capsule meanwhile, unlocked
                return found

            with mock.patch.object(core, "capsule_payload", slow):
                first = threading.Thread(target=run_cli, args=("checkpoint", "--session-id", "s1", "--set", "next_action=fire two"),
                                         name="first")
                first.start()
                first_read.wait(5)
                run_cli("checkpoint", "--session-id", "s1", "--set", "validation=cone seven bent")
                first.join(10)
            text = capsule(paths)
        self.assertIn("- next_action: fire two\n", text)
        self.assertIn("- validation: cone seven bent\n", text)

    def test_the_last_resume_point_event_is_never_dropped(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            core.checkpoint(root, {**CAPSULE, "resume_point": "fired batch one " + "x" * 200}, "s1")
            saved = capsule(paths)
            # The same single event; the rest of the capsule now fits only without it.
            with self.assertRaises(MemoryError):
                core.checkpoint(root, {**CAPSULE, "resume_point": "fired batch one " + "x" * 200,
                                       "next_action": "fire " + "y" * 5595}, "s1")
            kept = capsule(paths)
        self.assertEqual(saved, kept)  # the saved capsule stays, its resume_point too

    def test_a_write_without_tasks_keeps_them_and_an_empty_list_clears_them(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            core.checkpoint(root, {**CAPSULE, "tasks": [{"id": "PR-3", "title": "Fix the shelf parser", "status": "doing"}]}, "s1")
            before = tasks_in(capsule(paths))
            # A session started before tasks existed writes its capsule the old way.
            core.checkpoint(root, {**CAPSULE, "next_action": "fire batch three"}, "s1")
            kept = tasks_in(capsule(paths))
            core.checkpoint(root, {**CAPSULE, "tasks": []}, "s1")
            cleared = tasks_in(capsule(paths))
        self.assertEqual(before, kept)
        self.assertEqual([], cleared)

    def test_a_set_on_a_task_with_an_input_without_tasks_changes_the_saved_table(self) -> None:
        with router_baseline.environment() as paths:
            root = paths["memory"]
            core.checkpoint(root, {**CAPSULE, "tasks": [{"id": "PR-3", "title": "Fix the shelf parser", "status": "doing"}]}, "s1")
            core.checkpoint_report(root, {**CAPSULE, "next_action": "fire batch three"}, "s1",
                                   ["tasks.PR-4.title=Glaze the lids", "tasks.PR-4.status=todo"])
            text = capsule(paths)
        self.assertEqual(["PR-3", "PR-4"], sorted(task["id"] for task in tasks_in(text)))
        self.assertIn("- next_action: fire batch three", text)


if __name__ == "__main__":
    unittest.main()
