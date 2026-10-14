#!/usr/bin/env python3
"""Prove whether reranking can meet retrieval gates without running a model.

This tool consumes an existing development-only raw-candidate cache.  It does
not embed text, call a reranker, query held-out cases, or mutate the memory
ledger.  Its purpose is to reject structurally impossible experiments before
they spend inference or evaluation budget.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from benchmark_hybrid import _expand_cases, _load_suite, _select_cases  # noqa: E402


DEFAULT_SUITE = PROJECT_ROOT / "evaluation" / "retrieval-suite-v2.json"
DEFAULT_WORKDIR = (
    PROJECT_ROOT
    / ".cache"
    / "evaluation"
    / "retrieval-v2-dev-e5-structured-smoke"
)
DEFAULT_SHORTLIST_SIZES = (3, 8, 16, 32, 64)
FINAL_HIT_AT_3_GATE = 0.85
CANDIDATE_RECALL_MARGIN_GATE = 0.95


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze a cached semantic candidate pool without inference."
    )
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _query_sha256(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _rank(candidates: list[dict[str, Any]], expected_id: str) -> int | None:
    for index, candidate in enumerate(candidates, start=1):
        if candidate["record_id"] == expected_id:
            return index
    return None


def _validate_result(
    result: dict[str, Any], suite: dict[str, Any], suite_digest: str
) -> None:
    if result.get("evaluation_phase") != "development":
        raise ValueError("candidate ceiling accepts development-only results")
    suite_result = result.get("suite")
    if not isinstance(suite_result, dict):
        raise ValueError("result has no suite manifest")
    if suite_result.get("suite_id") != suite.get("suite_id"):
        raise ValueError("result suite ID does not match the requested suite")
    if suite_result.get("suite_sha256") != suite_digest:
        raise ValueError("result suite digest does not match the frozen suite")
    selected_splits = suite_result.get("selected_splits")
    if selected_splits != {"development": 80, "held_out": 0}:
        raise ValueError("result does not prove a development-only selection")


def _load_raw_candidates(
    raw_dir: Path, cases: list[dict[str, Any]]
) -> tuple[dict[str, list[dict[str, Any]]], str, str]:
    expected_cases = [case for case in cases if case["category"] != "exact"]
    expected_names = {f"{case['case_id']}.json" for case in expected_cases}
    actual_paths = sorted(raw_dir.glob("*.json"))
    actual_names = {path.name for path in actual_paths}
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        raise ValueError(
            f"raw cache does not exactly match development cases; "
            f"missing={missing}, unexpected={unexpected}"
        )

    by_case: dict[str, list[dict[str, Any]]] = {}
    manifest = hashlib.sha256()
    index_fingerprint: str | None = None
    for case in expected_cases:
        path = raw_dir / f"{case['case_id']}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("case_id") != case["case_id"]:
            raise ValueError(f"invalid raw case identity: {path.name}")
        if payload.get("query_sha256") != _query_sha256(str(case["query"])):
            raise ValueError(f"raw query digest mismatch: {path.name}")
        fingerprint = payload.get("index_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError(f"raw case has no index fingerprint: {path.name}")
        if index_fingerprint is None:
            index_fingerprint = fingerprint
        elif index_fingerprint != fingerprint:
            raise ValueError("raw cases were produced from different candidate indexes")

        rows = payload.get("candidates")
        if not isinstance(rows, list) or len(rows) > 64:
            raise ValueError(f"raw candidates must be a list of at most 64: {path.name}")
        validated: list[dict[str, Any]] = []
        seen: set[str] = set()
        previous_score = math.inf
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"raw candidate is not an object: {path.name}")
            record_id = row.get("record_id")
            score = row.get("score")
            if not isinstance(record_id, str) or not isinstance(score, (int, float)):
                raise ValueError(f"raw candidate has invalid fields: {path.name}")
            score = float(score)
            if not math.isfinite(score) or score > previous_score:
                raise ValueError(f"raw candidates are not score-sorted: {path.name}")
            if record_id in seen:
                raise ValueError(f"raw candidate ID is duplicated: {path.name}")
            seen.add(record_id)
            previous_score = score
            validated.append({**row, "score": score})
        by_case[str(case["case_id"])] = validated
        manifest.update(path.name.encode("utf-8"))
        manifest.update(b"\0")
        manifest.update(_sha256(path).encode("ascii"))
        manifest.update(b"\n")
    return by_case, manifest.hexdigest(), index_fingerprint or ""


def compute_candidate_ceiling(
    cases: list[dict[str, Any]],
    raw_by_case: dict[str, list[dict[str, Any]]],
    *,
    shortlist_sizes: tuple[int, ...] = DEFAULT_SHORTLIST_SIZES,
) -> dict[str, Any]:
    """Return oracle ceilings; expected IDs never enter a scorer treatment."""

    if not shortlist_sizes or any(size <= 0 or size > 64 for size in shortlist_sizes):
        raise ValueError("shortlist sizes must be between 1 and 64")
    if tuple(sorted(set(shortlist_sizes))) != shortlist_sizes:
        raise ValueError("shortlist sizes must be unique and increasing")
    if not {32, 64}.issubset(shortlist_sizes):
        raise ValueError("shortlist sizes must include the 32 and 64 stop-rule bounds")

    categories: dict[str, Any] = {}
    for category in ("english_paraphrase", "cross_language"):
        target_cases = [case for case in cases if case["category"] == category]
        if not target_cases:
            raise ValueError(f"no {category} cases were provided")
        required = math.ceil(FINAL_HIT_AT_3_GATE * len(target_cases))
        ranks: dict[str, int | None] = {}
        for case in target_cases:
            expected_id = case.get("expected_id")
            if not isinstance(expected_id, str):
                raise ValueError(f"target case has no expected ID: {case['case_id']}")
            rows = raw_by_case.get(str(case["case_id"]))
            if rows is None:
                raise ValueError(f"raw candidates are missing: {case['case_id']}")
            ranks[str(case["case_id"])] = _rank(rows, expected_id)

        by_shortlist: dict[str, Any] = {}
        for size in shortlist_sizes:
            found = sum(rank is not None and rank <= size for rank in ranks.values())
            by_shortlist[str(size)] = {
                "targets_present": found,
                "cases": len(target_cases),
                "oracle_hit_at_3_ceiling": round(found / len(target_cases), 4),
                "can_meet_final_hit_at_3_gate": found >= required,
                "reranker_mistake_budget_at_gate": max(0, found - required),
                "hits_short_of_gate": max(0, required - found),
                "candidate_recall_margin_gate_met": (
                    found / len(target_cases) >= CANDIDATE_RECALL_MARGIN_GATE
                ),
                "missing_case_ids": sorted(
                    case_id
                    for case_id, rank in ranks.items()
                    if rank is None or rank > size
                ),
            }
        categories[category] = {
            "cases": len(target_cases),
            "final_hit_at_3_gate": FINAL_HIT_AT_3_GATE,
            "minimum_required_hits": required,
            "raw_target_ranks": ranks,
            "by_shortlist": by_shortlist,
        }

    k32 = [categories[name]["by_shortlist"]["32"] for name in categories]
    k64 = [categories[name]["by_shortlist"]["64"] for name in categories]
    hard_cases = sorted(
        {
            case_id
            for name in categories
            for case_id in categories[name]["by_shortlist"]["32"][
                "missing_case_ids"
            ]
        }
    )
    return {
        "categories": categories,
        "structural_findings": {
            "reranker_only_k32_rejected": not all(
                row["can_meet_final_hit_at_3_gate"] for row in k32
            ),
            "reranker_only_k64_theoretically_possible": all(
                row["can_meet_final_hit_at_3_gate"] for row in k64
            ),
            "reranker_only_k64_has_safety_margin": all(
                row["candidate_recall_margin_gate_met"] for row in k64
            ),
            "development_hard_cases_missing_at_k32": hard_cases,
            "conclusion": (
                "Do not spend reranker inference yet. Improve candidate generation "
                "on development until both language groups reach at least 95% "
                "candidate recall, then freeze the treatment before reranking."
            ),
        },
    }


def build_report(
    suite_path: Path, result_path: Path, raw_dir: Path
) -> dict[str, Any]:
    suite, suite_digest = _load_suite(suite_path)
    all_cases = _expand_cases(suite)
    development = _select_cases(all_cases, "development")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("result must be a JSON object")
    _validate_result(result, suite, suite_digest)
    raw_by_case, raw_manifest_digest, index_fingerprint = _load_raw_candidates(
        raw_dir, development
    )
    ceiling = compute_candidate_ceiling(development, raw_by_case)
    metrics = result["metrics"]
    return {
        "schema_version": 1,
        "analysis": "development_candidate_discrimination_ceiling",
        "source": {
            "suite_id": suite["suite_id"],
            "suite_sha256": suite_digest,
            "result_sha256": _sha256(result_path),
            "raw_manifest_sha256": raw_manifest_digest,
            "index_fingerprint": index_fingerprint,
            "raw_candidate_files": len(raw_by_case),
        },
        "execution": {
            "reused_cached_candidates": True,
            "new_embedding_queries": 0,
            "reranker_calls": 0,
            "remote_model_calls": 0,
            "gpt_calls": 0,
            "held_out_queried_cases": 0,
        },
        "baseline_end_to_end": {
            "english_paraphrase_hit_at_3": metrics["english_paraphrase"][
                "development"
            ]["hit_at_3"],
            "cross_language_hit_at_3": metrics["cross_language"]["development"][
                "hit_at_3"
            ],
            "no_answer_false_positive_rate": metrics["no_answer"]["development"][
                "false_positive_rate"
            ],
            "scope_intrusion_rate": metrics["scope_decoy"]["development"][
                "intrusion_rate"
            ],
        },
        "candidate_ceiling": ceiling,
        "next_comparison_contract": {
            "phase": "development-only",
            "first_treatment": "candidate-generation-or-query-decomposition",
            "candidate_recall_gate": CANDIDATE_RECALL_MARGIN_GATE,
            "final_hit_at_3_gate": FINAL_HIT_AT_3_GATE,
            "no_answer_false_positive_rate_max": 0.10,
            "scope_intrusion_rate_max": 0.0,
            "semantic_query_p95_ms_max": 3000.0,
            "treatment_input_allowed_fields": [
                "query",
                "opaque_candidate_key",
                "semantic_document_text",
                "upstream_score",
            ],
            "treatment_input_forbidden_fields": [
                "case_id",
                "category",
                "split",
                "expected_id",
                "record_id",
                "evidence_path",
            ],
            "stop_rules": [
                "Reject reranker-only K<=32 without inference.",
                "Do not open held-out while changing candidate generation.",
                "Do not run a reranker until both language candidate-recall gates pass.",
                "Do not enable production semantic retrieval from development evidence.",
            ],
        },
        "claim_boundary": (
            "This is an oracle upper-bound analysis of an already cached development "
            "candidate pool. It proves when reranking cannot succeed; it does not "
            "prove that any candidate-expansion or reranking treatment will succeed."
        ),
    }


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    args = _arguments()
    workdir = args.workdir.expanduser().resolve()
    result_path = (args.result or (workdir / "result.json")).expanduser().resolve()
    raw_dir = (args.raw_dir or (workdir / "raw")).expanduser().resolve()
    try:
        report = build_report(args.suite.expanduser().resolve(), result_path, raw_dir)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"analyze_candidate_ceiling: {exc}", file=sys.stderr)
        return 2
    if args.output:
        _write_json_atomic(args.output.expanduser().resolve(), report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
