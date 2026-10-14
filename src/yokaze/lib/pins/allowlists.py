"""Stage → allowlist pathspecs for pin comparison."""

from __future__ import annotations

ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "brod": (
        "src/yokaze/lib/brod/ops.py",
        "src/yokaze/lib/brod/geometry.py",
        "src/yokaze/lib/brod/naps2_deskew.py",
        "src/yokaze/lib/brod/osd.py",
        "src/yokaze/lib/brod/rotation_plan.py",
        "src/yokaze/lib/brod/blanks.py",
        "src/yokaze/lib/brod/document.py",
        # intentionally omit apply.py / replay.py — frozen reconstruct path
    ),
    "crop": ("src/yokaze/lib/crop/",),
    "tone": ("src/yokaze/lib/tone/",),
}
