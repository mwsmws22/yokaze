"""Stage pin gates: allowlists, registry, sync plan, entrypoint helpers."""

from __future__ import annotations

from yokaze.lib.pins.allowlists import ALLOWLISTS
from yokaze.lib.pins.policy import (
    SyncPlan,
    allowlist_matches,
    plan_sync_geometry,
    require_clean_for,
    require_pins_match,
)
from yokaze.lib.pins.registry import (
    STAGES,
    bump_pins,
    git_head_sha,
    load_stage_pins,
    stage_pins_path,
    stage_pins_repo_root,
    write_stage_pins,
)

__all__ = [
    "ALLOWLISTS",
    "STAGES",
    "SyncPlan",
    "allowlist_matches",
    "bump_pins",
    "git_head_sha",
    "load_stage_pins",
    "plan_sync_geometry",
    "require_clean_for",
    "require_pins_match",
    "stage_pins_path",
    "stage_pins_repo_root",
    "write_stage_pins",
]
