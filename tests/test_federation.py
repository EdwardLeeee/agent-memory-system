from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from agent_memory.federation import (
    FederationConfigError,
    collect_source_entries,
    install_source_hook,
    list_source_configs,
    load_source_config,
    orientation_summary,
    parse_minimal_toml,
)


# A settlement schema that belongs to no real project. The parser may read
# only the field names a source config names; every fixture uses these.
NEUTRAL_SETTLEMENT_OPTIONS = (
    'subject_key = "name"\nversion_key = "rev"\nconsequences_key = "method_notes"\n'
    'errata_key = "corrections"\nerrata_reason_key = "why"\nerrata_field_key = "path"\n'
    'errata_claim_flag_key = "material"\npredictions_key = "calls"\n'
    'prediction_outcome_key = "result"\nprediction_miss_value = "wrong"\n'
    'prediction_hit_value = "right"\nprediction_claim_key = "what"\n'
    'prediction_provenance_key = "basis"\nprediction_house_key = "ours"\n'
    'prediction_actual_key = "real"\nprediction_error_keys = ["gap", "comment"]\n'
)


def _init_fixture_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "fixture@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "fixture"], check=True
    )


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", message],
        check=True,
    )


class MinimalTomlTests(unittest.TestCase):
    def test_parses_tables_arrays_of_tables_and_string_lists(self) -> None:
        parsed = parse_minimal_toml(
            "# a comment\n"
            "[source]\n"
            'repo = "/tmp/example"\n'
            'affinity = ["project:a", "project:b"]\n'
            "\n"
            "[[parser]]\n"
            'kind = "adr"\n'
            'glob = "docs/adr/*.md"\n'
            "\n"
            "[[parser]]\n"
            'kind = "git-log"\n'
            'since = "2026-08-01"\n'
        )
        self.assertEqual(
            {"repo": "/tmp/example", "affinity": ["project:a", "project:b"]},
            parsed["source"],
        )
        self.assertEqual(2, len(parsed["parser"]))
        self.assertEqual("adr", parsed["parser"][0]["kind"])
        self.assertEqual("2026-08-01", parsed["parser"][1]["since"])

    def test_rejects_an_unquoted_scalar(self) -> None:
        with self.assertRaises(FederationConfigError):
            parse_minimal_toml("[source]\nrepo = /tmp/example\n")

    def test_rejects_a_malformed_line(self) -> None:
        with self.assertRaises(FederationConfigError):
            parse_minimal_toml("[source]\njust some text\n")


class SourceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.config_root = Path(self.tempdir.name) / "config"
        self.config_root.mkdir()
        (self.config_root / "sources.d").mkdir()
        self.repo = Path(self.tempdir.name) / "repo"
        _init_fixture_repo(self.repo)

    def _write_config(self, name: str, text: str) -> None:
        (self.config_root / "sources.d" / f"{name}.toml").write_text(
            text, encoding="utf-8"
        )

    def test_missing_config_is_a_federation_config_error(self) -> None:
        with self.assertRaises(FederationConfigError):
            load_source_config(self.config_root, "does-not-exist")

    def test_a_nonexistent_repo_path_is_rejected(self) -> None:
        self._write_config(
            "broken",
            '[source]\nrepo = "/no/such/path"\naffinity = ["project:x"]\n'
            "\n[[parser]]\nkind = \"adr\"\nglob = \"docs/*.md\"\n",
        )
        with self.assertRaises(FederationConfigError):
            load_source_config(self.config_root, "broken")

    def test_an_unsupported_parser_kind_is_rejected(self) -> None:
        self._write_config(
            "bad-kind",
            f'[source]\nrepo = "{self.repo}"\naffinity = ["project:x"]\n'
            '\n[[parser]]\nkind = "unknown-kind"\nglob = "*.md"\n',
        )
        with self.assertRaises(FederationConfigError):
            load_source_config(self.config_root, "bad-kind")

    def test_list_source_configs_returns_registered_names_sorted(self) -> None:
        self._write_config(
            "zeta",
            f'[source]\nrepo = "{self.repo}"\naffinity = ["project:z"]\n'
            '\n[[parser]]\nkind = "adr"\nglob = "docs/*.md"\n',
        )
        self._write_config(
            "alpha",
            f'[source]\nrepo = "{self.repo}"\naffinity = ["project:a"]\n'
            '\n[[parser]]\nkind = "adr"\nglob = "docs/*.md"\n',
        )
        self.assertEqual(["alpha", "zeta"], list_source_configs(self.config_root))

    def test_list_source_configs_is_empty_without_a_sources_directory(self) -> None:
        empty_root = Path(self.tempdir.name) / "no-sources-dir"
        empty_root.mkdir()
        self.assertEqual([], list_source_configs(empty_root))


