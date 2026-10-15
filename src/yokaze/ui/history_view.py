"""History tab — browse scan archives (raw only)."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import archives
from .archive_send import send_archive, stage_archive
from .canvas import FitImageCanvas
from .case_page_sidebar import CasePageSidebar, make_page_row
from .lab_shell import LabShell, make_switch_row
from .send_targets import SEND_TARGET_LABELS, SEND_TARGET_ORDER, ordered_send_targets
from .staging_tray import StagingTray, require_paperless_send_gates


class HistoryView(LabShell):
    def __init__(
        self,
        toast_overlay: Adw.ToastOverlay | None = None,
        staging_tray: StagingTray | None = None,
        on_staged: Callable[[], None] | None = None,
        on_paperless_sent: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(empty_text="No history yet.\nScan something first.")
        self._toast = toast_overlay
        self._tray = staging_tray
        self._on_staged = on_staged
        self._on_paperless_sent = on_paperless_sent
        self._archive: str | None = None
        self._page: int | None = None
        self._context_archive: str | None = None
        self._case_menu_popover: Gtk.PopoverMenu | None = None
        self._send_busy = False

        self._count_label = Gtk.Label(label="", xalign=0)
        self._count_label.add_css_class("heading")
        self.header.append(self._count_label)

        self._flat_switch = Gtk.Switch()
        self._flat_switch.set_active(True)
        self._flat_switch.connect("notify::active", self._on_flat_pages_changed)
        self.header.append(make_switch_row("Page jump", self._flat_switch))

        self._sidebar = CasePageSidebar(
            self.cases_box,
            on_page_selected=self._on_sidebar_page,
            focus_host=self.scroll,
        )
        self._sidebar.set_flat_pages(True)
        self._sidebar.bind_nav_keys(self.side)
        self._sidebar.bind_nav_keys(self.scroll)

        self._reveal_btn = Gtk.Button(label="Reveal")
        self._reveal_btn.connect("clicked", self._on_reveal)
        self._delete_btn = Gtk.Button(label="Delete")
        self._delete_btn.add_css_class("destructive-action")
        self._delete_btn.connect("clicked", self._on_delete)

        self._send_btn = Gtk.MenuButton(label="Send to")
        self._send_btn.set_always_show_arrow(True)

        for w in (self._send_btn, self._reveal_btn, self._delete_btn):
            w.set_can_focus(False)
            self.toolbar_start.append(w)

        self._canvas = FitImageCanvas()
        self.content.append(self._canvas)

        self._sidebar.bind_nav_keys(self.right)
        self._sidebar.bind_nav_keys(self._canvas)

        actions = Gio.SimpleActionGroup()
        delete_action = Gio.SimpleAction.new("delete-archive", None)
        delete_action.connect("activate", self._on_delete_archive_action)
        actions.add_action(delete_action)
        for target in SEND_TARGET_ORDER:
            act = Gio.SimpleAction.new(f"send-{target}", None)
            act.connect(
                "activate",
                lambda _a, _p, t=target: self._on_send_action(t),  # type: ignore[misc]
            )
            actions.add_action(act)
        self.insert_action_group("history", actions)
        self.refresh_send_targets()

        self._set_actions_sensitive(False)
        self.refresh()

    def refresh_send_targets(self) -> None:
        menu = Gio.Menu()
        for target in ordered_send_targets():
            menu.append(SEND_TARGET_LABELS[target], f"history.send-{target}")
        self._send_btn.set_menu_model(menu)
        self._send_btn.set_sensitive(menu.get_n_items() > 0)

    def on_show(self) -> None:
        self.refresh_send_targets()
        self.refresh()

    def handle_nav_key(self, keyval: int) -> bool:
        return self._sidebar.handle_nav_key(keyval)

    def _window(self) -> Gtk.Window | None:
        root = self.get_root()
        return root if isinstance(root, Gtk.Window) else None

    def _notify(self, message: str, *, detail: str = "", error: bool = False) -> None:
        """Toast for short feedback; alert when there's longer failure detail."""
        short = (message.splitlines()[0] if message else "").strip()
        if len(short) > 80:
            short = short[:77] + "…"
        if self._toast is not None and short:
            if error or detail.strip():
                from .error_ui import error_toast

                self._toast.add_toast(error_toast(short, timeout=6))
            else:
                self._toast.add_toast(Adw.Toast(title=short))
        if detail.strip():
            self._show_alert(message.splitlines()[0] if message else "Error", detail)

    def _show_alert(self, summary: str, detail: str) -> None:
        from .error_ui import present_error

        present_error(self._window(), summary, detail)

    def _on_flat_pages_changed(self, *_a) -> None:
        self._sidebar.set_flat_pages(self._flat_switch.get_active())

    def _set_actions_sensitive(self, enabled: bool) -> None:
        can = enabled and not self._send_busy
        self._send_btn.set_sensitive(can)
        self._reveal_btn.set_sensitive(enabled)
        self._delete_btn.set_sensitive(enabled)

    def refresh(self, *, select: tuple[str, int] | None = None) -> None:
        prev = select if select is not None else (self._archive, self._page)
        self._sidebar.clear()
        names = archives.list_archives()
        total_pages = 0
        for name in names:
            try:
                pages = archives.list_archive_pages(name)
            except FileNotFoundError:
                continue
            # Omit archives with no resolvable pages (nothing to select/view).
            if not pages:
                continue
            total_pages += len(pages)
            rows = [
                make_page_row(f"page-{page:04d}", payload=(name, page))
                for page in pages
            ]
            self._sidebar.add_case(
                name,
                rows,
                setup_expander=lambda exp, n=name: self._bind_case_context_menu(exp, n),
            )

        self.empty_label.set_visible(not self._sidebar.case_order)
        if total_pages:
            self._count_label.set_text(
                f"{total_pages} page" + ("" if total_pages == 1 else "s")
            )
        else:
            self._count_label.set_text("No pages")

        if prev[0] and prev[1] is not None:
            restored = self._sidebar.select_matching(
                prev[0], lambda p, want=prev: p == want
            )
            if restored:
                return
            # Archive still there but that page gone — land on its first page.
            if self._sidebar.select_first(prev[0]):
                return
        if self._sidebar.case_order:
            self._sidebar.select_first(self._sidebar.case_order[0])
        else:
            self._archive = None
            self._page = None
            self._set_actions_sensitive(False)
            self._canvas.clear_image()

    def _on_sidebar_page(self, payload) -> None:
        archive, page = payload
        self._archive = archive
        self._page = page
        self._context_archive = None
        self._set_actions_sensitive(True)
        self._reload_canvas()

    def _reload_canvas(self) -> None:
        if not self._archive or self._page is None:
            self._canvas.clear_image()
            return
        path = archives.archive_raw_path(self._archive, self._page)
        if path is None or not path.is_file():
            self._canvas.clear_image()
            return
        try:
            self._canvas.set_image_file(str(path))
        except GLib.Error:
            self._canvas.clear_image()

    def _bind_case_context_menu(self, widget: Gtk.Widget, archive: str) -> None:
        gesture = Gtk.GestureClick()
        gesture.set_button(Gdk.BUTTON_SECONDARY)
        gesture.connect("pressed", self._on_case_secondary_click, archive)
        widget.add_controller(gesture)

    def _on_case_secondary_click(
        self,
        gesture: Gtk.GestureClick,
        n_press: int,
        x: float,
        y: float,
        archive: str,
    ) -> None:
        if n_press != 1:
            return
        widget = gesture.get_widget()
        if widget is None:
            return

        self._context_archive = archive
        if self._case_menu_popover is not None:
            self._case_menu_popover.unparent()
            self._case_menu_popover = None

        menu = Gio.Menu()
        for target in ordered_send_targets():
            menu.append(
                f"Send to {SEND_TARGET_LABELS[target]}",
                f"history.send-{target}",
            )
        menu.append("Delete archive", "history.delete-archive")

        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda *_: setattr(self, "_case_menu_popover", None))
        popover.popup()
        self._case_menu_popover = popover

    def _on_delete_archive_action(self, _action, _param) -> None:
        if not self._context_archive:
            return
        self._delete_archive(self._context_archive)

    def _on_send_action(self, target: str) -> None:
        archive = self._context_archive or self._archive
        if not archive:
            return
        if target == "stage":
            self._send_to_stage(archive)
        elif target in ("paperless", "naps2", "folder"):
            self._send_deliver(archive, target)

    def _set_send_busy(self, busy: bool) -> None:
        self._send_busy = busy
        self._set_actions_sensitive(bool(self._archive))

    def _send_to_stage(self, archive: str) -> None:
        if self._tray is None:
            self._notify("Stage tray not available", error=True)
            return
        if self._send_busy:
            return
        if self._case_menu_popover is not None:
            self._case_menu_popover.popdown()
        self._set_send_busy(True)
        self._notify(f"Staging {archive}…")

        def work() -> None:
            try:
                assert self._tray is not None
                added = stage_archive(archive, self._tray, mode="from_history")
                GLib.idle_add(self._on_stage_done, archive, added)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_send_failed, "Stage failed", str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_stage_done(self, archive: str, added: int) -> bool:
        self._set_send_busy(False)
        if self._on_staged is not None:
            self._on_staged()
        else:
            self._notify(f"Staged {added} page(s) from {archive}")
        return False

    def _send_deliver(self, archive: str, target: str) -> None:
        if self._send_busy:
            return
        if self._case_menu_popover is not None:
            self._case_menu_popover.popdown()
        label = SEND_TARGET_LABELS.get(target, target)  # type: ignore[arg-type]
        # Gate before toast / tip replay so dirty tree fails immediately.
        if target == "paperless":
            try:
                require_paperless_send_gates(action="Send to Paperless")
            except Exception as exc:  # noqa: BLE001
                self._notify("Paperless send failed", detail=str(exc), error=True)
                return
        self._set_send_busy(True)
        self._notify(f"Sending {archive} to {label}…")

        def work() -> None:
            try:
                msg = send_archive(archive, target=target, mode="from_history")  # type: ignore[arg-type]
                GLib.idle_add(self._on_deliver_done, msg, target)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(
                    self._on_send_failed, f"{label} send failed", str(exc)
                )

        threading.Thread(target=work, daemon=True).start()

    def _on_deliver_done(self, msg: str, target: str) -> bool:
        self._set_send_busy(False)
        self._notify(msg)
        if target == "paperless" and self._on_paperless_sent is not None:
            self._on_paperless_sent()
        return False

    def _on_send_failed(self, summary: str, detail: str) -> bool:
        self._set_send_busy(False)
        self._notify(summary, detail=detail, error=True)
        return False

    def _on_reveal(self, *_a) -> None:
        if not self._archive:
            return
        info = archives.archive_info(self._archive)
        subprocess.Popen(["xdg-open", str(info["path"])])

    def _on_delete(self, *_a) -> None:
        if not self._archive:
            return
        self._delete_archive(self._archive)

    def _delete_archive(self, name: str) -> None:
        if self._case_menu_popover is not None:
            self._case_menu_popover.popdown()

        # Prefer the next archive down; if we deleted the last, go up one.
        select_after: tuple[str, int] | None = None
        order = list(self._sidebar.case_order)
        try:
            idx = order.index(name)
        except ValueError:
            idx = -1
        if self._archive == name:
            page = self._page or 1
            if idx >= 0 and idx + 1 < len(order):
                select_after = (order[idx + 1], page)
            elif idx > 0:
                select_after = (order[idx - 1], page)
        elif self._archive is not None and self._page is not None:
            select_after = (self._archive, self._page)

        try:
            archives.delete_archive(name)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Delete failed: {exc}", error=True)
            return
        self._context_archive = None
        self.refresh(select=select_after)
