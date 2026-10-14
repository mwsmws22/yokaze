"""Dataclasses for GT crop lab."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from yokaze.lib.geometry import CropRect as CropRect  # re-export


@dataclass
class GroundTruth:
    case: str
    page: int
    space: str
    image_w: int
    image_h: int
    crop: CropRect
    raw: str
    deskew_angle_deg: float = 0.0
    updated: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["crop"] = self.crop.as_dict()
        return d

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> GroundTruth:
        return cls(
            case=str(data["case"]),
            page=int(data["page"]),
            space=str(data.get("space", "brod")),
            image_w=int(data["image_w"]),
            image_h=int(data["image_h"]),
            crop=CropRect.from_dict(data["crop"]),
            raw=str(data.get("raw", "")),
            deskew_angle_deg=float(data.get("deskew_angle_deg", 0.0)),
            updated=str(data.get("updated", "")),
        )


@dataclass
class PageRef:
    case: str
    page: int
    raw_path: str

    @property
    def page_pad(self) -> str:
        return f"{self.page:04d}"

    @property
    def label(self) -> str:
        return f"{self.case} p{self.page}"