class ParserFixtureTests(unittest.TestCase):
    """Exercise every parser against a small, hermetic fixture repo."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.config_root = Path(self.tempdir.name) / "config"
        (self.config_root / "sources.d").mkdir(parents=True)
        self.repo = Path(self.tempdir.name) / "repo"
        _init_fixture_repo(self.repo)

        (self.repo / "docs" / "adr").mkdir(parents=True)
        (self.repo / "docs" / "adr" / "0001-example.md").write_text(
            "# ADR-0001: Use a bounded excerpt\n\n"
            "- Status: Accepted\n- Date: 2026-09-22\n\n"
            "## Context\n\nDerived entries were unbounded and crowded a query's "
            "whole token budget.\n\n"
            "## Decision\n\nBound every derived excerpt to 400 characters.\n\n"
            "## Consequences\n\nA reader must open the source file for the full text.\n",
            encoding="utf-8",
        )
        (self.repo / "docs" / "adr" / "0002-no-decision-section.md").write_text(
            "# ADR-0002: Withdrawn\n\n- Status: Withdrawn\n\n## Context\n\nDropped.\n",
            encoding="utf-8",
        )

        settlements = self.repo / "reports" / "settlements" / "ACME"
        settlements.mkdir(parents=True)
        (settlements / "v1.json").write_text(
            json.dumps(
                {
                    "name": "ACME",
                    "rev": "v1",
                    "method_notes": {
                        "sizing_bias": "The sizing assumption underestimated demand by 12%."
                    },
                    "corrections": ["The published multiple used the wrong denominator."],
                    "calls": [
                        {
                            "what": "quarterly revenue",
                            "ours": "10.0",
                            "real": "12.0",
                            "result": "wrong",
                            "gap": "-16.7",
                            "comment": "Took the guidance midpoint without adjustment.",
                        },
                        {
                            "what": "share count",
                            "ours": "1.0",
                            "real": "1.0",
                            "result": "right",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        (self.repo / "research").mkdir()
        (self.repo / "research" / "2026-09-01-note.md").write_text(
            "# A research note\n\n"
            "## First finding\n\nSomething true about the market.\n\n"
            "## Second finding\n\nA silent truncation trap in a data source.\n",
            encoding="utf-8",
        )

        _commit_all(self.repo, "trap: document the silent truncation bug")
        _commit_all(self.repo, "an unrelated commit")

        (self.config_root / "sources.d" / "fixture.toml").write_text(
            f'[source]\nrepo = "{self.repo}"\naffinity = ["project:fixture"]\n'
            'orientation = ["docs/adr/"]\n'
            '\n[[parser]]\nkind = "adr"\nglob = "docs/adr/*.md"\n'
            '\n[[parser]]\nkind = "settlement"\nglob = "reports/settlements/*/*.json"\n'
            + NEUTRAL_SETTLEMENT_OPTIONS
            + '\n[[parser]]\nkind = "markdown-note"\nglob = "research/*.md"\n'
            '\n[[parser]]\nkind = "git-log"\nsince = "2020-01-01"\n',
            encoding="utf-8",
        )
        self.config = load_source_config(self.config_root, "fixture")

    def test_adr_parser_only_emits_entries_with_a_decision_section(self) -> None:
        entries = collect_source_entries(self.config)
        decisions = [e for e in entries if e["metadata"]["kind"] == "decision"]
        self.assertEqual(1, len(decisions))
        entry = decisions[0]
        self.assertIn("Bound every derived excerpt", entry["body"])
        self.assertEqual(["project:fixture"], entry["metadata"]["affinity"])
        self.assertEqual("global", entry["metadata"]["scope"])
        self.assertEqual("docs/adr/0001-example.md", entry["metadata"]["origin"]["path"])
        self.assertIsNotNone(entry["metadata"]["origin"]["blob_sha256"])

    def test_settlement_parser_emits_consequences_errata_and_misses_only(self) -> None:
        entries = collect_source_entries(self.config)
        lessons = [
            e
            for e in entries
            if e["metadata"]["kind"] == "lesson" and "settlement" in e["metadata"]["tags"]
        ]
        bodies = " ".join(e["body"] for e in lessons)
        self.assertIn("underestimated demand by 12%", bodies)
        self.assertIn("wrong denominator", bodies)
        self.assertIn("guidance midpoint without adjustment", bodies)
        self.assertNotIn("share count", bodies)
        for entry in lessons:
            self.assertEqual("ACME", entry["metadata"]["applies_to"])

    def test_markdown_note_parser_emits_one_entry_per_h2_section(self) -> None:
        entries = collect_source_entries(self.config)
        sections = [e for e in entries if "research-note" in e["metadata"]["tags"]]
        self.assertEqual(2, len(sections))
        anchors = {e["metadata"]["origin"]["anchor"] for e in sections}
        self.assertEqual({"First finding", "Second finding"}, anchors)
        truncation_entry = next(
            e for e in sections if e["metadata"]["origin"]["anchor"] == "Second finding"
        )
        self.assertIn("silent truncation trap", truncation_entry["body"])

    def test_only_a_section_that_records_a_mistake_is_a_lesson(self) -> None:
        entries = collect_source_entries(self.config)
        kinds = {
            e["metadata"]["origin"]["anchor"]: e["metadata"]["kind"]
            for e in entries
            if "research-note" in e["metadata"]["tags"]
        }
        # "A silent truncation trap" names a trap in its first sentence.
        self.assertEqual("lesson", kinds["Second finding"])
        self.assertEqual("note", kinds["First finding"])

    def test_a_reference_note_renders_through_a_value_section(self) -> None:
        entries = collect_source_entries(self.config)
        note = next(
            e for e in entries if e["metadata"]["origin"]["anchor"] == "First finding"
        )
        # The renderer reads `## Value` for reference material; without it the
        # entry would render as an empty value.
        self.assertIn("## Value\n\n", note["body"])
        self.assertNotIn("## Rule", note["body"])
        self.assertNotIn("lesson_status", note["metadata"])
        self.assertNotIn("applies_to", note["metadata"])

    def test_heading_and_first_sentence_signals_and_hand_off_exclusions(self) -> None:
        signal_repo = Path(self.tempdir.name) / "signal-repo"
        _init_fixture_repo(signal_repo)
        (signal_repo / "research").mkdir()
        (signal_repo / "research" / "errata.md").write_text(
            "# 勘誤內容：SOXQ v9\n\n"
            "## 一、錯在哪裡（兩份報告同一個根因）\n\n組合倍數把新台幣當美元。\n\n"
            "## 更正（2026-09-22）：上面的結論是錯的\n\n錯在抓取方式。\n\n"
            "## 四、給 RD 的備註\n\n這份勘誤請照抄進結算檔。\n\n"
            "## 一、常數區（RD 逐字複製）\n\n```yaml\nerrata: 3\n```\n\n"
            "## 實測結果\n\n樣本只有兩個循環。\n",
            encoding="utf-8",
        )
        _commit_all(signal_repo, "add errata note")
        (self.config_root / "sources.d" / "signal.toml").write_text(
            f'[source]\nrepo = "{signal_repo}"\naffinity = ["project:signal"]\n'
            '\n[[parser]]\nkind = "markdown-note"\nglob = "research/*.md"\n'
            'reference_headings = ["給 RD", "常數區"]\n',
            encoding="utf-8",
        )
        kinds = {
            e["metadata"]["origin"]["anchor"]: e["metadata"]["kind"]
            for e in collect_source_entries(load_source_config(self.config_root, "signal"))
        }
        self.assertEqual("lesson", kinds["一、錯在哪裡（兩份報告同一個根因）"])
        self.assertEqual("lesson", kinds["更正（2026-09-22）：上面的結論是錯的"])
        # The H1 title says 勘誤, but only the section's own heading and first
        # sentence count, and the source's configured hand-off headings are
        # never lessons.
        self.assertEqual("note", kinds["四、給 RD 的備註"])
        self.assertEqual("note", kinds["一、常數區（RD 逐字複製）"])
        self.assertEqual("note", kinds["實測結果"])

    def test_git_log_parser_matches_only_flagged_commits(self) -> None:
        entries = collect_source_entries(self.config)
        commits = [e for e in entries if "commit-message" in e["metadata"]["tags"]]
        self.assertEqual(1, len(commits))
        self.assertIn("silent truncation bug", commits[0]["body"])

    def test_a_commit_subject_is_a_note_never_a_lesson(self) -> None:
        entries = collect_source_entries(self.config)
        commit = next(e for e in entries if "commit-message" in e["metadata"]["tags"])
        self.assertEqual("note", commit["metadata"]["kind"])
        self.assertIn("## Value\n\n", commit["body"])
        self.assertNotIn("applies_to", commit["metadata"])

    def test_free_text_method_consequence_is_indexed(self) -> None:
        macro = self.repo / "reports" / "settlements" / "MACRO"
        macro.mkdir(parents=True)
        (macro / "v9.json").write_text(
            json.dumps(
                {
                    "name": "MACRO",
                    "rev": "v9",
                    "method_notes": "The risk premium inherited a currency error.",
                }
            ),
            encoding="utf-8",
        )
        entries = collect_source_entries(self.config)
        methods = [
            e
            for e in entries
            if e["metadata"]["origin"]["path"] == "reports/settlements/MACRO/v9.json"
        ]
        self.assertEqual(1, len(methods))
        self.assertEqual("method", methods[0]["metadata"]["origin"]["anchor"])
        self.assertEqual("method", methods[0]["metadata"]["lesson_category"])
        self.assertIn("inherited a currency error", methods[0]["body"])

    def test_an_erratum_is_anchored_by_its_own_field_path(self) -> None:
        acme = self.repo / "reports" / "settlements" / "ACME" / "v2.json"
        acme.write_text(
            json.dumps(
                {
                    "name": "ACME",
                    "rev": "v2",
                    "corrections": [
                        {"path": "summary", "why": "Copied from v1."},
                        {"path": "thesis.0", "why": "Copied from v1."},
                    ],
                }
            ),
            encoding="utf-8",
        )
        groups = [
            e
            for e in collect_source_entries(self.config)
            if e["metadata"]["origin"]["path"].endswith("ACME/v2.json")
        ]
        # Same first sentence, so one group; each member keeps its own anchor.
        self.assertEqual(1, len(groups))
        self.assertEqual(
            [
                {"path": "reports/settlements/ACME/v2.json", "anchor": "errata:summary"},
                {"path": "reports/settlements/ACME/v2.json", "anchor": "errata:thesis.0"},
            ],
            groups[0]["metadata"]["members"],
        )

    def test_long_content_is_bounded_to_roughly_four_hundred_characters(self) -> None:
        long_repo = Path(self.tempdir.name) / "long-repo"
        _init_fixture_repo(long_repo)
        (long_repo / "docs" / "adr").mkdir(parents=True)
        (long_repo / "docs" / "adr" / "0001-long.md").write_text(
            "# ADR-0001: Long\n\n## Context\n\nshort\n\n"
            f"## Decision\n\n{'x' * 5000}\n",
            encoding="utf-8",
        )
        _commit_all(long_repo, "add long adr")
        (self.config_root / "sources.d" / "long.toml").write_text(
            f'[source]\nrepo = "{long_repo}"\naffinity = ["project:long"]\n'
            '\n[[parser]]\nkind = "adr"\nglob = "docs/adr/*.md"\n',
            encoding="utf-8",
        )
        config = load_source_config(self.config_root, "long")
        entries = collect_source_entries(config)
        self.assertEqual(1, len(entries))
        # The excerpt must not be able to consume a whole query's token budget
        # on its own, which an untruncated multi-thousand-character dump would.
        self.assertLess(len(entries[0]["body"]), 600)

    def test_every_entry_id_is_unique_even_with_cjk_only_headings(self) -> None:
        cjk_repo = Path(self.tempdir.name) / "cjk-repo"
        _init_fixture_repo(cjk_repo)
        (cjk_repo / "research").mkdir()
        (cjk_repo / "research" / "note.md").write_text(
            "# 標題\n\n## 第一段\n\n內容一\n\n## 第二段\n\n內容二\n\n## 第三段\n\n內容三\n",
            encoding="utf-8",
        )
        _commit_all(cjk_repo, "add cjk note")
        (self.config_root / "sources.d" / "cjk.toml").write_text(
            f'[source]\nrepo = "{cjk_repo}"\naffinity = ["project:cjk"]\n'
            '\n[[parser]]\nkind = "markdown-note"\nglob = "research/*.md"\n',
            encoding="utf-8",
        )
        config = load_source_config(self.config_root, "cjk")
        entries = collect_source_entries(config)
        ids = [e["metadata"]["id"] for e in entries]
        self.assertEqual(3, len(entries))
        self.assertEqual(len(ids), len(set(ids)))

    def test_orientation_summary_counts_files_recursively(self) -> None:
        self.assertIn("2 docs/adr", orientation_summary(self.config))

    def test_a_broken_source_file_does_not_crash_the_whole_sync(self) -> None:
        (self.repo / "reports" / "settlements" / "ACME" / "broken.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        entries = collect_source_entries(self.config)
        self.assertTrue(entries)


class HeadingLevelTests(unittest.TestCase):
    """A decision log whose ### headings are the decisions (Phase 3)."""

    def test_heading_level_three_splits_at_decisions(self) -> None:
        from agent_memory.federation import parse_markdown_note

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "log.md").write_text(
                "# Decisions\n\n## 2026-09-30\n\nPreamble for the day.\n\n"
                "### D-1 Pick the engine\n\nUse the 2D engine.\n\n"
                "### D-2 No flying mages\n\nMages stay on the ground.\n\n"
                "## 2026-10-01\n\n### D-3 Free first release\n\nNo ads.\n",
                encoding="utf-8",
            )
            entries = parse_markdown_note("demo", ["project:demo"], repo, "log.md", [], "3")
            self.assertEqual(
                ["D-1 Pick the engine", "D-2 No flying mages", "D-3 Free first release"],
                [e["metadata"]["origin"]["anchor"] for e in entries],
            )
            self.assertNotIn("Preamble", " ".join(e["body"] for e in entries))
            self.assertEqual(2, len(parse_markdown_note("demo", ["project:demo"], repo, "log.md")))

    def test_heading_level_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "sources.d").mkdir()
            (home / "sources.d" / "demo.toml").write_text(
                f'[source]\nrepo = "{home}"\naffinity = ["project:demo"]\n\n'
                '[[parser]]\nkind = "markdown-note"\nglob = "*.md"\nheading_level = "4"\n',
                encoding="utf-8",
            )
            with self.assertRaises(FederationConfigError):
                load_source_config(home, "demo")


