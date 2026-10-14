"""Load/save/validate ``data/stage-pins.json``; bump helpers."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from pathlib import Path

STAGES: tuple[str, ...] = ("brod", "crop", "tone")


def stage_pins_repo_root() -> Path:
    """Checkout root holding ``data/stage-pins.json`` (dev layout)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "data").is_dir():
            return parent
    return here.parents[4]


def stage_pins_path(repo: Path) -> Path:
    return repo / "data" / "stage-pins.json"


def git_head_sha(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def load_stage_pins(repo: Path) -> dict[str, str]:
    path = stage_pins_path(repo)
    if not path.is_file():
        raise RuntimeError(f"missing stage-pins registry: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"corrupt stage-pins registry: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"corrupt stage-pins registry: {path}")
    missing = [stage for stage in STAGES if not isinstance(data.get(stage), str) or not data[stage]]
    if missing:
        raise RuntimeError(
            f"incomplete stage-pins registry: missing or invalid {', '.join(missing)}"
        )
    return {stage: data[stage] for stage in STAGES}


def write_stage_pins(repo: Path, pins: dict[str, str]) -> None:
    missing = [stage for stage in STAGES if stage not in pins]
    if missing:
        raise RuntimeError(
            f"incomplete stage-pins write: missing {', '.join(missing)}"
        )
    path = stage_pins_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {stage: pins[stage] for stage in STAGES}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def bump_pins(repo: Path, *, stages: Iterable[str], sha: str) -> dict[str, str]:
    pins = load_stage_pins(repo)
    updated = dict(pins)
    for stage in stages:
        updated[stage] = sha
    write_stage_pins(repo, updated)
    return updated
