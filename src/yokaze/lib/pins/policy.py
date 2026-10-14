"""Dirty + pin match gates; sync plan for geometry catch-up."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from yokaze.lib.metadata import require_clean_tree
from yokaze.lib.pins.allowlists import ALLOWLISTS
from yokaze.lib.pins.registry import load_stage_pins
from yokaze.lib.util.errors import YokazeError

SyncPlan = Literal["full", "crop_only", "noop"]


def allowlist_matches(*, repo: Path, stage: str, pin: str, head: str) -> bool:
    paths = ALLOWLISTS[stage]
    proc = subprocess.run(
        ["git", "diff", "--quiet", pin, head, "--", *paths],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
    raise RuntimeError(f"git diff failed for stage {stage} (pin {pin}): {detail}")


def plan_sync_geometry(*, repo: Path, pins: dict[str, str], head: str) -> SyncPlan:
    brod_ok = allowlist_matches(repo=repo, stage="brod", pin=pins["brod"], head=head)
    crop_ok = allowlist_matches(repo=repo, stage="crop", pin=pins["crop"], head=head)
    if not brod_ok:
        return "full"
    if not crop_ok:
        return "crop_only"
    return "noop"


def require_clean_for(action: str, *, repo: Path) -> None:
    try:
        require_clean_tree(cwd=repo, action=action)
    except RuntimeError as exc:
        raise YokazeError(str(exc)) from exc


def require_pins_match(
    stages: Iterable[str],
    *,
    repo: Path,
    head: str | None = None,
) -> None:
    try:
        pins = load_stage_pins(repo)
    except RuntimeError as exc:
        raise YokazeError(str(exc)) from exc
    head_ref = head if head is not None else "HEAD"
    try:
        mismatched = [
            stage
            for stage in stages
            if not allowlist_matches(repo=repo, stage=stage, pin=pins[stage], head=head_ref)
        ]
    except RuntimeError as exc:
        raise YokazeError(str(exc)) from exc
    if mismatched:
        joined = ", ".join(mismatched)
        raise YokazeError(
            f"refusing action: stage pin(s) out of date for {joined} "
            f"(run sync-geometry or tune finish as needed)"
        )
