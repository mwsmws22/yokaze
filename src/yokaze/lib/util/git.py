"""Tiny git helpers shared by deliver metadata and lab expect."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path


def git_repo_root(*, start: Path | None = None) -> Path | None:
    """Nearest ancestor of ``start`` (default: this file) that contains ``.git``."""
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def git_commit_short(*, cwd: Path | None = None) -> str:
    root = cwd if cwd is not None else git_repo_root()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    return (proc.stdout or "").strip() or "unknown"


def git_commit_paths(
    paths: Iterable[Path | str],
    message: str,
    *,
    cwd: Path | None = None,
) -> str:
    """Stage ``paths`` and create one local commit (never pushes). Returns short SHA."""
    root = Path(cwd) if cwd is not None else git_repo_root()
    if root is None:
        raise RuntimeError("git commit: not inside a git repository")
    root = root.resolve()
    rels: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        try:
            rels.append(str(path.relative_to(root)))
        except ValueError as exc:
            raise RuntimeError(f"git commit: path outside repo: {path}") from exc
    if not rels:
        raise RuntimeError("git commit: no paths")
    add = subprocess.run(
        ["git", "add", "--", *rels],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if add.returncode != 0:
        detail = (add.stderr or add.stdout or "").strip() or f"exit {add.returncode}"
        raise RuntimeError(f"git add failed: {detail}")
    commit = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode != 0:
        detail = (commit.stderr or commit.stdout or "").strip() or f"exit {commit.returncode}"
        raise RuntimeError(f"git commit failed: {detail}")
    return git_commit_short(cwd=root)
