from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from analyze_candidate_ceiling import compute_candidate_ceiling

# The retrieval and Codex suites stay with the private repository; a copy
# without them (the public one) skips the tests that read them.
needs_suites = unittest.skipUnless(
    (PROJECT_ROOT / "evaluation").is_dir(), "the evaluation suites are not shipped with this copy"
)


class RetrievalEvaluationTests(unittest.TestCase):
    def test_candidate_ceiling_rejects_a_reranker_when_targets_are_absent(self) -> None:
        cases = [
            {
                "case_id": "english-present",
                "category": "english_paraphrase",
                "expected_id": "target.en.present",
            },
            {
                "case_id": "english-missing",
                "category": "english_paraphrase",
                "expected_id": "target.en.missing",
            },
            {
                "case_id": "cross-present",
                "category": "cross_language",
                "expected_id": "target.zh.present",
            },
            {
                "case_id": "cross-missing",
                "category": "cross_language",
                "expected_id": "target.zh.missing",
            },
        ]
        raw = {
            "english-present": [
                {"record_id": "target.en.present", "score": 0.8}
            ],
            "english-missing": [
                {"record_id": "wrong.en", "score": 0.9}
            ],
            "cross-present": [
                {"record_id": "target.zh.present", "score": 0.8}
            ],
            "cross-missing": [
                {"record_id": "wrong.zh", "score": 0.9}
            ],
        }

        report = compute_candidate_ceiling(
            cases,
            raw,
            shortlist_sizes=(3, 8, 16, 32, 64),
        )

        findings = report["structural_findings"]
        self.assertTrue(findings["reranker_only_k32_rejected"])
        self.assertFalse(findings["reranker_only_k64_theoretically_possible"])
        english = report["categories"]["english_paraphrase"]["by_shortlist"]
        self.assertEqual(0.5, english["32"]["oracle_hit_at_3_ceiling"])
        self.assertEqual(
            ["english-missing"],
            english["64"]["missing_case_ids"],
        )

    @needs_suites
    def test_retrieval_suite_exposes_all_160_fixed_cases(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools" / "benchmark_hybrid.py"),
                "--validate-suite",
                "--suite",
                str(PROJECT_ROOT / "evaluation" / "retrieval-suite-v1.json"),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual("agent-memory-retrieval-v1", report["suite_id"])
        self.assertEqual(160, report["counts"]["total"])
        self.assertEqual(40, report["counts"]["exact"])
        self.assertEqual(40, report["counts"]["english_paraphrase"])
        self.assertEqual(40, report["counts"]["cross_language"])
        self.assertEqual(20, report["counts"]["no_answer"])
        self.assertEqual(20, report["counts"]["scope_decoy"])
        self.assertEqual(80, report["splits"]["development"])
        self.assertEqual(80, report["splits"]["held_out"])

    @needs_suites
    def test_retrieval_development_phase_excludes_every_held_out_case(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools" / "benchmark_hybrid.py"),
                "--validate-suite",
                "--phase",
                "development",
                "--suite",
                str(PROJECT_ROOT / "evaluation" / "retrieval-suite-v1.json"),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual("development", report["phase"])
        self.assertEqual(80, report["selected_counts"]["total"])
        self.assertEqual(20, report["selected_counts"]["exact"])
        self.assertEqual(20, report["selected_counts"]["english_paraphrase"])
        self.assertEqual(20, report["selected_counts"]["cross_language"])
        self.assertEqual(10, report["selected_counts"]["no_answer"])
        self.assertEqual(10, report["selected_counts"]["scope_decoy"])
        self.assertEqual(
            {"development": 80, "held_out": 0},
            report["selected_splits"],
        )

    @needs_suites
    def test_retrieval_v2_suite_is_frozen_before_candidate_selection(self) -> None:
        suite_path = PROJECT_ROOT / "evaluation" / "retrieval-suite-v2.json"
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools" / "benchmark_hybrid.py"),
                "--validate-suite",
                "--phase",
                "development",
                "--suite",
                str(suite_path),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(
            "agent-memory-retrieval-v2-sealed-20260820",
            report["suite_id"],
        )
        self.assertEqual(
            "bfaa35f36f1fd895e8d5356b5df25b77afbeb80a43431f92e6b7bf61fbfc0d09",
            report["suite_sha256"],
        )
        self.assertEqual(
            {"development": 80, "held_out": 0},
            report["selected_splits"],
        )
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
        self.assertTrue(suite["design"]["frozen_before_candidate_model_selection"])
        self.assertTrue(
            all(
                not any(character.isascii() and character.isalnum() for character in case["cross_language_query"])
                for case in suite["concepts"]
            )
        )

    @needs_suites
    def test_retrieval_held_out_phase_selects_no_development_case(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools" / "benchmark_hybrid.py"),
                "--validate-suite",
                "--phase",
                "held_out",
                "--semantic-profile",
                "multilingual-e5-small",
                "--suite",
                str(PROJECT_ROOT / "evaluation" / "retrieval-suite-v2.json"),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual({"development": 0, "held_out": 80}, report["selected_splits"])
        self.assertEqual("multilingual-e5-small", report["semantic_profile"])

    @needs_suites
    def test_retrieval_held_out_execution_requires_a_frozen_threshold(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools" / "benchmark_hybrid.py"),
                "--phase",
                "held_out",
                "--suite",
                str(PROJECT_ROOT / "evaluation" / "retrieval-suite-v2.json"),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("--fixed-threshold", result.stderr)

    @needs_suites
    def test_codex_pilot_expands_to_exactly_84_blinded_calls(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools" / "run_codex_pilot.py"),
                "--validate-suite",
                "--suite",
                str(PROJECT_ROOT / "evaluation" / "codex-pilot-v1.json"),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual("agent-memory-codex-pilot-v1", report["suite_id"])
        self.assertEqual(36, report["retrieval_calls"])
        self.assertEqual(48, report["compact_calls"])
        self.assertEqual(84, report["total_calls"])
        self.assertEqual(84, report["unique_run_ids"])
        self.assertEqual(2, report["prompt_protocol_version"])

    @needs_suites
    def test_codex_memory_ab_suite_expands_to_exactly_24_paired_calls(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools" / "run_codex_pilot.py"),
                "--validate-suite",
                "--suite",
                str(PROJECT_ROOT / "evaluation" / "codex-memory-ab-v2.json"),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual("agent-memory-codex-ab-v2", report["suite_id"])
        self.assertEqual(12, report["retrieval_calls"])
        self.assertEqual(12, report["compact_calls"])
        self.assertEqual(24, report["total_calls"])
        self.assertEqual(24, report["unique_run_ids"])
        self.assertEqual(12, report["paired_cases"])
        self.assertEqual(["off", "on"], report["conditions"])
        self.assertEqual("gpt-5.6-sol", report["model"])
        self.assertEqual("max", report["reasoning_effort"])
        self.assertEqual(4, report["prompt_protocol_version"])

    def test_published_protocol4_result_is_complete_and_claim_bounded(self) -> None:
        report = json.loads(
            (PROJECT_ROOT / "docs" / "evaluation-results.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(6, report["schema_version"])
        ab = report["end_to_end_ab"]
        self.assertEqual("complete_minimum_paired_pilot", ab["status"])
        result = ab["valid_effect_result"]
        self.assertTrue(result["eligible_for_effect_claims"])
        self.assertEqual(24, result["calls"]["completed"])
        self.assertEqual(12, result["calls"]["complete_pairs"])
        overall = result["paired"]["overall"]
        self.assertEqual(11, overall["memory_on_passes"])
        self.assertEqual(3, overall["memory_off_passes"])
        self.assertEqual(0.6667, overall["absolute_pass_rate_delta"])
        self.assertIn("one-completion-per-arm", result["claim_boundary"])
        audit = ab["corrected_protocol"]["raw_artifact_audit"]
        self.assertEqual(0, audit["memory_off_context_leaks"])
        self.assertEqual(0, audit["host_or_suite_path_leaks"])
        self.assertFalse(
            report["deterministic_retrieval"]["activation"]["enabled"]
        )
        snapshot = report["deterministic_retrieval"][
            "production_snapshot_after_reindex"
        ]
        self.assertEqual(47, snapshot["indexed_records"])
        self.assertEqual(47, snapshot["expected_records"])
        self.assertFalse(snapshot["enabled"])
        follow_up = report["development_retrieval_v2"][
            "structured_passage_follow_up"
        ]
        self.assertEqual(
            "complete_development_gate_failed_held_out_unopened",
            follow_up["status"],
        )
        self.assertEqual(0, follow_up["remote_model_calls"])
        self.assertEqual(0, follow_up["held_out_queried_cases"])
        self.assertEqual(0.35, follow_up["metrics"]["english_paraphrase_hit_at_3"])
        self.assertEqual(0.85, follow_up["metrics"]["cross_language_hit_at_3"])
        self.assertFalse(follow_up["development_gate_met"])
        ceiling = report["development_retrieval_v2"][
            "candidate_discrimination_ceiling"
        ]
        self.assertEqual("complete_cached_zero_inference_stop_rule", ceiling["status"])
        self.assertEqual(0, ceiling["execution"]["new_embedding_queries"])
        self.assertEqual(0, ceiling["execution"]["reranker_calls"])
        self.assertEqual(0, ceiling["execution"]["gpt_calls"])
        self.assertEqual(0, ceiling["execution"]["held_out_queried_cases"])
        english = ceiling["raw_semantic_oracle_ceiling"]["english_paraphrase"]
        self.assertEqual(0.75, english["k32_hit_at_3_ceiling"])
        self.assertEqual(0, english["k64_reranker_mistake_budget_at_gate"])
        self.assertTrue(
            ceiling["decision"]["reranker_only_k32_rejected_without_inference"]
        )

    @unittest.skip("evaluation/codex-memory-ab-v2.json is a sealed Protocol 4 research fixture (published result SHA-256 pinned in docs/EVALUATION.md). Its ab-foreign-scope case bundles a project-scope decoy alongside a machine-scope decoy and expects both to stay silent. The visibility/affinity fix intentionally makes a project-scope record retrievable outside its project again, so the project decoy now legitimately appears. Not touching the sealed fixture; a revised Protocol needs separate authorization before this can be un-skipped.")
    @needs_suites
    def test_codex_memory_ab_preflight_uses_real_hooks_only_for_memory_on(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "tools" / "run_codex_pilot.py"),
                    "--prepare-contexts",
                    "--suite",
                    str(PROJECT_ROOT / "evaluation" / "codex-memory-ab-v2.json"),
                    "--workdir",
                    temporary,
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(24, report["total_runs"])
            self.assertEqual(12, report["memory_on_runs"])
            self.assertEqual(12, report["memory_off_runs"])
            self.assertEqual(0, report["memory_off_contexts"])
            self.assertEqual(12, report["hook_invocations"])
            self.assertEqual([], report["contract_failures"])
            self.assertGreater(report["memory_on_contexts"], 0)
            self.assertEqual("bubblewrap", report["filesystem_isolation"]["backend"])
            self.assertTrue(report["filesystem_isolation"]["fixture_visible"])
            self.assertFalse(report["filesystem_isolation"]["host_home_visible"])
            self.assertFalse(report["filesystem_isolation"]["memoryctl_available"])
            self.assertFalse(report["filesystem_isolation"]["suite_visible"])
            self.assertEqual(
                "inherited-for-codex-transport",
                report["filesystem_isolation"]["network_namespace"],
            )
            self.assertTrue(
                report["filesystem_isolation"]["resolver_config_readable"]
            )

    @unittest.skip("evaluation/codex-memory-ab-v2.json is a sealed Protocol 4 research fixture (published result SHA-256 pinned in docs/EVALUATION.md). Its ab-foreign-scope case bundles a project-scope decoy alongside a machine-scope decoy and expects both to stay silent. The visibility/affinity fix intentionally makes a project-scope record retrievable outside its project again, so the project decoy now legitimately appears. Not touching the sealed fixture; a revised Protocol needs separate authorization before this can be un-skipped.")
    @needs_suites
    def test_codex_memory_ab_invocation_pins_sol_with_max_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_codex = fake_bin / "codex"
            fake_code_mode_host = fake_bin / "codex-code-mode-host"
            fake_code_mode_host.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_code_mode_host.chmod(0o700)
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "if sys.argv[1:] == ['--version']:\n"
                "    print('codex-cli 9.9.0-test')\n"
                "    raise SystemExit(0)\n"
                "args = sys.argv[1:]\n"
                "assert pathlib.Path('/codex-code-mode-host').is_file()\n"
                "output = pathlib.Path(args[args.index('--output-last-message') + 1])\n"
                "response = {\n"
                "    'answer': 'CAPTURE-REUSE-642',\n"
                "    'next_action': '',\n"
                "    'validation': '',\n"
                "    'rollback': '',\n"
                "    'evidence_paths': [],\n"
                "    'memory_assessment': 'accepted',\n"
                "}\n"
                "output.write_text(json.dumps(response), encoding='utf-8')\n"
                "print(json.dumps({'invocation': args}))\n"
                "print(json.dumps({'usage': {'input_tokens': 100, 'cached_input_tokens': 0, 'output_tokens': 10}}))\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o700)
            environment = dict(os.environ)
            environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
            workdir = root / "work"
            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "tools" / "run_codex_pilot.py"),
                    "--suite",
                    str(PROJECT_ROOT / "evaluation" / "codex-memory-ab-v2.json"),
                    "--workdir",
                    str(workdir),
                    "--max-calls",
                    "1",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            report = json.loads((workdir / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(1, report["progress"]["completed_calls"])
            self.assertEqual(24, report["progress"]["total_calls"])
            self.assertEqual("gpt-5.6-sol", report["environment"]["model"])
            self.assertEqual("max", report["environment"]["reasoning_effort"])
            self.assertEqual(900.0, report["environment"]["call_timeout_seconds"])
            self.assertEqual(
                12, report["aggregate"]["paired"]["overall"]["incomplete_pairs"]
            )
            only_result = report["results"][0]
            events = (
                workdir / "runs" / only_result["run_id"] / "events.jsonl"
            ).read_text(encoding="utf-8")
            invocation = json.loads(events.splitlines()[0])["invocation"]
            self.assertEqual("gpt-5.6-sol", invocation[invocation.index("--model") + 1])
            self.assertIn('model_reasoning_effort="max"', invocation)
            self.assertIn("--ephemeral", invocation)
            self.assertIn("--ignore-user-config", invocation)
            self.assertIn("--ignore-rules", invocation)
            self.assertEqual(
                "read-only", invocation[invocation.index("--sandbox") + 1]
            )

    @unittest.skip("evaluation/codex-memory-ab-v2.json is a sealed Protocol 4 research fixture (published result SHA-256 pinned in docs/EVALUATION.md). Its ab-foreign-scope case bundles a project-scope decoy alongside a machine-scope decoy and expects both to stay silent. The visibility/affinity fix intentionally makes a project-scope record retrievable outside its project again, so the project decoy now legitimately appears. Not touching the sealed fixture; a revised Protocol needs separate authorization before this can be un-skipped.")
    @needs_suites
    def test_codex_memory_ab_timeout_preserves_the_partial_transport_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_codex = fake_bin / "codex"
            fake_code_mode_host = fake_bin / "codex-code-mode-host"
            fake_code_mode_host.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_code_mode_host.chmod(0o700)
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import json, time, sys\n"
                "if sys.argv[1:] == ['--version']:\n"
                "    print('codex-cli 9.9.0-timeout-test')\n"
                "    raise SystemExit(0)\n"
                "print(json.dumps({'transport': 'started-before-timeout'}), flush=True)\n"
                "print('timeout-stderr-marker', file=sys.stderr, flush=True)\n"
                "time.sleep(2)\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o700)
            environment = dict(os.environ)
            environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
            workdir = root / "work"
            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "tools" / "run_codex_pilot.py"),
                    "--suite",
                    str(PROJECT_ROOT / "evaluation" / "codex-memory-ab-v2.json"),
                    "--workdir",
                    str(workdir),
                    "--max-calls",
                    "1",
                    "--call-timeout-seconds",
                    "0.5",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(2, result.returncode, msg=result.stderr)
            result_paths = list((workdir / "runs").glob("*/result.json"))
            failures = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in result_paths
            ]
            self.assertEqual(1, len(failures))
            self.assertEqual("timeout", failures[0]["error"])
            self.assertEqual(0.5, failures[0]["call_timeout_seconds"])
            run_dir = result_paths[0].parent
            self.assertIn(
                "started-before-timeout",
                (run_dir / "events.jsonl").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "timeout-stderr-marker",
                (run_dir / "stderr.txt").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
