"""Compare stage: the page canvas a lab tab shows, one at a time.

Every tab that displays "this page vs that page" (Preview, Tune) drives this
class rather than owning canvases itself, so pane layout, fit-vs-keep-zoom,
gutters and the toolbar "showing" labels exist in exactly one place.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .canvas import FitImageCanvas

# Outer padding lives in `.lab-canvas-stage` (styles.CSS). Panes are built at
# fit_scale=1.0 so that gutter is never stacked with a second per-canvas inset.


class ViewIntent(Enum):
    """Why a pane is being painted: re-fit the image, or keep the user's view.

    This replaces raw ``keep_view`` booleans at call sites. A different image
    must re-fit (its aspect ratio may differ); a re-render of the same image
    must preserve whatever the user zoomed and panned to.
    """

    NEW_IMAGE = "new_image"
    SAME_IMAGE = "same_image"

    @property
    def keep_view(self) -> bool:
        return self is ViewIntent.SAME_IMAGE


PanePainter = Callable[[FitImageCanvas, ViewIntent], None]


@dataclass(frozen=True)
class PaneContent:
    """One side of the stage: what to call it, and how to draw it."""

    label: str
    paint: PanePainter
    note: str = ""


class ComparePaneStage:
    """Dark canvas host showing one page.

    Owns everything the lab tabs used to re-implement per tab: the pane
    widget, the gutter, which page is visible, the Space flip that keeps
    zoom, the light/dark background, and the "showing" plus note labels the
    tab places in its toolbar.
    """

    def __init__(self) -> None:
        self._show_second = False
        self._primary: PaneContent | None = None
        self._secondary: PaneContent | None = None
        self._panes: list[FitImageCanvas] = []
        self._bg_light = False

        self.widget = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.widget.set_hexpand(True)
        self.widget.set_vexpand(True)
        self.widget.add_css_class("lab-canvas")
        self.widget.add_css_class("lab-canvas-stage")

        self.showing_label = Gtk.Label(label="", xalign=0)
        self.showing_label.add_css_class("heading")
        self.note_label = Gtk.Label(label="", xalign=0)
        self.note_label.add_css_class("lab-muted")
        self.note_label.set_hexpand(True)

    # --- content -----------------------------------------------------------

    def set_content(
        self,
        primary: PaneContent | None = None,
        secondary: PaneContent | None = None,
        *,
        intent: ViewIntent = ViewIntent.NEW_IMAGE,
        publish_labels: bool = True,
    ) -> None:
        """Point the stage at pages. ``intent`` decides refit vs keep zoom.

        ``publish_labels`` false keeps the toolbar caption on the image that
        is still visible. The caller publishes once the new pixels are drawn.
        """
        self._primary = primary
        self._secondary = secondary
        self.render(intent=intent, publish_labels=publish_labels)

    def render(self, *, intent: ViewIntent, publish_labels: bool = True) -> None:
        contents = self.visible_contents()
        self._ensure_panes(len(contents))
        for pane, content in zip(self._panes, contents):
            content.paint(pane, intent)
        if publish_labels:
            self._update_labels(contents)

    def publish_labels(self) -> None:
        """Name whatever the stage is showing now. Call after those pixels land."""
        self._update_labels(self.visible_contents())

    def refresh_primary(self, *, intent: ViewIntent) -> None:
        """Repaint only the primary page — the live-tuning path.

        A no-op while the secondary page is showing, so slider drags never
        touch (or reload) the baseline.
        """
        for pane, content in zip(self._panes, self.visible_contents()):
            if content is self._primary:
                content.paint(pane, intent)

    @property
    def showing_second(self) -> bool:
        return self._show_second

    def show_primary(self) -> None:
        """Next render shows the primary page."""
        self._show_second = False

    def show_second_page(self) -> None:
        """Next render shows the secondary page."""
        self._show_second = True

    def flip(self, *, publish_labels: bool = True) -> bool:
        """Space: swap which page is shown, keeping zoom and pan.

        The stage holds one canvas, so this repaints the pixels underneath
        the user's current view.
        """
        if self._secondary is None:
            return False
        self._show_second = not self._show_second
        self.render(intent=ViewIntent.SAME_IMAGE, publish_labels=publish_labels)
        return True

    def ensure_primary_visible(self) -> bool:
        """If the baseline is showing, switch back to the primary page.

        Used when the user edits live knobs while Space-flipped to the
        secondary page — ``refresh_primary`` is a no-op in that state.
        """
        if self._secondary is None or not self._show_second:
            return False
        self._show_second = False
        self.render(intent=ViewIntent.SAME_IMAGE)
        return True

    def visible_contents(self) -> list[PaneContent]:
        """The page on screen."""
        if self._primary is None:
            return []
        if self._show_second and self._secondary is not None:
            return [self._secondary]
        return [self._primary]

    # --- panes -------------------------------------------------------------

    @property
    def panes(self) -> tuple[FitImageCanvas, ...]:
        return tuple(self._panes)

    def set_background_light(self, light: bool) -> None:
        self._bg_light = light
        if light:
            self.widget.add_css_class("lab-canvas-light")
        else:
            self.widget.remove_css_class("lab-canvas-light")
        for pane in self._panes:
            pane.set_background_light(light)

    def _ensure_panes(self, count: int) -> None:
        """Add or drop canvases only when the count changes, so a flip or a
        re-render reuses the same widget — and therefore its zoom and pan."""
        while len(self._panes) > count:
            self.widget.remove(self._panes.pop())
        while len(self._panes) < count:
            pane = FitImageCanvas(fit_scale=1.0)
            pane.set_background_light(self._bg_light)
            self._panes.append(pane)
            self.widget.append(pane)

    def _update_labels(self, contents: list[PaneContent]) -> None:
        self.showing_label.set_text(" · ".join(c.label for c in contents))
        self.note_label.set_text(" · ".join(c.note for c in contents if c.note))
