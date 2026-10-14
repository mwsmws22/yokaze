"""Shared Annotate/Preview case → page sidebar widgets and arrow navigation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from .lab_shell import scroll_widget_into_view


RANKED_LIST = "\0ranked"


def make_case_header(case: str, suffix: str | None = None) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    box.set_margin_top(4)
    box.set_margin_bottom(4)
    box.set_margin_end(8)

    title = Gtk.Label(label=case, xalign=0, hexpand=True)
    title.add_css_class("heading")
    box.append(title)
    if suffix is not None:
        progress = Gtk.Label(label=suffix, xalign=1)
        progress.add_css_class("dim-label")
        box.append(progress)
    return box


def make_page_row(
    label: str,
    *,
    payload: Any,
    done: bool | None = None,
    band: str | None = None,
    mark: str | None = None,
) -> Gtk.ListBoxRow:
    row = Gtk.ListBoxRow()
    row.add_css_class("annotate-page-row")
    row._page_payload = payload  # type: ignore[attr-defined]

    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    box.set_margin_start(16)
    box.set_margin_end(8)
    box.set_margin_top(6)
    box.set_margin_bottom(6)

    text = Gtk.Label(label=label, xalign=0, hexpand=True)
    text.set_ellipsize(Pango.EllipsizeMode.END)
    box.append(text)
    if done is not None:
        check = Gtk.Label(label="✓" if done else " ", xalign=1, width_chars=1)
        if done:
            text.add_css_class("dim-label")
            check.add_css_class("dim-label")
        box.append(check)
    if band in {"fail", "warn"}:
        status = Gtk.Label(label=band, xalign=1)
        status.add_css_class(f"crop-band-{band}")
        box.append(status)
    elif mark:
        status = Gtk.Label(label=mark, xalign=1)
        box.append(status)

    row.set_child(box)
    return row


class CasePageSidebar:
    """Case expanders + page lists with Annotate-style Left/Right/Up/Down nav."""

    def __init__(
        self,
        cases_box: Gtk.Box,
        *,
        on_page_selected: Callable[[Any], None],
        focus_host: Gtk.Widget | None = None,
    ) -> None:
        self._cases_box = cases_box
        self._on_page_selected = on_page_selected
        self._focus_host = focus_host
        self.expanders: dict[str, Gtk.Expander] = {}
        self.lists: dict[str, Gtk.ListBox] = {}
        self.case_order: list[str] = []
        self.focused_case: str | None = None
        self._selected_case: str | None = None
        self._suppress_expander = False
        self._last_payload: Any = None
        self._flat_pages = False

    def set_flat_pages(self, enabled: bool) -> None:
        """When on, Up/Down walk all pages across cases; collapse keeps selection."""
        self._flat_pages = enabled
        if not enabled:
            return
        self.focused_case = None
        self._set_case_focus(None)
        if self._last_payload is not None:
            for case in self.case_order:
                if self.select_matching(case, lambda p, want=self._last_payload: p == want):
                    return
        if self.case_order:
            self.select_first(self.case_order[0])

    def bind_nav_keys(self, widget: Gtk.Widget) -> None:
        ctrl = Gtk.EventControllerKey.new()
        ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ctrl.connect("key-pressed", self._on_nav_key_pressed)
        widget.add_controller(ctrl)

    def _on_nav_key_pressed(
        self,
        controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        # Stand down when a slider/switch/dropdown has focus — otherwise CAPTURE
        # on the lab shell eats Left/Right/Up/Down before the control sees them.
        widget = controller.get_widget()
        root = widget.get_root() if widget is not None else None
        focus = root.get_focus() if root is not None else None
        if focus is not None and isinstance(
            focus, (Gtk.DropDown, Gtk.Editable, Gtk.Scale, Gtk.Switch)
        ):
            return False
        return self.handle_nav_key(keyval)

    def clear(self) -> None:
        self._suppress_expander = True
        try:
            while (child := self._cases_box.get_first_child()) is not None:
                self._cases_box.remove(child)
            self.expanders.clear()
            self.lists.clear()
            self.case_order.clear()
            self.focused_case = None
            self._selected_case = None
            self._last_payload = None
            self._set_case_focus(None)
        finally:
            self._suppress_expander = False

    def add_case(
        self,
        case: str,
        rows: list[Gtk.ListBoxRow],
        *,
        header: Gtk.Widget | None = None,
        expanded: bool = False,
        setup_expander: Callable[[Gtk.Expander], None] | None = None,
    ) -> None:
        expander = Gtk.Expander()
        expander.set_can_focus(False)
        expander.add_css_class("annotate-case-expander")
        expander.set_label_widget(header if header is not None else make_case_header(case))
        expander.set_expanded(expanded)
        expander.connect("notify::expanded", self._on_case_expanded, case)
        if setup_expander is not None:
            setup_expander(expander)

        page_list = self._new_page_list()
        for row in rows:
            page_list.append(row)

        expander.set_child(page_list)
        self._cases_box.append(expander)
        self.expanders[case] = expander
        self.lists[case] = page_list
        self.case_order.append(case)

    def add_ranked_rows(self, rows: list[Gtk.ListBoxRow]) -> None:
        """One corpus-wide page list, with no case headers."""
        page_list = self._new_page_list()
        for row in rows:
            page_list.append(row)
        self._cases_box.append(page_list)
        self.lists[RANKED_LIST] = page_list
        self.case_order.append(RANKED_LIST)

    def _new_page_list(self) -> Gtk.ListBox:
        page_list = Gtk.ListBox()
        page_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        page_list.add_css_class("navigation-sidebar")
        page_list.connect("row-selected", self._on_row_selected)
        self.bind_nav_keys(page_list)
        return page_list

    def handle_nav_key(self, keyval: int) -> bool:
        if self._flat_pages:
            return self._handle_flat_nav_key(keyval)

        if keyval == Gdk.KEY_Left:
            case = self.current_case()
            if not case:
                return False
            self.collapse_and_focus_case(case)
            return True

        if keyval == Gdk.KEY_Right:
            case = self.current_case()
            if not case:
                return False
            expander = self.expanders.get(case)
            if expander is None:
                return False
            if not expander.get_expanded():
                expander.set_expanded(True)
            if not self.in_page_nav_mode():
                self.select_first(case)
            return True

        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            if self._move_page(keyval):
                return True
            return self._move_case(keyval)

        return False

    def _handle_flat_nav_key(self, keyval: int) -> bool:
        """Cross-case page chain: c1p1 → c1p2 → c2p1 → … without case-focus mode."""
        if keyval == Gdk.KEY_Left:
            case = self._selected_case
            if case:
                self._collapse_case(case)
            return True

        if keyval == Gdk.KEY_Right:
            case = self._selected_case
            expander = self.expanders.get(case) if case else None
            if expander is not None and not expander.get_expanded():
                self._suppress_expander = True
                try:
                    expander.set_expanded(True)
                finally:
                    self._suppress_expander = False
            return True

        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            return self._move_flat_page(keyval)

        return False

    def _all_page_entries(self) -> list[tuple[str, Gtk.ListBoxRow]]:
        entries: list[tuple[str, Gtk.ListBoxRow]] = []
        for case in self.case_order:
            for row in self._pages_in_case(case):
                entries.append((case, row))
        return entries

    def _move_flat_page(self, keyval: int) -> bool:
        entries = self._all_page_entries()
        if not entries:
            return True

        idx = -1
        for i, (_case, row) in enumerate(entries):
            if getattr(row, "_page_payload", None) == self._last_payload:
                idx = i
                break
        if idx < 0 and self._selected_case:
            selected = self._selected_row(self._selected_case)
            if selected is not None:
                for i, (_case, row) in enumerate(entries):
                    if row is selected:
                        idx = i
                        break

        if keyval == Gdk.KEY_Down:
            if idx < 0:
                next_idx = 0
            elif idx + 1 >= len(entries):
                return True
            else:
                next_idx = idx + 1
        elif idx < 0:
            next_idx = len(entries) - 1
        elif idx <= 0:
            return True
        else:
            next_idx = idx - 1

        case, row = entries[next_idx]
        self._select_row(case, row)
        return True

    def current_case(self) -> str | None:
        if self.focused_case:
            return self.focused_case
        return self._selected_case

    def in_page_nav_mode(self) -> bool:
        if self.focused_case is not None or not self._selected_case:
            return False
        expander = self.expanders.get(self._selected_case)
        if expander is None:
            return self._selected_case in self.lists
        return expander.get_expanded()

    def focus_case(self, case: str) -> None:
        """Highlight a collapsed case and show its first page in the content pane."""
        if case not in self.expanders:
            return
        self.unselect_all()
        self.focused_case = case
        self._selected_case = case
        self._set_case_focus(case)
        self._restore_keyboard_focus()
        first = self._first_payload(case)
        if first is not None:
            self._notify_page(first)

    def collapse_and_focus_case(self, case: str) -> None:
        self._collapse_case(case)
        self.focus_case(case)

    def select_first(self, case: str, *, grab_focus: bool = True) -> bool:
        page_list = self.lists.get(case)
        if page_list is None:
            return False
        row = page_list.get_row_at_index(0)
        if row is None:
            return False
        return self._select_row(case, row, grab_focus=grab_focus)

    def _first_payload(self, case: str) -> Any | None:
        page_list = self.lists.get(case)
        if page_list is None:
            return None
        row = page_list.get_row_at_index(0)
        if row is None:
            return None
        return getattr(row, "_page_payload", None)

    def select_matching(
        self,
        case: str,
        match: Callable[[Any], bool],
        *,
        grab_focus: bool = True,
    ) -> bool:
        page_list = self.lists.get(case)
        if page_list is None:
            return False
        for i in range(10_000):
            row = page_list.get_row_at_index(i)
            if row is None:
                break
            payload = getattr(row, "_page_payload", None)
            if match(payload):
                return self._select_row(case, row, grab_focus=grab_focus)
        return False

    def select_payload(self, payload: Any, *, grab_focus: bool = False) -> bool:
        """Select the row whose payload matches, in whichever list holds it."""
        for case in self.case_order:
            if self.select_matching(
                case, lambda p, want=payload: p == want, grab_focus=grab_focus
            ):
                return True
        return False

    def reveal_selected(self) -> None:
        """Scroll the selected page into view. A rebuilt list starts at the top."""
        case = self._selected_case
        if case is None:
            return
        row = self._selected_row(case)
        if row is None:
            return
        if self._reveal_row(row):
            return
        GLib.idle_add(self._reveal_selected_later, row)

    def _reveal_selected_later(self, row: Gtk.ListBoxRow) -> bool:
        if row.get_parent() is None:
            return False
        self._reveal_row(row)
        return False

    def _reveal_row(self, row: Gtk.Widget) -> bool:
        scroll = row.get_ancestor(Gtk.ScrolledWindow)
        if scroll is None:
            return False
        return scroll_widget_into_view(scroll, row)

    def unselect_all(self, except_list: Gtk.ListBox | None = None) -> None:
        for page_list in self.lists.values():
            if page_list is not except_list:
                page_list.unselect_all()

    def clear_selection_state(self) -> None:
        self.unselect_all()
        self.focused_case = None
        self._selected_case = None
        self._last_payload = None
        self._set_case_focus(None)

    def _notify_page(self, payload: Any) -> None:
        """Fire page callback only when the content page actually changes."""
        if payload is None or payload == self._last_payload:
            return
        self._last_payload = payload
        self._on_page_selected(payload)

    def _select_row(
        self, case: str, row: Gtk.ListBoxRow, *, grab_focus: bool = True
    ) -> bool:
        expander = self.expanders.get(case)
        page_list = self.lists.get(case)
        if page_list is None:
            return False

        if expander is not None:
            prev_case = self._selected_case
            self._suppress_expander = True
            try:
                expander.set_expanded(True)
                if self._flat_pages:
                    # Only collapse other corpora when selection moves to a new case.
                    if prev_case is not None and prev_case != case:
                        self._collapse_other_cases(case)
                else:
                    self._collapse_other_cases(case)
            finally:
                self._suppress_expander = False

        self.unselect_all()
        self.focused_case = None
        self._set_case_focus(None)
        page_list.select_row(row)
        if grab_focus:
            row.grab_focus()
        return True

    def _collapse_case(self, case: str) -> None:
        expander = self.expanders.get(case)
        if expander and expander.get_expanded():
            expander.set_expanded(False)

    def _collapse_and_focus_next_case(self, case: str) -> None:
        try:
            idx = self.case_order.index(case)
        except ValueError:
            return
        if idx + 1 >= len(self.case_order):
            return
        self._collapse_case(case)
        self.focus_case(self.case_order[idx + 1])

    def _collapse_other_cases(self, keep: str) -> None:
        self._suppress_expander = True
        try:
            for other, expander in self.expanders.items():
                if other != keep and expander.get_expanded():
                    expander.set_expanded(False)
        finally:
            self._suppress_expander = False

    def _set_case_focus(self, case: str | None) -> None:
        for name, expander in self.expanders.items():
            if case is not None and name == case:
                expander.add_css_class("annotate-case-focused")
            else:
                expander.remove_css_class("annotate-case-focused")

    def _restore_keyboard_focus(self) -> None:
        """Keep arrows working after collapse hides the focused page row."""
        host = self._focus_host
        if host is None:
            return
        host.set_focusable(True)
        host.grab_focus()

    def _pages_in_case(self, case: str) -> list[Gtk.ListBoxRow]:
        page_list = self.lists.get(case)
        if page_list is None:
            return []
        rows: list[Gtk.ListBoxRow] = []
        for i in range(10_000):
            row = page_list.get_row_at_index(i)
            if row is None:
                break
            rows.append(row)
        return rows

    def _selected_row(self, case: str) -> Gtk.ListBoxRow | None:
        page_list = self.lists.get(case)
        if page_list is None:
            return None
        return page_list.get_selected_row()

    def _move_page(self, keyval: int) -> bool:
        if not self.in_page_nav_mode() or not self._selected_case:
            return False
        case = self._selected_case
        rows = self._pages_in_case(case)
        selected = self._selected_row(case)
        if not rows or selected is None:
            return False
        try:
            idx = rows.index(selected)
        except ValueError:
            return False

        if keyval == Gdk.KEY_Down:
            if idx + 1 >= len(rows):
                self._collapse_and_focus_next_case(case)
                return True
            self._select_row(case, rows[idx + 1])
        elif idx <= 0:
            self.collapse_and_focus_case(case)
        else:
            self._select_row(case, rows[idx - 1])
        return True

    def _move_case(self, keyval: int) -> bool:
        cases = self.case_order
        if not cases:
            return True
        current = self.current_case()
        if current is None:
            target = cases[0] if keyval == Gdk.KEY_Down else cases[-1]
        else:
            try:
                idx = cases.index(current)
            except ValueError:
                idx = -1
            if keyval == Gdk.KEY_Down:
                if idx + 1 >= len(cases):
                    return True
                target = cases[idx + 1]
            elif idx <= 0:
                return True
            else:
                target = cases[idx - 1]
        self.focus_case(target)
        return True

    def _on_case_expanded(
        self,
        expander: Gtk.Expander,
        _pspec: object,
        case: str,
    ) -> None:
        if self._suppress_expander:
            return
        if expander.get_expanded():
            if self._flat_pages:
                # Keep other cases expanded until a page elsewhere is selected.
                return
            self._collapse_other_cases(case)
            if not self.in_page_nav_mode() or self._selected_case != case:
                self.select_first(case)
        elif self._flat_pages:
            # Keep the current page selected while the case is collapsed.
            return
        elif self._selected_case == case:
            self.focus_case(case)

    def _on_row_selected(self, lb: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        new_case: str | None = None
        for case, page_list in self.lists.items():
            if page_list is lb:
                new_case = case
                break
        prev_case = self._selected_case
        if (
            self._flat_pages
            and new_case is not None
            and prev_case is not None
            and prev_case != new_case
        ):
            self._suppress_expander = True
            try:
                self._collapse_other_cases(new_case)
            finally:
                self._suppress_expander = False

        self.unselect_all(except_list=lb)
        self.focused_case = None
        self._set_case_focus(None)
        self._selected_case = new_case
        self._notify_page(getattr(row, "_page_payload", None))
