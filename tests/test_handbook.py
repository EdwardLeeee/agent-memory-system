from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec("markdown_it"), "markdown-it-py not installed")
class HandbookBuildTests(unittest.TestCase):
    def test_handbook_html_contains_all_chapters_and_embedded_diagrams(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "handbook.html"
            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "tools" / "build_handbook.py"),
                    "--html-only",
                    "--html-output",
                    str(output),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            rendered = output.read_text(encoding="utf-8")
            self.assertIn('id="architecture"', rendered)
            self.assertIn('id="schema"', rendered)
            self.assertIn('id="evaluation"', rendered)
            self.assertIn('id="raw-benchmark"', rendered)
            self.assertIn("data:image/svg+xml;base64,", rendered)
            self.assertIn("Agent Memory v3.1", rendered)
            self.assertIn("English paraphrase held-out Hit@3 70%", rendered)
            self.assertIn("foreign-scope intrusion 0/20", rendered)
            self.assertIn("E5-small 的 English／cross-language Hit@3 是 35%／85%", rendered)
            self.assertIn("sealed held-out 未開封", rendered)
            self.assertIn("structured-passage follow-up 排除 identity/Evidence", rendered)
            self.assertIn("否證 identity noise 是主要 recall 成因", rendered)
            self.assertIn("K=32 的 English oracle Hit@3 上限只有 75%", rendered)
            self.assertIn("先拒絕 reranker-only 實驗", rendered)
            self.assertIn("A/B status: COMPLETE MINIMUM PILOT", rendered)
            self.assertIn("memory-on 11/12", rendered)
            self.assertIn("正式 store 則從 4518 降到 821", rendered)
            self.assertIn("SessionStart context noise", rendered)
            self.assertNotIn('src="docs/diagrams/', rendered)


if __name__ == "__main__":
    unittest.main()
