"""The PreToolUse hook (spec 記憶路由樹 v1.1, 4.2 with review 8.7-8.13).

Runs in the closed fixture environment of tests/router_baseline.py: failure
records written before the index is built, so .cache/triggers.json is a real
reindex product. Subprocess tests go through bin/memoryctl, the line the
adapter installs.
"""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import router_baseline  # noqa: E402
from test_router import put  # noqa: E402
from agent_memory import core, doctor, pretool  # noqa: E402
from agent_memory.lexical_index import command_phrases  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MEMORYCTL = PROJECT_ROOT / "bin" / "memoryctl"
SILENT = {"continue": True}
EXAMPLE = PROJECT_ROOT / "docs" / "router.example.toml"


def failures(memory: Path) -> None:
    put(memory, "failure", "failure.git.reset_soft_onto_moved_base_fixture",
        symptom="git reset --soft origin/main 把別人剛合併的改動一起壓進 commit。",
        fix="壓完先跑 `git diff --name-only` 確認只動自己的路徑，再推。",
        retry_when="下次要用 git reset --soft 壓 commit 之前。")
    put(memory, "failure", "failure.example.pkill_matched_own_shell",
        symptom="pkill -f 依整條指令列比對，殺到自己的 shell。",
        fix="先用 pgrep -a 看清楚再殺 PID。",
        retry_when="要依名稱結束程序時。")
    put(memory, "failure", "failure.fixture.compose_down_volumes", triggers=[r"^docker\s+compose\s+down\b.*-v"],
        symptom="Compose down with volumes wiped the database.", fix="Back up the volume first.")
    put(memory, "failure", "failure.fixture.expired_deploy", expires_at="2000-01-01",
        symptom="make deploy pushed a stale build.", retry_when="Before make deploy.")
    # Learned in project alpha (its id names the project); one judged to
    # apply only there.
    put(memory, "failure", "failure.alpha.purge_without_backup",
        symptom="make purge-cache wiped the shared drive.", retry_when="Before make purge-cache.")
    put(memory, "failure", "failure.alpha.alpha_only_rule", applies="origin",
        symptom="make seed-alpha needs the alpha fixtures.", retry_when="Before make seed-alpha.")
    # Phrases that help queries would match on the old rule (ruling 51 e).
    put(memory, "failure", "failure.fixture.rebase_onto_moving_main",
        symptom="`git rebase` onto a moving main replayed other people's commits.", retry_when="Before every `git rebase`.")
    put(memory, "failure", "failure.fixture.checks_loop_exits_early",
        symptom="A loop around gh pr checks stopped at the first pending run.", retry_when="Before waiting on gh pr checks.")
    # Real commands that only mention --help in a quoted string or a
    # comment, or name a branch called help (ruling 52 c).
    put(memory, "failure", "failure.fixture.commit_all_swept_a_file",
        symptom="`git commit -am` swept a stray build file into the commit.", retry_when="Before every `git commit`.")
    put(memory, "failure", "failure.fixture.pr_against_wrong_base",
        symptom="`gh pr create` opened the PR against the wrong base.", retry_when="Before `gh pr create`.")
    put(memory, "failure", "failure.fixture.checkout_dropped_edits",
        symptom="`git checkout` over local edits discarded them.", retry_when="Before `git checkout`.")
    # Ruling 54: the live free trigger, matched outside quotes and heredocs only.
    look = r"(?:du|df|free|nproc|uptime|echo|cat|ls|ps|top|vmstat|swapon|grep|head|tail)"
    put(memory, "failure", "failure.fixture.free_then_run", symptom="free -m printed and the run went ahead.",
        triggers=[r"\bfree\b[^|;&]*(?:(?:;|&&)\s*" + look + r"\b[^;&]*)*(?:;|&&)\s*(?!" + look + r"\b)[^\s;&|]"])
    put(memory, "failure", "failure.fixture.other_machine", scope="machine:someone-else",
        symptom="systemctl restart broke the other machine.", retry_when="Before systemctl restart.")


