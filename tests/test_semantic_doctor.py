"""The weekly doctor's embedding step (spec 論文借鏡改善 B1).

A stand-in interpreter answers for the child process, so the real
subprocess path runs without numpy or the model.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_memory import core, doctor  # noqa: E402

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def failure(record_id: str, scope: str, text: str) -> dict:
    return {"id": record_id, "scope": scope, "source": "locally-verified", "evidence": "Fixture.", "priority": "normal",
            "symptom": text, "attempt": text, "root_cause": text, "verified_fix": text, "retry_when": text}


def experience(record_id: str, text: str) -> dict:
    return {"id": record_id, "scope": "global", "source": "locally-verified", "evidence": "Fixture.",
            "situation": text, "expectation": text, "action": text, "observed_outcome": text, "applicability": text,
            "decision_links": []}


class SemanticDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root, self.config, self.reports = base / "memory", base / "config", base / "reports"
        self.python = base / "venv" / "python"
        self.python.parent.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "AGENT_MEMORY_HOME": str(self.root), "AGENT_MEMORY_CONFIG_HOME": str(self.config),
            "MEMORYCTL_DOCTOR_CAPPED": "1", "AGENT_MEMORY_SEMANTIC_PYTHON": str(self.python)})
        self.env.start()
        self.floor = mock.patch.object(doctor, "SEMANTIC_MEMORY_FLOOR_MB", 0, create=True)
        self.floor.start()
        core.initialize(self.root, "fixture-machine")

    def tearDown(self) -> None:
        self.floor.stop()
        self.env.stop()
        self.tmp.cleanup()

    def child(self, answer: dict | None = None, exit_code: int = 0, stderr: str = "") -> None:
        """The stand-in interpreter and an installed model's manifest."""
        (self.python.parent / "answer.json").write_text(json.dumps(answer or {}), encoding="utf-8")
        self.python.write_text("#!/bin/sh\ncat > /dev/null\n"
                               + (f"echo '{stderr}' >&2\nexit {exit_code}\n" if exit_code else
                                  f"cat '{self.python.parent / 'answer.json'}'\n"), encoding="utf-8")
        self.python.chmod(0o755)
        manifest = self.root / ".cache" / "semantic" / "model" / "agent-memory-model.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}", encoding="utf-8")

    def report(self) -> tuple[dict, str]:
        return doctor.build_report(self.root, self.config, NOW, report_dir=self.reports,
                                   projects_dir=Path(self.tmp.name) / "projects", run_tests=False)

    def test_a_pair_the_words_miss_joins_by_meaning(self) -> None:
        core.record(self.root, "failure", failure("failure.alpha.disk_quota_filled", "project:alpha",
                                                  "Bulk artifact download exhausted tmpfs quota."))
        core.record(self.root, "failure", failure("failure.beta.scratch_dump_root", "project:beta",
                                                  "Scratch dumps consumed root partition space."))
        self.child({"pairs": [["failure.alpha.disk_quota_filled", "failure.beta.scratch_dump_root", 0.81]],
                    "count": 2, "seconds": 1.0, "peak_rss_kb": 512000})
        report, markdown = self.report()
        pairs = report["signals"]["cross_project"]["pairs"]
        self.assertEqual([(["failure.alpha.disk_quota_filled", "failure.beta.scratch_dump_root"], 0.81)],
                         [(p["ids"], p.get("semantic")) for p in pairs])
        section = markdown.split("## 三、", 1)[1].split("## 四", 1)[0]
        self.assertIn("（語意相似 0.81（用字", section)
        self.assertIn("- 向量配對：用字沒抓到、語意相似 ≥ 0.75 的多找到 1 對", section)
        self.assertIn("向量子行程 500.0 MB", markdown)
        # It reaches the merge drafts like any other pair.
        self.assertEqual(1, len(report["signals"]["cross_project"]["drafts"]["groups"]))

    def test_records_no_topic_word_reaches_get_a_topic_suggestion(self) -> None:
        core.record(self.root, "experience", experience("experience.kiln.glaze_crazing", "Glaze crazing after a fast cone ten cooldown."))
        core.record(self.root, "experience", experience("experience.kiln.slow_cooling", "A slow cooldown past cone ten stops glaze crazing early."))
        # Its text holds its topic's name, so the topic already opens on it.
        core.record(self.root, "experience", experience("experience.kiln.named", "The kiln glaze cone schedule."))
        self.child({"pairs": [["experience.kiln.glaze_crazing", "experience.kiln.slow_cooling", 0.7],
                              ["experience.kiln.glaze_crazing", "experience.kiln.named", 0.9]],
                    "count": 3, "seconds": 1.0, "peak_rss_kb": 400000})
        report, markdown = self.report()
        topics = report["signals"]["cross_project"].get("semantic", {}).get("topics", [])
        self.assertEqual([("kiln", "unworded", ["experience.kiln.glaze_crazing", "experience.kiln.slow_cooling"])],
                         [(t["name"], t["status"], t["members"]) for t in topics])
        self.assertIn("crazing", topics[0]["words"])
        section = markdown.split("## 五、", 1)[1]
        self.assertIn("「kiln」（既有主題，目前沒有觸發詞，2 筆）：觸發詞 ", section)
        self.assertIn("就會取代內建的主題清單", section)

    def test_without_the_environment_or_the_model_the_report_goes_on(self) -> None:
        core.record(self.root, "failure", failure("failure.alpha.disk_quota_filled", "project:alpha", "Quota filled."))
        core.record(self.root, "failure", failure("failure.beta.scratch_dump_root", "project:beta", "Root partition full."))
        report, markdown = self.report()
        self.assertIn("- 向量配對：略過（沒有向量環境（", markdown)
        self.assertIn("- 主題建議：略過（沒有向量環境（", markdown)
        self.child({"pairs": []})
        (self.root / ".cache" / "semantic" / "model" / "agent-memory-model.json").unlink()
        report, markdown = self.report()
        self.assertIn("- 向量配對：略過（沒有本機模型（memoryctl semantic setup）），只用用字相似度", markdown)
        self.assertFalse(any("semantic" in p for p in report["signals"]["cross_project"]["pairs"]))

    def test_a_failing_child_is_reported_and_the_report_goes_on(self) -> None:
        self.child(exit_code=3, stderr="onnxruntime could not load the model")
        report, markdown = self.report()
        self.assertIn("- 向量配對：略過（onnxruntime could not load the model），只用用字相似度", markdown)
        self.assertEqual("onnxruntime could not load the model", report["signals"]["cross_project"]["semantic"]["error"])


