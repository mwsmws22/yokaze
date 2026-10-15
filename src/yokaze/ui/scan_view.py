"""Scan tab for the unified Yokaze app."""

from __future__ import annotations

import shutil
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from yokaze.lib.util.errors import CropFailedError, YokazeError
from yokaze.pipeline.ports.deliver import DeliverTarget
from yokaze.pipeline.wiring import default_pipeline

from yokaze import ui as ui_pkg
from .enhance_queue import EnhanceQueue
from .send_targets import SEND_TARGET_LABELS, SEND_TARGET_ORDER, enabled_send_targets
from .staging_tray import StagingTray

TARGET_LABELS = {
    "paperless": "Paperless",
    "naps2": "NAPS2",
    "stage": "Stage",
    "folder": "Folder",
}

STATE_IDLE = "idle"
STATE_SCANNING = "scanning"
STATE_FINISHING = "finishing"
STATE_AWAITING_NEXT = "next"


class CommandResult:
    def __init__(self, ok: bool, output: str) -> None:
        self.ok = ok
        self.output = output


def run_async(work: Callable[[], CommandResult], on_done) -> None:
    """Run work off the UI thread, then deliver the result on the UI thread."""

    def worker() -> None:
        result = work()
        GLib.idle_add(on_done, result)

    threading.Thread(target=worker, daemon=True).start()


