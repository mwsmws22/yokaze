"""Local Paperless delivery index: UUID → page composition + overlays."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from yokaze.ui.models import CropRect


def _default_path() -> Path:
    from yokaze.lib import paths as app_paths

    return app_paths.resolve_deliveries_path()


@dataclass
class DeliveryPage:
    archive: str
    page: int
    crop: CropRect
    rotate: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "archive": self.archive,
            "page": int(self.page),
            "crop": self.crop.as_dict(),
            "rotate": int(self.rotate) % 360,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DeliveryPage:
        return cls(
            archive=str(data["archive"]),
            page=int(data["page"]),
            crop=CropRect.from_dict(data["crop"]),
            rotate=int(data.get("rotate", 0)) % 360,
        )


@dataclass
class Delivery:
    task_uuid: str
    brod_pin: str
    pages: list[DeliveryPage]
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    )
    name: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "task_uuid": self.task_uuid,
            "brod_pin": self.brod_pin,
            "created_at": self.created_at,
            "name": self.name,
            "pages": [p.to_json() for p in self.pages],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Delivery:
        pages = [DeliveryPage.from_json(p) for p in data.get("pages") or []]
        return cls(
            task_uuid=str(data["task_uuid"]),
            brod_pin=str(data["brod_pin"]),
            pages=pages,
            created_at=str(data.get("created_at") or ""),
            name=str(data.get("name") or ""),
        )

    @property
    def label(self) -> str:
        base = self.name or self.task_uuid[:8]
        return f"{base} · {len(self.pages)}p"


def load_deliveries(path: Path | None = None) -> list[Delivery]:
    target = path or _default_path()
    if not target.is_file():
        return []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw.get("deliveries") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[Delivery] = []
    for row in rows:
        if isinstance(row, dict) and row.get("task_uuid") and row.get("pages"):
            try:
                out.append(Delivery.from_json(row))
            except (KeyError, TypeError, ValueError):
                continue
    out.sort(key=lambda d: d.created_at, reverse=True)
    return out


def save_deliveries(deliveries: list[Delivery], path: Path | None = None) -> None:
    target = path or _default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"deliveries": [d.to_json() for d in deliveries]}
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def append_delivery(delivery: Delivery, path: Path | None = None) -> None:
    target = path or _default_path()
    existing = load_deliveries(target)
    existing.insert(0, delivery)
    save_deliveries(existing, target)


def get_delivery(task_uuid: str, path: Path | None = None) -> Delivery | None:
    for d in load_deliveries(path):
        if d.task_uuid == task_uuid:
            return d
    return None
