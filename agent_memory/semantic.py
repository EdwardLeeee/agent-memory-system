from __future__ import annotations

import base64
import importlib.util
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .embedding import (
    DEFAULT_PROFILE_NAME,
    LocalOnnxEncoder,
    active_profile,
    get_profile,
)
from .semantic_document import (
    SEMANTIC_DOCUMENT_CONTRACT_SHA256,
    SEMANTIC_DOCUMENT_SCHEMA_VERSION,
)


_DEFAULT_PROFILE = get_profile(DEFAULT_PROFILE_NAME)
MODEL_ID = _DEFAULT_PROFILE.model_id
MODEL_REVISION = _DEFAULT_PROFILE.revision
MODEL_FILE = _DEFAULT_PROFILE.model_file
MODEL_SHA256 = _DEFAULT_PROFILE.model_sha256
MODEL_BYTES = _DEFAULT_PROFILE.model_bytes
MODEL_DIMENSION = _DEFAULT_PROFILE.dimension
SEMANTIC_INDEX_SCHEMA_VERSION = 3
ACTIVATION_SCHEMA_VERSION = 1
SEMANTIC_QUERY_TIMEOUT_SECONDS = 3.0
SEMANTIC_MAINTENANCE_TIMEOUT_SECONDS = 15.0
DEFAULT_MIN_SIMILARITY = 0.25
MODEL_MANIFEST_NAME = "agent-memory-model.json"
MODEL_PROMOTION_JOURNAL = "model-promotion.json"


def _profile():
    try:
        return active_profile()
    except ValueError as exc:
        raise SemanticUnavailable(str(exc), code="invalid-profile") from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_entries(model_root: Path) -> list[dict[str, Any]]:
    """Every file the encoder depends on, not just the ONNX graph.

    Tokenizer, config, and special-token files change what the model computes
    just as surely as the weights do, so integrity has to cover all of them.
    """
    entries = []
    for path in sorted(model_root.rglob("*")):
        if not path.is_file() or path.name == MODEL_MANIFEST_NAME:
            continue
        entries.append(
            {
                "path": path.relative_to(model_root).as_posix(),
                "size": path.stat().st_size,
                "sha256": _file_sha256(path),
            }
        )
    return entries


