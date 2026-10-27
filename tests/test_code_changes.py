"""The weekly "where the program may need changing" section (spec 論文借鏡改善 A5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

from agent_memory import core, doctor, replay  # noqa: E402
import router_baseline  # noqa: E402

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def human(text: str, minutes: int) -> dict:
    return {"type": "user", "sessionId": "s1", "cwd": "/work/app", "timestamp": (NOW - timedelta(minutes=minutes)).isoformat(),
            "promptSource": "typed", "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


def hook(record_id: str) -> dict:
    return {"type": "attachment", "attachment": {"type": "hook_additional_context", "hookEvent": "UserPromptSubmit",
                                                 "content": [f"# Durable Memory Context Pack\n\n### {record_id}\n- value: v\n"]}}


def search(minutes: int) -> dict:
    return {"type": "assistant", "cwd": "/work/app", "timestamp": (NOW - timedelta(minutes=minutes)).isoformat(),
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "memoryctl search --query kiln"}}]}}


def reply(minutes: int) -> dict:
    return {"type": "assistant", "cwd": "/work/app", "timestamp": (NOW - timedelta(minutes=minutes)).isoformat(),
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}


class CodeChangeSectionTests(unittest.TestCase):
    def test_the_ai_searching_memory_itself_is_counted_by_the_turns_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            path = projects / "-work-app" / "s1.jsonl"
            path.parent.mkdir()
            entries = [
                human("A question the pack answered badly", 50), hook("experience.web.kiln_note"), search(49), reply(48),
                human("A question with no pack at all", 40), search(39), reply(38),
                human("A question the pack answered", 30), hook("experience.web.kiln_note"), reply(29),
            ]
            path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
            # Counted in the same pass as the whole-session numbers (F12).
            measure = replay.Measure({"experience.web.kiln_note": "knowledge/web"}, lambda cwd: set(), None)
            measure.timeline(replay.timeline(path, "claude"))
        self.assertEqual({"calls": 2, "after": {"knowledge/web": 1, "沒有注入": 1}}, measure.result()["search"])

    def test_each_item_says_what_to_do(self) -> None:
        checks = [{"id": "golden", "value": [
            {"text": "原型的範圍包含哪些兵種", "ok": False, "why": "缺 decision.x"}, {"text": "ok", "ok": True}]}]
        usage = {"never_used_top": [{"id": "fact.x.noisy", "injections": 9}],
                 "search_after": {"沒有注入": 3, "knowledge/web": 1}}
        failure_layer = {"speed_bumps": {"rm-rf": {"denied": 4, "released": 4, "changed": 0},
                                         "git-reset-hard": {"denied": 5, "released": 1, "changed": 0}}}
        lines = "\n".join(doctor.code_change_lines(checks, usage, failure_layer, {"records": [{"id": "a"}]}))
        self.assertIn("golden 失敗：「原型的範圍包含哪些兵種」（缺 decision.x）", lines)
        self.assertIn("危險清單可能誤擋：rm-rf 擋 4 次，其中 4 次照原指令放行", lines)
        self.assertNotIn("git-reset-hard 擋", lines)
        self.assertIn("被注入很多次卻從沒被用到：fact.x.noisy（9 次）", lines)
        self.assertIn("路由可能漏了：AI 自己跑 memoryctl search 的輪次（依那一輪的注入來源）：沒有注入 3 輪、knowledge/web 1 輪", lines)
        self.assertIn("被標礙事的記憶：1 筆", lines)
        self.assertIn("doctor 不會自己改程式", lines)

    def test_global_records_naming_a_project_are_listed_for_that_project_to_decide(self) -> None:
        # Not another project's in the weekly count, but maybe scoped wider than meant (F16 re-check).
        def records(root: Path) -> None:
            for n in range(6):
                core.record(root, "fact", {"id": f"fact.beta.cone_{n}", "scope": "global", "source": "locally-verified",
                                           "evidence": "Fixture.", "value": f"Cone {n} is fired in the beta kiln."})

        with router_baseline.environment(extra_records=records) as paths, \
                mock.patch.dict(os.environ, {"MEMORYCTL_DOCTOR_CAPPED": "1"}):
            _, markdown = doctor.build_report(paths["memory"], paths["config"], NOW, report_dir=paths["base"] / "reports",
                                              projects_dir=paths["base"] / "projects", run_tests=False)
        section = markdown.split("## 五、可能需要改程式的地方（資訊）", 1)[1]
        # The fixture's own global beta experience is one of them.
        self.assertIn("- scope 可能太寬：beta 有 7 筆是 global，id 卻帶專案名（experience.beta.zircon_probe、fact.beta.cone_0、"
                      "fact.beta.cone_1、fact.beta.cone_2、fact.beta.cone_3 等 7 筆）→ 不算跨專案注入；"
                      "要不要縮成 project:beta，由那個專案決定，doctor 不會改", section)

    def test_the_weekly_report_has_the_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, config, reports = base / "memory", base / "config", base / "reports"
            with mock.patch.dict(os.environ, {"AGENT_MEMORY_HOME": str(root), "AGENT_MEMORY_CONFIG_HOME": str(config),
                                              "MEMORYCTL_DOCTOR_CAPPED": "1"}):
                core.initialize(root, "fixture-machine")
                _, markdown = doctor.build_report(root, config, NOW, report_dir=reports, projects_dir=base / "projects",
                                                  run_tests=False)
        section = markdown.split("## 五、可能需要改程式的地方（資訊）", 1)[1]
        self.assertIn("- golden：", section)
        self.assertIn("- 危險清單：沒有放行比例偏高的項目", section)
        self.assertIn("- 路由：本週沒有 AI 自己再去查記憶的輪次", section)


if __name__ == "__main__":
    unittest.main()
