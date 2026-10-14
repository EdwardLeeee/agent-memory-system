"""Repository-level history for the canonical ledger.

Git is an audit surface, not the source of truth: a store works without it, so
nothing here may raise into a write path that has already committed to disk.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

from .storage import MemoryError
from .storage import git_environment


def _run_git(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments], env=git_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise MemoryError(f"git {' '.join(arguments)} failed: {detail}")
    return result


def git_commit_all(root: Path, message: str) -> bool:
    if not (root / ".git").is_dir():
        return False
    _run_git(root, ["add", "-A"])
    staged = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--quiet"], env=git_environment(),
        check=False,
    )
    if staged.returncode == 0:
        return False
    if staged.returncode != 1:
        raise MemoryError("git could not inspect staged memory changes")
    _run_git(
        root,
        [
            "-c",
            "user.name=Agent Memory",
            "-c",
            "user.email=memory@localhost",
            "commit",
            "--no-gpg-sign",
            "-m",
            message,
        ],
    )
    return True


def git_commit_durable(root: Path, message: str) -> bool:
    if not (root / ".git").is_dir():
        return False
    paths = [
        relative
        for relative in (
            "manifest.json",
            "records",
            "archive",
            "events",
            "sessions",
            "migration",
        )
        if (root / relative).is_file()
        or (
            (root / relative).is_dir()
            and any(path.is_file() for path in (root / relative).rglob("*"))
        )
    ]
    if not paths:
        return False
    _run_git(root, ["add", "-A", "--", *paths])
    staged = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--quiet", "--", *paths], env=git_environment(),
        check=False,
    )
    if staged.returncode == 0:
        return False
    if staged.returncode != 1:
        raise MemoryError("git could not inspect staged durable-memory changes")
    _run_git(
        root,
        [
            "-c",
            "user.name=Agent Memory",
            "-c",
            "user.email=memory@localhost",
            "commit",
            "--no-gpg-sign",
            "-m",
            message,
            "--",
            *paths,
        ],
    )
    return True


def ensure_git_clean(root: Path, action: str) -> None:
    status = _run_git(root, ["status", "--porcelain", "--untracked-files=normal"])
    if status.stdout.strip():
        raise MemoryError(
            f"cannot {action} with uncommitted repository changes; "
            "commit or remove them first"
        )