class ExampleConfigTests(unittest.TestCase):
    def test_the_example_source_config_loads(self) -> None:
        example = Path(__file__).resolve().parents[1] / "docs" / "sources.example.toml"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "my-app"
            repo.mkdir()
            (base / "sources.d").mkdir()
            (base / "sources.d" / "my-app.toml").write_text(
                example.read_text(encoding="utf-8").replace("/home/you/src/my-app", str(repo)), encoding="utf-8"
            )
            config = load_source_config(base, "my-app")
        self.assertEqual("origin/main", config["ref"])
        self.assertEqual(["adr", "markdown-note", "markdown-note", "settlement", "git-log"], [p["kind"] for p in config["parsers"]])


class SourceRefTests(unittest.TestCase):
    """Ruling 43: a source with `ref` reads what was fetched, not the checkout."""

    def test_a_ref_source_reads_the_fetched_branch_and_doctor_sees_it_move(self) -> None:
        import os

        from agent_memory import doctor
        from agent_memory.federation import fetch_source_ref, source_ref_commit

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            seed = base / "seed"
            _init_fixture_repo(seed)
            (seed / "notes").mkdir()
            (seed / "notes" / "old.md").write_text("# Old\n\n## Outpost one\n\nThe first outpost.\n", encoding="utf-8")
            _commit_all(seed, "first")
            subprocess.run(["git", "-C", str(seed), "branch", "-M", "main"], check=True)
            origin = base / "origin.git"
            subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
            local = base / "local"
            subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True)
            # Someone merges on the remote; the local checkout stays behind.
            subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(origin)], check=True)
            (seed / "notes" / "new.md").write_text("# New\n\n## Outpost two\n\nThe hilltop outpost.\n", encoding="utf-8")
            _commit_all(seed, "second")
            subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)

            config_root = base / "config"
            (config_root / "sources.d").mkdir(parents=True)
            (config_root / "sources.d" / "demo.toml").write_text(
                f'[source]\nrepo = "{local}"\naffinity = ["project:demo"]\nref = "origin/main"\n\n'
                '[[parser]]\nkind = "markdown-note"\nglob = "notes/*.md"\n',
                encoding="utf-8",
            )
            config = load_source_config(config_root, "demo")
            self.assertEqual("origin/main", config["ref"])
            before = {e["metadata"]["origin"]["path"] for e in collect_source_entries(config)}
            self.assertEqual({"notes/old.md"}, before)
            stale = source_ref_commit(config)

            self.assertTrue(fetch_source_ref(config))
            entries = collect_source_entries(config)
            self.assertEqual({"notes/old.md", "notes/new.md"}, {e["metadata"]["origin"]["path"] for e in entries})
            self.assertFalse((local / "notes" / "new.md").exists())
            new = next(e for e in entries if e["metadata"]["origin"]["path"] == "notes/new.md")
            blob = subprocess.run(["git", "-C", str(local), "rev-parse", "refs/agent-memory/origin/main:notes/new.md"],
                                  text=True, capture_output=True, check=True).stdout.strip()
            self.assertEqual(blob, new["metadata"]["origin"]["blob_sha256"])

            # doctor compares the synced commit with the ref.
            (config_root / "runtime" / "sources").mkdir(parents=True)
            (config_root / "runtime" / "sources" / "demo.json").write_text(
                json.dumps({"synced_at": "2026-10-14T00:00:00+00:00", "ref_commit": stale}), encoding="utf-8"
            )
            check = doctor.check_sources(config_root)
            self.assertEqual("WARN", check["status"])
            self.assertIn("origin/main 比同步新 1 個 commit", check["detail"])
            (config_root / "runtime" / "sources" / "demo.json").write_text(
                json.dumps({"synced_at": "2026-10-14T00:00:00+00:00", "ref_commit": source_ref_commit(config)}),
                encoding="utf-8",
            )
            self.assertEqual("OK", doctor.check_sources(config_root)["status"])

            # A remote that cannot be reached leaves the ref as it was.
            subprocess.run(["git", "-C", str(local), "remote", "set-url", "origin", str(base / "gone.git")], check=True)
            self.assertFalse(fetch_source_ref(config))
            self.assertEqual(2, len(collect_source_entries(config)))


