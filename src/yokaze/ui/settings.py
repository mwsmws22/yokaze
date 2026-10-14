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
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._toast_overlay = toast_overlay
        self._on_scanners_changed = on_scanners_changed
        self._devices: list[str] = [config.AUTO_DEVICE]
        self._loaded = False

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
        scanner_group.set_description(
            "Choose a Fujitsu ScanSnap or leave auto-detect to find one at scan time."
        )

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
        paper_group.set_description(
            "ADF scan window. Auto stops at the sheet end (up to 3 m). "
            "A4 uses a fixed A4 + bleed frame. Card scans always use A4."
        )
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
        performance_group.set_description(
            "How many scanned batches can be cropped/enhanced at once in the "
            "background. 1 = sequential (safest); higher lets the next batch "
            "process while an earlier one is still running, if your machine "
            "has the cores to spare."
        )

        workers_adjustment = Gtk.Adjustment(
            lower=1, upper=16, step_increment=1, page_increment=1
        )
        self._workers_row = Adw.SpinRow(
            title="Enhance workers", adjustment=workers_adjustment
        )
        self._workers_row.set_value(saved.enhance_workers)
        performance_group.add(self._workers_row)
        page.add(performance_group)

        from yokaze import ui as ui_pkg

        data_group = Adw.PreferencesGroup()
        data_group.set_title("Data")
        data_group.set_description(
            "Where scan archives are stored. Default is the XDG data directory "
            "for this app. Choose a folder to override, or reset to the default."
        )
        self._data_dir_row = Adw.ActionRow(title="Archives folder")
        self._data_dir_row.set_subtitle(str(ui_pkg.DATA))
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
        paperless_group.set_description(
            "API credentials for uploading finished scans."
        )

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

        self._save_btn = Gtk.Button(label="Save")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.connect("clicked", lambda *_: self._save())
        save_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        save_bar.set_halign(Gtk.Align.CENTER)
        save_bar.set_margin_top(8)
        save_bar.set_margin_bottom(8)
        save_bar.append(self._save_btn)
        body.append(save_bar)

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

    def _refresh_revision(self) -> None:
        self._revision_label.set_text(_git_revision_line())

    def on_show(self) -> None:
        self._refresh_revision()
        self._refresh_data_dir_row()
        if not self._loaded:
            self._loaded = True
            self.refresh_scanners()

    def _set_scanners(self, detected: list[str], selected: str) -> None:
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

    def _current_config(self) -> config.ScannerConfig:
        # Prefer the live config so a Ctrl+D toggle between open and Save is kept.
        live = config.load_config().developer_mode
        return config.ScannerConfig(
            scanner_device=self._current_device(),
            paperless_url=self._url_row.get_text().strip(),
            paperless_token=self._token_row.get_text(),
            enhance_workers=int(self._workers_row.get_value()),
            developer_mode=bool(live),
            paper_size=self._current_paper_size(),
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

        self._data_dir_row.set_subtitle(str(ui_pkg.DATA))

    def _apply_data_dir(self, path: Path | None) -> None:
        from yokaze.lib import paths as app_paths
        from yokaze import ui as ui_pkg

        try:
            app_paths.save_data_dir_override(path)
            ui_pkg.refresh_paths()
        except OSError as exc:
            self._show_error(str(exc))
            return
        self._refresh_data_dir_row()
        self._toast_overlay.add_toast(Adw.Toast(title="Archives folder updated"))

    def _choose_data_dir(self) -> None:
        dialog = Gtk.FileDialog(title="Choose archives data folder")
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

    def _save(self) -> None:
        cfg = self._current_config()
        if not cfg.paperless_url or not cfg.paperless_token:
            self._show_error("Paperless URL and API token are required.")
            return

        self._save_btn.set_sensitive(False)

        def work() -> None:
            try:
                config.save_config(cfg)
                GLib.idle_add(self._on_save_done, True, "Settings saved")
            except OSError as exc:
                GLib.idle_add(self._on_save_done, False, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_save_done(self, ok: bool, message: str) -> None:
        self._save_btn.set_sensitive(True)
        if ok:
            self._toast_overlay.add_toast(Adw.Toast(title=message))
            self._notify_scanners_changed()
        else:
            self._show_error(message)

    def _show_error(self, message: str) -> None:
        from .error_ui import present_error

        root = self.get_root()
        parent = root if isinstance(root, Gtk.Window) else None
        present_error(parent, "Could not save settings", message)
