from __future__ import annotations

from pathlib import Path
from typing import Protocol

from yokaze.lib.util.types import DeliverTarget

__all__ = ["DeliverPort", "DeliverTarget"]


class DeliverPort(Protocol):
    def deliver(
        self,
        pdf: Path,
        *,
        target: DeliverTarget,
        name: str | None = None,
        case: str | None = None,
    ) -> str | None:
        """Paperless returns consume task UUID; other targets return None."""
        ...
