"""Confirmation dialogs for destructive UI actions."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402


def confirm_action(
    widget: Gtk.Widget,
    heading: str,
    body: str,
    *,
    on_yes: Callable[[], None],
    confirm_label: str = "Continue",
    destructive: bool = False,
    confirm_id: str = "ok",
) -> Adw.AlertDialog:
    """Ask before continuing. Cancel is the default and the close response."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response(confirm_id, confirm_label)
    appearance = (
        Adw.ResponseAppearance.DESTRUCTIVE
        if destructive
        else Adw.ResponseAppearance.SUGGESTED
    )
    dialog.set_response_appearance(confirm_id, appearance)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        if response == confirm_id:
            on_yes()

    dialog.connect("response", on_response)
    root = widget.get_root()
    parent = root if isinstance(root, Gtk.Window) else None
    dialog.present(parent)
    return dialog


def confirm_delete_corpus(
    widget: Gtk.Widget, case: str, on_confirm: Callable[[], None]
) -> Adw.AlertDialog:
    """Ask before removing a corpus. Cancel is the default and the close response."""
    return confirm_action(
        widget,
        f"Delete {case}?",
        "The pages and annotations for this corpus will be removed.",
        on_yes=on_confirm,
        confirm_label="Delete",
        destructive=True,
        confirm_id="delete",
    )