class ScanDoc:
    """Accumulates one or more capture batches, then finalizes via DocumentPipeline.

    Each batch's enhancement (preprocess/crop) runs on a background
    `EnhanceQueue` so the scanner is free for the next batch the moment the
    physical scan finishes, rather than waiting for the previous batch to be
    fully processed.
    """

    def __init__(self, *, on_enhance_error: Callable[[str], None] | None = None) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="yokaze-"))
        self.pages = 0
        self.scan_name = datetime.now().strftime("scan-%Y-%m-%d-%H%M%S")
        self.archive_dir = ui_pkg.ARCHIVES / self.scan_name
        self._pipeline = default_pipeline()
        self._last_color_mode = "color"
        self._enhance_mode_locked = False
        self._crops_applied = True
        self._enhance_queue = EnhanceQueue(
            self._pipeline,
            case_slug="archive",
            on_error=on_enhance_error,
            archive_dir=self.archive_dir,
        )

    def capture(self, color_mode: str) -> CommandResult:
        self._last_color_mode = color_mode
        try:
            batch = self._pipeline.capture_only(
                self.workdir,
                color_mode=color_mode,
                save_raw_dir=self.archive_dir,
            )
            self.pages += len(batch.new_pages)
            self._enhance_queue.submit(batch)
            return CommandResult(True, f"Scanned {len(batch.new_pages)} page(s)")
        except YokazeError as exc:
            return CommandResult(False, str(exc))
        except Exception as exc:  # noqa: BLE001 — surface in UI
            return CommandResult(False, str(exc))

    def finalize(self, target: str, name: str | None = None) -> CommandResult:
        pdf_out = self.workdir / f"{name or self.scan_name}.pdf"
        try:
            enhance_error = self._enhance_queue.wait()
            if enhance_error:
                return CommandResult(False, enhance_error)
            # Capture detect/burn-in crops before optional burn for delivery record.
            crops = self._enhance_queue.page_crops()
            self._burn_crops_if_needed()
            _pdf, task_uuid = self._pipeline.finalize_workdir(
                self.workdir,
                pdf_out,
                color_mode=self._last_color_mode,
                target=target,  # type: ignore[arg-type]
                name=name or self.scan_name,
            )
            if target == "paperless" and task_uuid:
                self._record_paperless_delivery(task_uuid, crops)
            msg = _done_message(target, name or self.scan_name)
            self._cleanup_workdir()
            return CommandResult(True, msg)
        except YokazeError as exc:
            return CommandResult(False, str(exc))
        except Exception as exc:  # noqa: BLE001
            return CommandResult(False, str(exc))

    def _record_paperless_delivery(self, task_uuid: str, crops: dict) -> None:
        from yokaze.lib.pins.registry import load_stage_pins, stage_pins_repo_root
        from yokaze.lib.util.types import CropRect as LibCropRect
        from yokaze.ui.deliveries import Delivery, DeliveryPage, append_delivery
        from yokaze.ui.models import CropRect

        pages_out: list[DeliveryPage] = []
        # Use enhance-queue PageWork (archive page_num), not batch-*-page stem parse.
        for page in self._enhance_queue.enhanced_pages():
            lib = (
                page.crop
                if isinstance(page.crop, LibCropRect)
                else crops.get(page.path.name) or crops.get(page.path.stem)
            )
            if isinstance(lib, LibCropRect):
                crop = CropRect(lib.x, lib.y, lib.w, lib.h)
            else:
                # Fallback: full frame of current file (already cropped if burn-in).
                from yokaze.lib.util.io import image_size

                w, h = image_size(page.path)
                crop = CropRect(0, 0, w, h)
            pages_out.append(
                DeliveryPage(
                    archive=self.scan_name,
                    page=int(page.page_num),
                    crop=crop,
                    rotate=0,
                )
            )
        if not pages_out:
            return
        brod_pin = load_stage_pins(stage_pins_repo_root())["brod"]
        append_delivery(
            Delivery(
                task_uuid=task_uuid,
                brod_pin=brod_pin,
                pages=pages_out,
                name=self.scan_name,
            )
        )

    def set_stage_enhance(self, staging: bool) -> None:
        """Lock enhance mode on first capture: Stage keeps brod + detect overlay."""
        if self._enhance_mode_locked:
            return
        self._enhance_queue.set_apply_crop(not staging)
        self._crops_applied = not staging
        self._enhance_mode_locked = True

    def _burn_crops_if_needed(self) -> None:
        """If Stage-mode enhance left brod files, burn crop before Paperless/NAPS2."""
        if self._crops_applied:
            return
        from yokaze.pipeline.document import _list_workdir_pages

        for page in _list_workdir_pages(self.workdir):
            try:
                self._pipeline.crop.crop_page(page)
            except CropFailedError:
                pass
        self._crops_applied = True

    def finalize_to_tray(self, tray: StagingTray) -> CommandResult:
        """Wait enhance, append brod pages + detect crops to tray, drop workdir."""
        try:
            enhance_error = self._enhance_queue.wait()
            if enhance_error:
                return CommandResult(False, enhance_error)
            crops = self._enhance_queue.page_crops()
            if not crops:
                crops = _detect_workdir_crops(self._pipeline, self.workdir)
            archive_pages = {
                page.path.resolve(): int(page.page_num)
                for page in self._enhance_queue.enhanced_pages()
            }
            added = tray.append_workdir(
                self.workdir,
                color_mode=self._last_color_mode,
                scan_name=self.scan_name,
                page_crops=crops,  # type: ignore[arg-type]
                archive_pages=archive_pages,
            )
            if added == 0:
                return CommandResult(False, "Nothing to stage (no pages)")
            self._cleanup_workdir()
            return CommandResult(
                True, f"Staged {added} page(s) from {self.scan_name}"
            )
        except YokazeError as exc:
            return CommandResult(False, str(exc))
        except Exception as exc:  # noqa: BLE001
            return CommandResult(False, str(exc))

    def discard(self) -> None:
        self._enhance_queue.cancel()
        self._cleanup_workdir()

    def _cleanup_workdir(self) -> None:
        if self.workdir.is_dir():
            shutil.rmtree(self.workdir, ignore_errors=True)


def _done_message(target: str, name: str) -> str:
    if target == "paperless":
        return f"Done: {name}.pdf uploaded to Paperless"
    if target == "naps2":
        return f"Done: opened {name}.pdf in NAPS2"
    if target == "stage":
        return f"Staged: {name}"
    return f"Done: {name}.pdf"


def _detect_workdir_crops(pipeline, workdir: Path) -> dict[Path, object]:
    """Map workdir TIFF → detect_crop rect (or None on soft miss)."""
    from yokaze.lib.util.types import CropRect as LibCropRect

    out: dict[Path, LibCropRect | None] = {}
    paths = sorted(
        {p.resolve(): p for p in (*workdir.glob("*.tiff"), *workdir.glob("*.tif"))}.values(),
        key=lambda p: p.name,
    )
    for path in paths:
        try:
            rect, _w, _h = pipeline.crop.detect_crop(path)
            out[path.resolve()] = rect
        except CropFailedError:
            out[path.resolve()] = None
    return out