def event(command: Any, session: str = "s1", **extra: Any) -> dict[str, Any]:
    payload = {
        "session_id": session,
        "cwd": "/tmp",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": "toolu_1",
    }
    payload.update(extra)
    return payload


def context_of(output: dict[str, Any]) -> str:
    return output.get("hookSpecificOutput", {}).get("additionalContext", "")


def decision_of(output: dict[str, Any]) -> str | None:
    return output.get("hookSpecificOutput", {}).get("permissionDecision")


class PreToolUseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # The held pkill shows the fixture's own failure record (router.toml
        # links it; the built-in list links to no record).
        cls._env = router_baseline.environment(
            extra_records=failures,
            router_toml='[failures.speed_bump_failures]\nkill-by-name = ["failure.example.pkill_matched_own_shell"]\n',
        )
        cls.paths = cls._env.__enter__()
        cls.root = cls.paths["memory"]
        cls.config = cls.paths["config"]
        cls.counter = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls._env.__exit__(None, None, None)

    def session(self) -> str:
        type(self).counter += 1
        return f"session-{self.counter}"

    def decide(self, command: Any, session: str | None = None, agent: str = "claude", **extra: Any) -> dict[str, Any]:
        output, _, _ = pretool.decide(event(command, session or self.session(), **extra), agent)
        return output

    def run_hook(self, stdin: str, agent: str = "claude", env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(MEMORYCTL), "hook", "pre-tool-use", "--agent", agent],
            input=stdin, text=True, capture_output=True, env=env or dict(os.environ), timeout=30,
        )

    def health(self) -> dict[str, Any]:
        return json.loads((self.config / "runtime" / "health" / "pre_tool_use.json").read_text(encoding="utf-8"))

    def write_router(self, text: str) -> None:
        path = self.config / "router.toml"
        before = path.read_text(encoding="utf-8") if path.exists() else None
        path.write_text(text, encoding="utf-8")
        self.addCleanup(lambda: path.write_text(before, encoding="utf-8") if before is not None else path.unlink(missing_ok=True))

    # --- 4.2.4 ---------------------------------------------------------------

    def test_a_failure_whose_phrase_matches_is_injected_once_per_session(self) -> None:
        session = self.session()
        output = self.decide("git reset --soft origin/main", session)
        context = context_of(output)
        self.assertEqual("PreToolUse", output["hookSpecificOutput"]["hookEventName"])
        self.assertNotIn("permissionDecision", output["hookSpecificOutput"])
        self.assertTrue(context.startswith("## FOR THIS COMMAND\n" + core.CITATION_REQUEST))
        self.assertIn("### failure.git.reset_soft_onto_moved_base_fixture\n- kind: failure\n- value: ", context)
        self.assertIn("- symptom: ", context)
        self.assertIn("- retry_when: ", context)
        self.assertTrue(context.endswith("## SOURCE IDS\n- failure.git.reset_soft_onto_moved_base_fixture"))
        self.assertEqual(SILENT, self.decide("git reset --soft origin/main", session))
        self.assertEqual(SILENT, self.decide("ls -la"))

    def test_a_lesson_from_another_project_says_where_it_was_learned(self) -> None:
        outside = context_of(self.decide("make purge-cache", cwd=str(self.paths["neutral"])))
        self.assertIn("### failure.alpha.purge_without_backup", outside)
        line = next(l for l in outside.splitlines() if l.startswith("- 學到的地方："))
        self.assertRegex(line, r"^- 學到的地方：alpha，\d{4}-\d{2}-\d{2}$")
        lines = outside.splitlines()
        self.assertEqual(lines.index(line) - 1, next(i for i, l in enumerate(lines) if l.startswith("- value: ")))
        inside = context_of(self.decide("make purge-cache", cwd=str(self.paths["alpha"])))
        self.assertIn("### failure.alpha.purge_without_backup", inside)
        self.assertNotIn("學到的地方", inside)

    def test_a_lesson_learned_here_and_elsewhere_still_says_where(self) -> None:
        self.assertEqual("- 學到的地方：alpha、beta，2026-10-14",
                         pretool.origin_line(["alpha", "beta"], "2026-10-14T00:00:00+00:00", {"alpha"}))
        self.assertIsNone(pretool.origin_line(["alpha"], "2026-10-14T00:00:00+00:00", {"alpha"}))
        self.assertEqual("- 學到的地方：a、b 等 3 處，2026-10-14",
                         pretool.origin_line(["a", "b", "c"], "2026-10-14", set()))

    def test_applies_origin_triggers_only_in_its_project(self) -> None:
        self.assertEqual(SILENT, self.decide("make seed-alpha", cwd=str(self.paths["neutral"])))
        self.assertIn("### failure.alpha.alpha_only_rule", context_of(self.decide("make seed-alpha", cwd=str(self.paths["alpha"]))))

    def test_read_only_git_phrases_never_trigger(self) -> None:
        phrases = command_phrases("壓完先跑 `git diff --stat` 和 git show --stat，再 git reset --soft；git diff --quiet 確認")
        self.assertEqual(["git reset --soft"], phrases)
        # A phrase already in an older triggers.json is skipped at match time too.
        entries = [{"id": "failure.x.old", "scope": "global", "phrases": ["git diff --stat", "git show --stat"]}]
        self.assertEqual([], pretool.trigger_hits("git diff --stat HEAD~1", entries, None))
        self.assertEqual([], pretool.trigger_hits("git show --stat", entries, None))

    def test_help_and_version_queries_never_inject(self) -> None:
        # Each of these matches a fixture phrase (git rebase, gh pr checks,
        # git reset --soft), so the old rule injected them.
        for command in (
            "git rebase --help", "git rebase -h", "git rebase --onto x --help", "gh pr checks --help 2>&1",
            "git reset --soft --help", "gh --version 2>&1", "gh pr checks --help > /dev/null",
        ):
            with self.subTest(command=command):
                self.assertEqual(SILENT, self.decide(command))
        self.assertIn("### failure.fixture.rebase_onto_moving_main", context_of(self.decide("git rebase origin/main")))
        self.assertIn("### failure.fixture.checks_loop_exits_early", context_of(self.decide("gh pr checks 12 2>&1")))
        self.assertIn("### failure.git.reset_soft", context_of(self.decide("git reset --soft HEAD~1")))
        phrases = command_phrases("先跑 gh --version，`git push --help` 看參數，再寫 gh pr checks 的迴圈")
        self.assertNotIn("gh --version", phrases)
        self.assertNotIn("git push --help", phrases)
        self.assertIn("gh pr checks", phrases)

    def test_help_in_a_quoted_string_or_a_comment_is_no_help_query(self) -> None:
        for command, record_id in (
            ('git commit -am "Document the --help output"', "failure.fixture.commit_all_swept_a_file"),
            ('gh pr create --body "run x --help"', "failure.fixture.pr_against_wrong_base"),
            ("git reset --soft HEAD~3  # see --help", "failure.git.reset_soft_onto_moved_base_fixture"),
            ("gh pr checks 12 --watch # docs say --help", "failure.fixture.checks_loop_exits_early"),
            ("git checkout help", "failure.fixture.checkout_dropped_edits"),
            ("git rebase origin/main#--help", "failure.fixture.rebase_onto_moving_main"),
        ):
            with self.subTest(command=command):
                self.assertIn(f"### {record_id}", context_of(self.decide(command)))
        for command in ("git commit --help", 'gh pr create "--help"', "git help", "gh version 2>&1", "git checkout -h"):
            with self.subTest(command=command):
                self.assertEqual(SILENT, self.decide(command))
        self.assertIn("git checkout help", command_phrases("run git checkout help to switch"))
        self.assertNotIn("git checkout --help", command_phrases("run `git checkout --help` first"))

    def test_a_phrase_must_start_a_command_segment(self) -> None:
        self.assertIn("### failure.git.reset_soft", context_of(self.decide("cd repo && git reset --soft origin/main")))
        self.assertIn("### failure.git.reset_soft", context_of(self.decide("sudo git reset --soft HEAD~2")))
        for command in (
            'echo "git reset --soft origin/main"',
            "python3 - <<'EOF'\nprint('git reset --soft origin/main')\nEOF",
            "rg -n 'git reset --soft' docs/",
        ):
            with self.subTest(command=command):
                self.assertEqual(SILENT, self.decide(command))

    def test_an_explicit_trigger_matches_after_leading_assignments(self) -> None:
        context = context_of(self.decide("COMPOSE_FILE=x.yml docker   compose down  -v"))
        self.assertIn("### failure.fixture.compose_down_volumes", context)

    def test_verified_fix_commands_are_not_triggers(self) -> None:
        # The fix names `git diff --name-only`; running it is doing the right thing.
        self.assertIsNone(decision_of(self.decide("git diff --name-only HEAD~1..HEAD")))
        self.assertNotIn("FOR THIS COMMAND", context_of(self.decide("git diff --name-only HEAD~1..HEAD")))

    def test_expired_and_foreign_machine_failures_are_not_injected(self) -> None:
        self.assertEqual(SILENT, self.decide("make deploy"))
        self.assertEqual(SILENT, self.decide("systemctl restart nginx"))

    def test_speed_bumps_deny_once_then_pass(self) -> None:
        session = self.session()
        first = self.decide("git push --force", session)
        self.assertEqual("deny", decision_of(first))
        reason = first["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("再執行一次就會放行", reason)
        self.assertIn("強制推送會覆蓋遠端歷史", reason)
        self.assertNotIn("additionalContext", first["hookSpecificOutput"])
        self.assertIsNone(decision_of(self.decide("git push -f origin x", session)))
        for command in ("git push", "git push origin main", "git push -u origin main"):
            self.assertIsNone(decision_of(self.decide(command)))

    def test_recursive_rm_without_force_is_denied_once(self) -> None:
        session = self.session()
        first = self.decide("rm -r build", session)
        self.assertEqual("deny", decision_of(first))
        self.assertIn("遞迴刪除無法復原", first["hookSpecificOutput"]["permissionDecisionReason"])
        # Same id, so the forced form passes too once confirmed.
        self.assertIsNone(decision_of(self.decide("rm -rf build", session)))

    def test_with_speed_bumps_off_nothing_is_ever_denied(self) -> None:
        self.write_router("[failures]\nspeed_bump_enabled = false\n")
        for command in ("git push --force", "rm -rf x", "git reset --hard"):
            self.assertIsNone(decision_of(self.decide(command)))

    def test_a_released_speed_bump_injects_its_failure_record(self) -> None:
        session = self.session()
        first = self.decide("pkill -f vite", session)
        self.assertIn("failure.example.pkill_matched_own_shell：", first["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIn("依據 failure.example.pkill_matched_own_shell", first["hookSpecificOutput"]["permissionDecisionReason"])
        second = self.decide("pkill -f vite", session)
        self.assertIsNone(decision_of(second))
        self.assertIn("### failure.example.pkill_matched_own_shell", context_of(second))

    def test_a_link_to_an_unknown_speed_bump_is_reported(self) -> None:
        self.write_router('[failures.speed_bump_failures]\nno-such-bump = ["failure.example.x"]\n')
        self.assertIn("failures.speed_bump_failures names no speed bump: no-such-bump", core.router_toml_problems())

    def test_router_disabled_stops_injection_but_not_speed_bumps(self) -> None:
        self.write_router("enabled = false\n")
        self.assertEqual(SILENT, self.decide("git reset --soft origin/main"))
        self.assertEqual("deny", decision_of(self.decide("git reset --hard")))

    # --- 8.10 ----------------------------------------------------------------

    def test_commands_that_only_mention_danger_are_never_denied(self) -> None:
        for command in (
            "git push origin main && rm -f tmp", 'rg -n "git reset --hard" docs/', 'echo "rm -rf"',
            'rg "DROP TABLE" src/', "rm -f --verbose x", "rm -f x", "rm x", "git clean -n",
            "git restore --staged .", "git commit -m 'git reset --hard' && git push",
            "git push origin main 2>&1 | tail -1", "kill 1234", "cat <<EOF\ngit reset --hard\nEOF",
        ):
            with self.subTest(command=command):
                self.assertIsNone(decision_of(self.decide(command)))

    def test_every_dangerous_form_is_denied_the_first_time(self) -> None:
        for command in (
            "kill $(pgrep -f x)", "pgrep -f x | xargs kill", "rm -Rf x", "rm -r -f x", "rm --recursive --force x",
            "rm -r build", "rm --recursive build",
            'psql -c "drop table t"', "git checkout -- . && make", "git push origin +main", "git push origin main -fu",
            "FOO=1 sudo git reset --hard", "git -C ~/repo reset --hard", "/usr/bin/git reset --hard",
            "find . -name x -exec rm -rf {} \\;", "find . -name x -exec rm -rf {} +", 'bash -c "git reset --hard"',
            "nice -n 10 rm -rf x", "timeout -k 5 30 rm -rf x", "git reset --hard 2>&1", "git clean -fd",
            "git branch -D old", "git stash drop", "herdr server stop", "gh repo delete x",
            "gh api -X DELETE /repos/x", "podman rm -f c", "dropdb x", "bash <<EOF\ngit reset --hard\nEOF",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", decision_of(self.decide(command)))

    def test_an_explicit_trigger_ignores_quotes_heredocs_and_comments(self) -> None:
        # Ruling 54.
        for command in (
            "python3 - <<'EOF'\nprint(\"free -m && pytest -q\")\nEOF",
            'git commit -m "free -m && make"',
            "echo 'free -m; npm test'",
            "ls  # free -m && pytest -q",
        ):
            with self.subTest(command=command):
                self.assertNotIn("failure.fixture.free_then_run", context_of(self.decide(command)))
        for command in (
            "free -m && pytest -q", "free -m && pytest -q  # it's fine", "bash <<EOF\nfree -m && pytest -q\nEOF",
            'FOO="a b" free -m; make test',
        ):
            with self.subTest(command=command):
                self.assertIn("### failure.fixture.free_then_run", context_of(self.decide(command)))
        context = context_of(self.decide('COMPOSE_FILE="x y.yml" docker   compose down  -v'))
        self.assertIn("### failure.fixture.compose_down_volumes", context)

    def test_an_apostrophe_in_a_comment_keeps_the_rules_working(self) -> None:
        # Ruling 53 a: shlex failed on the comment's apostrophe and the whole
        # line, no longer starting with the command word, was matched.
        for command in (
            "cd repo && git reset --hard origin/main  # it's clean",
            "FOO=1 git reset --hard  # it's clean", "sudo git reset --hard # don't care",
            "git -C ~/repo reset --hard # it's fine", "git status; git reset --hard # won't hurt\necho done",
            'cd repo && git reset --hard origin/main && echo "oops',
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", decision_of(self.decide(command)))
        for command in ("git reset --soft HEAD~1  # don't lose the work", "cd x && git reset --soft HEAD~2 # it's ok"):
            with self.subTest(command=command):
                self.assertIn("### failure.git.reset_soft", context_of(self.decide(command)))
        # A quoted or mid-word # is no comment.
        self.assertEqual("deny", decision_of(self.decide('echo "# it\'s" && git reset --hard')))
        self.assertEqual(["echo a#b", "git reset --hard"], pretool.command_segments("echo a#b; git reset --hard"))
        self.assertEqual(["echo it's", "git reset --hard"],
                         pretool.command_segments("echo \"it's\" # note\ngit reset --hard"))

    # --- 8.12 ----------------------------------------------------------------

    def test_two_speed_bumps_in_one_command_are_denied_together(self) -> None:
        session = self.session()
        first = self.decide("git reset --hard && git clean -fd", session)
        reason = first["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("git reset --hard 會丟掉", reason)
        self.assertIn("git clean 會刪掉", reason)
        self.assertIsNone(decision_of(self.decide("git reset --hard && git clean -fd", session)))
        folder = pretool.state_dir(session, None)
        state = json.loads((folder / "bump.git-reset-hard.json").read_text(encoding="utf-8"))
        self.assertEqual((1, 1), (state["deny_count"], state["pass_count"]))
        self.assertEqual(state["first_command_sha256"], state["released_command_sha256"])
        self.assertNotIn("git reset", json.dumps(state))

    def test_subagents_have_their_own_markers(self) -> None:
        session = self.session()
        self.assertEqual("deny", decision_of(self.decide("git reset --hard", session, agent_id="sub-1")))
        self.assertEqual("deny", decision_of(self.decide("git reset --hard", session)))

    def test_an_unwritable_state_folder_never_denies(self) -> None:
        parent = self.config / "runtime" / "pretooluse"
        parent.mkdir(parents=True, exist_ok=True)
        parent.chmod(0o000)
        try:
            output, status, code = pretool.decide(event("pkill -f vite", self.session()), "claude")
        finally:
            parent.chmod(0o700)
        self.assertIsNone(decision_of(output))
        self.assertIn("### failure.example.pkill_matched_own_shell", context_of(output))
        self.assertEqual(("error", "state-write-failed"), (status, code))

    def test_a_failure_named_in_the_prompt_is_not_injected_again(self) -> None:
        session = self.session()
        prompt = {
            "session_id": session, "cwd": str(self.paths["neutral"]), "hook_event_name": "UserPromptSubmit",
            "prompt": "上次 failure.git.reset_soft_onto_moved_base_fixture 怎麼修", "prompt_id": "p1",
        }
        output = core.handle_hook(self.root, "user-prompt-submit", prompt, agent="claude")
        self.assertIn("### failure.git.reset_soft_onto_moved_base_fixture", output["hookSpecificOutput"]["additionalContext"])
        marker = pretool.state_dir(session, None) / "injected.failure.git.reset_soft_onto_moved_base_fixture.json"
        self.assertEqual("UserPromptSubmit", json.loads(marker.read_text(encoding="utf-8"))["event"])
        self.assertEqual(SILENT, self.decide("git reset --soft origin/main", session))

    def test_old_session_folders_are_pruned_when_a_new_one_starts(self) -> None:
        parent = self.config / "runtime" / "pretooluse"
        stale = parent / "stale-session" / "main"
        stale.mkdir(parents=True)
        (stale / "bump.x.json").write_text("{}", encoding="utf-8")
        old = time.time() - 15 * 24 * 3600
        os.utime(stale.parent, (old, old))
        self.decide("git reset --hard")
        self.assertFalse((parent / "stale-session").exists())

    # --- 8.13 ----------------------------------------------------------------

    def test_a_broken_pattern_skips_only_its_entry(self) -> None:
        text = EXAMPLE.read_text(encoding="utf-8").replace("pattern = '^rm(?=", "pattern = '^rm((?=", 1)
        self.write_router(text)
        output, status, code = pretool.decide(event("git push --force", self.session()), "claude")
        self.assertEqual("deny", decision_of(output))
        self.assertEqual("router-toml-invalid", code)
        self.assertIsNone(decision_of(self.decide("rm -rf x")))
        self.assertIn("rm-rf", "\n".join(core.router_toml_problems()))

    def test_invalid_toml_behaves_like_no_file(self) -> None:
        self.write_router('[[failures.speed_bump]]\nid = "x"\npattern = "\\s"\nreason = "x"\n')
        output, _, code = pretool.decide(event("git push --force", self.session()), "claude")
        self.assertEqual("deny", decision_of(output))
        self.assertEqual("router-toml-invalid", code)

    # --- 8.8 -----------------------------------------------------------------

    def test_every_bad_input_is_silent_with_exit_zero(self) -> None:
        for stdin in (
            "not json", "[1, 2]", json.dumps({**event("ls"), "hook_event_name": "PostToolUse"}),
            json.dumps({**event("ls"), "tool_input": {}}), json.dumps({**event("ls"), "tool_name": "Read"}),
            json.dumps(event(["git", "reset"])),
        ):
            with self.subTest(stdin=stdin[:40]):
                result = self.run_hook(stdin)
                self.assertEqual(0, result.returncode)
                self.assertEqual(SILENT, json.loads(result.stdout))

    def test_errors_and_the_time_limit_end_in_silence(self) -> None:
        def run(**patches: Any) -> dict[str, Any]:
            out = io.StringIO()
            with mock.patch.multiple(pretool, **patches):
                code = pretool.main(["--agent", "claude"], io.StringIO(json.dumps(event("git push --force", self.session()))), out)
            self.assertEqual(0, code)
            return json.loads(out.getvalue())

        self.assertEqual(SILENT, run(load_router_settings=mock.Mock(side_effect=OSError("disk"))))
        self.assertEqual(SILENT, run(_load_triggers=mock.Mock(side_effect=OSError("disk"))))
        self.assertEqual(SILENT, run(speed_bump_hits=mock.Mock(side_effect=RuntimeError("boom"))))
        self.assertEqual("error", self.health()["status"])

        def slow(event: dict[str, Any], agent: str) -> Any:
            time.sleep(1)
            return {"continue": True, "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}}, "ok", None

        out = io.StringIO()
        with mock.patch.object(pretool, "TIME_LIMIT_SECONDS", 0.1):
            self.assertEqual(0, pretool.main([], io.StringIO(json.dumps(event("ls"))), out, decide_fn=slow))
        self.assertEqual(SILENT, json.loads(out.getvalue()))
        self.assertEqual("time-limit", self.health()["code"])

    def test_a_codex_shaped_payload_is_served(self) -> None:
        payload = {**event("git reset --soft origin/main", self.session()), "turn_id": "turn-1", "tool_use_id": "call_1"}
        payload.pop("cwd")
        result = self.run_hook(json.dumps(payload), agent="codex")
        self.assertEqual(0, result.returncode)
        self.assertIn("### failure.git.reset_soft", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])

    # --- 8.9 -----------------------------------------------------------------

    def test_the_entry_point_imports_nothing_heavy(self) -> None:
        result = subprocess.run(
            [sys.executable, "-X", "importtime", str(MEMORYCTL), "hook", "pre-tool-use", "--agent", "claude"],
            input=json.dumps(event("git reset --soft origin/main", self.session())),
            text=True, capture_output=True, timeout=30,
        )
        json.loads(result.stdout)
        for module in ("agent_memory.core", "agent_memory.adapters", "agent_memory.federation", "agent_memory.lexical_index",
                       "agent_memory.semantic", "agent_memory.semantic_document", "agent_memory.cli", "sqlite3", "argparse"):
            self.assertFalse(
                [line for line in result.stderr.splitlines() if line.rstrip().endswith(f" {module}")], module
            )

    def test_the_cli_path_answers_too(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "agent_memory.cli", "hook", "pre-tool-use", "--agent", "claude"],
            input=json.dumps(event("git push --force", self.session())), text=True, capture_output=True,
            cwd=PROJECT_ROOT, env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}, timeout=30,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual("deny", decision_of(json.loads(result.stdout)))
        self.assertEqual(
            "deny",
            decision_of(core.handle_hook(self.root, "pre-tool-use", event("git reset --hard", self.session()), agent="claude")),
        )


class TriggerExtractionTests(unittest.TestCase):
    def test_phrases_are_command_words_with_a_subcommand_or_flag(self) -> None:
        phrases = command_phrases(
            "跑 `git diff --name-only` 再推；別用 git reset --soft origin/main，也別 pkill -f。"
            " Use gh pr checks and apt-get install. `rm` alone is not a trigger; git and gh."
        )
        for phrase in ("git reset --soft", "pkill -f", "gh pr checks", "apt-get install"):
            self.assertIn(phrase, phrases)
        self.assertNotIn("rm", phrases)
        self.assertFalse([p for p in phrases if p in ("git", "git and", "gh")])

    def test_the_stoplist_and_non_commands_are_never_phrases(self) -> None:
        from agent_memory.storage import ROUTER_DEFAULT_PHRASE_STOPLIST

        phrases = command_phrases(
            "跑 git status 再 git diff --name-only，然後 git add -A；別碰 `.venv` 或 `name/`。", ROUTER_DEFAULT_PHRASE_STOPLIST
        )
        # git diff is a read-only check (ruling 49), not a phrase.
        self.assertEqual(["git add -A"], phrases)

    def test_a_failure_write_refreshes_the_triggers_and_bad_triggers_are_refused(self) -> None:
        with router_baseline.environment(extra_records=failures) as paths:
            root = paths["memory"]
            payload = {
                "id": "failure.fixture.new_rsync", "scope": "global", "source": "locally-verified", "evidence": "Test.",
                "symptom": "rsync --delete removed the backup.", "attempt": "a.", "root_cause": "r.",
                "verified_fix": "Dry-run first.", "retry_when": "Before rsync --delete.",
                "triggers": [r"^rsync\b.*--delete"],
            }
            core.record(root, "failure", payload)
            entries = json.loads((root / ".cache" / "triggers.json").read_text(encoding="utf-8"))["entries"]
            entry = next(e for e in entries if e["id"] == "failure.fixture.new_rsync")
            self.assertEqual([r"^rsync\b.*--delete"], entry["explicit"])
            self.assertIn("rsync --delete", entry["phrases"])
            with self.assertRaises(core.MemoryError):
                core.record(root, "failure", {**payload, "id": "failure.fixture.bad_regex", "triggers": ["(unclosed"]})
            path = root / "records" / "failure" / "failure.fixture.new_rsync.md"
            metadata, body = core.decode_record(path)
            path.write_text(core.encode_record({**metadata, "triggers": ["(unclosed"]}, body), encoding="utf-8")
            _, errors = core.validate(root)
            self.assertTrue([e for e in errors if "failure trigger does not compile" in e])


class FailureLayerReportTests(unittest.TestCase):
    def test_state_files_become_the_weekly_lines(self) -> None:
        now = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            folder = config / "runtime" / "pretooluse" / "s1" / "main"
            folder.mkdir(parents=True)
            (folder / "bump.git-force-push.json").write_text(json.dumps({
                "first_at": "2026-10-10T00:00:00+00:00", "first_command_sha256": "a", "deny_count": 1,
                "released_at": "2026-10-10T00:01:00+00:00", "released_command_sha256": "b", "pass_count": 1,
            }), encoding="utf-8")
            (folder / "bump.rm-rf.json").write_text(json.dumps({
                "first_at": "2026-09-01T00:00:00+00:00", "deny_count": 5,
            }), encoding="utf-8")
            (folder / "injected.failure.x.json").write_text(json.dumps({
                "at": "2026-10-11T00:00:00+00:00", "event": "PreToolUse",
            }), encoding="utf-8")
            (folder / "injected.failure.y.json").write_text(json.dumps({
                "at": "2026-10-11T00:00:00+00:00", "event": "UserPromptSubmit",
            }), encoding="utf-8")
            signal = doctor.failure_layer_signal(config, now)
        self.assertEqual(1, signal["injected"])
        self.assertEqual({"git-force-push": {"denied": 1, "released": 1, "changed": 1}}, signal["speed_bumps"])


if __name__ == "__main__":
    unittest.main()
