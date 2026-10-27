"""Markdown views of the ledger kept where another tool reads them (spec 實際使用回饋改善 F14)."""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from agent_memory import cli, core, doctor  # noqa: E402
import router_baseline  # noqa: E402

# The words a reader of the file sees, pinned here.
MARKER = "> 由記憶系統自動產生，不要手改；新增或修改請用 memoryctl record／supersede"
CONFIG = """
store = "{store}"

[[target]]
name = "alpha-errors"
path = "{path}"
kind = "failure"
{filters}
intro = "每次被更正，當回合用 memoryctl record 記一筆。"
frontmatter = {{ name = "alpha-errors", description = "the lead's own mistakes, grouped by pattern", type = "feedback" }}

[[target.group]]
tag = "模式1"
title = "模式 1：沒查就說"
"""


def setup(paths: dict, filters: str = 'id_prefix = "failure.alpha.lead_"') -> Path:
    folder = paths["base"] / "agent-notes"
    folder.mkdir()
    target = folder / "alpha-errors.md"
    (paths["config"] / "generate.toml").write_text(CONFIG.format(store=paths["memory"], path=target, filters=filters),
                                                   encoding="utf-8")
    return target


def failure(root: Path, record_id: str, tags: list[str], symptom: str = "Told the user a date nobody had estimated.") -> None:
    core.record(root, "failure", {
        "id": record_id, "scope": "global", "source": "locally-verified", "evidence": "Fixture.", "tags": tags,
        "symptom": symptom, "attempt": "Gave a number.", "root_cause": "Said it before checking.",
        "verified_fix": "Get the estimate from whoever does the work first.", "retry_when": "Before telling the user a date.",
    })


