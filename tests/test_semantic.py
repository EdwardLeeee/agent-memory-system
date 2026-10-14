from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from agent_memory import semantic


class EmbeddingProfileTests(unittest.TestCase):
    def test_e5_profile_applies_role_specific_prefixes_outside_canonical_text(self) -> None:
        from agent_memory.embedding import get_profile, prepare_text

        profile = get_profile("multilingual-e5-small")

        self.assertEqual("query: 記憶故障政策", prepare_text(profile, "query", "記憶故障政策"))
        self.assertEqual(
            "passage: Memory failures stay silent.",
            prepare_text(profile, "passage", "Memory failures stay silent."),
        )

    def test_symmetric_profiles_do_not_inherit_e5_prefixes(self) -> None:
        from agent_memory.embedding import get_profile, prepare_text

        profile = get_profile("jina-v2-base-zh")

        self.assertEqual("記憶故障政策", prepare_text(profile, "query", "記憶故障政策"))
        self.assertEqual(
            "Memory failures stay silent.",
            prepare_text(profile, "passage", "Memory failures stay silent."),
        )

    def test_profile_fingerprint_covers_the_encoding_contract(self) -> None:
        from dataclasses import replace
        from agent_memory.embedding import get_profile

        profile = get_profile("multilingual-e5-small")

        self.assertNotEqual(
            profile.contract_sha256,
            replace(profile, query_prefix="search: ").contract_sha256,
        )


class SemanticDocumentTests(unittest.TestCase):
    def test_fact_passage_excludes_identity_metadata_and_evidence(self) -> None:
        from agent_memory.semantic_document import build_semantic_document

        document = build_semantic_document(
            {
                "id": "fact.memory.lifecycle_fail_open",
                "kind": "fact",
                "scope": "global",
                "tags": ["lifecycle", "callbacks"],
            },
            """# fact.memory.lifecycle_fail_open

## Value

Memory callbacks fail open and remain silent.

## Evidence

/private/fixture/callback-log.json
""",
        )

        self.assertEqual(
            "Value\nMemory callbacks fail open and remain silent.", document.text
        )
        self.assertNotIn("fact.memory", document.text)
        self.assertNotIn("global", document.text)
        self.assertNotIn("callback-log", document.text)
        self.assertEqual(
            hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
            document.content_hash,
        )

    def test_experience_passage_keeps_outcome_and_applicability_not_links(self) -> None:
        from agent_memory.semantic_document import build_semantic_document

        document = build_semantic_document(
            {"id": "experience.memory.retry", "kind": "experience"},
            """# experience.memory.retry

## Situation

The first retry policy was too aggressive.

## Expectation

Every retry would help.

## Action

Measured outcomes before changing the policy.

## Observed Outcome

Repeated retries amplified the failure.

## Decision Links

- decision.memory.retry: contradicts

## Applicability

Use this lesson only for non-idempotent operations.

## Evidence

/private/retry-trace.json
""",
        )

        self.assertIn("Observed Outcome\nRepeated retries amplified", document.text)
        self.assertIn("Applicability\nUse this lesson only", document.text)
        self.assertNotIn("decision.memory.retry", document.text)
        self.assertNotIn("retry-trace", document.text)

    def test_document_contract_is_explicit_and_versioned(self) -> None:
        from agent_memory.semantic_document import (
            SEMANTIC_DOCUMENT_CONTRACT_SHA256,
            SEMANTIC_DOCUMENT_SCHEMA_VERSION,
        )

        self.assertEqual(2, SEMANTIC_DOCUMENT_SCHEMA_VERSION)
        self.assertRegex(SEMANTIC_DOCUMENT_CONTRACT_SHA256, r"^[0-9a-f]{64}$")

    def test_excluded_only_record_never_falls_back_to_evidence(self) -> None:
        from agent_memory.semantic_document import build_semantic_document

        with self.assertRaisesRegex(ValueError, "no retrievable content"):
            build_semantic_document(
                {"id": "fact.memory.invalid_evidence_only", "kind": "fact"},
                """# fact.memory.invalid_evidence_only

## Evidence

/private/must-not-be-embedded.json
""",
            )


class SemanticSetupTests(unittest.TestCase):
    def test_setup_persists_every_field_required_by_status_verification(self) -> None:
        from dataclasses import replace
        from agent_memory.embedding import get_profile

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "memory"
            config = Path(temporary) / "config"
            payload = b"pinned-test-model"
            digest = hashlib.sha256(payload).hexdigest()
            profile = replace(
                get_profile("minilm-l12-v2"),
                model_bytes=len(payload),
                model_sha256=digest,
            )

            def fake_snapshot_download(**kwargs: object) -> None:
                destination = Path(str(kwargs["local_dir"])) / profile.model_file
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)

            huggingface_hub = types.ModuleType("huggingface_hub")
            huggingface_hub.snapshot_download = fake_snapshot_download  # type: ignore[attr-defined]
            with (
                mock.patch.object(semantic, "_profile", return_value=profile),
                mock.patch.object(
                    semantic.importlib.util, "find_spec", return_value=object()
                ),
                mock.patch.dict(sys.modules, {"huggingface_hub": huggingface_hub}),
            ):
                state = semantic.setup(root, config)
                manifest = json.loads(
                    (
                        root
                        / ".cache"
                        / "semantic"
                        / "model"
                        / "agent-memory-model.json"
                    ).read_text(encoding="utf-8")
                )

            self.assertTrue(state["model_installed"])
            self.assertEqual(digest, manifest["sha256"])
            self.assertEqual(len(payload), manifest["bytes"])
            self.assertEqual(profile.model_file, manifest["model_file"])
            self.assertEqual(profile.contract_sha256, manifest["contract_sha256"])


if __name__ == "__main__":
    unittest.main()