class TopicWordTests(unittest.TestCase):
    """Trigger words leave out everyday English and the tail of a longer
    Chinese word (spec 實際使用回饋改善 F15)."""

    def test_everyday_english_and_chinese_word_tails_are_not_offered(self) -> None:
        texts = {
            "experience.kiln.one": ["Kiln crazing: the variable runtime fact. 在 Windows 分割區休眠，出分後再看。"],
            "experience.kiln.two": ["Crazing on the kiln; variable runtime fact again. Windows 分割區或休眠，出分後重測。"],
        }
        candidates = {record_id: {"topic": "kiln"} for record_id in texts}
        pairs = {frozenset(texts): 0.9}
        topics = doctor.topic_suggestions(candidates, pairs, texts, {}, {"topics": []})
        words = topics[0]["words"]
        self.assertIn("crazing", words)
        self.assertIn("分割", words)  # the head of 分割區 is a word
        self.assertIn("休眠", words)  # after 或, a function character
        for left_out in ("variable", "runtime", "fact", "割區", "分後"):
            self.assertNotIn(left_out, words)

    def test_a_word_that_mostly_follows_another_in_its_members_is_still_a_word(self) -> None:
        texts = {
            "experience.kiln.one": ["記憶系統的容器化很穩，重開也不掉。"],
            "experience.kiln.two": ["記憶系統的容器化不穩，重開會掉。"],
            # Elsewhere in the ledger each stands alone.
            "fact.kiln.other": ["系統提示要短。", "容器一律用 podman。"],
        }
        candidates = {record_id: {"topic": "kiln"} for record_id in list(texts)[:2]}
        pairs = {frozenset(list(texts)[:2]): 0.9}
        words = doctor.topic_suggestions(candidates, pairs, texts, {}, {"topics": []})[0]["words"]
        self.assertIn("系統", words)
        self.assertIn("容器", words)
        self.assertNotIn("憶系", words)  # 記憶系統 cut across its two words


    def test_a_bigram_with_one_of_the_doctor_s_own_function_characters_is_not_offered(self) -> None:
        # router.toml's list lacks 之 還 要; the doctor adds them (retrieval re-check R4).
        texts = {
            "experience.kiln.one": ["窯門之後還要等，釉面龜裂距離之前的測試很遠。"],
            "experience.kiln.two": ["窯門關上之後還要等，釉面龜裂要量距離之差。"],
        }
        candidates = {record_id: {"topic": "kiln"} for record_id in texts}
        pairs = {frozenset(texts): 0.9}
        words = doctor.topic_suggestions(candidates, pairs, texts, {}, {"topics": []})[0]["words"]
        self.assertIn("窯門", words)
        for left_out in ("之後", "還要", "離之"):
            self.assertNotIn(left_out, words)


if __name__ == "__main__":
    unittest.main()
