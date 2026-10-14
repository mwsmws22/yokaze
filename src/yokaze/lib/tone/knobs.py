"""Package-owned tone/finish knobs (tone/balance/sharpen).

Production reads ``default.conf`` next to this module — never ``data/``,
never a user ``environment.d`` overlay. Tune Save and profile promote
overwrite that file (full knob copy + ``PROFILE=`` meta).
"""

from __future__ import annotations

from pathlib import Path

PROFILE_META_KEY = "PROFILE"

# Shipped product knobs; lives under lib/tone/ so the tone pin allowlist covers it.
FINISH_DEFAULT_PATH = Path(__file__).resolve().parent / "default.conf"

# Fallbacks when the package file is missing or a key is absent (pre-calibration Base).
FINISH_KNOB_FALLBACKS: dict[str, float] = {
    "TONE_BLACK_PCT": 6.0,
    "TONE_WHITE_PCT": 90.0,
    "TONE_GAMMA": 1.2,
    "TONE_CONTRAST": 1.0,
    "TONE_SATURATION": 1.0,
    "TONE_BRIGHTNESS": 1.0,
    "TONE_BALANCE_R": 1.0,
    "TONE_BALANCE_G": 1.0,
    "TONE_BALANCE_B": 1.0,
    "SHARPEN_AMOUNT": 0.0,
    "SHARPEN_RADIUS": 1.0,
    "SHARPEN_THRESHOLD": 0.0,
}

_KNOB_KEYS = tuple(FINISH_KNOB_FALLBACKS.keys())


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_env_file(path: Path, values: dict[str, str], *, header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {header}"]
    lines += [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n")


def knobs_only(values: dict[str, str]) -> dict[str, str]:
    """Drop meta keys (e.g. PROFILE) that must not be applied as finish knobs."""
    return {k: v for k, v in values.items() if k != PROFILE_META_KEY}


def package_knob_strings(*, path: Path | None = None) -> dict[str, str]:
    """String knobs from the package default file (no PROFILE)."""
    return knobs_only(parse_env_file(path or FINISH_DEFAULT_PATH))


def load_finish_knobs(*, path: Path | None = None) -> dict[str, float]:
    """Numeric finish knobs for the real scan path.

    Reads the package default only. ``PROFILE`` is ignored. Missing file or
    keys fall back to pre-calibration Base literals.
    """
    raw = package_knob_strings(path=path)
    out: dict[str, float] = {}
    for key, default in FINISH_KNOB_FALLBACKS.items():
        text = raw.get(key)
        if text is None or text == "":
            out[key] = default
            continue
        try:
            out[key] = float(text)
        except ValueError:
            out[key] = default
    # Amount 0 when SHARPEN is explicitly off (Save writes the flag).
    sharpen_flag = raw.get("SHARPEN")
    if sharpen_flag is not None and sharpen_flag.lower() in {"0", "false", "no"}:
        out["SHARPEN_AMOUNT"] = 0.0
    return out


def write_finish_default(
    values: dict[str, str],
    *,
    profile: str | None = None,
    path: Path | None = None,
) -> None:
    """Overwrite the shipped finish default (knob copy + optional PROFILE meta)."""
    target = path or FINISH_DEFAULT_PATH
    knobs = knobs_only(values)
    ordered: dict[str, str] = {}
    if profile:
        ordered[PROFILE_META_KEY] = profile
    ordered.update(knobs)
    write_env_file(
        target,
        ordered,
        header="Tone default — shipped product knobs",
    )


def finish_default_profile(*, path: Path | None = None) -> str | None:
    """``PROFILE=`` meta from the package default, if set."""
    return parse_env_file(path or FINISH_DEFAULT_PATH).get(PROFILE_META_KEY)