def _artifacts_digest(entries: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _manifest_artifacts(manifest: dict[str, Any]) -> list[dict[str, Any]] | None:
    entries = manifest.get("artifacts")
    if not isinstance(entries, list) or not entries:
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        if not isinstance(entry.get("path"), str) or not entry["path"]:
            return None
        if isinstance(entry.get("size"), bool) or not isinstance(entry.get("size"), int):
            return None
        if not isinstance(entry.get("sha256"), str) or not _HEX64_RE.match(
            entry["sha256"]
        ):
            return None
    if manifest.get("artifacts_sha256") != _artifacts_digest(entries):
        return None
    return entries


def fast_model_check(root: Path, manifest: dict[str, Any]) -> str | None:
    """Cheap tamper detection: presence and size for every declared artifact.

    Hooks cannot afford to rehash a model on the prompt path, but they can
    notice that a file vanished or changed length.
    """
    entries = _manifest_artifacts(manifest)
    if entries is None:
        return "manifest-artifacts-invalid"
    model_root = _model_root(root)
    for entry in entries:
        path = model_root / entry["path"]
        try:
            if not path.is_file() or path.stat().st_size != entry["size"]:
                return "model-artifact-changed"
        except OSError:
            return "model-artifact-unreadable"
    return None


def verify_model_artifacts(root: Path) -> str | None:
    """Full verification: recompute every declared artifact's SHA-256."""
    manifest_path = _model_manifest(root)
    if not manifest_path.is_file():
        return "model-manifest-missing"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "model-manifest-unreadable"
    if not isinstance(manifest, dict):
        return "model-manifest-invalid"
    profile = _profile()
    if (
        manifest.get("name") != profile.name
        or manifest.get("model_id") != profile.model_id
        or manifest.get("revision") != profile.revision
        or manifest.get("sha256") != profile.model_sha256
        or manifest.get("contract_sha256") != profile.contract_sha256
    ):
        return "model-manifest-profile-mismatch"
    entries = _manifest_artifacts(manifest)
    if entries is None:
        return "manifest-artifacts-invalid"
    model_root = _model_root(root)
    declared = {entry["path"] for entry in entries}
    present = {
        path.relative_to(model_root).as_posix()
        for path in model_root.rglob("*")
        if path.is_file() and path.name != MODEL_MANIFEST_NAME
    }
    if declared != present:
        return "model-artifact-set-mismatch"
    for entry in entries:
        path = model_root / entry["path"]
        try:
            if path.stat().st_size != entry["size"]:
                return "model-artifact-changed"
            if _file_sha256(path) != entry["sha256"]:
                return "model-artifact-corrupt"
        except OSError:
            return "model-artifact-unreadable"
    if profile.model_file not in declared:
        return "model-artifact-set-mismatch"
    return None


def _model_allow_patterns() -> list[str]:
    return ["*.json", "*.txt", "*.model", "1_Pooling/**", _profile().model_file]


_HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class SemanticCandidate:
    """One vector hit that has passed the full provenance contract."""

    record_id: str
    score: float
    content_hash: str


@dataclass(frozen=True)
class SemanticQueryOutcome:
    """Why a semantic query produced what it produced.

    `success` and `no_match` both mean the backend ran and its answer can be
    trusted. Every other status means the backend did not answer, so callers
    must fall back to unmodified lexical behaviour rather than treating the
    empty candidate list as evidence of absence.
    """

    status: str
    candidates: tuple[SemanticCandidate, ...] = ()
    code: str | None = None

    @property
    def answered(self) -> bool:
        return self.status in ("success", "no_match")


SEMANTIC_QUERY_STATUSES = (
    "success",
    "no_match",
    "unavailable",
    "failed",
    "circuit_open",
)


class SemanticUnavailable(RuntimeError):
    """The optional local semantic backend is not ready for explicit use."""

    def __init__(self, message: str, *, code: str = "semantic-unavailable") -> None:
        super().__init__(message)
        self.code = code


def _semantic_root(root: Path) -> Path:
    return root / ".cache" / "semantic"


def _model_root(root: Path) -> Path:
    return _semantic_root(root) / "model"


def _model_manifest(root: Path) -> Path:
    return _model_root(root) / MODEL_MANIFEST_NAME


def _index_path(root: Path) -> Path:
    return _semantic_root(root) / "index.sqlite3"


def _dirty_path(root: Path) -> Path:
    return _semantic_root(root) / "index.dirty"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _documents_fingerprint(documents: list[dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for document in sorted(documents, key=lambda item: item["record_id"]):
        digest.update(document["record_id"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(document["content_hash"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


ACTIVATION_FIELDS = (
    "schema_version",
    "state",
    "profile",
    "model_sha256",
    "embedding_contract_sha256",
    "semantic_document_contract_sha256",
    "dimension",
    "index_source_fingerprint",
    "min_similarity",
    "fusion_weights",
    "suite_digest",
    "split",
    "metrics",
    "gates",
    "reviewed_at",
    "reviewed_by",
)


def _activation_path(config_root: Path) -> Path:
    return config_root / "semantic-activation.json"


def _activation_digest(artifact: dict[str, Any]) -> str:
    body = {key: artifact.get(key) for key in ACTIVATION_FIELDS}
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _gates_passed(metrics: Any, gates: Any) -> bool:
    """Every declared gate must have a metric that meets it."""
    if not isinstance(metrics, dict) or not isinstance(gates, dict) or not gates:
        return False
    for name, requirement in gates.items():
        if not isinstance(requirement, dict):
            return False
        value = metrics.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        value = float(value)
        if not math.isfinite(value):
            return False
        minimum = requirement.get("min")
        maximum = requirement.get("max")
        if minimum is not None and value < float(minimum):
            return False
        if maximum is not None and value > float(maximum):
            return False
        if minimum is None and maximum is None:
            return False
    return True


def _matching_activation(root: Path, artifact: dict[str, Any]) -> dict[str, Any] | None:
    """The artifact itself when it still describes this store, otherwise None."""
    profile = _profile()
    expected = {
        "profile": profile.name,
        "model_sha256": profile.model_sha256,
        "embedding_contract_sha256": profile.contract_sha256,
        "semantic_document_contract_sha256": SEMANTIC_DOCUMENT_CONTRACT_SHA256,
        "dimension": str(profile.dimension),
        "index_source_fingerprint": query_fingerprint(root)["source_fingerprint"],
    }
    if any(artifact.get(key) != value for key, value in expected.items()):
        return None
    return artifact


def load_activation(config_root: Path) -> tuple[dict[str, Any] | None, str]:
    """Return the reviewed activation artifact and why it is or is not usable."""
    path = _activation_path(config_root)
    if not path.is_file():
        return None, "no-activation-artifact"
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "activation-unreadable"
    if not isinstance(artifact, dict):
        return None, "activation-not-object"
    if artifact.get("schema_version") != ACTIVATION_SCHEMA_VERSION:
        return None, "activation-schema-mismatch"
    if artifact.get("artifact_sha256") != _activation_digest(artifact):
        return None, "activation-digest-mismatch"
    if artifact.get("state") != "approved":
        return None, "activation-not-approved"
    if not _gates_passed(artifact.get("metrics"), artifact.get("gates")):
        return None, "activation-gates-not-met"
    return artifact, "approved"


def build_activation_artifact(
    root: Path,
    *,
    min_similarity: float,
    fusion_weights: dict[str, float],
    suite_digest: str,
    split: str,
    metrics: dict[str, float],
    gates: dict[str, dict[str, float]],
    reviewed_at: str,
    reviewed_by: str,
) -> dict[str, Any]:
    """Build an artifact, refusing to mint one whose metrics miss their gates.

    Deliberately not exposed as a CLI command: an artifact must come from an
    evaluation run that actually passed, never from an operator asserting it.
    """
    if not _gates_passed(metrics, gates):
        raise SemanticUnavailable(
            "refusing to build an activation artifact whose metrics miss their gates",
            code="activation-gates-not-met",
        )
    if not math.isfinite(min_similarity) or not 0.0 <= min_similarity <= 1.0:
        raise SemanticUnavailable(
            "activation threshold must be finite and within 0..1",
            code="activation-threshold-invalid",
        )
    profile = _profile()
    artifact = {
        "schema_version": ACTIVATION_SCHEMA_VERSION,
        "state": "approved",
        "profile": profile.name,
        "model_sha256": profile.model_sha256,
        "embedding_contract_sha256": profile.contract_sha256,
        "semantic_document_contract_sha256": SEMANTIC_DOCUMENT_CONTRACT_SHA256,
        "dimension": str(profile.dimension),
        "index_source_fingerprint": query_fingerprint(root)["source_fingerprint"],
        "min_similarity": float(min_similarity),
        "fusion_weights": dict(fusion_weights),
        "suite_digest": suite_digest,
        "split": split,
        "metrics": dict(metrics),
        "gates": dict(gates),
        "reviewed_at": reviewed_at,
        "reviewed_by": reviewed_by,
    }
    artifact["artifact_sha256"] = _activation_digest(artifact)
    return artifact


def write_activation_artifact(config_root: Path, artifact: dict[str, Any]) -> Path:
    path = _activation_path(config_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _config_path(config_root: Path) -> Path:
    return config_root / "semantic.json"


def _runtime_path(config_root: Path) -> Path:
    return config_root / "runtime" / "semantic.json"


def _load_config(config_root: Path) -> dict[str, Any]:
    """Parse semantic config strictly.

    A malformed config must never reach ranking code as a silently coerced
    value, and must never escape as an untyped exception. Every rejection
    carries a stable code so hooks can stay silent while explicit commands
    report something actionable.
    """
    path = _config_path(config_root)
    if not path.is_file():
        return {"enabled": False, "min_similarity": DEFAULT_MIN_SIMILARITY}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SemanticUnavailable(
            f"cannot read semantic config: {path}", code="config-unreadable"
        ) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SemanticUnavailable(
            f"semantic config is not valid JSON: {path}", code="config-invalid-json"
        ) from exc
    if not isinstance(value, dict):
        raise SemanticUnavailable(
            "semantic config must be a JSON object", code="config-not-object"
        )
    config = dict(value)
    config.setdefault("enabled", False)
    config.setdefault("min_similarity", DEFAULT_MIN_SIMILARITY)
    if not isinstance(config["enabled"], bool):
        raise SemanticUnavailable(
            "semantic config field `enabled` must be true or false",
            code="config-enabled-not-bool",
        )
    threshold = config["min_similarity"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise SemanticUnavailable(
            "semantic config field `min_similarity` must be a number",
            code="config-threshold-not-number",
        )
    threshold = float(threshold)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise SemanticUnavailable(
            "semantic config field `min_similarity` must be finite and within 0..1",
            code="config-threshold-out-of-range",
        )
    config["min_similarity"] = threshold
    return config


def _load_runtime(config_root: Path) -> dict[str, Any]:
    path = _runtime_path(config_root)
    if not path.is_file():
        return {"consecutive_failures": 0}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"consecutive_failures": 0}
    return value if isinstance(value, dict) else {"consecutive_failures": 0}


def _write_runtime(config_root: Path, value: dict[str, Any]) -> None:
    path = _runtime_path(config_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="semantic-runtime-", suffix=".json", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _circuit_is_open(runtime: dict[str, Any]) -> bool:
    value = runtime.get("circuit_open_until")
    if not value:
        return False
    try:
        return datetime.fromisoformat(str(value)) > datetime.now(timezone.utc)
    except ValueError:
        return False


def _record_query_result(config_root: Path, error_code: str | None) -> None:
    runtime = _load_runtime(config_root)
    runtime["checked_at"] = _utc_now()
    if error_code is None:
        runtime.update(
            {
                "consecutive_failures": 0,
                "last_success_at": runtime["checked_at"],
            }
        )
        runtime.pop("last_error_code", None)
        runtime.pop("circuit_open_until", None)
    else:
        failures = int(runtime.get("consecutive_failures", 0)) + 1
        runtime["consecutive_failures"] = failures
        runtime["last_error_code"] = error_code
        if failures >= 3:
            runtime["circuit_open_until"] = (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).replace(microsecond=0).isoformat()
    _write_runtime(config_root, runtime)


def _write_config(config_root: Path, value: dict[str, Any]) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    config_root.chmod(0o700)
    path = _config_path(config_root)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="semantic-", suffix=".json", dir=config_root
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def set_enabled(config_root: Path, enabled: bool) -> None:
    value = _load_config(config_root)
    value["enabled"] = enabled
    _write_config(config_root, value)


def set_min_similarity(
    config_root: Path,
    value: float,
    *,
    calibration: dict[str, Any] | None = None,
) -> None:
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise SemanticUnavailable("semantic similarity threshold must be between 0 and 1")
    config = _load_config(config_root)
    config["min_similarity"] = round(float(value), 4)
    if calibration is not None:
        config["calibration"] = calibration
    _write_config(config_root, config)


def status(
    root: Path,
    config_root: Path,
    documents: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    profile = _profile()
    manifest_path = _model_manifest(root)
    model_path = _model_root(root) / profile.model_file
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
        except (OSError, json.JSONDecodeError):
            manifest = {}
    model_installed = (
        model_path.is_file()
        and model_path.stat().st_size == profile.model_bytes
        and manifest.get("name") == profile.name
        and manifest.get("model_id") == profile.model_id
        and manifest.get("revision") == profile.revision
        and manifest.get("sha256") == profile.model_sha256
        and manifest.get("contract_sha256") == profile.contract_sha256
    )
    model_code: str | None = None if model_installed else "model-not-installed"
    if model_installed:
        model_code = fast_model_check(root, manifest)
        if model_code is not None:
            model_installed = False
    config = _load_config(config_root)
    runtime = _load_runtime(config_root)
    index_path = _index_path(root)
    index_status = "missing"
    indexed_records = 0
    index_generated_at: str | None = None
    if index_path.is_file():
        try:
            connection = sqlite3.connect(index_path)
            meta = dict(connection.execute("SELECT key, value FROM meta"))
            indexed_records = int(
                connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
            )
            connection.close()
            index_generated_at = meta.get("generated_at")
            index_status = "present"
            if (
                meta.get("index_schema_version")
                != str(SEMANTIC_INDEX_SCHEMA_VERSION)
                or meta.get("model_sha256") != profile.model_sha256
                or meta.get("embedding_contract_sha256")
                != profile.contract_sha256
                or meta.get("semantic_document_contract_sha256")
                != SEMANTIC_DOCUMENT_CONTRACT_SHA256
                or meta.get("dimension") != str(profile.dimension)
            ):
                index_status = "invalid"
            elif documents is not None and (
                meta.get("source_fingerprint")
                != _documents_fingerprint(documents)
                or indexed_records != len(documents)
            ):
                index_status = "stale"
            elif documents is not None:
                index_status = "ready"
        except (OSError, sqlite3.Error, TypeError, ValueError):
            index_status = "invalid"
    if _dirty_path(root).exists() and index_status in {"present", "ready"}:
        index_status = "stale"

    artifact, activation_code = load_activation(config_root)
    enabled = bool(config.get("enabled", False))
    threshold = float(config.get("min_similarity", DEFAULT_MIN_SIMILARITY))
    threshold_calibrated = False
    if artifact is None:
        activation_state = "evaluation-required"
    elif artifact != _matching_activation(root, artifact):
        activation_state = "stale"
        activation_code = "activation-fingerprint-mismatch"
    else:
        threshold = float(artifact["min_similarity"])
        threshold_calibrated = True
        activation_state = "approved" if enabled else "disabled"
        activation_code = None if enabled else "activation-approved-but-disabled"
    return {
        "backend": "local-onnx",
        "profile": profile.name,
        "model_id": profile.model_id,
        "revision": profile.revision,
        "model_file": profile.model_file,
        "model_sha256": profile.model_sha256,
        "dimension": profile.dimension,
        "embedding_contract_sha256": profile.contract_sha256,
        "semantic_document_schema_version": SEMANTIC_DOCUMENT_SCHEMA_VERSION,
        "semantic_document_contract_sha256": SEMANTIC_DOCUMENT_CONTRACT_SHA256,
        "query_prefix": profile.query_prefix,
        "passage_prefix": profile.passage_prefix,
        "dependency_available": all(
            importlib.util.find_spec(module) is not None
            for module in ("numpy", "onnxruntime", "tokenizers")
        ),
        "enabled": enabled,
        "activation_state": activation_state,
        "activation_code": activation_code,
        "min_similarity": threshold,
        "threshold_calibrated": threshold_calibrated,
        "calibration": config.get("calibration"),
        "model_installed": model_installed,
        "model_code": model_code,
        "index_status": index_status,
        "index_path": str(index_path),
        "indexed_records": indexed_records,
        "expected_records": len(documents) if documents is not None else None,
        "vector_coverage": (
            round(indexed_records / len(documents), 4)
            if documents
            else (1.0 if documents == [] and indexed_records == 0 else 0.0)
        ),
        "index_generated_at": index_generated_at,
        "last_error_code": runtime.get("last_error_code"),
        "last_success_at": runtime.get("last_success_at"),
        "consecutive_failures": int(runtime.get("consecutive_failures", 0)),
        "circuit_open": _circuit_is_open(runtime),
        "circuit_open_until": runtime.get("circuit_open_until"),
    }


def _recover_promotion(semantic_root: Path, model_root: Path) -> None:
    """Finish or undo a promotion that a previous run did not complete."""
    journal = semantic_root / MODEL_PROMOTION_JOURNAL
    if not journal.is_file():
        return
    try:
        state = json.loads(journal.read_text(encoding="utf-8"))
        backup = semantic_root / str(state["backup"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        journal.unlink(missing_ok=True)
        return
    if backup.is_dir():
        if not model_root.exists():
            os.replace(backup, model_root)
        else:
            shutil.rmtree(backup, ignore_errors=True)
    journal.unlink(missing_ok=True)


def _promote_model(
    semantic_root: Path, staging: Path, model_root: Path, validate: "Any"
) -> None:
    """Swap in a new model, keeping the previous one until the new one holds.

    Without this the old model is moved aside first and simply lost if the
    replace or the post-promotion check fails, leaving no working encoder.
    """
    backup = semantic_root / (
        "model-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    journal = semantic_root / MODEL_PROMOTION_JOURNAL
    journal.write_text(
        json.dumps(
            {"backup": backup.name, "started_at": _utc_now()},
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    journal.chmod(0o600)
    had_previous = model_root.exists()

    def restore() -> None:
        if had_previous and backup.is_dir():
            if model_root.exists():
                shutil.rmtree(model_root, ignore_errors=True)
            os.replace(backup, model_root)

    try:
        if had_previous:
            os.replace(model_root, backup)
        os.replace(staging, model_root)
    except OSError as exc:
        restore()
        journal.unlink(missing_ok=True)
        raise SemanticUnavailable(
            "semantic model promotion failed; the previous model was restored",
            code="model-promotion-failed",
        ) from exc
    code = validate()
    if code is not None:
        restore()
        journal.unlink(missing_ok=True)
        raise SemanticUnavailable(
            f"promoted semantic model failed verification: {code}; "
            "the previous model was restored",
            code=code,
        )
    # Only now is the previous model expendable. A cleanup failure must not
    # invalidate a promotion that already succeeded.
    if had_previous and backup.is_dir():
        shutil.rmtree(backup, ignore_errors=True)
    journal.unlink(missing_ok=True)


def setup(root: Path, config_root: Path) -> dict[str, Any]:
    profile = _profile()
    current = status(root, config_root)
    if current["model_installed"]:
        return current
    if any(
        importlib.util.find_spec(module) is None
        for module in ("huggingface_hub", "numpy", "onnxruntime", "tokenizers")
    ):
        raise SemanticUnavailable(
            "install the semantic extra first: `python3 -m pip install '.[semantic]'`"
        )
    from huggingface_hub import snapshot_download

    semantic_root = _semantic_root(root)
    semantic_root.mkdir(parents=True, exist_ok=True)
    semantic_root.chmod(0o700)
    _recover_promotion(semantic_root, _model_root(root))
    staging = Path(tempfile.mkdtemp(prefix="model-staging-", dir=semantic_root))
    try:
        snapshot_download(
            repo_id=profile.model_id,
            revision=profile.revision,
            local_dir=staging,
            allow_patterns=_model_allow_patterns(),
            max_workers=2,
        )
        model_path = staging / profile.model_file
        if not model_path.is_file() or model_path.stat().st_size != profile.model_bytes:
            raise SemanticUnavailable("downloaded semantic model has an unexpected size")
        digest = hashlib.sha256()
        with model_path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != profile.model_sha256:
            raise SemanticUnavailable("downloaded semantic model failed SHA-256 verification")
        # Hash every encoder dependency, not just the graph: a tampered
        # tokenizer or config changes what the model computes.
        entries = _artifact_entries(staging)
        manifest = {
            **profile.manifest(),
            "artifacts": entries,
            "artifacts_sha256": _artifacts_digest(entries),
            "verified_at": _utc_now(),
        }
        manifest_path = staging / MODEL_MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        _promote_model(
            semantic_root,
            staging,
            _model_root(root),
            lambda: verify_model_artifacts(root),
        )
        return status(root, config_root)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _embedding_model(root: Path):
    return LocalOnnxEncoder(_model_root(root), _profile(), threads=2)


def reindex(
    root: Path,
    config_root: Path,
    documents: list[dict[str, str]],
) -> int:
    profile = _profile()
    current = status(root, config_root, documents)
    if not current["model_installed"]:
        raise SemanticUnavailable("run `memoryctl semantic setup` before reindexing")
    import numpy as np

    semantic_root = _semantic_root(root)
    semantic_root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="index-", suffix=".sqlite3", dir=semantic_root
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary)
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE vectors (
                record_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                vector BLOB NOT NULL
            );
            """
        )
        model = _embedding_model(root) if documents else None
        batch_size = 4
        for start in range(0, len(documents), batch_size):
            batch = documents[start : start + batch_size]
            embeddings = list(
                model.embed(
                    [document["text"] for document in batch],
                    role="passage",
                    batch_size=batch_size,
                )
            )
            for document, embedding in zip(batch, embeddings):
                vector = np.asarray(embedding, dtype=np.float32)
                if vector.shape != (profile.dimension,):
                    raise SemanticUnavailable("semantic model returned an invalid vector")
                norm = float(np.linalg.norm(vector))
                if norm <= 0:
                    raise SemanticUnavailable("semantic model returned an empty vector")
                vector /= norm
                connection.execute(
                    "INSERT INTO vectors(record_id, content_hash, vector) VALUES (?, ?, ?)",
                    (
                        document["record_id"],
                        document["content_hash"],
                        vector.tobytes(),
                    ),
                )
        meta = {
            "index_schema_version": str(SEMANTIC_INDEX_SCHEMA_VERSION),
            "embedding_profile": profile.name,
            "embedding_contract_sha256": profile.contract_sha256,
            "semantic_document_contract_sha256": SEMANTIC_DOCUMENT_CONTRACT_SHA256,
            "model_id": profile.model_id,
            "model_revision": profile.revision,
            "model_sha256": profile.model_sha256,
            "dimension": str(profile.dimension),
            "record_count": str(len(documents)),
            "source_fingerprint": _documents_fingerprint(documents),
            "generated_at": _utc_now(),
        }
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)", sorted(meta.items())
        )
        connection.commit()
        connection.close()
        connection = None
        temporary.chmod(0o600)
        os.replace(temporary, _index_path(root))
        _dirty_path(root).unlink(missing_ok=True)
        return len(documents)
    except (OSError, sqlite3.Error) as exc:
        raise SemanticUnavailable(f"cannot build semantic index: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)


def mark_stale(root: Path) -> None:
    if not _index_path(root).is_file():
        return
    path = _dirty_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="semantic-dirty-", suffix=".txt", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_utc_now() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def embed_text(root: Path, text: str) -> bytes:
    import numpy as np

    profile = _profile()
    model = _embedding_model(root)
    embedding = next(iter(model.embed([text], role="passage", batch_size=1)))
    vector = np.asarray(embedding, dtype=np.float32)
    if vector.shape != (profile.dimension,):
        raise SemanticUnavailable("semantic model returned an invalid vector")
    norm = float(np.linalg.norm(vector))
    if norm <= 0:
        raise SemanticUnavailable("semantic model returned an empty vector")
    vector /= norm
    return vector.tobytes()


def incremental_update(
    root: Path,
    config_root: Path,
    document: dict[str, str],
    documents: list[dict[str, str]],
) -> bool:
    profile = _profile()
    if _dirty_path(root).exists() or not _index_path(root).is_file():
        return False
    override = os.environ.get("AGENT_MEMORY_SEMANTIC_MAINTENANCE_WORKER")
    command = (
        [override]
        if override
        else [os.fspath(Path(os.sys.executable)), "-m", "agent_memory.semantic_worker"]
    )
    environment = os.environ.copy()
    environment.update(
        {
            "AGENT_MEMORY_HOME": str(root),
            "AGENT_MEMORY_CONFIG_HOME": str(config_root),
            "OMP_NUM_THREADS": "2",
            "OMP_THREAD_LIMIT": "2",
        }
    )
    try:
        result = subprocess.run(
            command,
            input=json.dumps(
                {"operation": "embed", "text": document["text"]},
                ensure_ascii=False,
            ),
            text=True,
            capture_output=True,
            check=False,
            timeout=SEMANTIC_MAINTENANCE_TIMEOUT_SECONDS,
            env=environment,
        )
        if result.returncode != 0:
            return False
        payload = json.loads(result.stdout)
        encoded = payload.get("vector_b64") if isinstance(payload, dict) else None
        if not isinstance(encoded, str):
            return False
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) != profile.dimension * 4:
            return False
        values = struct.unpack(f"<{profile.dimension}f", raw)
        if not all(math.isfinite(value) for value in values):
            return False
        norm = math.sqrt(sum(value * value for value in values))
        if norm <= 0:
            return False
        normalized = struct.pack(
            f"<{profile.dimension}f", *(value / norm for value in values)
        )
        connection = sqlite3.connect(_index_path(root), timeout=5.0)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO vectors(record_id, content_hash, vector)
                VALUES (?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    vector=excluded.vector
                """,
                (
                    document["record_id"],
                    document["content_hash"],
                    normalized,
                ),
            )
            count = int(connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])
            if count != len(documents):
                connection.rollback()
                return False
            updates = {
                "record_count": str(count),
                "source_fingerprint": _documents_fingerprint(documents),
                "generated_at": _utc_now(),
            }
            connection.executemany(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                sorted(updates.items()),
            )
            connection.commit()
        finally:
            connection.close()
        _dirty_path(root).unlink(missing_ok=True)
        return True
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        sqlite3.Error,
    ):
        return False


def query_index(root: Path, query: str, limit: int) -> list[dict[str, Any]]:
    import numpy as np

    profile = _profile()
    model = _embedding_model(root)
    embedding = next(iter(model.embed([query], role="query", batch_size=1)))
    query_vector = np.asarray(embedding, dtype=np.float32)
    norm = float(np.linalg.norm(query_vector))
    if norm <= 0:
        return []
    query_vector /= norm
    connection = sqlite3.connect(_index_path(root))
    scored = []
    try:
        rows = connection.execute(
            "SELECT record_id, content_hash, vector FROM vectors"
        )
        for record_id, content_hash, blob in rows:
            vector = np.frombuffer(blob, dtype=np.float32)
            if vector.shape != (profile.dimension,):
                continue
            scored.append(
                {
                    "record_id": str(record_id),
                    "content_hash": str(content_hash),
                    "score": float(np.dot(query_vector, vector)),
                }
            )
    finally:
        connection.close()
    scored.sort(key=lambda item: (-item["score"], item["record_id"]))
    return scored[:limit]


def query_fingerprint(root: Path) -> dict[str, str]:
    """Identity of the encoder and corpus a candidate set must have come from.

    Bound once per worker run rather than repeated on every candidate: all
    candidates from one run necessarily share it, so validating it once is
    equivalent and cheaper.
    """
    profile = _profile()
    source_fingerprint = ""
    index_path = _index_path(root)
    if index_path.is_file():
        try:
            connection = sqlite3.connect(index_path)
            try:
                meta = dict(connection.execute("SELECT key, value FROM meta"))
            finally:
                connection.close()
            source_fingerprint = str(meta.get("source_fingerprint", ""))
        except sqlite3.Error:
            source_fingerprint = ""
    return {
        "profile": profile.name,
        "embedding_contract_sha256": profile.contract_sha256,
        "semantic_document_contract_sha256": SEMANTIC_DOCUMENT_CONTRACT_SHA256,
        "dimension": str(profile.dimension),
        "source_fingerprint": source_fingerprint,
    }


def _parse_worker_candidates(
    payload: Any,
    *,
    limit: int,
    expected_fingerprint: dict[str, str],
    threshold: float,
) -> tuple[SemanticCandidate, ...] | None:
    """Validate a worker payload, or return None if it violates the contract.

    A partially valid payload is rejected whole. Silently dropping malformed
    rows would let a wrong encoder, a stale index, or a duplicated record id
    reach ranking as if it had been verified.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("fingerprint") != expected_fingerprint:
        return None
    rows = payload.get("candidates")
    if not isinstance(rows, list) or len(rows) > limit:
        return None

    parsed: list[SemanticCandidate] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        record_id = row.get("record_id")
        score = row.get("score")
        content_hash = row.get("content_hash")
        if not isinstance(record_id, str) or not record_id:
            return None
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return None
        score = float(score)
        if not math.isfinite(score):
            return None
        if not isinstance(content_hash, str) or not _HEX64_RE.match(content_hash):
            return None
        if record_id in seen:
            return None
        seen.add(record_id)
        if score < threshold:
            continue
        parsed.append(
            SemanticCandidate(
                record_id=record_id, score=score, content_hash=content_hash
            )
        )
    return tuple(parsed)


def query_candidates(
    root: Path,
    config_root: Path,
    query: str,
    *,
    limit: int = 64,
) -> SemanticQueryOutcome:
    runtime = _load_runtime(config_root)
    if _circuit_is_open(runtime):
        return SemanticQueryOutcome("circuit_open", code="circuit-open")
    override = os.environ.get("AGENT_MEMORY_SEMANTIC_WORKER")
    current = status(root, config_root)
    if not override and (
        not current["model_installed"] or current["index_status"] != "present"
    ):
        raise SemanticUnavailable(
            "run `memoryctl semantic setup` and `memoryctl semantic reindex` first",
            code="backend-not-ready",
        )
    command = (
        [override]
        if override
        else [
            os.fspath(Path(os.sys.executable)),
            "-m",
            "agent_memory.semantic_worker",
        ]
    )
    environment = os.environ.copy()
    environment.update(
        {
            "AGENT_MEMORY_HOME": str(root),
            "AGENT_MEMORY_CONFIG_HOME": str(config_root),
            "OMP_NUM_THREADS": "2",
            "OMP_THREAD_LIMIT": "2",
        }
    )

    def failed(code: str) -> SemanticQueryOutcome:
        _record_query_result(config_root, code)
        return SemanticQueryOutcome("failed", code=code)

    try:
        result = subprocess.run(
            command,
            input=json.dumps({"query": query, "limit": limit}),
            text=True,
            capture_output=True,
            check=False,
            timeout=SEMANTIC_QUERY_TIMEOUT_SECONDS,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return failed("worker-timeout")
    except OSError:
        return failed("worker-unavailable")
    if result.returncode != 0:
        return failed("low-memory" if result.returncode == 3 else "worker-failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return failed("worker-invalid-output")

    threshold = float(current["min_similarity"])
    candidates = _parse_worker_candidates(
        payload,
        limit=limit,
        expected_fingerprint=query_fingerprint(root),
        threshold=threshold,
    )
    if candidates is None:
        return failed("worker-invalid-output")
    _record_query_result(config_root, None)
    if not candidates:
        return SemanticQueryOutcome("no_match")
    return SemanticQueryOutcome("success", candidates=candidates)