class ScanView(Gtk.Box):
    def __init__(
        self,
        *,
        on_archive_changed: Callable[[], None] | None = None,
        staging_tray: StagingTray | None = None,
        on_staged: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_archive_changed = on_archive_changed
        self._staging_tray = staging_tray if staging_tray is not None else StagingTray()
        self._on_staged = on_staged
        self.doc: ScanDoc | None = None
        self.state = STATE_IDLE

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

        scan_page = Adw.PreferencesPage()
        scan_page.set_title("Scan")

        self._page_group, (self.page_single, self.page_multi) = (
            self._build_choice_group(
                "Pages",
                [("Scan single page", True), ("Scan multi page", False)],
            )
        )
        self._color_group, (self.color_color, self.color_gray, self.color_auto) = (
            self._build_choice_group(
                "Color",
                [("Color", True), ("Grayscale", False), ("Auto", False)],
            )
        )
        self._send_group, self._send_buttons = self._build_send_group()

        scan_page.add(self._page_group)
        scan_page.add(self._color_group)
        scan_page.add(self._send_group)
        body.append(scan_page)
        self.refresh_send_targets()

        # Back-compat attribute names used by older tests / callers.
        self.send_paperless = self._send_buttons["paperless"]
        self.send_stage = self._send_buttons["stage"]
        self.send_naps2 = self._send_buttons["naps2"]
        self.send_folder = self._send_buttons["folder"]

        self.scan_button = Gtk.Button(label="Scan")
        self.scan_button.add_css_class("suggested-action")
        self.scan_button.connect("clicked", lambda _b: self.trigger_scan())

        self.finish_button = Gtk.Button(label="Finish")
        self.finish_button.set_visible(False)
        self.finish_button.connect("clicked", lambda _b: self._finalize())

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.set_margin_top(8)
        buttons.append(self.scan_button)
        buttons.append(self.finish_button)
        body.append(buttons)

        self.status = Gtk.Label(label="Ready", xalign=0.5)
        self.status.add_css_class("dim-label")
        self.status.set_halign(Gtk.Align.CENTER)
        self.status.set_margin_top(4)
        body.append(self.status)

    def _window(self) -> Gtk.Window | None:
        root = self.get_root()
        return root if isinstance(root, Gtk.Window) else None

    def _build_choice_group(
        self, title: str, options: list[tuple[str, bool]]
    ) -> tuple[Adw.PreferencesGroup, list[Gtk.CheckButton]]:
        group = Adw.PreferencesGroup()
        group.set_title(title)

        buttons: list[Gtk.CheckButton] = []
        radio_group: Gtk.CheckButton | None = None
        for label, active in options:
            row = Adw.ActionRow(title=label)
            button = Gtk.CheckButton()
            if radio_group is None:
                radio_group = button
            else:
                button.set_group(radio_group)
            button.set_active(active)
            button.set_can_focus(False)
            row.add_prefix(button)
            row.set_activatable_widget(button)
            group.add(row)
            buttons.append(button)

        return group, buttons

    def _build_send_group(
        self,
    ) -> tuple[Adw.PreferencesGroup, dict[str, Gtk.CheckButton]]:
        group = Adw.PreferencesGroup()
        group.set_title("Send to")
        buttons: dict[str, Gtk.CheckButton] = {}
        rows: dict[str, Adw.ActionRow] = {}
        radio_group: Gtk.CheckButton | None = None
        for target in SEND_TARGET_ORDER:
            row = Adw.ActionRow(title=f"Send to {SEND_TARGET_LABELS[target]}")
            button = Gtk.CheckButton()
            if radio_group is None:
                radio_group = button
            else:
                button.set_group(radio_group)
            button.set_can_focus(False)
            row.add_prefix(button)
            row.set_activatable_widget(button)
            group.add(row)
            buttons[target] = button
            rows[target] = row
        self._send_rows = rows
        return group, buttons

    def refresh_send_targets(self) -> None:
        """Show only destinations enabled in Settings; keep a valid selection."""
        enabled = enabled_send_targets()
        current = self._send_target()
        for target, row in self._send_rows.items():
            row.set_visible(target in enabled)
        ordered = [t for t in SEND_TARGET_ORDER if t in enabled]
        pick = current if current in enabled else (ordered[0] if ordered else "paperless")
        for target, button in self._send_buttons.items():
            button.set_active(target == pick)

    # --- Scan flow -------------------------------------------------------

    def trigger_scan(self) -> None:
        """Act on a scan request from the on-screen or hardware button."""
        if self.state in (STATE_SCANNING, STATE_FINISHING):
            self.status.set_text("Please wait...")
            return
        if self.state == STATE_IDLE:
            ui_pkg.ARCHIVES.mkdir(parents=True, exist_ok=True)
            self.doc = ScanDoc(on_enhance_error=self._on_enhance_error)
            self._capture()
        elif self.state == STATE_AWAITING_NEXT:
            self._capture()

    def _color_mode(self) -> str:
        if self.color_color.get_active():
            return "color"
        if self.color_gray.get_active():
            return "gray"
        return "auto"

    def _send_target(self) -> DeliverTarget | str:
        for target, button in self._send_buttons.items():
            if button.get_active():
                return target
        return "paperless"

    def _wants_stage(self) -> bool:
        return self._send_buttons["stage"].get_active()

    def _capture(self) -> None:
        self._set_scanning("Scanning...")
        color_mode = self._color_mode()
        assert self.doc is not None
        self.doc.set_stage_enhance(self._wants_stage())
        run_async(
            lambda: self.doc.capture(color_mode),
            self.on_capture_done,
        )

    def _notify_archive_changed(self) -> None:
        if self._on_archive_changed:
            self._on_archive_changed()

    def on_capture_done(self, result: CommandResult) -> None:
        if not result.ok:
            summary = _capture_error_summary(result.output)
            detail = _capture_error_detail(result.output)
            if self.doc is None or self.doc.pages == 0:
                self._fail(summary, detail)
                return
            self._await_next()
            self._show_alert(
                summary, detail, retry_scan=_can_retry_scan(summary)
            )
            return

        self._notify_archive_changed()

        if self.page_multi.get_active():
            self._await_next()
        else:
            self._finalize_detached()

    def _await_next(self) -> None:
        self._set_awaiting_next()
        assert self.doc is not None
        self.status.set_text(
            f"Scanned {self.doc.pages} page(s). Load the next page, then Scan."
        )

    def _finalize_detached(self) -> None:
        """Single-page: process and deliver without blocking the scan button."""
        target = self._send_target()
        doc = self.doc
        if doc is None:
            return
        pages = doc.pages
        self.doc = None
        self._set_idle()
        if target == "stage":
            self.status.set_text(f"Scanned {pages} page(s). Staging in background...")
            run_async(
                lambda: doc.finalize_to_tray(self._staging_tray),
                self._on_detached_stage_done,
            )
            return
        self.status.set_text(f"Scanned {pages} page(s). Finishing in background...")
        run_async(
            lambda: doc.finalize(target, doc.scan_name),
            self._on_detached_finalize_done,
        )

    def _on_detached_stage_done(self, result: CommandResult) -> None:
        if result.ok:
            self.status.set_text(_last_line(result.output) or "Staged.")
            self._notify_archive_changed()
            if self._on_staged:
                self._on_staged()
        else:
            self.status.set_text("Staging failed")
            self._show_alert("Staging failed", result.output)

    def _on_detached_finalize_done(self, result: CommandResult) -> None:
        if result.ok:
            self.status.set_text(_last_line(result.output) or "Done.")
            self._notify_archive_changed()
        else:
            self.status.set_text("Finishing failed")
            self._show_alert("Finishing failed", result.output)

    def _finalize(self) -> None:
        target = self._send_target()
        if target == "stage":
            self._do_stage()
        else:
            self._do_finalize(str(target))

    def _do_stage(self) -> None:
        self._set_finishing("Staging...")
        assert self.doc is not None
        run_async(
            lambda: self.doc.finalize_to_tray(self._staging_tray),
            self.on_stage_done,
        )

    def _do_finalize(self, target: str) -> None:
        self._set_finishing("Finishing...")
        assert self.doc is not None
        name = self.doc.scan_name
        run_async(
            lambda: self.doc.finalize(target, name),
            self.on_finalize_done,
        )

    def on_stage_done(self, result: CommandResult) -> None:
        self.doc = None
        if result.ok:
            self.status.set_text(_last_line(result.output) or "Staged.")
            self._set_idle()
            self._notify_archive_changed()
            if self._on_staged:
                self._on_staged()
        else:
            self._fail("Staging failed", result.output)

    def on_finalize_done(self, result: CommandResult) -> None:
        self.doc = None
        if result.ok:
            self.status.set_text(_last_line(result.output) or "Done.")
            self._set_idle()
            self._notify_archive_changed()
        else:
            self._fail("Finishing failed", result.output)

    # --- UI state --------------------------------------------------------

    def _set_scanning(self, message: str) -> None:
        self.state = STATE_SCANNING
        self.status.set_text(message)
        self.scan_button.set_sensitive(False)
        self.finish_button.set_sensitive(False)
        self._page_group.set_sensitive(False)
        self._color_group.set_sensitive(False)
        self._send_group.set_sensitive(False)

    def _set_finishing(self, message: str) -> None:
        self.state = STATE_FINISHING
        self.status.set_text(message)
        self.scan_button.set_sensitive(False)
        self.finish_button.set_sensitive(False)
        self._page_group.set_sensitive(False)
        self._color_group.set_sensitive(False)
        self._send_group.set_sensitive(False)

    def _set_awaiting_next(self) -> None:
        self.state = STATE_AWAITING_NEXT
        self.scan_button.set_label("Scan next page")
        self.scan_button.set_sensitive(True)
        self.finish_button.set_visible(True)
        self.finish_button.set_sensitive(True)
        self._color_group.set_sensitive(True)
        self._send_group.set_sensitive(True)

    def _set_idle(self) -> None:
        self.state = STATE_IDLE
        self.scan_button.set_label("Scan")
        self.scan_button.set_sensitive(True)
        self.finish_button.set_visible(False)
        self._page_group.set_sensitive(True)
        self._color_group.set_sensitive(True)
        self._send_group.set_sensitive(True)

    def _fail(self, summary: str, detail: str) -> None:
        if self.doc is not None:
            self.doc.discard()
            self.doc = None
        self.status.set_text(summary)
        self._set_idle()
        self._show_alert(summary, detail, retry_scan=_can_retry_scan(summary))

    def _on_enhance_error(self, message: str) -> None:
        """Background enhancement hit an unexpected (non-crop) error.

        Called from the EnhanceQueue worker thread — hop to the UI thread
        before touching widgets. This is a bug, not a scan-quality issue
        (a bad crop is handled silently), so it interrupts right away
        instead of waiting for Finish. The document isn't auto-discarded;
        Finish will still refuse to deliver, but the pages stay so the
        user can decide (retry, screenshot the error, etc).
        """
        GLib.idle_add(
            self._show_alert, "Background processing hit an unexpected error", message
        )

    def _show_alert(
        self, summary: str, detail: str = "", *, retry_scan: bool = False
    ) -> None:
        from .error_ui import present_error

        present_error(
            self._window(),
            summary,
            detail,
            on_retry=self.trigger_scan if retry_scan else None,
        )


def _last_line(text: str) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _capture_error_summary(output: str) -> str:
    text = output or ""
    lower = text.lower()
    if (
        "no yokaze scanner found" in lower
        or "not connected" in lower
        or "disconnected" in lower
        or "invalid argument" in lower
    ):
        return "Scanner not connected"
    if "scanner busy" in lower or "lock" in lower or "busy" in lower:
        return "Scanner busy"
    if "both the feeder" in lower or "both inputs" in lower:
        return "Document in both inputs"
    if "no document detected" in lower or "feeder empty" in lower:
        return "No document detected"
    if "jammed" in lower or "feeder jam" in lower:
        return "Feeder jammed — clear the paper path"
    if "scanner error" in lower or "device i/o" in lower or "cannot load" in lower:
        return "Scanner error"
    return "Scan failed"


def _can_retry_scan(summary: str) -> bool:
    """Empty feeder: the same scan can be started again once paper is loaded."""
    return summary == "No document detected"


def _capture_error_detail(output: str) -> str:
    for line in reversed(
        [line.strip() for line in (output or "").splitlines() if line.strip()]
    ):
        if line.startswith(
            (
                "Error:",
                "Scanner error:",
                "Scanner busy",
                "No document",
                "Documents loaded",
                "Documents in both",
            )
        ):
            return line
        if line.lower().startswith("scanimage:"):
            return line.split(":", 1)[1].strip()
    return _last_line(output)
