#!/usr/bin/env python3
"""Run the fixed v3.1 lexical/hybrid retrieval evaluation.

The generated store, model index, and per-query raw candidates live in a resumable
work directory. They are derived state and never touch the canonical memory ledger.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_memory.core import (  # noqa: E402
    build_context,
    config_home,
    encode_record,
    initialize,
    reindex,
    semantic_documents,
    validate,
)
from agent_memory.semantic import (  # noqa: E402
    set_enabled,
    set_min_similarity,
    status as semantic_status,
)
from agent_memory.embedding import (  # noqa: E402
    DEFAULT_PROFILE_NAME,
    PROFILE_ENV,
    PROFILES,
    get_profile,
)
from agent_memory.semantic_document import (  # noqa: E402
    SEMANTIC_DOCUMENT_CONTRACT_SHA256,
    SEMANTIC_DOCUMENT_SCHEMA_VERSION,
)


FIXED_TIME = "2026-08-13T00:00:00+00:00"
DEFAULT_SUITE = PROJECT_ROOT / "evaluation" / "retrieval-suite-v1.json"
DEFAULT_WORKDIR = PROJECT_ROOT / ".cache" / "evaluation" / "retrieval-v1"
DEFAULT_MODEL_CACHE = PROJECT_ROOT / ".cache" / "semantic" / "model"
QUERY_CWD = Path("/tmp/current-benchmark-project")
MAX_QUERY_RSS_KIB = 500 * 1024
MAX_VECTOR_INDEX_BYTES = 40 * 1024 * 1024
MAX_QUERY_P95_MS = 3_000.0
MAX_LEXICAL_P95_MS = 250.0
EVALUATION_PHASES = {"development", "held_out", "full"}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--validate-suite", action="store_true")
    parser.add_argument(
        "--phase",
        choices=tuple(sorted(EVALUATION_PHASES)),
        default="full",
        help=(
            "development queries only the calibration split and can never enable "
            "semantic retrieval; full preserves the legacy development+held-out run"
        ),
    )
    parser.add_argument("--records", type=int, default=10_000)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument("--model-cache", type=Path, default=DEFAULT_MODEL_CACHE)
    parser.add_argument(
        "--semantic-profile",
        choices=tuple(sorted(PROFILES)),
        default=DEFAULT_PROFILE_NAME,
    )
    parser.add_argument(
        "--fixed-threshold",
        type=float,
        help="required for held_out; must be frozen from development before this run",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-pass", action="store_true")
    return parser.parse_args()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_suite(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.expanduser().resolve().read_bytes()
    suite = json.loads(raw)
    if not isinstance(suite, dict) or suite.get("schema_version") != 1:
        raise ValueError("suite must be a schema_version 1 JSON object")
    concepts = suite.get("concepts")
    no_answer = suite.get("no_answer_queries")
    decoys = suite.get("scope_decoys")
    if not isinstance(concepts, list) or len(concepts) != 40:
        raise ValueError("suite must contain exactly 40 concepts")
    required = {
        "key",
        "value",
        "exact_query",
        "paraphrase_query",
        "cross_language_query",
    }
    for index, concept in enumerate(concepts):
        if not isinstance(concept, dict) or required - concept.keys():
            raise ValueError(f"concept {index} is missing required fields")
        if any(not str(concept[field]).strip() for field in required):
            raise ValueError(f"concept {index} contains an empty field")
    if not isinstance(no_answer, list) or len(no_answer) != 20:
        raise ValueError("suite must contain exactly 20 no-answer queries")
    if not isinstance(decoys, dict):
        raise ValueError("suite scope_decoys must be an object")
    if int(decoys.get("machine_count", 0)) != 10 or int(
        decoys.get("project_count", 0)
    ) != 10:
        raise ValueError("suite must define 10 machine and 10 project decoys")
    expected = suite.get("case_contract", {})
    counts = _suite_counts(suite)
    if any(int(expected.get(key, -1)) != value for key, value in counts.items()):
        raise ValueError("suite case_contract does not match expanded case counts")
    return suite, _sha256_bytes(raw)


def _stable_half(keys: list[str], suite_id: str) -> set[int]:
    order = sorted(
        range(len(keys)),
        key=lambda index: hashlib.sha256(
            f"{suite_id}:{keys[index]}".encode("utf-8")
        ).hexdigest(),
    )
    return set(order[: len(order) // 2])


def _suite_counts(suite: dict[str, Any]) -> dict[str, int]:
    concepts = len(suite["concepts"])
    no_answer = len(suite["no_answer_queries"])
    decoys = int(suite["scope_decoys"]["machine_count"]) + int(
        suite["scope_decoys"]["project_count"]
    )
    return {
        "exact": concepts,
        "english_paraphrase": concepts,
        "cross_language": concepts,
        "no_answer": no_answer,
        "scope_decoy": decoys,
        "total": concepts * 3 + no_answer + decoys,
    }


def _case_counts(cases: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        category: sum(case["category"] == category for case in cases)
        for category in (
            "exact",
            "english_paraphrase",
            "cross_language",
            "no_answer",
            "scope_decoy",
        )
    }
    counts["total"] = len(cases)
    return counts


def _expand_cases(suite: dict[str, Any]) -> list[dict[str, Any]]:
    suite_id = str(suite["suite_id"])
    concepts = suite["concepts"]
    development_concepts = _stable_half(
        [str(item["key"]) for item in concepts], suite_id + ":concept"
    )
    no_answer = suite["no_answer_queries"]
    development_no_answer = _stable_half(
        [str(index) for index in range(len(no_answer))], suite_id + ":no-answer"
    )
    decoy_total = int(suite["scope_decoys"]["machine_count"]) + int(
        suite["scope_decoys"]["project_count"]
    )
    development_decoys = _stable_half(
        [str(index) for index in range(decoy_total)], suite_id + ":decoy"
    )
    cases: list[dict[str, Any]] = []
    for index, concept in enumerate(concepts):
        split = "development" if index in development_concepts else "held_out"
        expected_id = f"benchmark.semantic.{concept['key']}"
        for category, field in (
            ("exact", "exact_query"),
            ("english_paraphrase", "paraphrase_query"),
            ("cross_language", "cross_language_query"),
        ):
            cases.append(
                {
                    "case_id": f"{category}-{concept['key']}",
                    "category": category,
                    "split": split,
                    "query": str(concept[field]),
                    "expected_id": expected_id,
                }
            )
    for index, query in enumerate(no_answer):
        cases.append(
            {
                "case_id": f"no-answer-{index:02d}",
                "category": "no_answer",
                "split": (
                    "development" if index in development_no_answer else "held_out"
                ),
                "query": str(query),
                "expected_id": None,
            }
        )
    machine_count = int(suite["scope_decoys"]["machine_count"])
    template = str(suite["scope_decoys"]["query_template"])
    for index in range(decoy_total):
        subtype = "machine" if index < machine_count else "project"
        cases.append(
            {
                "case_id": f"scope-decoy-{subtype}-{index:02d}",
                "category": "scope_decoy",
                "subtype": subtype,
                "split": (
                    "development" if index in development_decoys else "held_out"
                ),
                "query": template.format(index=index),
                "expected_id": None,
                "forbidden_id": f"benchmark.decoy.{subtype}.{index:02d}",
            }
        )
    return cases


def _select_cases(
    cases: list[dict[str, Any]], phase: str
) -> list[dict[str, Any]]:
    if phase not in EVALUATION_PHASES:
        raise ValueError("evaluation phase must be development, held_out, or full")
    if phase == "development":
        return [case for case in cases if case["split"] == "development"]
    if phase == "held_out":
        return [case for case in cases if case["split"] == "held_out"]
    return list(cases)


def _validation_report(
    suite: dict[str, Any],
    digest: str,
    phase: str = "full",
    semantic_profile: str = DEFAULT_PROFILE_NAME,
) -> dict[str, Any]:
    cases = _expand_cases(suite)
    selected = _select_cases(cases, phase)
    return {
        "suite_id": suite["suite_id"],
        "suite_sha256": digest,
        "counts": _suite_counts(suite),
        "splits": {
            split: sum(case["split"] == split for case in cases)
            for split in ("development", "held_out")
        },
        "phase": phase,
        "semantic_profile": semantic_profile,
        "selected_counts": _case_counts(selected),
        "selected_splits": {
            split: sum(case["split"] == split for case in selected)
            for split in ("development", "held_out")
        },
    }


def _fact_body(record_id: str, value: str, evidence: str) -> str:
    return (
        f"# {record_id}\n\n"
        f"## Value\n\n{value}\n\n"
        f"## Evidence\n\n{evidence}\n"
    )


def _write_fact(
    root: Path,
    record_id: str,
    value: str,
    *,
    scope: str = "global",
    evidence: str = "Deterministic v3.1 retrieval evaluation fixture.",
) -> None:
    metadata = {
        "schema_version": 3,
        "id": record_id,
        "kind": "fact",
        "status": "active",
        "scope": scope,
        "source": "locally-verified",
        "priority": "normal",
        "tags": ["benchmark", "retrieval-v1"],
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
        "last_verified": "2026-08-13",
        "expires_at": None,
        "version": 1,
        "supersedes_event": None,
    }
    rendered = encode_record(metadata, _fact_body(record_id, value, evidence))
    path = root / "records" / "fact" / f"{record_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    event_id = "event-benchmark-" + record_id.replace(".", "-")
    event = {
        "schema_version": 3,
        "event_id": event_id,
        "action": "recorded",
        "record_id": record_id,
        "record_kind": "fact",
        "timestamp": FIXED_TIME,
        "previous_sha256": None,
        "new_sha256": _sha256_bytes(rendered.encode("utf-8")),
    }
    event_path = root / "events" / "2026" / "08" / f"{event_id}.json"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    event_path.write_text(
        json.dumps(event, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _prepare_store(
    workdir: Path,
    suite: dict[str, Any],
    suite_digest: str,
    record_count: int,
    phase: str,
    semantic_profile: str,
) -> tuple[Path, Path, list[dict[str, Any]]]:
    if record_count < 160:
        raise ValueError("--records must be at least 160")
    workdir.mkdir(parents=True, exist_ok=True)
    root = workdir / "memory"
    config = workdir / "config"
    marker_path = workdir / "workload.json"
    profile = get_profile(semantic_profile)
    marker = {
        "suite_id": suite["suite_id"],
        "suite_sha256": suite_digest,
        "records": record_count,
        "embedding_profile": profile.name,
        "embedding_contract_sha256": profile.contract_sha256,
        "semantic_document_contract_sha256": SEMANTIC_DOCUMENT_CONTRACT_SHA256,
        "model_id": profile.model_id,
        "model_revision": profile.revision,
        "model_sha256": profile.model_sha256,
    }
    if phase != "full":
        marker["evaluation_phase"] = phase
    if marker_path.is_file():
        if json.loads(marker_path.read_text(encoding="utf-8")) != marker:
            raise ValueError(
                "workdir belongs to a different suite/workload; choose a new --workdir"
            )
    else:
        _write_json_atomic(marker_path, marker)
    os.environ["AGENT_MEMORY_HOME"] = str(root)
    os.environ["AGENT_MEMORY_CONFIG_HOME"] = str(config)
    initialize(root, "benchmark-machine")
    cases = _expand_cases(suite)
    for concept in suite["concepts"]:
        _write_fact(
            root,
            f"benchmark.semantic.{concept['key']}",
            str(concept["value"]),
        )
    decoy_config = suite["scope_decoys"]
    machine_count = int(decoy_config["machine_count"])
    decoy_total = machine_count + int(decoy_config["project_count"])
    template = str(decoy_config["query_template"])
    for index in range(decoy_total):
        subtype = "machine" if index < machine_count else "project"
        scope = (
            "machine:foreign-benchmark"
            if subtype == "machine"
            else str(decoy_config["foreign_project_scope"])
        )
        _write_fact(
            root,
            f"benchmark.decoy.{subtype}.{index:02d}",
            template.format(index=index) + " belongs outside the current scope.",
            scope=scope,
        )
    _write_fact(
        root,
        "benchmark.wifi.evidence_path",
        "Laptop wireless resumed after the network driver was reloaded.",
        evidence="Verified from /backup/memory-hook-architecture/wifi-snapshot.json.",
    )
    reserved = len(suite["concepts"]) + decoy_total + 1
    for index in range(record_count - reserved):
        _write_fact(
            root,
            f"benchmark.filler.r{index:05d}",
            (
                f"Neutral synthetic observation {index:05d} stores archival marker "
                f"nebula{index:05d} and lattice{index:05d}."
            ),
        )
    machine_path = config / "machine.json"
    machine = json.loads(machine_path.read_text(encoding="utf-8"))
    machine["scope_paths"] = {
        str(decoy_config["foreign_project_scope"]): str(
            decoy_config["foreign_project_path"]
        )
    }
    _write_json_atomic(machine_path, machine)
    active, errors = validate(root)
    if errors:
        raise RuntimeError("generated store failed validation: " + "; ".join(errors[:3]))
    if active != record_count:
        raise RuntimeError(f"expected {record_count} records, validated {active}")
    return root, config, cases


def _link_model(root: Path, source: Path) -> None:
    source = source.expanduser().resolve()
    if not (source / "agent-memory-model.json").is_file():
        raise ValueError(
            "verified local model cache is missing; run `memoryctl semantic setup` first"
        )
    destination = root / ".cache" / "semantic" / "model"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if destination.resolve() != source:
            raise ValueError("workdir semantic model points to a different cache")
        return
    destination.symlink_to(source, target_is_directory=True)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return ordered[index]


def _source_ids(context: str) -> list[str]:
    marker = "## SOURCE IDS"
    if marker not in context:
        return []
    return [
        line[2:].strip()
        for line in context.split(marker, 1)[1].splitlines()
        if line.startswith("- ")
    ]


def _documents_digest(documents: list[dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for document in sorted(documents, key=lambda item: item["record_id"]):
        digest.update(document["record_id"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(document["content_hash"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _semantic_reindex(
    root: Path, config: Path, documents: list[dict[str, str]], workdir: Path
) -> dict[str, Any]:
    current = semantic_status(root, config, documents)
    metrics_path = workdir / "semantic-reindex.json"
    if current["index_status"] == "ready" and metrics_path.is_file():
        return json.loads(metrics_path.read_text(encoding="utf-8"))
    timing_path = workdir / "semantic-reindex-rss.txt"
    command = [
        "/usr/bin/time",
        "-f",
        "%e %M",
        "-o",
        str(timing_path),
        sys.executable,
        "-m",
        "agent_memory.cli",
        "--home",
        str(root),
        "semantic",
        "reindex",
    ]
    environment = os.environ.copy()
    environment["AGENT_MEMORY_CONFIG_HOME"] = str(config)
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("semantic reindex failed: " + result.stderr.strip())
    elapsed, rss = timing_path.read_text(encoding="utf-8").split()
    metrics = {
        "elapsed_ms": round(float(elapsed) * 1000, 3),
        "peak_rss_kib": int(rss),
        "stdout": result.stdout.strip(),
    }
    _write_json_atomic(metrics_path, metrics)
    return metrics


def _raw_candidate_query(
    root: Path,
    config: Path,
    case: dict[str, Any],
    raw_dir: Path,
    index_fingerprint: str,
) -> dict[str, Any]:
    path = raw_dir / f"{case['case_id']}.json"
    query_hash = _sha256_bytes(str(case["query"]).encode("utf-8"))
    if path.is_file():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if (
            cached.get("query_sha256") == query_hash
            and cached.get("index_fingerprint") == index_fingerprint
        ):
            return cached
    environment = os.environ.copy()
    environment.update(
        {
            "AGENT_MEMORY_HOME": str(root),
            "AGENT_MEMORY_CONFIG_HOME": str(config),
            "OMP_NUM_THREADS": "2",
            "OMP_THREAD_LIMIT": "2",
        }
    )
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "agent_memory.semantic_worker"],
            cwd=PROJECT_ROOT,
            env=environment,
            input=json.dumps({"query": case["query"], "limit": 64}),
            text=True,
            capture_output=True,
            check=False,
            timeout=3.2,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"semantic query timed out: {case['case_id']}") from exc
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    if result.returncode != 0:
        raise RuntimeError(
            f"semantic query failed for {case['case_id']}: {result.stderr.strip()}"
        )
    payload = json.loads(result.stdout)
    cached = {
        "case_id": case["case_id"],
        "query_sha256": query_hash,
        "index_fingerprint": index_fingerprint,
        "elapsed_ms": elapsed_ms,
        "candidates": payload.get("candidates", []),
    }
    _write_json_atomic(path, cached)
    return cached


def _rank(candidate_rows: list[dict[str, Any]], expected_id: str) -> int | None:
    for index, row in enumerate(candidate_rows, start=1):
        if row.get("record_id") == expected_id:
            return index
    return None


def _calibrate(
    cases: list[dict[str, Any]], raw: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    semantic = [
        case
        for case in cases
        if case["split"] == "development"
        and case["category"] in {"english_paraphrase", "cross_language"}
    ]
    no_answer = [
        case
        for case in cases
        if case["split"] == "development" and case["category"] == "no_answer"
    ]
    rows: list[dict[str, Any]] = []
    # Similarity distributions are model-specific (E5 commonly clusters around
    # 0.7-1.0), so calibration must cover the full cosine range rather than a
    # MiniLM-shaped 0.10-0.50 window.
    for integer in range(0, 101):
        threshold = integer / 100
        hits = 0
        for case in semantic:
            candidates = raw[case["case_id"]]["candidates"]
            rank = _rank(candidates, str(case["expected_id"]))
            expected = next(
                (row for row in candidates if row.get("record_id") == case["expected_id"]),
                None,
            )
            if (
                rank is not None
                and rank <= 3
                and expected is not None
                and float(expected["score"]) >= threshold
            ):
                hits += 1
        false_answers = sum(
            bool(raw[case["case_id"]]["candidates"])
            and float(raw[case["case_id"]]["candidates"][0]["score"]) >= threshold
            for case in no_answer
        )
        rows.append(
            {
                "threshold": round(threshold, 2),
                "semantic_hit_at_3": round(hits / len(semantic), 4),
                "no_answer_false_positive_rate": round(
                    false_answers / len(no_answer), 4
                ),
            }
        )
    eligible = [
        row
        for row in rows
        if row["semantic_hit_at_3"] >= 0.85
        and row["no_answer_false_positive_rate"] <= 0.10
    ]
    pool = eligible or rows
    selected = max(
        pool,
        key=lambda row: (
            row["semantic_hit_at_3"] - row["no_answer_false_positive_rate"],
            -row["no_answer_false_positive_rate"],
            row["threshold"],
        ),
    )
    return {
        "selected": selected,
        "development_gate_met": bool(eligible),
        "sweep": rows,
    }


def _cached_worker(
    workdir: Path, cases: list[dict[str, Any]], raw: dict[str, dict[str, Any]]
) -> Path:
    mapping = {
        str(case["query"]): raw[case["case_id"]]["candidates"]
        for case in cases
        if case["case_id"] in raw
    }
    mapping_path = workdir / "raw-candidates-map.json"
    _write_json_atomic(mapping_path, mapping)
    worker = workdir / "cached-semantic-worker"
    worker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "payload = json.load(sys.stdin)\n"
        "mapping = json.loads((pathlib.Path(__file__).parent / "
        "'raw-candidates-map.json').read_text(encoding='utf-8'))\n"
        "print(json.dumps({'candidates': mapping.get(str(payload['query']), [])}))\n",
        encoding="utf-8",
    )
    worker.chmod(0o700)
    return worker


def _measure_query_rss(root: Path, config: Path, query: str, workdir: Path) -> int:
    output = workdir / "query-rss.txt"
    environment = os.environ.copy()
    environment.update(
        {
            "AGENT_MEMORY_HOME": str(root),
            "AGENT_MEMORY_CONFIG_HOME": str(config),
            "OMP_NUM_THREADS": "2",
            "OMP_THREAD_LIMIT": "2",
        }
    )
    result = subprocess.run(
        [
            "/usr/bin/time",
            "-f",
            "%M",
            "-o",
            str(output),
            sys.executable,
            "-m",
            "agent_memory.semantic_worker",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        input=json.dumps({"query": query, "limit": 64}),
        text=True,
        capture_output=True,
        check=False,
        timeout=5.0,
    )
    if result.returncode != 0:
        raise RuntimeError("RSS query probe failed: " + result.stderr.strip())
    return int(output.read_text(encoding="utf-8").strip())


def _metric(rows: list[dict[str, Any]], category: str, split: str) -> dict[str, Any]:
    subset = [
        row
        for row in rows
        if row["category"] == category and row["split"] == split
    ]
    if category in {"exact", "english_paraphrase", "cross_language"}:
        return {
            "cases": len(subset),
            "hit_at_1": round(sum(row["rank"] == 1 for row in subset) / len(subset), 4),
            "hit_at_3": round(
                sum(row["rank"] is not None and row["rank"] <= 3 for row in subset)
                / len(subset),
                4,
            ),
            "failures": [row for row in subset if row["rank"] is None or row["rank"] > 3],
        }
    if category == "no_answer":
        return {
            "cases": len(subset),
            "false_positive_rate": round(
                sum(bool(row["top_ids"]) for row in subset) / len(subset), 4
            ),
            "failures": [row for row in subset if row["top_ids"]],
        }
    return {
        "cases": len(subset),
        "intrusion_rate": round(
            sum(not row["passed"] for row in subset) / len(subset), 4
        ),
        "failures": [row for row in subset if not row["passed"]],
    }


def _run(args: argparse.Namespace, suite: dict[str, Any], digest: str) -> dict[str, Any]:
    os.environ[PROFILE_ENV] = str(args.semantic_profile)
    profile = get_profile(str(args.semantic_profile))
    workdir = args.workdir.expanduser().resolve()
    root, config, cases = _prepare_store(
        workdir,
        suite,
        digest,
        int(args.records),
        args.phase,
        profile.name,
    )
    selected_cases = _select_cases(cases, args.phase)
    _link_model(root, args.model_cache)
    lexical_started = time.perf_counter()
    lexical_count = reindex(root)
    lexical_index_ms = round((time.perf_counter() - lexical_started) * 1000, 3)
    documents = semantic_documents(root)
    semantic_reindex = _semantic_reindex(root, config, documents, workdir)
    index_fingerprint = _documents_digest(documents)

    raw_cases = [case for case in selected_cases if case["category"] != "exact"]
    raw: dict[str, dict[str, Any]] = {}
    raw_dir = workdir / "raw"
    for position, case in enumerate(raw_cases, start=1):
        print(
            f"semantic query {position}/{len(raw_cases)}: {case['case_id']}",
            file=sys.stderr,
            flush=True,
        )
        raw[case["case_id"]] = _raw_candidate_query(
            root, config, case, raw_dir, index_fingerprint
        )
    if args.phase == "held_out":
        threshold = float(args.fixed_threshold)
        calibration = {
            "selected": {
                "threshold": threshold,
                "source": "frozen-development-result",
            },
            "development_gate_met": None,
            "sweep": [],
        }
    else:
        calibration = _calibrate(selected_cases, raw)
        threshold = float(calibration["selected"]["threshold"])
    set_min_similarity(
        config,
        threshold,
        calibration={
            "suite_id": suite["suite_id"],
            "suite_sha256": digest,
            "selected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "development": calibration["selected"],
        },
    )
    set_enabled(config, False)
    worker = _cached_worker(workdir, raw_cases, raw)
    previous_worker = os.environ.get("AGENT_MEMORY_SEMANTIC_WORKER")
    os.environ["AGENT_MEMORY_SEMANTIC_WORKER"] = str(worker)
    rows: list[dict[str, Any]] = []
    lexical_latencies: list[float] = []
    fusion_latencies: list[float] = []
    try:
        for case in selected_cases:
            mode = "lexical" if case["category"] == "exact" else "hybrid"
            started = time.perf_counter()
            context = build_context(
                root,
                QUERY_CWD,
                str(case["query"]),
                1800,
                False,
                None,
                mode,
            )
            elapsed = (time.perf_counter() - started) * 1000
            (lexical_latencies if mode == "lexical" else fusion_latencies).append(
                elapsed
            )
            ids = _source_ids(context)
            rank = None
            if case.get("expected_id"):
                try:
                    rank = ids.index(str(case["expected_id"])) + 1
                except ValueError:
                    pass
            row = {
                **case,
                "rank": rank,
                "top_ids": ids[:3],
                "elapsed_ms_without_model": round(elapsed, 3),
            }
            if case["category"] == "scope_decoy":
                row["passed"] = str(case["forbidden_id"]) not in ids
            rows.append(row)
    finally:
        if previous_worker is None:
            os.environ.pop("AGENT_MEMORY_SEMANTIC_WORKER", None)
        else:
            os.environ["AGENT_MEMORY_SEMANTIC_WORKER"] = previous_worker

    wifi_context = build_context(
        root,
        QUERY_CWD,
        "memory hook architecture",
        1200,
        False,
        None,
        "lexical",
    )
    wifi_false_positive_absent = (
        "benchmark.wifi.evidence_path" not in _source_ids(wifi_context)
    )
    query_durations = [float(item["elapsed_ms"]) for item in raw.values()]
    query_rss_kib = _measure_query_rss(
        root, config, str(raw_cases[0]["query"]), workdir
    )
    evaluated_splits = (
        ("development",)
        if args.phase == "development"
        else (("held_out",) if args.phase == "held_out" else ("development", "held_out"))
    )
    metrics = {
        category: {
            split: _metric(rows, category, split)
            for split in evaluated_splits
        }
        for category in (
            "exact",
            "english_paraphrase",
            "cross_language",
            "no_answer",
            "scope_decoy",
        )
    }
    quality_split = "development" if args.phase == "development" else "held_out"
    gates = {
        f"exact_{quality_split}_hit_at_1": (
            metrics["exact"][quality_split]["hit_at_1"] == 1.0
        ),
        f"english_paraphrase_{quality_split}_hit_at_3": (
            metrics["english_paraphrase"][quality_split]["hit_at_3"] >= 0.85
        ),
        f"cross_language_{quality_split}_hit_at_3": (
            metrics["cross_language"][quality_split]["hit_at_3"] >= 0.85
        ),
        f"no_answer_{quality_split}_false_positive_rate": (
            metrics["no_answer"][quality_split]["false_positive_rate"] <= 0.10
        ),
        "scope_intrusion_zero": all(
            metrics["scope_decoy"][split]["intrusion_rate"] == 0.0
            for split in evaluated_splits
        ),
        "wifi_evidence_path_false_positive_absent": wifi_false_positive_absent,
        "semantic_query_p95_within_timeout": (
            _percentile(query_durations, 0.95) <= MAX_QUERY_P95_MS
        ),
        "semantic_query_peak_rss_within_500_mib": query_rss_kib <= MAX_QUERY_RSS_KIB,
        "semantic_vector_index_within_40_mib": (
            (root / ".cache" / "semantic" / "index.sqlite3").stat().st_size
            <= MAX_VECTOR_INDEX_BYTES
        ),
        "lexical_query_p95_within_250_ms": (
            _percentile(lexical_latencies, 0.95) <= MAX_LEXICAL_P95_MS
        ),
    }
    phase_pass = all(gates.values())
    overall_pass = phase_pass if args.phase in {"held_out", "full"} else False
    set_enabled(config, overall_pass)
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "suite": _validation_report(
            suite, digest, args.phase, profile.name
        ),
        "evaluation_phase": args.phase,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "embedding_profile": profile.name,
            "embedding_contract_sha256": profile.contract_sha256,
            "semantic_document_schema_version": SEMANTIC_DOCUMENT_SCHEMA_VERSION,
            "semantic_document_contract_sha256": SEMANTIC_DOCUMENT_CONTRACT_SHA256,
            "model_id": profile.model_id,
            "model_revision": profile.revision,
            "model_sha256": profile.model_sha256,
            "model_dimension": profile.dimension,
        },
        "workload": {
            "active_records": len(documents),
            "lexical_indexed_records": lexical_count,
            "workdir": str(workdir),
        },
        "calibration": calibration,
        "metrics": metrics,
        "performance": {
            "lexical_index_ms": lexical_index_ms,
            "semantic_reindex": semantic_reindex,
            "lexical_query_p50_ms": round(statistics.median(lexical_latencies), 3),
            "lexical_query_p95_ms": round(_percentile(lexical_latencies, 0.95), 3),
            "semantic_worker_query_p50_ms": round(
                statistics.median(query_durations), 3
            ),
            "semantic_worker_query_p95_ms": round(
                _percentile(query_durations, 0.95), 3
            ),
            "fusion_render_p95_ms_without_model": round(
                _percentile(fusion_latencies, 0.95), 3
            ),
            "semantic_query_peak_rss_kib": query_rss_kib,
            "lexical_index_bytes": (
                root / ".cache" / "index.sqlite3"
            ).stat().st_size,
            "semantic_index_bytes": (
                root / ".cache" / "semantic" / "index.sqlite3"
            ).stat().st_size,
        },
        "wifi_probe": {
            "query": "memory hook architecture",
            "forbidden_id": "benchmark.wifi.evidence_path",
            "passed": wifi_false_positive_absent,
        },
        "gates": gates,
        "phase_pass": phase_pass,
        "overall_pass": overall_pass,
        "activation": (
            "disabled because development-only evaluation cannot activate production"
            if args.phase == "development"
            else (
                "enabled in the isolated evaluation config"
                if overall_pass
                else "disabled because at least one held-out gate failed"
            )
        ),
        "claim_boundary": (
            "This deterministic suite measures candidate retrieval, abstention, scope "
            "filtering, latency, and memory. It does not by itself prove improved LLM "
            "behavior; the separate blinded Codex pilot addresses that question."
        ),
    }
    return result


def main() -> int:
    args = _arguments()
    try:
        if (
            args.phase == "held_out"
            and not args.validate_suite
            and args.fixed_threshold is None
        ):
            raise ValueError(
                "--phase held_out requires --fixed-threshold from the frozen development result"
            )
        if args.fixed_threshold is not None and not (
            0.0 <= float(args.fixed_threshold) <= 1.0
        ):
            raise ValueError("--fixed-threshold must be between 0 and 1")
        suite, digest = _load_suite(args.suite)
        if args.validate_suite:
            print(
                json.dumps(
                    _validation_report(
                        suite, digest, args.phase, args.semantic_profile
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
            )
            return 0
        result = _run(args, suite, digest)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"benchmark_hybrid: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    output = args.output or (args.workdir / "result.json")
    _write_json_atomic(output.expanduser().resolve(), result)
    print(rendered, end="")
    if args.require_pass and not result["phase_pass"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