class HookEnvironmentTests(unittest.TestCase):
    """Ruling 52 a: a sync started by a worktree's post-commit hook reads
    every source from its own repository."""

    def setUp(self) -> None:
        import os
        import sys

        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root_dir = Path(__file__).resolve().parents[1]
        base = Path(self.tempdir.name)
        self.config, self.store = base / "config", base / "store"
        self.repos = {"aaa": (base / "aaa", "apple"), "bbb": (base / "bbb", "banana")}
        for name, (repo, word) in self.repos.items():
            _init_fixture_repo(repo)
            (repo / "notes").mkdir()
            (repo / "notes" / "n.md").write_text(f"# N\n\n## {word} section\n\nThe {word} note.\n", encoding="utf-8")
            _commit_all(repo, "first")
            subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
        self.worktree = base / "aaa-wt"
        subprocess.run(
            ["git", "-C", str(self.repos["aaa"][0]), "worktree", "add", "-q", "-b", "side", str(self.worktree)], check=True,
        )
        (self.config / "sources.d").mkdir(parents=True)
        for name, (repo, _) in self.repos.items():
            (self.config / "sources.d" / f"{name}.toml").write_text(
                f'[source]\nrepo = "{repo}"\naffinity = ["project:{name}"]\nref = "main"\n\n'
                '[[parser]]\nkind = "markdown-note"\nglob = "notes/*.md"\n',
                encoding="utf-8",
            )
        self.env = {**os.environ, "AGENT_MEMORY_HOME": str(self.store), "AGENT_MEMORY_CONFIG_HOME": str(self.config),
                    "PYTHONPATH": str(self.root_dir)}
        for name in ("AGENT_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            self.env.pop(name, None)
        self.cli = [sys.executable, "-m", "agent_memory.cli"]
        subprocess.run([*self.cli, "init", "--machine-id", "m"], env=self.env, check=True, capture_output=True, cwd=self.root_dir)

    def assert_bbb_untouched(self) -> None:
        import sqlite3

        connection = sqlite3.connect(self.store / ".cache" / "index.sqlite3")
        bodies = dict(connection.execute("SELECT record_id, body FROM records WHERE record_id LIKE 'federation.bbb.%'"))
        connection.close()
        self.assertTrue(bodies)
        self.assertTrue(all("banana" in body and "apple" not in body for body in bodies.values()), bodies)
        cache = json.loads((self.store / ".cache" / "source_worktrees.json").read_text(encoding="utf-8"))
        self.assertEqual([str(self.repos["bbb"][0].resolve())], [str(Path(p).resolve()) for p in cache["sources"]["bbb"]])

    def test_a_worktree_commit_hook_leaves_other_sources_alone(self) -> None:
        import time

        subprocess.run([*self.cli, "sources", "install-hook", "aaa"], env=self.env, check=True, capture_output=True, cwd=self.root_dir)
        (self.worktree / "notes" / "n2.md").write_text("# N2\n\n## apple two\n\nAnother apple.\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.worktree), "add", "-A"], env=self.env, check=True)
        subprocess.run(["git", "-C", str(self.worktree), "commit", "-q", "-m", "second"], env=self.env, check=True)
        record = self.config / "runtime" / "sources" / "aaa.json"
        deadline = time.monotonic() + 60
        while not record.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertTrue(record.exists(), "the hook's sync never finished")
        time.sleep(0.5)
        self.assert_bbb_untouched()
        hook = (self.repos["aaa"][0] / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")
        self.assertIn("env -u GIT_DIR -u GIT_INDEX_FILE", hook)

    def test_a_sync_run_with_the_worktree_environment_reads_each_repository(self) -> None:
        # Ruling 53 b: without the hook line's env -u (a hook installed
        # before ruling 52 has the old line), the program itself must drop
        # what git hands a hook.
        git_dir = self.repos["aaa"][0].resolve() / ".git" / "worktrees" / self.worktree.name
        self.assertTrue(git_dir.is_dir())
        env = {**self.env, "GIT_DIR": str(git_dir), "GIT_INDEX_FILE": str(git_dir / "index"), "GIT_PREFIX": ""}
        result = subprocess.run([*self.cli, "sources", "sync", "aaa"], env=env, capture_output=True, text=True, cwd=self.worktree)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assert_bbb_untouched()


class FetchAndMissingRefTests(unittest.TestCase):
    """Ruling 51 (c), (d), (g)."""

    def test_a_hung_fetch_leaves_no_process_behind(self) -> None:
        import os
        import time
        from unittest import mock

        from agent_memory.federation import fetch_source_ref

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bin_dir = base / "bin"
            bin_dir.mkdir()
            pids = base / "pids"
            fake = bin_dir / "git"
            fake.write_text(
                "#!/bin/sh\n"
                '[ "$3" = remote ] && { echo origin; exit 0; }\n'
                f'[ -t 0 ] && echo tty >> "{base}/stdin-was-tty"\n'
                f'echo "$GIT_TERMINAL_PROMPT" > "{base}/prompt"\n'
                f'sleep 300 & echo $! >> "{pids}"\n'
                f'echo $$ >> "{pids}"\n'
                "sleep 300\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
                started = time.monotonic()
                ok = fetch_source_ref({"repo": base, "ref": "origin/main"}, timeout=1.0)
            self.assertFalse(ok)
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual("0", (base / "prompt").read_text().strip())
            self.assertFalse((base / "stdin-was-tty").exists())
            time.sleep(0.2)
            for pid in [int(line) for line in pids.read_text().split()]:
                with self.assertRaises(ProcessLookupError, msg=f"process {pid} survived"):
                    os.kill(pid, 0)

    def test_a_missing_ref_is_reported_and_a_local_ref_is_not_fetched(self) -> None:
        from agent_memory import doctor
        from agent_memory.federation import SourceTree, fetch_source_ref, ref_is_remote

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_fixture_repo(repo)
            (repo / "a.md").write_text("# A\n\n## One\n\nText.\n", encoding="utf-8")
            _commit_all(repo, "first")
            subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
            local = SourceTree(repo, "main")
            self.assertFalse(local.missing_ref)
            self.assertEqual(head, local.commit)
            self.assertFalse(ref_is_remote("main", repo))
            self.assertFalse(fetch_source_ref({"repo": repo, "ref": "main"}))
            typo = SourceTree(repo, "origin/mian")
            self.assertTrue(typo.missing_ref)
            self.assertEqual(["a.md"], typo.files("*.md"))

            config_root = Path(tmp) / "config"
            (config_root / "sources.d").mkdir(parents=True)
            (config_root / "sources.d" / "demo.toml").write_text(
                f'[source]\nrepo = "{repo}"\naffinity = ["project:demo"]\nref = "origin/mian"\n\n'
                '[[parser]]\nkind = "markdown-note"\nglob = "*.md"\n',
                encoding="utf-8",
            )
            (config_root / "runtime" / "sources").mkdir(parents=True)
            (config_root / "runtime" / "sources" / "demo.json").write_text(json.dumps({
                "synced_at": "2026-10-14T00:00:00+00:00", "ref_commit": None, "ref_problem": "ref origin/mian 不存在",
            }), encoding="utf-8")
            check = doctor.check_sources(config_root)
            self.assertEqual("WARN", check["status"])
            self.assertIn("ref origin/mian 不存在", check["detail"])

    def test_only_a_ref_under_a_listed_remote_is_fetched(self) -> None:
        # Ruling 52 f: release/1.0 is a local branch, not remote "release".
        import os
        import sys

        from agent_memory.federation import SourceTree, ref_is_remote

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_fixture_repo(repo)
            (repo / "a.md").write_text("# A\n\n## One\n\nText.\n", encoding="utf-8")
            _commit_all(repo, "first")
            subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
            subprocess.run(["git", "-C", str(repo), "branch", "release/1.0"], check=True)
            subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(Path(tmp) / "origin.git")], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(Path(tmp) / "origin.git")], check=True)
            store, config = Path(tmp) / "store", Path(tmp) / "config"
            (config / "sources.d").mkdir(parents=True)
            (config / "sources.d" / "demo.toml").write_text(
                f'[source]\nrepo = "{repo}"\naffinity = ["project:demo"]\nref = "release/1.0"\n\n'
                '[[parser]]\nkind = "markdown-note"\nglob = "*.md"\n',
                encoding="utf-8",
            )
            env = {**os.environ, "AGENT_MEMORY_HOME": str(store), "AGENT_MEMORY_CONFIG_HOME": str(config)}
            cli = [sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "memoryctl")]
            subprocess.run([*cli, "init", "--machine-id", "m"], env=env, check=True, capture_output=True)
            result = subprocess.run([*cli, "sources", "sync", "demo"], env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("at release/1.0", result.stdout)
            self.assertNotIn("fetch failed", result.stdout + result.stderr)
            self.assertFalse(ref_is_remote("release/1.0", repo))
            self.assertTrue(ref_is_remote("origin/main", repo))
            self.assertFalse(SourceTree(repo, "release/1.0").missing_ref)

    def test_a_remote_whose_name_holds_a_slash_is_fetched(self) -> None:
        # Ruling 53 e: `git remote add team/up …`; the ref team/up/main must
        # fetch branch main from remote team/up (the first segment, "team",
        # names no remote).
        from agent_memory.federation import SourceTree, fetch_source_ref

        with tempfile.TemporaryDirectory() as tmp:
            upstream = Path(tmp) / "upstream"
            _init_fixture_repo(upstream)
            (upstream / "a.md").write_text("# A\n\n## One\n\nText.\n", encoding="utf-8")
            _commit_all(upstream, "first")
            subprocess.run(["git", "-C", str(upstream), "branch", "-M", "main"], check=True)
            repo = Path(tmp) / "repo"
            subprocess.run(["git", "clone", "-q", str(upstream), str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "team/up", str(upstream)], check=True)
            (upstream / "b.md").write_text("# B\n\n## Two\n\nMore.\n", encoding="utf-8")
            _commit_all(upstream, "second")
            head = subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
            self.assertTrue(fetch_source_ref({"repo": repo, "ref": "team/up/main"}))
            self.assertEqual(head, SourceTree(repo, "team/up/main").commit)


class PrivateFetchRefTests(unittest.TestCase):
    """Ruling 55: a sync never moves the refs every worktree of the project
    shares; it fetches into refs/agent-memory/<remote>/<branch> and reads
    that."""

    def git(self, repo: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout.strip()

    def test_a_sync_leaves_origin_main_and_fetch_head_alone(self) -> None:
        import os
        import sqlite3
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            upstream = base / "upstream"
            _init_fixture_repo(upstream)
            (upstream / "notes").mkdir()
            (upstream / "notes" / "n.md").write_text("# N\n\n## apple section\n\nThe apple note.\n", encoding="utf-8")
            _commit_all(upstream, "first")
            self.git(upstream, "branch", "-M", "main")
            repo = base / "repo"
            subprocess.run(["git", "clone", "-q", str(upstream), str(repo)], check=True)
            self.git(repo, "worktree", "add", "-q", "-b", "side", str(base / "repo-wt"))
            before = self.git(repo, "rev-parse", "refs/remotes/origin/main")
            (upstream / "notes" / "n.md").write_text("# N\n\n## banana section\n\nThe banana note.\n", encoding="utf-8")
            _commit_all(upstream, "second")
            self.git(upstream, "tag", "v-upstream")
            upstream_head = self.git(upstream, "rev-parse", "HEAD")
            fetch_head = repo / ".git" / "FETCH_HEAD"
            fetch_head_before = fetch_head.read_bytes() if fetch_head.exists() else None

            store, config = base / "store", base / "config"
            (config / "sources.d").mkdir(parents=True)
            (config / "sources.d" / "demo.toml").write_text(
                f'[source]\nrepo = "{repo}"\naffinity = ["project:demo"]\nref = "origin/main"\n\n'
                '[[parser]]\nkind = "markdown-note"\nglob = "notes/*.md"\n',
                encoding="utf-8",
            )
            env = {**os.environ, "AGENT_MEMORY_HOME": str(store), "AGENT_MEMORY_CONFIG_HOME": str(config)}
            cli = [sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "memoryctl")]
            subprocess.run([*cli, "init", "--machine-id", "m"], env=env, check=True, capture_output=True)

            def sync() -> dict:
                result = subprocess.run([*cli, "sources", "sync", "demo"], env=env, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                return json.loads((config / "runtime" / "sources" / "demo.json").read_text(encoding="utf-8"))

            def bodies() -> str:
                connection = sqlite3.connect(store / ".cache" / "index.sqlite3")
                text = " ".join(body for (body,) in connection.execute(
                    "SELECT body FROM records WHERE record_id LIKE 'federation.demo.%'"))
                connection.close()
                return text

            record = sync()
            self.assertEqual(before, self.git(repo, "rev-parse", "refs/remotes/origin/main"))
            self.assertEqual(upstream_head, self.git(repo, "rev-parse", "refs/agent-memory/origin/main"))
            self.assertEqual(fetch_head_before, fetch_head.read_bytes() if fetch_head.exists() else None)
            self.assertEqual("", self.git(repo, "tag", "--list", "v-upstream"))
            self.assertEqual(upstream_head, record["ref_commit"])
            self.assertIn("banana", bodies())
            self.assertNotIn("apple", bodies())

            # The remote is gone: the private ref already fetched is read.
            self.git(repo, "remote", "set-url", "origin", str(base / "nowhere"))
            (upstream / "notes" / "n.md").write_text("# N\n\n## cherry section\n\nThe cherry note.\n", encoding="utf-8")
            _commit_all(upstream, "third")
            record = sync()
            self.assertEqual(upstream_head, record["ref_commit"])
            self.assertFalse(record.get("ref_problem"))
            self.assertIn("banana", bodies())
            self.assertEqual(before, self.git(repo, "rev-parse", "refs/remotes/origin/main"))


class InstallHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.config_root = Path(self.tempdir.name) / "config"
        (self.config_root / "sources.d").mkdir(parents=True)
        self.repo = Path(self.tempdir.name) / "repo"
        _init_fixture_repo(self.repo)
        (self.repo / "docs" / "adr").mkdir(parents=True)
        (self.repo / "docs" / "adr" / "0001.md").write_text(
            "# ADR-0001\n\n## Decision\n\nSomething.\n", encoding="utf-8"
        )
        _commit_all(self.repo, "add adr")
        (self.config_root / "sources.d" / "fixture.toml").write_text(
            f'[source]\nrepo = "{self.repo}"\naffinity = ["project:fixture"]\n'
            '\n[[parser]]\nkind = "adr"\nglob = "docs/adr/*.md"\n',
            encoding="utf-8",
        )
        self.memory_root = Path(self.tempdir.name) / "memory"
        self.memoryctl_path = Path(self.tempdir.name) / "bin" / "memoryctl"

    def test_installs_a_hook_that_never_blocks_the_commit(self) -> None:
        hook_path = install_source_hook(
            self.config_root, self.memory_root, self.memoryctl_path, "fixture"
        )
        text = hook_path.read_text(encoding="utf-8")
        self.assertIn("agent-memory-sources-sync:start", text)
        self.assertIn("|| true", text)
        self.assertIn(str(self.memoryctl_path), text)
        self.assertTrue(hook_path.stat().st_mode & 0o100, "hook must be executable")

    def test_reinstalling_preserves_unrelated_existing_hook_content(self) -> None:
        hook_path = self.repo / ".git" / "hooks" / "post-commit"
        hook_path.write_text(
            "#!/bin/sh\necho 'existing unrelated hook'\n", encoding="utf-8"
        )
        install_source_hook(
            self.config_root, self.memory_root, self.memoryctl_path, "fixture"
        )
        text = hook_path.read_text(encoding="utf-8")
        self.assertIn("existing unrelated hook", text)
        self.assertEqual(1, text.count("agent-memory-sources-sync:start"))

        # Installing again must not duplicate the block.
        install_source_hook(
            self.config_root, self.memory_root, self.memoryctl_path, "fixture"
        )
        text = hook_path.read_text(encoding="utf-8")
        self.assertEqual(1, text.count("agent-memory-sources-sync:start"))
        self.assertEqual(1, text.count("existing unrelated hook"))

    def test_a_non_git_repo_is_rejected(self) -> None:
        non_repo = Path(self.tempdir.name) / "not-a-repo"
        non_repo.mkdir()
        (self.config_root / "sources.d" / "notgit.toml").write_text(
            f'[source]\nrepo = "{non_repo}"\naffinity = ["project:x"]\n'
            '\n[[parser]]\nkind = "adr"\nglob = "*.md"\n',
            encoding="utf-8",
        )
        with self.assertRaises(FederationConfigError):
            install_source_hook(
                self.config_root, self.memory_root, self.memoryctl_path, "notgit"
            )


if __name__ == "__main__":
    unittest.main()


class CandidateGroupingTests(unittest.TestCase):
    """Settlement findings are grouped by root cause; recurrence is computed."""

    SEEDED = (
        "The v1 publisher was seeded from the v0 one and this label was not updated. "
        "It tells a reader which model produced the numbers."
    )

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.config_root = Path(self.tempdir.name) / "config"
        (self.config_root / "sources.d").mkdir(parents=True)
        self.repo = Path(self.tempdir.name) / "repo"
        _init_fixture_repo(self.repo)
        self._settlement("ALPHA", "v1", corrections=[
            {"path": f"field.{index}", "why": self.SEEDED, "material": index == 0}
            for index in range(3)
        ] + [
            {"path": "field.3", "why": "Same copy-paste residue; the guarantee is correct."},
            {"path": "field.4", "why": "A decimal point was lost in the multiple."},
            {"path": "field.5", "why": "Short one."},
        ])
        self._settlement("BETA", "v1", corrections=[
            {"path": "field.0", "why": self.SEEDED},
            {"path": "field.1", "why": "Short one."},
        ])
        self._settlement("GAMMA", "v2", corrections=[
            {"path": "field.0", "why": "The yield used a local-currency figure as dollars."},
            {"path": "field.1", "why": "The yield used a local-currency figure as dollars."},
        ])
        self._settlement("DELTA", "v2", corrections=[
            {"path": "field.0", "why": "The premium is the inverse of GAMMA v2's multiple and inherits its error."},
        ])
        self._settlement("EPSILON", "v3", calls=[
            {"what": "Q2 FY27 revenue", "basis": "guidance", "result": "wrong", "ours": "9", "real": "10"},
            {"what": "Q3 FY27 revenue (house view)", "basis": "guidance", "result": "right"},
            {"what": "Q2 FY27 margin", "basis": "house", "result": "wrong"},
        ])
        self._settlement("ZETA", "v1", calls=[
            {"what": "Q1 FY28 revenue", "basis": "guidance", "result": "wrong"},
        ])
        _commit_all(self.repo, "add settlements")
        self._write_config()

    def _settlement(self, name: str, rev: str, corrections=(), calls=()) -> None:
        folder = self.repo / "reports" / "settlements" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{rev}.json").write_text(
            json.dumps({"name": name, "rev": rev, "corrections": list(corrections), "calls": list(calls)}),
            encoding="utf-8",
        )

    def _write_config(self, extra: str = "") -> None:
        (self.config_root / "sources.d" / "fixture.toml").write_text(
            f'[source]\nrepo = "{self.repo}"\naffinity = ["project:fixture"]\n'
            '\n[[parser]]\nkind = "settlement"\nglob = "reports/settlements/*/*.json"\n'
            + NEUTRAL_SETTLEMENT_OPTIONS
            + extra,
            encoding="utf-8",
        )

    def _groups(self) -> list[dict]:
        config = load_source_config(self.config_root, "fixture")
        return [e["metadata"] for e in collect_source_entries(config)]

    def _group_with(self, anchor_path: str, anchor: str) -> dict:
        for metadata in self._groups():
            if {"path": anchor_path, "anchor": anchor} in metadata["members"]:
                return metadata
        self.fail(f"no group contains {anchor_path}#{anchor}")

    def test_fan_out_of_one_root_cause_is_one_group(self) -> None:
        group = self._group_with("reports/settlements/ALPHA/v1.json", "errata:field.0")
        alpha = next(o for o in group["observations"] if o["subject"] == "ALPHA")
        # Three identical reasons plus the "Same …" line that points back.
        self.assertEqual(4, alpha["n_fields"])
        self.assertEqual(1, alpha["n_claim_flagged"])

    def test_identical_specific_sentence_in_two_reports_is_a_candidate(self) -> None:
        group = self._group_with("reports/settlements/BETA/v1.json", "errata:field.0")
        self.assertTrue(group["candidate"])
        self.assertEqual({"reports": 2, "subjects": 2, "inherited": 0}, group["recurrence"])
        self.assertEqual("ALPHA,BETA", group["applies_to"])
        # The earlier report's finding is the group's origin.
        self.assertEqual("reports/settlements/ALPHA/v1.json", group["origin"]["path"])

    def test_a_short_generic_sentence_does_not_merge_reports(self) -> None:
        alpha = self._group_with("reports/settlements/ALPHA/v1.json", "errata:field.5")
        beta = self._group_with("reports/settlements/BETA/v1.json", "errata:field.1")
        self.assertNotEqual(alpha["id"], beta["id"])
        self.assertFalse(alpha["candidate"])

    def test_naming_another_report_merges_into_its_single_errata_group(self) -> None:
        group = self._group_with("reports/settlements/DELTA/v2.json", "errata:field.0")
        self.assertEqual(
            ["GAMMA v2", "DELTA v2"],
            [f"{o['subject']} {o['version']}" for o in group["observations"]],
        )
        self.assertTrue(group["candidate"])
        # The report that names the other is shown as inheriting, not as a
        # second independent occurrence.
        self.assertEqual(1, group["recurrence"]["inherited"])
        self.assertEqual(["GAMMA v2"], group["observations"][1]["inherits_from"])

    def test_the_referenced_report_represents_a_class_group_whatever_the_file_order(self) -> None:
        # AARDVARK sorts first by path but names ZULU as the source of its error.
        self._settlement("AARDVARK", "v1", corrections=[
            {"path": "x", "why": "The yield inherits ZULU v1's unit error.", "cause": "unit"},
        ])
        self._settlement("ZULU", "v1", corrections=[
            {"path": "y", "why": "Local currency was divided by a dollar price.", "cause": "unit"},
        ])
        self._write_config('group_key = "cause"\n')
        group = self._group_with("reports/settlements/ZULU/v1.json", "errata:y")
        self.assertEqual("reports/settlements/ZULU/v1.json", group["origin"]["path"])
        self.assertEqual(
            ["ZULU v1", "AARDVARK v1"],
            [f"{o['subject']} {o['version']}" for o in group["observations"]],
        )
        self.assertEqual(1, group["recurrence"]["inherited"])
        self.assertIn("errata-class-unit", group["id"])

    def test_a_large_unrelated_report_outweighs_a_one_finding_source(self) -> None:
        # ALEPH (1 finding) is named by BET (1 finding); GIMEL has 30 findings
        # of the same class and names nobody. GIMEL shows the error most.
        self._settlement("ALEPH", "v1", corrections=[
            {"path": "a", "why": "The window note was kept from v0.", "cause": "stale"},
        ])
        self._settlement("BET", "v1", corrections=[
            {"path": "b", "why": "Same as ALEPH v1: the window note was kept.", "cause": "stale"},
        ])
        self._settlement("GIMEL", "v1", corrections=[
            {"path": f"g{index}", "why": "The publisher was seeded from v0 and this label was not updated.", "cause": "stale"}
            for index in range(30)
        ])
        self._write_config('group_key = "cause"\n')
        group = self._group_with("reports/settlements/ALEPH/v1.json", "errata:a")
        self.assertEqual("reports/settlements/GIMEL/v1.json", group["origin"]["path"])
        self.assertEqual("GIMEL", group["observations"][0]["subject"])
        self.assertEqual(1, group["recurrence"]["inherited"])

    def test_a_source_always_outweighs_its_own_inheritor(self) -> None:
        # ALEPH has one finding but BET, with thirty, names it: 1 + 30 > 30.
        self._settlement("ALEPH", "v1", corrections=[
            {"path": "a", "why": "The unit was not converted.", "cause": "unit"},
        ])
        self._settlement("BET", "v1", corrections=[
            {"path": f"b{index}", "why": "Inherits ALEPH v1's unconverted unit.", "cause": "unit"}
            for index in range(30)
        ])
        self._write_config('group_key = "cause"\n')
        group = self._group_with("reports/settlements/BET/v1.json", "errata:b0")
        self.assertEqual("reports/settlements/ALEPH/v1.json", group["origin"]["path"])

    def test_without_references_a_report_naming_no_other_represents_the_group(self) -> None:
        self._settlement("AARDVARK", "v1", corrections=[
            {"path": "x", "why": "Same slip as in an unrelated OTHER v9 table.", "cause": "slip"},
        ])
        self._settlement("ZULU", "v1", corrections=[
            {"path": "y", "why": "A figure was left at its default.", "cause": "slip"},
        ])
        self._settlement("OTHER", "v9", corrections=[
            {"path": "z", "why": "Unrelated.", "cause": "elsewhere"},
        ])
        self._write_config('group_key = "cause"\n')
        group = self._group_with("reports/settlements/ZULU/v1.json", "errata:y")
        self.assertEqual("reports/settlements/ZULU/v1.json", group["origin"]["path"])
        self.assertEqual(0, group["recurrence"]["inherited"])

    def test_a_fallback_group_value_is_grouped_by_its_literal_reason(self) -> None:
        self._settlement("ETA", "v1", corrections=[
            {"path": "a", "why": "A chart axis was mislabeled in the appendix.", "cause": "misc"},
        ])
        self._settlement("THETA", "v1", corrections=[
            {"path": "b", "why": "The disclaimer cited last year's filing date.", "cause": "misc"},
        ])
        self._write_config('group_key = "cause"\n')
        merged = self._group_with("reports/settlements/THETA/v1.json", "errata:b")
        self.assertEqual(2, merged["recurrence"]["reports"])
        self._write_config('group_key = "cause"\ngroup_key_fallback_values = ["misc"]\n')
        separate = self._group_with("reports/settlements/THETA/v1.json", "errata:b")
        self.assertEqual(1, separate["recurrence"]["reports"])
        self.assertFalse(separate["candidate"])

    def test_misses_group_by_basis_and_claim_and_count_hits(self) -> None:
        group = self._group_with("reports/settlements/EPSILON/v3.json", "miss:Q2 FY27 revenue")
        self.assertEqual({"misses": 2, "hits": 1}, group["counter"])
        self.assertTrue(group["candidate"])
        margin = self._group_with("reports/settlements/EPSILON/v3.json", "miss:Q2 FY27 margin")
        self.assertEqual({"misses": 1, "hits": 0}, margin["counter"])
        self.assertFalse(margin["candidate"])

    def test_a_configured_group_key_merges_differently_worded_errata(self) -> None:
        self._settlement("ETA", "v1", corrections=[
            {"path": "a", "why": "Stale text carried over from the prior version.", "cause": "stale"},
        ])
        self._settlement("THETA", "v1", corrections=[
            {"path": "b", "why": "前一版的句子沒有更新。", "cause": "stale"},
        ])
        self._write_config('group_key = "cause"\n')
        group = self._group_with("reports/settlements/THETA/v1.json", "errata:b")
        self.assertEqual(2, group["recurrence"]["reports"])
        self.assertTrue(group["candidate"])

    def test_ids_do_not_depend_on_a_finding_position(self) -> None:
        before = self._group_with("reports/settlements/EPSILON/v3.json", "miss:Q2 FY27 revenue")["id"]
        path = self.repo / "reports" / "settlements" / "EPSILON" / "v3.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["corrections"] = [{"path": "new", "why": "A newly noticed typo."}]
        path.write_text(json.dumps(data), encoding="utf-8")
        after = self._group_with("reports/settlements/EPSILON/v3.json", "miss:Q2 FY27 revenue")["id"]
        self.assertEqual(before, after)

    def test_an_unset_section_produces_nothing(self) -> None:
        (self.config_root / "sources.d" / "fixture.toml").write_text(
            f'[source]\nrepo = "{self.repo}"\n'
            '\n[[parser]]\nkind = "settlement"\nglob = "reports/settlements/*/*.json"\n',
            encoding="utf-8",
        )
        self.assertEqual([], self._groups())

    def test_an_unknown_parser_option_is_rejected(self) -> None:
        self._write_config('reason_field = "why"\n')
        with self.assertRaises(FederationConfigError):
            load_source_config(self.config_root, "fixture")


class ProjectIndependenceTests(unittest.TestCase):
    def test_parser_code_names_no_project_specific_field(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "agent_memory" / "federation.py").read_text(
            encoding="utf-8"
        )
        for name in (
            "why_it_is_wrong",
            "consequences_for_the_method",
            "input_provenance",
            "changes_a_claim",
            "house_error",
            "error_class",
            "給 RD",
            "常數區",
            '"ticker"',
        ):
            self.assertNotIn(name, source)


class LessonLabelTests(unittest.TestCase):
    def test_inherited_reports_are_labeled_as_a_fact(self) -> None:
        from agent_memory.core import lesson_label

        label = lesson_label(
            {
                "promoted": False,
                "candidate": True,
                "observations": [
                    {"subject": "ALPHA", "version": "v9"},
                    {"subject": "BETA", "version": "v9"},
                ],
                "recurrence": {"reports": 2, "subjects": 2, "inherited": 1},
                "counter": {},
            }
        )
        self.assertEqual("(候選泛化, 2 reports: ALPHA v9, BETA v9; 1 inherited)", label)
