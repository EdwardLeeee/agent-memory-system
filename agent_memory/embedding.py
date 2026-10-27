from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Iterator, Literal, Sequence


EmbeddingRole = Literal["query", "passage"]
PROFILE_ENV = "AGENT_MEMORY_SEMANTIC_PROFILE"
DEFAULT_PROFILE_NAME = "minilm-l12-v2"


@dataclass(frozen=True)
class EmbeddingProfile:
    name: str
    model_id: str
    revision: str
    model_file: str
    model_sha256: str
    model_bytes: int
    dimension: int
    max_length: int
    pooling: str
    normalization: bool
    query_prefix: str
    passage_prefix: str
    license: str

    @property
    def contract_sha256(self) -> str:
        contract = {
            "dimension": self.dimension,
            "max_length": self.max_length,
            "model_file": self.model_file,
            "model_id": self.model_id,
            "model_sha256": self.model_sha256,
            "normalization": self.normalization,
            "passage_prefix": self.passage_prefix,
            "pooling": self.pooling,
            "query_prefix": self.query_prefix,
            "revision": self.revision,
        }
        rendered = json.dumps(contract, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    def manifest(self) -> dict[str, object]:
        value = asdict(self)
        value["contract_sha256"] = self.contract_sha256
        value["bytes"] = value.pop("model_bytes")
        value["sha256"] = value.pop("model_sha256")
        return value


PROFILES = {
    profile.name: profile
    for profile in (
        EmbeddingProfile(
            name="minilm-l12-v2",
            model_id="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            revision="a3ded7f4d0d725c6fc251504622d65b2516a3aa1",
            model_file="onnx/model_quint8_avx2.onnx",
            model_sha256="98a01d88b7de996cdea58c32ca71208c09968d143798814b2ea09d3439dc334f",
            model_bytes=118_453_870,
            dimension=384,
            max_length=512,
            pooling="mean",
            normalization=True,
            query_prefix="",
            passage_prefix="",
            license="apache-2.0",
        ),
        EmbeddingProfile(
            name="multilingual-e5-small",
            model_id="intfloat/multilingual-e5-small",
            revision="614241f622f53c4eeff9890bdc4f31cfecc418b3",
            model_file="onnx/model_qint8_avx512_vnni.onnx",
            model_sha256="dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88",
            model_bytes=118_346_824,
            dimension=384,
            max_length=512,
            pooling="mean",
            normalization=True,
            query_prefix="query: ",
            passage_prefix="passage: ",
            license="mit",
        ),
        EmbeddingProfile(
            name="jina-v2-base-zh",
            model_id="jinaai/jina-embeddings-v2-base-zh",
            revision="c1ff9086a89a1123d7b5eff58055a665db4fb4b9",
            model_file="onnx/model_quantized.onnx",
            model_sha256="0a221ee9e6a6647ccc59cee7bdd26a7b8cf0c0cd3481a65f358d9585a23f02f4",
            model_bytes=161_565_239,
            dimension=768,
            max_length=512,
            pooling="mean",
            normalization=True,
            query_prefix="",
            passage_prefix="",
            license="apache-2.0",
        ),
    )
}


def get_profile(name: str) -> EmbeddingProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValueError(
            "unknown semantic profile; choose one of: " + ", ".join(sorted(PROFILES))
        ) from exc


def active_profile() -> EmbeddingProfile:
    return get_profile(os.environ.get(PROFILE_ENV, DEFAULT_PROFILE_NAME))


def prepare_text(profile: EmbeddingProfile, role: EmbeddingRole, text: str) -> str:
    if role == "query":
        return profile.query_prefix + text
    if role == "passage":
        return profile.passage_prefix + text
    raise ValueError("embedding role must be query or passage")


class LocalOnnxEncoder:
    """Small local encoder that hides tokenizer, ONNX, pooling, and prefix details."""

    def __init__(
        self,
        model_root: Path,
        profile: EmbeddingProfile,
        *,
        threads: int = 2,
        max_length: int | None = None,
    ) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import AddedToken, Tokenizer

        # onnxruntime 1.31 can send telemetry events and keeps a device id
        # (F17); the pinned 1.23.2 has neither. Off wherever the version can
        # turn it off, so an upgrade adds none (designer 2026-10-22).
        if hasattr(ort, "disable_telemetry_events"):
            ort.disable_telemetry_events()
        self._np = np
        self.profile = profile
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = threads
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        options.add_session_config_entry("session.disable_prepacking", "1")
        self._session = ort.InferenceSession(
            str(model_root / profile.model_file),
            providers=["CPUExecutionProvider"],
            sess_options=options,
        )

        config = json.loads((model_root / "config.json").read_text(encoding="utf-8"))
        tokenizer_config = json.loads(
            (model_root / "tokenizer_config.json").read_text(encoding="utf-8")
        )
        special_tokens = json.loads(
            (model_root / "special_tokens_map.json").read_text(encoding="utf-8")
        )
        tokenizer = Tokenizer.from_file(str(model_root / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=max_length or profile.max_length)
        if not tokenizer.padding:
            tokenizer.enable_padding(
                pad_id=int(config.get("pad_token_id", 0)),
                pad_token=str(tokenizer_config["pad_token"]),
            )
        for token in special_tokens.values():
            if isinstance(token, str):
                tokenizer.add_special_tokens([token])
            elif isinstance(token, dict):
                tokenizer.add_special_tokens([AddedToken(**token)])
        self._tokenizer = tokenizer
        self._input_names = {item.name for item in self._session.get_inputs()}

    def _embed_batch(
        self, texts: Sequence[str], role: EmbeddingRole
    ) -> list[object]:
        np = self._np
        prepared = [prepare_text(self.profile, role, text) for text in texts]
        encoded = self._tokenizer.encode_batch(prepared)
        input_ids = np.asarray([item.ids for item in encoded], dtype=np.int64)
        attention = np.asarray(
            [item.attention_mask for item in encoded], dtype=np.int64
        )
        inputs = {"input_ids": input_ids}
        if "attention_mask" in self._input_names:
            inputs["attention_mask"] = attention
        if "token_type_ids" in self._input_names:
            inputs["token_type_ids"] = np.zeros_like(input_ids)
        hidden = np.asarray(self._session.run(None, inputs)[0], dtype=np.float32)
        if hidden.ndim != 3 or hidden.shape[2] != self.profile.dimension:
            raise RuntimeError("semantic model returned an invalid hidden-state shape")
        if self.profile.pooling != "mean":
            raise RuntimeError("unsupported semantic pooling contract")
        mask = attention[..., None].astype(bool)
        denominator = attention.sum(axis=1)[..., None]
        if np.any(denominator <= 0):
            raise RuntimeError("semantic tokenizer returned an empty input")
        pooled = np.where(mask, hidden, 0.0).sum(axis=1) / denominator
        if self.profile.normalization:
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            if np.any(norms <= 0) or not np.all(np.isfinite(norms)):
                raise RuntimeError("semantic model returned an empty vector")
            pooled = pooled / norms
        if not np.all(np.isfinite(pooled)):
            raise RuntimeError("semantic model returned a non-finite vector")
        return [np.asarray(row, dtype=np.float32) for row in pooled]

    def embed(
        self,
        texts: Iterable[str],
        *,
        role: EmbeddingRole,
        batch_size: int = 4,
    ) -> Iterator[object]:
        if batch_size < 1:
            raise ValueError("embedding batch_size must be positive")
        batch: list[str] = []
        for text in texts:
            batch.append(str(text))
            if len(batch) == batch_size:
                yield from self._embed_batch(batch, role)
                batch = []
        if batch:
            yield from self._embed_batch(batch, role)
