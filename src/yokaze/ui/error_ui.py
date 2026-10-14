"""Error toasts/dialogs with right-click (and button) Copy."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402


def copy_text(text: str) -> None:
    display = Gdk.Display.get_default()
    if display is None:
        return
    display.get_clipboard().set(text)


def attach_copy_menu(widget: Gtk.Widget, text: str) -> None:
    """Secondary-click → small Copy popover on ``widget``."""
    gesture = Gtk.GestureClick()
    gesture.set_button(Gdk.BUTTON_SECONDARY)

    def on_pressed(
        click: Gtk.GestureClick, _n_press: int, x: float, y: float
    ) -> None:
        click.set_state(Gtk.EventSequenceState.CLAIMED)
        popover = Gtk.Popover()
        button = Gtk.Button(label="Copy")
        button.add_css_class("flat")

        def on_copy(*_a) -> None:
            copy_text(text)
            popover.popdown()

        button.connect("clicked", on_copy)
        popover.set_child(button)
        popover.set_parent(widget)
        area = Gdk.Rectangle()
        area.x = int(x)
        area.y = int(y)
        area.width = 1
        area.height = 1
        popover.set_pointing_to(area)
        popover.popup()

    gesture.connect("pressed", on_pressed)
    widget.add_controller(gesture)


def error_toast(message: str, *, timeout: int = 6) -> Adw.Toast:
    """Toast whose title supports right-click Copy (and a Copy button)."""
    text = (message or "").strip() or "Error"
    toast = Adw.Toast(timeout=timeout)
    toast.set_use_markup(False)
    label = Gtk.Label(
        label=text,
        xalign=0.0,
        wrap=True,
        max_width_chars=72,
        selectable=True,
    )
    attach_copy_menu(label, text)
    toast.set_custom_title(label)
    toast.set_button_label("Copy")
    toast.connect("button-clicked", lambda _t: copy_text(text))
    return toast


def present_error(
    parent: Gtk.Window | None,
    heading: str,
    detail: str = "",
    *,
    on_retry: Callable[[], None] | None = None,
) -> Adw.Dialog:
    """Modal error dialog: selectable body, right-click Copy, Copy button.

    ``on_retry`` adds a Try again button that closes the dialog, then runs.
    """
    body = (detail or "").strip() or (heading or "").strip() or "Error"
    title = (heading or "").strip() or "Error"
    clipboard = body if body == title else f"{title}\n\n{body}"

    dialog = Adw.Dialog()
    dialog.set_title(title)
    dialog.set_content_width(520)
    dialog.set_content_height(280)

    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    toolbar.add_top_bar(header)

    label = Gtk.Label(
        label=body,
        xalign=0.0,
        yalign=0.0,
        wrap=True,
        selectable=True,
        max_width_chars=64,
    )
    label.set_margin_start(16)
    label.set_margin_end(16)
    label.set_margin_top(8)
    label.set_margin_bottom(8)
    attach_copy_menu(label, clipboard)

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_child(label)
    toolbar.set_content(scrolled)

    copy_btn = Gtk.Button(label="Copy")
    copy_btn.connect("clicked", lambda *_a: copy_text(clipboard))
    close_btn = Gtk.Button(label="Close")
    close_btn.connect("clicked", lambda *_a: dialog.close())

    actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    actions.set_halign(Gtk.Align.END)
    actions.set_margin_start(16)
    actions.set_margin_end(16)
    actions.set_margin_top(8)
    actions.set_margin_bottom(12)
    actions.append(copy_btn)
    actions.append(close_btn)
    if on_retry is not None:
        retry_btn = Gtk.Button(label="Try again")
        retry_btn.add_css_class("suggested-action")

        def _retry(*_a: object) -> None:
            dialog.close()
            on_retry()

        retry_btn.connect("clicked", _retry)
        actions.append(retry_btn)
    else:
        copy_btn.add_css_class("suggested-action")
    toolbar.add_bottom_bar(actions)

    dialog.set_child(toolbar)
    # present() needs a parent widget in recent Adw; Window is fine.
    if parent is not None:
        dialog.present(parent)
    else:
        dialog.present()
    return dialog