def run_cli(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(list(args))
    return code, out.getvalue()


class ExportTests(unittest.TestCase):
    def test_a_failure_written_for_a_target_lands_in_its_file_and_no_other_does(self) -> None:
        with router_baseline.environment() as paths:
            target = setup(paths)
            failure(paths["memory"], "failure.alpha.lead_unestimated_date", ["模式1"])
            self.assertTrue(target.exists())
            written = target.read_text(encoding="utf-8")
            failure(paths["memory"], "failure.alpha.kiln_cracked", [])
            after = target.read_text(encoding="utf-8")
        self.assertTrue(written.startswith(
            '---\nname: alpha-errors\ndescription: "the lead\'s own mistakes, grouped by pattern"\nmetadata:\n  type: feedback\n---\n\n'
            + MARKER + "\n\n每次被更正，當回合用 memoryctl record 記一筆。\n\n## 模式 1：沒查就說\n\n"
            "### failure.alpha.lead_unestimated_date\n\n第 1 版，"))
        self.assertIn("#### Symptom\n\nTold the user a date nobody had estimated.", written)
        self.assertNotIn("kiln_cracked", after)
        self.assertEqual(written, after)  # nothing it shows changed, so not a byte did

    def test_a_supersede_that_takes_a_record_out_of_a_target_takes_it_off_the_file(self) -> None:
        with router_baseline.environment() as paths:
            target = setup(paths, filters='tag = "lead"')
            failure(paths["memory"], "failure.alpha.unestimated_date", ["lead", "模式1"])
            failure(paths["memory"], "failure.alpha.year_from_memory", ["lead"])
            self.assertTrue(target.exists())
            core.supersede(paths["memory"], "failure.alpha.unestimated_date", {
                "scope": "global", "source": "locally-verified", "evidence": "Fixture.", "tags": ["lead", "模式1"],
                "symptom": "Twice: told the user a date nobody had estimated.", "attempt": "Gave a number.",
                "root_cause": "Said it before checking.", "verified_fix": "Get the estimate first.",
                "retry_when": "Before telling the user a date.", "change_driver": "new-evidence", "change_reason": "A second case.",
            })
            superseded = target.read_text(encoding="utf-8")
            core.supersede(paths["memory"], "failure.alpha.year_from_memory", {
                "scope": "global", "source": "locally-verified", "evidence": "Fixture.", "tags": [],
                "symptom": "Wrote a year from memory.", "attempt": "None.", "root_cause": "Memory.",
                "verified_fix": "Find a source.", "retry_when": "Before writing a year.",
                "change_driver": "new-evidence", "change_reason": "No longer the lead's.",
            })
            moved = target.read_text(encoding="utf-8")
        self.assertIn("### failure.alpha.unestimated_date\n\n第 2 版", superseded)
        self.assertIn("Twice: told the user", superseded)
        self.assertIn("## 其他\n\n### failure.alpha.year_from_memory", superseded)  # no group's tag
        self.assertNotIn("year_from_memory", moved)

    def test_a_file_the_memory_system_did_not_write_is_kept_until_adopted(self) -> None:
        with router_baseline.environment() as paths:
            target = setup(paths)
            target.write_text("# The lead's ledger\n\nWritten by hand.\n", encoding="utf-8")
            failure(paths["memory"], "failure.alpha.lead_unestimated_date", ["模式1"])
            kept = target.read_text(encoding="utf-8")
            code, dry = run_cli("generate", "run", "--adopt", "--dry-run")
            after_dry_run = target.read_text(encoding="utf-8")
            adopted_code, _ = run_cli("generate", "run", "--adopt")
            adopted = target.read_text(encoding="utf-8")
            backups = sorted((paths["config"] / "generate-backups" / "alpha-errors").glob("*-original.md"))
            backed_up = [path.read_text(encoding="utf-8") for path in backups]
        self.assertEqual("# The lead's ledger\n\nWritten by hand.\n", kept)
        self.assertEqual((0, kept), (code, after_dry_run))
        self.assertIn("alpha-errors: adopted (dry run), 1 records; would back up (original)", dry)
        self.assertEqual(0, adopted_code)
        self.assertIn("### failure.alpha.lead_unestimated_date", adopted)
        self.assertEqual(["# The lead's ledger\n\nWritten by hand.\n"], backed_up)

    def test_a_written_file_changed_by_hand_is_backed_up_before_it_is_written_again(self) -> None:
        with router_baseline.environment() as paths:
            target = setup(paths)
            failure(paths["memory"], "failure.alpha.lead_unestimated_date", ["模式1"])
            self.assertTrue(target.exists())
            edited = target.read_text(encoding="utf-8") + "\nA note added by hand.\n"
            target.write_text(edited, encoding="utf-8")
            failure(paths["memory"], "failure.alpha.lead_year_from_memory", ["模式1"])
            rewritten = target.read_text(encoding="utf-8")
            backups = [path.read_text(encoding="utf-8")
                       for path in (paths["config"] / "generate-backups" / "alpha-errors").glob("*-edited.md")]
        self.assertNotIn("A note added by hand.", rewritten)
        self.assertIn("### failure.alpha.lead_year_from_memory", rewritten)
        self.assertEqual([edited], backups)

    def test_the_folder_and_the_file_mode_of_the_other_tool_stay_as_they_were(self) -> None:
        with router_baseline.environment() as paths:
            target = setup(paths)
            target.parent.chmod(0o775)
            failure(paths["memory"], "failure.alpha.lead_unestimated_date", ["模式1"])
            self.assertTrue(target.exists())
            target.chmod(0o664)
            failure(paths["memory"], "failure.alpha.lead_year_from_memory", ["模式1"])
            modes = (target.parent.stat().st_mode & 0o777, target.stat().st_mode & 0o777)
            leftovers = [path.name for path in target.parent.iterdir() if path.name != target.name]
        self.assertEqual((0o775, 0o664), modes)
        self.assertEqual([], leftovers)  # no temporary file, no backup beside the target

    def test_what_is_wrong_with_generate_toml_is_named_and_that_target_left_alone(self) -> None:
        from agent_memory.generate import load_targets

        with router_baseline.environment() as paths:
            entries = {
                "in-store": f'path = "{paths["memory"] / "x.md"}"\nkind = "failure"',
                "in-repo": f'path = "{paths["alpha"] / "x.md"}"\nkind = "failure"',
                "relative": 'path = "notes/x.md"\nkind = "failure"',
                "unfiltered": f'path = "{paths["base"] / "x.md"}"',
                "typo": f'path = "{paths["base"] / "y.md"}"\nkinds = "failure"',
            }
            (paths["config"] / "generate.toml").write_text(f'store = "{paths["memory"]}"\n\n' + "".join(
                f'[[target]]\nname = "{name}"\n{body}\n\n' for name, body in entries.items()), encoding="utf-8")
            targets, problems = load_targets()
        self.assertEqual([], targets)
        for name, words in (("in-store", "inside a memory store"), ("in-repo", "inside a git repository"),
                            ("relative", "absolute path"), ("unfiltered", "needs at least one of"),
                            ("typo", "unknown key 'kinds'")):
            self.assertTrue(any(f"target {name}" in problem and words in problem for problem in problems), (name, problems))

    def test_a_folder_or_file_linked_into_a_project_is_the_project_s(self) -> None:
        from agent_memory.generate import load_targets

        with router_baseline.environment() as paths:
            notes = paths["base"] / "notes"
            notes.mkdir()
            (paths["alpha"] / "docs").mkdir(exist_ok=True)
            (notes / "sub").symlink_to(paths["alpha"] / "docs")
            (notes / "linked.md").symlink_to(paths["alpha"] / "docs" / "linked.md")
            (paths["config"] / "generate.toml").write_text(f'store = "{paths["memory"]}"\n\n' + "".join(
                f'[[target]]\nname = "{name}"\npath = "{path}"\nkind = "failure"\n\n'
                for name, path in (("folder", notes / "sub" / "x.md"), ("file", notes / "linked.md"))), encoding="utf-8")
            targets, problems = load_targets()
        self.assertEqual([], [target["name"] for target in targets])
        for name in ("folder", "file"):
            self.assertTrue(any(f"target {name}" in problem and "inside a git repository" in problem for problem in problems),
                            (name, problems))

    def test_a_copy_of_the_ledger_leaves_the_real_file_alone(self) -> None:
        from agent_memory import replay

        with router_baseline.environment() as paths:
            target = setup(paths)
            failure(paths["memory"], "failure.alpha.lead_unestimated_date", ["模式1"])
            real = target.read_text(encoding="utf-8")
            # The way drafts are tried: a copy of the ledger and its configuration.
            store, config = replay.make_copy(paths["memory"], paths["config"], paths["base"] / "copy")
            with mock.patch.dict(os.environ, {"AGENT_MEMORY_HOME": str(store), "AGENT_MEMORY_CONFIG_HOME": str(config)}):
                failure(store, "failure.alpha.lead_draft_on_the_copy", ["模式1"])
                on_copy = doctor.check_generated(store, config)
            after_copy = target.read_text(encoding="utf-8")
            failure(paths["memory"], "failure.alpha.lead_year_from_memory", ["模式1"])
            rewritten = target.read_text(encoding="utf-8")
            backups = list((paths["config"] / "generate-backups").rglob("*.md"))
        self.assertEqual(real, after_copy)
        self.assertEqual("WARN", on_copy["status"])
        self.assertIn("generate.toml 寫的是另一份帳本", on_copy["detail"])
        self.assertIn("### failure.alpha.lead_year_from_memory", rewritten)
        self.assertNotIn("lead_draft_on_the_copy", rewritten)
        self.assertEqual([], backups)  # the real ledger does not take its own file for one changed by hand

    def test_a_copy_does_not_create_a_file_the_real_ledger_has_not_written_yet(self) -> None:
        from agent_memory import replay

        with router_baseline.environment() as paths:
            target = setup(paths)  # generate.toml says it is the real ledger's
            store, config = replay.make_copy(paths["memory"], paths["config"], paths["base"] / "copy")
            with mock.patch.dict(os.environ, {"AGENT_MEMORY_HOME": str(store), "AGENT_MEMORY_CONFIG_HOME": str(config)}):
                failure(store, "failure.alpha.lead_draft_on_the_copy", ["模式1"])
                _, out = run_cli("generate", "run", "--adopt")
            created_by_copy = target.exists()
            failure(paths["memory"], "failure.alpha.lead_unestimated_date", ["模式1"])
            real = target.read_text(encoding="utf-8")
        self.assertFalse(created_by_copy)
        self.assertIn(f"alpha-errors: not-this-store, 1 records; generate.toml is for the ledger at {paths['memory'].resolve()}", out)
        self.assertNotIn("lead_draft_on_the_copy", real)

    def test_generate_toml_must_say_which_ledger_it_is_for(self) -> None:
        from agent_memory.generate import load_targets

        with router_baseline.environment() as paths:
            (paths["config"] / "generate.toml").write_text(
                f'[[target]]\nname = "x"\npath = "{paths["base"] / "x.md"}"\nkind = "failure"\n', encoding="utf-8")
            targets, problems = load_targets()
        self.assertEqual([], targets)
        self.assertTrue(any("store must name the ledger" in problem for problem in problems), problems)

    def test_a_target_inside_another_ledger_or_its_configuration_is_refused(self) -> None:
        from agent_memory.generate import load_targets

        with router_baseline.environment() as paths:
            other_config, other_store = paths["base"] / "other-config", paths["base"] / "other-store"
            (other_config / "notes").mkdir(parents=True)
            (other_config / "machine.json").write_text("{}\n", encoding="utf-8")
            (other_store / "records").mkdir(parents=True)
            (other_store / "manifest.json").write_text("{}\n", encoding="utf-8")
            (paths["config"] / "generate.toml").write_text(f'store = "{paths["memory"]}"\n\n' + "".join(
                f'[[target]]\nname = "{name}"\npath = "{path}"\nkind = "failure"\n\n'
                for name, path in (("config", other_config / "notes" / "x.md"), ("store", other_store / "x.md"))),
                encoding="utf-8")
            targets, problems = load_targets()
        self.assertEqual([], [target["name"] for target in targets])
        for name in ("config", "store"):
            self.assertTrue(any(f"target {name}" in problem and "inside a memory store or a memory configuration" in problem
                                for problem in problems), (name, problems))

    def test_a_fenced_block_is_kept_as_it_is(self) -> None:
        with router_baseline.environment() as paths:
            target = setup(paths)
            failure(paths["memory"], "failure.alpha.lead_unestimated_date", ["模式1"],
                    symptom="Ran this before answering:\n\n```bash\n# list what shipped\n## not a heading\n\n\ngit log\n```")
            written = target.read_text(encoding="utf-8")
        self.assertIn("```bash\n# list what shipped\n## not a heading\n\n\ngit log\n```", written)
        self.assertIn("#### Symptom", written)  # a heading outside the block still moves under the id

    def test_a_file_in_another_encoding_is_somebody_s_and_the_next_target_is_still_written(self) -> None:
        with router_baseline.environment() as paths:
            target = setup(paths)
            target.write_bytes("# 主管的錯誤\r\n手寫的。\r\n".encode("big5"))
            second = paths["base"] / "agent-notes" / "second.md"
            with (paths["config"] / "generate.toml").open("a", encoding="utf-8") as handle:
                handle.write(f'\n[[target]]\nname = "second"\npath = "{second}"\nkind = "failure"\n')
            failure(paths["memory"], "failure.alpha.lead_unestimated_date", ["模式1"])
            self.assertTrue(second.exists())
            _, out = run_cli("generate", "run", "--adopt", "--name", "alpha-errors")
            backups = [path.read_bytes() for path in (paths["config"] / "generate-backups" / "alpha-errors").glob("*.md")]
        self.assertIn("alpha-errors: adopted", out)
        self.assertEqual(["# 主管的錯誤\r\n手寫的。\r\n".encode("big5")], backups)  # byte for byte, CRLF kept

    def test_two_sessions_writing_at_once_both_end_up_in_the_file(self) -> None:
        import threading

        from agent_memory import generate

        with router_baseline.environment() as paths:
            target = setup(paths)
            failure(paths["memory"], "failure.alpha.lead_unestimated_date", ["模式1"])
            read, first_read, done = generate._active_records, threading.Event(), threading.Event()

            def slow(*args: object, **kwargs: object) -> list:
                records = read(*args, **kwargs)
                if threading.current_thread().name == "first":
                    first_read.set()
                    done.wait(0.5)  # another session writes and regenerates meanwhile
                return records

            with mock.patch.object(generate, "_active_records", slow):
                first = threading.Thread(target=generate.run, args=(paths["memory"],), name="first")
                first.start()
                first_read.wait(5)
                failure(paths["memory"], "failure.alpha.lead_year_from_memory", ["模式1"])
                done.set()
                first.join(10)
            final = target.read_text(encoding="utf-8")
        self.assertIn("### failure.alpha.lead_year_from_memory", final)

    def test_a_target_that_is_a_link_is_refused(self) -> None:
        from agent_memory.generate import load_targets

        with router_baseline.environment() as paths:
            notes = paths["base"] / "notes"
            notes.mkdir()
            (notes / "real.md").write_text("Somebody's notes.\n", encoding="utf-8")
            (notes / "errors.md").symlink_to(notes / "real.md")
            (paths["config"] / "generate.toml").write_text(
                f'store = "{paths["memory"]}"\n\n[[target]]\nname = "linked"\npath = "{notes / "errors.md"}"\nkind = "failure"\n',
                encoding="utf-8")
            targets, problems = load_targets()
            run_cli("generate", "run", "--adopt")
            still_a_link = (notes / "errors.md").is_symlink()
        self.assertEqual([], targets)
        self.assertTrue(any("target linked" in problem and "is a link" in problem for problem in problems), problems)
        self.assertTrue(still_a_link)

    def test_the_weekly_doctor_regenerates_and_warns_on_a_file_left_alone(self) -> None:
        with router_baseline.environment() as paths:
            target = setup(paths)
            failure(paths["memory"], "failure.alpha.lead_unestimated_date", ["模式1"])
            self.assertTrue(target.exists())
            target.unlink()
            created = doctor.check_generated(paths["memory"], paths["config"])
            regenerated = target.exists()
            target.write_text("Written by hand.\n", encoding="utf-8")
            left_alone = doctor.check_generated(paths["memory"], paths["config"])
        self.assertEqual(("OK", "alpha-errors：新建，1 筆"), (created["status"], created["detail"]))
        self.assertTrue(regenerated)
        self.assertEqual("WARN", left_alone["status"])
        self.assertIn("是手寫的檔案，沒有覆蓋", left_alone["detail"])


if __name__ == "__main__":
    unittest.main()
