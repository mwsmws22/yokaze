"""Adw.Application entry for the unified Yokaze app."""

from __future__ import annotations

import signal
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID
from yokaze.lib.config import scanner as config

_VALID_TABS = frozenset(
    {
        "scan",
        "history",
        "stage",
        "paperless",
        "annotate",
        "preview",
        "tune-finish",
        "log",
        "settings",
    }
)


class YokazeApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self._pending_tab: str | None = None
        self._pending_scan = False
        self.add_main_option(
            "scan",
            ord("s"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Trigger a scan in the running window",
            None,
        )
        self.add_main_option(
            "tab",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.STRING,
            "Open tab: scan|history|stage|paperless|annotate|preview|tune-finish|log|settings",
            "NAME",
        )

    def do_startup(self) -> None:
        # Clear prefer-dark before Adw StyleManager initializes (lab dark-theme fix).
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", False)
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.DEFAULT)

        config.apply_config(config.load_config())

        scan_action = Gio.SimpleAction.new("scan", None)
        scan_action.connect("activate", self._on_scan_action)
        self.add_action(scan_action)

        GLib.unix_signal_add(
            GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self._on_scan_signal
        )

    def _on_scan_signal(self) -> bool:
        GLib.idle_add(self._trigger_scan)
        return True

    def _on_scan_action(self, _action, _param) -> None:
        GLib.idle_add(self._trigger_scan)

    def _ensure_window(self):
        from .window import MainWindow

        win = self.props.active_window
        if win is None:
            win = MainWindow(self)
        return win

    def _trigger_scan(self) -> bool:
        window = self._ensure_window()
        window.present()
        window.trigger_scan()
        return False

    def do_activate(self) -> None:
        window = self._ensure_window()
        if self._pending_tab:
            window.show_tab(self._pending_tab)
            self._pending_tab = None
        window.present()
        if self._pending_scan:
            self._pending_scan = False
            GLib.idle_add(self._trigger_scan)

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict()
        tab = None
        if options.contains("tab"):
            variant = options.lookup_value("tab")
            if variant is not None:
                tab = variant.get_string()
        want_scan = options.contains("scan")

        # Remote activation: primary already has a window.
        window = self.props.active_window
        if window is not None:
            if tab and tab in _VALID_TABS:
                window.show_tab(tab)
            window.present()
            if want_scan:
                GLib.idle_add(self._trigger_scan)
            return 0

        if tab and tab in _VALID_TABS:
            self._pending_tab = tab
        if want_scan:
            self._pending_scan = True
        self.activate()
        return 0


def main(argv: list[str] | None = None) -> int:
    app = YokazeApp()
    return app.run(argv or sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
