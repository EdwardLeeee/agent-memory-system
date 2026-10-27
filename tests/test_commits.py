"""Which commits a session made (spec 騰訊借鏡改善 T6)."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from agent_memory import cli, core, learned  # noqa: E402
import router_baseline  # noqa: E402

SESSION = "5e55a0aa-0000-4000-8000-0000000000aa"


def commit(repo: Path, name: str, title: str, when: datetime | None = None) -> str:
    (repo / name).write_text(title + "\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid",
           "GIT_COMMITTER_NAME": "f", "GIT_COMMITTER_EMAIL": "f@example.invalid"}
    if when is not None:
        env.update(GIT_AUTHOR_DATE=when.isoformat(), GIT_COMMITTER_DATE=when.isoformat())
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", title], check=True, env=env)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def sync_from(folder: Path, session: str | None = SESSION) -> None:
    """What a source's post-commit hook runs, from where the commit was made."""
    before = os.getcwd()
    env = {"CLAUDE_CODE_SESSION_ID": session} if session else {}
    os.chdir(folder)
    try:
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            cli.main(["sources", "sync", "alpha"])
    finally:
        os.chdir(before)


def notes(paths: dict) -> list[dict]:
    path = paths["config"] / "runtime" / "commits.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class CommitNoteTests(unittest.TestCase):
    def test_a_sessions_commit_is_noted_from_its_repository_or_a_worktree_and_nothing_else(self) -> None:
        with router_baseline.environment() as paths:
            alpha = paths["alpha"]
            main = commit(alpha, "kiln.txt", "Fire the kiln")
            sync_from(alpha)
            worktree = paths["base"] / "alpha-wt"
            subprocess.run(["git", "-C", str(alpha), "worktree", "add", "-q", "-b", "glaze", str(worktree)], check=True)
            branch = commit(worktree, "glaze.txt", "Glaze the shelf")
            sync_from(worktree)
            sync_from(worktree)  # the same commit once
            terminal = commit(alpha, "terminal.txt", "From the terminal")
            sync_from(alpha, session=None)  # a commit made in the user's own terminal: nobody's
            commit(alpha, "shelf.txt", "Old shelf", when=datetime.now(timezone.utc) - timedelta(minutes=5))
            sync_from(alpha)  # a sync run by hand on a HEAD five minutes old
            found = notes(paths)
        self.assertEqual([(SESSION, "alpha", main), (SESSION, "alpha", branch), ("", "alpha", terminal)],
                         [(note["session_id"], note["repo"], note["commit"]) for note in found])

    def test_what_the_reflog_says_was_committed_is_noted_though_head_moved_since(self) -> None:
        # The hook runs in the background: by the time sync reads the worktree, HEAD may have moved.
        def now_plus(seconds: int) -> datetime:
            return datetime.now(timezone.utc) + timedelta(seconds=seconds)

        from agent_memory import commits

        with router_baseline.environment() as paths:
            alpha, config = paths["alpha"], paths["config"]
            first = commit(alpha, "b.txt", "Step b")
            second = commit(alpha, "c.txt", "Step c")
            # b's hook, late: both commits are the session's, and both get noted.
            commits.note_commit(config, "alpha", alpha, alpha, SESSION, now_plus(0))
            back_to_back = [note["commit"] for note in notes(paths)]
            third = commit(alpha, "d.txt", "Step d")
            subprocess.run(["git", "-C", str(alpha), "checkout", "-q", "-b", "old", first], check=True)
            # d's hook after a checkout: the commit, not the commit checked out.
            commits.note_commit(config, "alpha", alpha, alpha, SESSION, now_plus(0))
            after_checkout = [note["commit"] for note in notes(paths)]
            fourth = commit(alpha, "e.txt", "Step e")
            subprocess.run(["git", "-C", str(alpha), "commit", "-q", "--amend", "--allow-empty", "-m", "Step e, again"],
                           check=True, env={**os.environ, "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid",
                                            "GIT_COMMITTER_NAME": "f", "GIT_COMMITTER_EMAIL": "f@example.invalid"})
            commits.note_commit(config, "alpha", alpha, alpha, SESSION, now_plus(0))
            after_amend = [note["commit"] for note in notes(paths)]
            head = subprocess.run(["git", "-C", str(alpha), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual([first, second], back_to_back)
        self.assertEqual([first, second, third], after_checkout)
        self.assertNotIn(fourth, after_amend)  # the amend replaced e
        self.assertEqual([first, second, third, head], after_amend)

    def test_a_revert_a_cherry_pick_a_rebase_and_a_merge_commit_are_noted_and_a_fast_forward_is_not(self) -> None:
        from agent_memory import commits

        env = {**os.environ, "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid",
               "GIT_COMMITTER_NAME": "f", "GIT_COMMITTER_EMAIL": "f@example.invalid", "GIT_EDITOR": "true"}
        with router_baseline.environment() as paths:
            alpha, config = paths["alpha"], paths["config"]

            def git(*args: str) -> str:
                return subprocess.run(["git", "-C", str(alpha), *args], check=True, env=env, capture_output=True,
                                      text=True).stdout.strip()

            def noted() -> list[str]:
                commits.note_commit(config, "alpha", alpha, alpha, SESSION, datetime.now(timezone.utc))
                return [note["commit"] for note in notes(paths)]

            trunk = git("rev-parse", "--abbrev-ref", "HEAD")
            git("checkout", "-q", "-b", "side")
            side = commit(alpha, "s.txt", "Side step")
            later = commit(alpha, "t.txt", "Side two")
            git("checkout", "-q", trunk)
            step = commit(alpha, "u.txt", "Trunk step")  # so the cherry-pick makes a new commit
            before = noted()
            git("cherry-pick", side)
            picked = git("rev-parse", "HEAD")
            git("revert", "--no-edit", "HEAD")
            reverted = git("rev-parse", "HEAD")
            git("checkout", "-q", "side")
            git("rebase", "-q", trunk)  # Side step is upstream already; Side two is picked anew
            rebased = git("rev-parse", "HEAD")
            git("checkout", "-q", trunk)
            git("merge", "-q", "--no-ff", "--no-edit", "side")
            merged = git("rev-parse", "HEAD")
            # A fast-forward to a commit made in another worktree makes no commit here.
            worktree = paths["base"] / "alpha-ff"
            git("worktree", "add", "-q", "-b", "elsewhere", str(worktree))
            elsewhere = commit(worktree, "w.txt", "Made elsewhere")
            git("merge", "-q", "elsewhere")
            fast_forward = git("rev-parse", "HEAD")
            after = noted()
        self.assertEqual([side, later, step], before)
        self.assertEqual(elsewhere, fast_forward)
        self.assertEqual([side, later, step, picked, reverted, rebased, merged], after)

    def test_a_cherry_pick_that_remakes_the_same_commit_is_noted_once(self) -> None:
        from agent_memory import commits

        moment = datetime.now(timezone.utc).replace(microsecond=0)
        env = {**os.environ, "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.invalid", "GIT_COMMITTER_NAME": "f",
               "GIT_COMMITTER_EMAIL": "f@example.invalid", "GIT_AUTHOR_DATE": moment.isoformat(),
               "GIT_COMMITTER_DATE": moment.isoformat()}
        with router_baseline.environment() as paths:
            alpha, config = paths["alpha"], paths["config"]

            def git(*args: str) -> str:
                return subprocess.run(["git", "-C", str(alpha), *args], check=True, env=env, capture_output=True,
                                      text=True).stdout.strip()

            trunk = git("rev-parse", "--abbrev-ref", "HEAD")
            git("checkout", "-q", "-b", "side")
            (alpha / "s.txt").write_text("Side step\n", encoding="utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", "Side step")
            side = git("rev-parse", "HEAD")
            git("checkout", "-q", trunk)
            git("cherry-pick", side)  # same parent, tree, message and second
            picked = git("rev-parse", "HEAD")
            commits.note_commit(config, "alpha", alpha, alpha, SESSION, moment)
            noted = [note["commit"] for note in notes(paths)]
        self.assertEqual(side, picked)
        self.assertEqual([side], noted)

    def test_a_records_context_lists_the_commits_its_session_made(self) -> None:
        with router_baseline.environment() as paths:
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION}):
                core.record(paths["memory"], "fact", {"id": "fact.alpha.kiln_fired", "scope": "global", "source": "locally-verified",
                                                      "evidence": "Fired.", "value": "The kiln was fired."})
            made = commit(paths["alpha"], "kiln.txt", "Fire the kiln")
            sync_from(paths["alpha"])
            text = learned.context_text(paths["memory"], "fact.alpha.kiln_fired", paths["base"] / "none", paths["base"] / "none")
        self.assertIn(f"這個 session 做過的 commit：\n- alpha {made[:8]} Fire the kiln", text)


if __name__ == "__main__":
    unittest.main()
