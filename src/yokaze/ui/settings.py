"""Settings tab — scanner device and Paperless credentials."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from yokaze.lib.config import scanner as config
from yokaze.ui.send_targets import SEND_TARGET_LABELS, SEND_TARGET_ORDER

_THREADS_TOOLTIP = (
    "How many scans can process in the background at once. "
    "With multiple threads, you can keep scanning while earlier pages are still processing."
)


def _info_button(help_text: str) -> Gtk.Button:
    """Foreground symbolic ⓘ; hover or click shows ``help_text`` (no tooltip)."""
    # help-about-symbolic recolors with theme fg (like tab icons).
    # dialog-information-symbolic is a fixed blue badge in Adwaita/Rewaita.
    icon = Gtk.Image.new_from_icon_name("help-about-symbolic")
    icon.set_pixel_size(16)
    btn = Gtk.Button()
    btn.set_child(icon)
    btn.add_css_class("flat")
    btn.add_css_class("settings-row-info")
    btn.set_valign(Gtk.Align.CENTER)
    btn.set_has_tooltip(False)
    btn.set_can_focus(False)
    label = Gtk.Label(label=help_text)
    label.set_wrap(True)
    label.set_max_width_chars(40)
    label.set_xalign(0)
    label.set_margin_start(12)
    label.set_margin_end(12)
    label.set_margin_top(10)
    label.set_margin_bottom(10)
    popover = Gtk.Popover()
    popover.set_autohide(True)
    popover.set_child(label)
    popover.set_parent(btn)

    hovering = {"btn": False, "pop": False}

    def show(*_a) -> None:
        popover.popup()

    def hide_if_idle() -> bool:
        if not hovering["btn"] and not hovering["pop"]:
            popover.popdown()
        return False

    def on_btn_enter(*_a) -> None:
        hovering["btn"] = True
        show()

    def on_btn_leave(*_a) -> None:
        hovering["btn"] = False
        GLib.timeout_add(120, hide_if_idle)

    def on_pop_enter(*_a) -> None:
        hovering["pop"] = True

    def on_pop_leave(*_a) -> None:
        hovering["pop"] = False
        GLib.timeout_add(120, hide_if_idle)

    btn.connect("clicked", show)

    btn_motion = Gtk.EventControllerMotion.new()
    btn_motion.connect("enter", on_btn_enter)
    btn_motion.connect("leave", on_btn_leave)
    btn.add_controller(btn_motion)

    pop_motion = Gtk.EventControllerMotion.new()
    pop_motion.connect("enter", on_pop_enter)
    pop_motion.connect("leave", on_pop_leave)
    popover.add_controller(pop_motion)
    return btn


def _threads_title_with_info(help_text: str) -> Gtk.Widget:
    """Title label + info icon immediately to its right."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    box.set_valign(Gtk.Align.CENTER)
    title = Gtk.Label(label="Threads", xalign=0)
    title.add_css_class("title")
    box.append(title)
    box.append(_info_button(help_text))
    return box


def _git_revision_line() -> str:
    """Short identity of the git tree this package was loaded from."""
    start = Path(__file__).resolve()
    repo = next((p for p in (start, *start.parents) if (p / ".git").exists()), None)
    if repo is None:
        return f"Code: not a git checkout · {start.parent}"

    def _git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()

    try:
        short = _git("rev-parse", "--short", "HEAD")
        dirty = "-dirty" if _git("status", "--porcelain") else ""
        branch = _git("branch", "--show-current") or "detached"
        return f"Code: {short}{dirty} · {branch} · {repo}"
    except (OSError, subprocess.SubprocessError):
        return f"Code: unknown · {repo}"


