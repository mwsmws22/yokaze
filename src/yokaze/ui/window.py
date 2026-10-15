"""Main Yokaze window: Scan | History | Stage | Paperless | Settings."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from .history_view import HistoryView
from .icons import header_brand, register_bundled_icons
from .paperless_view import PaperlessView
from .reload_app import restart_app
from . import scan_view as scan_view_module
from .scanner_status import ScannerStatus
from .settings import SettingsView
from .stage_view import StageView
from .staging_tray import StagingTray
from .styles import CSS

_TAB_NAMES = ("scan", "history", "stage", "paperless", "settings")

_DEFAULT_WIDTH = 1280
_DEFAULT_HEIGHT = 840

_NAV_KEYS = (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Up, Gdk.KEY_Down)
_STAGE_KEYS = _NAV_KEYS + (
    Gdk.KEY_Delete,
    Gdk.KEY_KP_Delete,
    Gdk.KEY_BackSpace,
)
_ENTER_KEYS = (Gdk.KEY_Return, Gdk.KEY_KP_Enter)


def _types_own_the_key(focus: Gtk.Widget | None, *types: type) -> bool:
    """True when the focused widget handles this key itself, so we stand down."""
    return focus is not None and isinstance(focus, types)


def _owns_shortcut(focus: Gtk.Widget | None) -> bool:
    """Shortcut keys (Space, b): only text entry and dropdowns keep them."""
    return _types_own_the_key(focus, Gtk.DropDown, Gtk.Editable)


def _owns_arrows(focus: Gtk.Widget | None) -> bool:
    """Arrow keys additionally belong to sliders and switches."""
    return _types_own_the_key(
        focus, Gtk.DropDown, Gtk.Editable, Gtk.Scale, Gtk.Switch
    )


def _dispatch_crop_canvas_keys(
    view,
    keyval: int,
    *,
    ctrl: bool,
    state: Gdk.ModifierType,
    focus: Gtk.Widget | None,
) -> bool | None:
    """Stage crop-canvas shortcuts. None means not handled here."""
    if keyval == Gdk.KEY_space and not _owns_shortcut(focus):
        flip = getattr(view, "flip_solo_background_shortcut", None)
        if callable(flip) and flip():
            return True
        return view.fit_shortcut()
    if (
        ctrl
        and keyval in (Gdk.KEY_z, Gdk.KEY_Z)
        and not (state & Gdk.ModifierType.SHIFT_MASK)
        and not _types_own_the_key(focus, Gtk.Editable)
    ):
        return view.undo_shortcut()
    if keyval in (Gdk.KEY_a, Gdk.KEY_A) and not _owns_shortcut(focus):
        return view.toggle_deskew_shortcut()
    if keyval in (Gdk.KEY_s, Gdk.KEY_S) and not _owns_shortcut(focus):
        if view.toggle_deskew_tip_shortcut():
            return True
        split = getattr(view, "split_shortcut", None)
        if callable(split) and split():
            return True
        return False
    if keyval in (Gdk.KEY_d, Gdk.KEY_D) and not _owns_shortcut(focus):
        if view.toggle_deskew_grid_shortcut():
            return True
        solo = getattr(view, "cycle_solo_preview_shortcut", None)
        if callable(solo) and solo():
            return True
        return False
    if keyval in (Gdk.KEY_c, Gdk.KEY_C) and not _owns_shortcut(focus):
        if view.cycle_deskew_grid_color_shortcut():
            return True
        return None
    if (
        not ctrl
        and not _owns_shortcut(focus)
        and view.focus_crop_corner_shortcut(keyval)
    ):
        return True
    return None


def _restored_window_size() -> tuple[int, int]:
    raw_w = os.environ.pop("YOKAZE_WINDOW_WIDTH", None)
    raw_h = os.environ.pop("YOKAZE_WINDOW_HEIGHT", None)
    if not raw_w or not raw_h:
        return _DEFAULT_WIDTH, _DEFAULT_HEIGHT
    try:
        width, height = int(raw_w), int(raw_h)
    except ValueError:
        return _DEFAULT_WIDTH, _DEFAULT_HEIGHT
    if width < 400 or height < 300:
        return _DEFAULT_WIDTH, _DEFAULT_HEIGHT
    return width, height


def _restored_staging_tray() -> tuple[StagingTray, str | None]:
    """Load durable Stage tray (survives quit and F5)."""
    select = os.environ.pop("YOKAZE_STAGE_SELECT", None)
    os.environ.pop("YOKAZE_STAGE_TRAY", None)
    tray = StagingTray.load_persisted()
    if select and tray.get(select) is not None:
        tray.selected_id = select
    return tray, tray.selected_id


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Yokaze")
        width, height = _restored_window_size()
        self.set_default_size(width, height)

        css = Gtk.CssProvider()
        css.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        register_bundled_icons(self.get_display())

        toast = Adw.ToastOverlay()
        self.set_content(toast)
        self._toast = toast

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toast.set_child(root)

        self._view_stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self._view_stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_can_focus(False)
        switcher.set_focusable(False)

        header = Adw.HeaderBar()
        header.pack_start(header_brand())
        header.set_title_widget(switcher)
        self._scanner_status = ScannerStatus()
        header.pack_end(self._scanner_status)

        root.append(header)
        root.append(self._view_stack)

        self._staging_tray, stage_select = _restored_staging_tray()
        self._history = HistoryView(
            toast_overlay=toast,
            staging_tray=self._staging_tray,
            on_staged=self._on_staged,
            on_paperless_sent=self._on_history_paperless_sent,
        )
        self._paperless = PaperlessView(
            toast,
            self._staging_tray,
            on_restaged=self._on_restaged,
        )
        self._stage = StageView(
            toast,
            self._staging_tray,
            on_tray_changed=self._on_stage_tray_changed,
        )
        if stage_select:
            self._stage.refresh(select=stage_select)
        self._scan_view = scan_view_module.ScanView(
            on_archive_changed=self._on_archives_changed,
            staging_tray=self._staging_tray,
            on_staged=self._on_staged,
        )
        self._view_stack.add_titled_with_icon(
            self._scan_view, "scan", "Scan", "document-scan-symbolic"
        )
        self._view_stack.add_titled_with_icon(
            self._history, "history", "History", "document-open-recent-symbolic"
        )
        self._view_stack.add_titled_with_icon(
            self._stage, "stage", "Stage", "view-list-symbolic"
        )
        self._view_stack.add_titled_with_icon(
            self._paperless,
            "paperless",
            "Paperless",
            "paperless-ngx-symbolic",
        )

        self._settings = SettingsView(
            toast,
            on_scanners_changed=self._scanner_status.refresh,
            on_settings_saved=self._on_settings_saved,
        )
        self._view_stack.add_titled_with_icon(
            self._settings, "settings", "Settings", "preferences-system-symbolic"
        )

        self._scanner_status.start(self)
        self._view_stack.set_visible_child_name("scan")

        self._view_stack.connect("notify::visible-child", self._on_visible_child_changed)

        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctrl)
        self.connect("close-request", self._on_close_request)

    def _on_close_request(self, *_a) -> bool:
        """Persist Stage overlays before the window goes away."""
        try:
            self._stage.prepare_reload()
        except Exception:  # noqa: BLE001 — never block quit
            pass
        return False

    def refresh(self) -> None:
        """Restart the app in-place to reload all code and data (F5)."""
        tab = self._view_stack.get_visible_child_name()
        width = self.get_width() if self.get_realized() else 0
        height = self.get_height() if self.get_realized() else 0
        self._stage.prepare_reload()
        err = restart_app(
            tab=tab,
            scan_state=self._scan_view.state,
            width=width,
            height=height,
            stage_select=self._stage.selected_id,
        )
        if err:
            from .error_ui import error_toast

            self._toast.add_toast(error_toast(err, timeout=6))

    def _on_key_pressed(
        self,
        _controller,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_F5:
            self.refresh()
            return True

        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        tab = self._view_stack.get_visible_child_name()
        focus = self.get_focus()

        if ctrl and keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_KP_Tab):
            shift = bool(state & Gdk.ModifierType.SHIFT_MASK) or keyval == Gdk.KEY_ISO_Left_Tab
            self._cycle_tab(backward=shift)
            return True

        if tab == "history" and keyval in _NAV_KEYS:
            return self._history.handle_nav_key(keyval)

        if tab == "stage":
            handled = _dispatch_crop_canvas_keys(
                self._stage, keyval, ctrl=ctrl, state=state, focus=focus
            )
            if handled is not None:
                return handled
            if (
                keyval in _ENTER_KEYS
                and not _owns_shortcut(focus)
                and self._stage.confirm_split_shortcut()
            ):
                return True
            if keyval in _STAGE_KEYS:
                return self._stage.handle_nav_key(keyval)

        if tab == "paperless" and keyval in _NAV_KEYS:
            return self._paperless.handle_nav_key(keyval)

        return False

    def _on_archives_changed(self) -> None:
        self._history.refresh()
        self._paperless.refresh()

    def _on_staged(self) -> None:
        self._stage.refresh()
        self.show_tab("stage")
        self._toast.add_toast(Adw.Toast(title="Added to Stage", timeout=2))

    def _on_history_paperless_sent(self) -> None:
        self._paperless.refresh()

    def _on_restaged(self) -> None:
        self._stage.refresh()
        self.show_tab("stage")

    def _on_stage_tray_changed(self) -> None:
        self._paperless.refresh()

    def _cycle_tab(self, *, backward: bool = False) -> None:
        tabs = _TAB_NAMES
        current = self._view_stack.get_visible_child_name()
        try:
            idx = tabs.index(current) if current else 0
        except ValueError:
            idx = 0
        step = -1 if backward else 1
        next_name = tabs[(idx + step) % len(tabs)]
        self.show_tab(next_name)

    def show_tab(self, name: str) -> None:
        if name not in _TAB_NAMES:
            return
        self._view_stack.set_visible_child_name(name)

    def trigger_scan(self) -> None:
        self.show_tab("scan")
        self._scan_view.trigger_scan()

    def _on_settings_saved(self) -> None:
        self._scan_view.refresh_send_targets()
        self._history.refresh_send_targets()
        self._stage.refresh_send_targets()
        self._stage.refresh(select=self._stage.selected_id)

    def _on_visible_child_changed(self, stack, _pspec) -> None:
        self._scanner_status.refresh()
        child = stack.get_visible_child()
        if child is self._settings:
            self._settings.on_show()
        elif child is self._history:
            self._history.on_show()
        elif child is self._stage:
            self._stage.on_show()
        elif child is self._paperless:
            self._paperless.on_show()