class SettingsView(Gtk.Box):
    def __init__(
        self,
        toast_overlay: Adw.ToastOverlay,
        *,
        on_scanners_changed: Callable[[], None] | None = None,
        on_settings_saved: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._toast_overlay = toast_overlay
        self._on_scanners_changed = on_scanners_changed
        self._on_settings_saved = on_settings_saved
        self._devices: list[str] = [config.AUTO_DEVICE]
        self._loaded = False
        self._suppress_autosave = True
        self._autosave_source = 0
        self._persist_busy = False
        self._persist_pending = False
        self._persist_restart_button = False

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        self.append(scrolled)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(12)
        body.set_margin_bottom(24)
        body.set_margin_start(12)
        body.set_margin_end(12)
        scrolled.set_child(body)

        saved = config.load_config()

        # One PreferencesPage so Adw group spacing stays even (stacked pages
        # double page padding between some sections and not others).
        page = Adw.PreferencesPage()

        scanner_group = Adw.PreferencesGroup()
        scanner_group.set_title("Device")

        self._scanner_model = Gtk.StringList()
        self._scanner_row = Adw.ComboRow(title="Scanner")
        self._scanner_row.set_model(self._scanner_model)
        scanner_group.add(self._scanner_row)

        self._refresh_btn = Gtk.Button(label="Refresh")
        self._refresh_btn.add_css_class("flat")
        self._refresh_btn.connect("clicked", lambda *_: self.refresh_scanners())
        refresh_row = Adw.ActionRow(title="Detect scanners")
        refresh_row.set_subtitle("Run scanimage -L")
        refresh_row.add_suffix(self._refresh_btn)
        scanner_group.add(refresh_row)
        page.add(scanner_group)

        paper_group = Adw.PreferencesGroup()
        paper_group.set_title("Paper")
        self._paper_size_ids = [config.PAPER_SIZE_AUTO, config.PAPER_SIZE_A4]
        self._paper_size_model = Gtk.StringList()
        self._paper_size_model.append("Auto (ALD)")
        self._paper_size_model.append("A4")
        self._paper_size_row = Adw.ComboRow(title="Paper size")
        self._paper_size_row.set_model(self._paper_size_model)
        paper_index = (
            self._paper_size_ids.index(saved.paper_size)
            if saved.paper_size in self._paper_size_ids
            else 0
        )
        self._paper_size_row.set_selected(paper_index)
        paper_group.add(self._paper_size_row)
        page.add(paper_group)

        performance_group = Adw.PreferencesGroup()
        performance_group.set_title("Background Processing")

        workers_adjustment = Gtk.Adjustment(
            lower=1, upper=16, step_increment=1, page_increment=1
        )
        # Empty SpinRow title; custom prefix keeps help icon after "Threads".
        self._workers_row = Adw.SpinRow(title="", adjustment=workers_adjustment)
        self._workers_row.set_activatable(False)
        self._workers_row.set_value(saved.enhance_workers)
        self._workers_row.add_prefix(_threads_title_with_info(_THREADS_TOOLTIP))
        performance_group.add(self._workers_row)
        page.add(performance_group)

        send_group = Adw.PreferencesGroup()
        send_group.set_title("Send to")
        self._send_switches: dict[str, Gtk.Switch] = {}
        for target in SEND_TARGET_ORDER:
            row = Adw.ActionRow(title=SEND_TARGET_LABELS[target])
            switch = Gtk.Switch()
            switch.set_valign(Gtk.Align.CENTER)
            switch.set_active(target in saved.send_targets)
            switch.connect(
                "notify::active",
                lambda _s, _p, t=target: self._on_send_target_toggled(t),
            )
            row.add_suffix(switch)
            row.set_activatable_widget(switch)
            send_group.add(row)
            self._send_switches[target] = switch

        self._folder_dir_row = Adw.ActionRow(title="Folder destination")
        folder_choose = Gtk.Button(label="Choose…")
        folder_choose.add_css_class("flat")
        folder_choose.connect("clicked", lambda *_: self._choose_folder_dir())
        folder_reset = Gtk.Button(label="Reset")
        folder_reset.add_css_class("flat")
        folder_reset.connect("clicked", lambda *_: self._set_folder_dir(""))
        self._folder_dir_row.add_suffix(folder_reset)
        self._folder_dir_row.add_suffix(folder_choose)
        self._folder_dir = saved.folder_dir
        self._folder_dir_row.set_subtitle(self._folder_dir or "(not set)")
        send_group.add(self._folder_dir_row)
        page.add(send_group)
        self._sync_folder_row()

        from yokaze import ui as ui_pkg

        data_group = Adw.PreferencesGroup()
        data_group.set_title("Data")
        self._data_dir_row = Adw.ActionRow(title="History folder")
        self._data_dir_row.set_subtitle(str(ui_pkg.HISTORY))
        choose_btn = Gtk.Button(label="Choose…")
        choose_btn.add_css_class("flat")
        choose_btn.connect("clicked", lambda *_: self._choose_data_dir())
        reset_btn = Gtk.Button(label="Reset")
        reset_btn.add_css_class("flat")
        reset_btn.connect("clicked", lambda *_: self._reset_data_dir())
        self._data_dir_row.add_suffix(reset_btn)
        self._data_dir_row.add_suffix(choose_btn)
        data_group.add(self._data_dir_row)
        page.add(data_group)

        paperless_group = Adw.PreferencesGroup()
        paperless_group.set_title("Paperless-ngx")

        self._url_row = Adw.EntryRow(title="Server URL")
        self._url_row.set_text(saved.paperless_url)
        paperless_group.add(self._url_row)

        self._token_row = Adw.PasswordEntryRow(title="API token")
        self._token_row.set_text(saved.paperless_token)
        paperless_group.add(self._token_row)

        self._test_btn = Gtk.Button(label="Test")
        self._test_btn.add_css_class("suggested-action")
        self._test_btn.connect("clicked", lambda *_: self._test_connection())
        test_btn_box = Gtk.Box()
        test_btn_box.set_valign(Gtk.Align.CENTER)
        test_btn_box.set_margin_top(8)
        test_btn_box.set_margin_bottom(8)
        test_btn_box.append(self._test_btn)
        test_row = Adw.ActionRow(title="Connection")
        self._test_status = Gtk.Label(label="", xalign=1)
        self._test_status.add_css_class("dim-label")
        test_row.add_suffix(self._test_status)
        test_row.add_suffix(test_btn_box)
        paperless_group.add(test_row)
        page.add(paperless_group)

        body.append(page)

        self._revision_label = Gtk.Label()
        self._revision_label.add_css_class("dim-label")
        self._revision_label.set_wrap(True)
        self._revision_label.set_justify(Gtk.Justification.CENTER)
        self._revision_label.set_halign(Gtk.Align.CENTER)
        self._revision_label.set_selectable(True)
        self._revision_label.set_margin_start(12)
        self._revision_label.set_margin_end(12)
        self._revision_label.set_margin_top(8)
        self._revision_label.set_margin_bottom(12)
        self.append(self._revision_label)
        self._refresh_revision()

        self._set_scanners([], saved.scanner_device)
        self._wire_autosave()
        self._suppress_autosave = False

    def _sync_folder_row(self) -> None:
        folder_on = self._send_switches["folder"].get_active()
        self._folder_dir_row.set_sensitive(folder_on)

    def _wire_autosave(self) -> None:
        self._scanner_row.connect(
            "notify::selected",
            lambda *_: self._schedule_autosave(restart_button=True),
        )
        self._paper_size_row.connect(
            "notify::selected", lambda *_: self._schedule_autosave()
        )
        self._workers_row.connect(
            "notify::value",
            lambda *_: self._schedule_autosave(debounce_ms=250),
        )
        self._url_row.connect(
            "notify::text",
            lambda *_: self._schedule_autosave(debounce_ms=400),
        )
        self._token_row.connect(
            "notify::text",
            lambda *_: self._schedule_autosave(debounce_ms=400),
        )

    def _schedule_autosave(
        self, *, debounce_ms: int = 0, restart_button: bool = False
    ) -> None:
        if self._suppress_autosave:
            return
        if self._autosave_source:
            GLib.source_remove(self._autosave_source)
            self._autosave_source = 0

        def run() -> bool:
            self._autosave_source = 0
            self._persist(restart_button=restart_button)
            return False

        if debounce_ms > 0:
            self._autosave_source = GLib.timeout_add(debounce_ms, run)
        else:
            run()

    def _on_send_target_toggled(self, target: str) -> None:
        if self._suppress_autosave:
            return
        switch = self._send_switches[target]
        if not self._current_send_targets():
            self._suppress_autosave = True
            switch.set_active(True)
            self._suppress_autosave = False
            self._toast_overlay.add_toast(
                Adw.Toast(title="Keep at least one Send to destination")
            )
            return
        if (
            target == config.SEND_TARGET_FOLDER
            and switch.get_active()
            and not self._folder_dir
        ):
            self._suppress_autosave = True
            switch.set_active(False)
            self._suppress_autosave = False
            self._toast_overlay.add_toast(
                Adw.Toast(title="Choose a Folder destination first")
            )
            return
        self._sync_folder_row()
        self._schedule_autosave()

    def _refresh_revision(self) -> None:
        self._revision_label.set_text(_git_revision_line())

    def on_show(self) -> None:
        self._refresh_revision()
        self._refresh_data_dir_row()
        if not self._loaded:
            self._loaded = True
            self.refresh_scanners()

    def _set_scanners(self, detected: list[str], selected: str) -> None:
        prev_suppress = self._suppress_autosave
        self._suppress_autosave = True
        try:
            while self._scanner_model.get_n_items() > 0:
                self._scanner_model.remove(0)

            self._devices = [config.AUTO_DEVICE]
            self._scanner_model.append("Auto-detect")
            for device in detected:
                if device not in self._devices:
                    self._devices.append(device)
                    self._scanner_model.append(config.scanner_label(device))
            if selected and selected not in self._devices:
                self._devices.append(selected)
                self._scanner_model.append(config.scanner_label(selected))

            index = self._devices.index(selected) if selected in self._devices else 0
            self._scanner_row.set_selected(index)
        finally:
            self._suppress_autosave = prev_suppress

    def refresh_scanners(self) -> None:
        selected = self._current_device()
        self._refresh_btn.set_sensitive(False)

        def work() -> None:
            devices = config.list_scanners()
            GLib.idle_add(self._on_scanners_refreshed, devices, selected)

        threading.Thread(target=work, daemon=True).start()

    def _notify_scanners_changed(self) -> None:
        if self._on_scanners_changed:
            self._on_scanners_changed()

    def _notify_settings_saved(self) -> None:
        if self._on_settings_saved:
            self._on_settings_saved()

    def _on_scanners_refreshed(self, devices: list[str], selected: str) -> None:
        self._set_scanners(devices, selected)
        self._refresh_btn.set_sensitive(True)
        self._notify_scanners_changed()

    def _current_device(self) -> str:
        index = self._scanner_row.get_selected()
        if index < 0 or index >= len(self._devices):
            return config.AUTO_DEVICE
        return self._devices[index]

    def _current_paper_size(self) -> str:
        index = self._paper_size_row.get_selected()
        if index < 0 or index >= len(self._paper_size_ids):
            return config.DEFAULT_PAPER_SIZE
        return self._paper_size_ids[index]

    def _current_send_targets(self) -> frozenset[str]:
        return frozenset(
            t for t, switch in self._send_switches.items() if switch.get_active()
        )

    def _current_config(self) -> config.ScannerConfig:
        # Prefer the live config so a Ctrl+D toggle is kept without a Save click.
        live = config.load_config().developer_mode
        return config.ScannerConfig(
            scanner_device=self._current_device(),
            paperless_url=self._url_row.get_text().strip(),
            paperless_token=self._token_row.get_text(),
            enhance_workers=int(self._workers_row.get_value()),
            developer_mode=bool(live),
            paper_size=self._current_paper_size(),
            send_targets=self._current_send_targets(),
            folder_dir=self._folder_dir,
        )

    def _test_connection(self) -> None:
        cfg = self._current_config()
        self._test_status.set_text("Testing…")
        self._test_btn.set_sensitive(False)

        def work() -> None:
            result = config.test_paperless(cfg.paperless_url, cfg.paperless_token)
            GLib.idle_add(self._on_test_done, *result)

        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, ok: bool, message: str) -> None:
        self._test_status.set_text(message)
        self._test_btn.set_sensitive(True)

    def _refresh_data_dir_row(self) -> None:
        from yokaze import ui as ui_pkg

        self._data_dir_row.set_subtitle(str(ui_pkg.HISTORY))

    def _apply_data_dir(self, path: Path | None) -> None:
        from yokaze.lib import paths as app_paths
        from yokaze import ui as ui_pkg

        try:
            app_paths.save_history_dir_override(path)
            ui_pkg.refresh_paths()
        except OSError as exc:
            self._show_error(str(exc))
            return
        self._refresh_data_dir_row()
        self._toast_overlay.add_toast(Adw.Toast(title="History folder updated"))

    def _choose_data_dir(self) -> None:
        dialog = Gtk.FileDialog(title="Choose history folder")
        root = self.get_root()
        parent = root if isinstance(root, Gtk.Window) else None

        def on_selected(dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            if folder is None:
                return
            path = Path(folder.get_path() or "")
            if not path:
                return
            self._apply_data_dir(path)

        dialog.select_folder(parent, None, on_selected)

    def _reset_data_dir(self) -> None:
        self._apply_data_dir(None)

    def _set_folder_dir(self, path: str) -> None:
        self._folder_dir = path
        self._folder_dir_row.set_subtitle(path or "(not set)")
        self._suppress_autosave = True
        try:
            if path:
                self._send_switches[config.SEND_TARGET_FOLDER].set_active(True)
            else:
                self._send_switches[config.SEND_TARGET_FOLDER].set_active(False)
        finally:
            self._suppress_autosave = False
        self._sync_folder_row()
        self._schedule_autosave()

    def _choose_folder_dir(self) -> None:
        dialog = Gtk.FileDialog(title="Choose folder destination")
        root = self.get_root()
        parent = root if isinstance(root, Gtk.Window) else None

        def on_selected(dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            if folder is None:
                return
            path = folder.get_path() or ""
            if path:
                self._set_folder_dir(path)

        dialog.select_folder(parent, None, on_selected)

    def _persist(self, *, restart_button: bool = False) -> None:
        if self._suppress_autosave:
            return
        if self._persist_busy:
            self._persist_pending = True
            self._persist_restart_button = (
                self._persist_restart_button or restart_button
            )
            return
        cfg = self._current_config()
        if not cfg.send_targets:
            return
        if config.SEND_TARGET_FOLDER in cfg.send_targets and not cfg.folder_dir:
            return
        self._persist_busy = True
        self._persist_restart_button = False

        def work() -> None:
            try:
                config.save_config(cfg, restart_button=restart_button)
                GLib.idle_add(self._on_persist_done, True, "")
            except OSError as exc:
                GLib.idle_add(self._on_persist_done, False, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_persist_done(self, ok: bool, message: str) -> bool:
        self._persist_busy = False
        if ok:
            self._notify_scanners_changed()
            self._notify_settings_saved()
        else:
            self._show_error(message)
        if self._persist_pending:
            self._persist_pending = False
            self._persist(restart_button=self._persist_restart_button)
        return False

    def _show_error(self, message: str) -> None:
        from .error_ui import present_error

        root = self.get_root()
        parent = root if isinstance(root, Gtk.Window) else None
        present_error(parent, "Could not save settings", message)
