"""Adwaita CSS for the crop lab."""

CSS = """
window {
  background-color: @window_bg_color;
}

.lab-sidebar {
  min-width: 220px;
  border-right: 1px solid alpha(currentColor, 0.15);
}

/* Stage adaptive thumb grid: denser cells in a wider pane. */
.lab-sidebar .stage-thumb-grid {
  margin-top: 2px;
}

.lab-sidebar .stage-thumb-grid .stage-thumb {
  border-radius: 6px;
  padding: 2px;
}

.lab-canvas {
  background-color: #1a1a1a;
}

.lab-canvas.lab-canvas-light {
  background-color: #ffffff;
}

/* Outer padding around the canvas. */
.lab-canvas-stage {
  padding: 16px;
}

.lab-content {
  /* Shared viewer host for Annotate / Preview / History */
  min-height: 0;
}

.lab-muted {
  opacity: 0.7;
}

/* Tune Finish: group tone / balance / sharpen / white-lock cleanly. */
.tune-section {
  padding: 6px 0 14px 0;
  margin-bottom: 6px;
  border-bottom: 1px solid alpha(currentColor, 0.12);
}

.tune-section-title {
  margin-bottom: 2px;
}

/* Knob value: tight width so edit chrome stays compact on the right. */
.tune-value {
  min-width: 4ch;
  max-width: 4ch;
}

.tune-actions {
  margin-top: 4px;
}

/* Adwaita tooltips default to translucent; sliders bleed through. */
tooltip.background {
  background-color: @popover_bg_color;
  opacity: 1;
}

tooltip label {
  color: @popover_fg_color;
}

.lab-score {
  font-family: monospace;
  font-size: 12px;
}


.annotate-cases {
  margin-top: 4px;
}

/* Flat case/delivery buttons: Adwaita suggested-action is nearly invisible here. */
.lab-sidebar .annotate-cases .stage-thumb {
  border-radius: 8px;
}

.lab-sidebar .annotate-cases .stage-thumb:hover {
  background-color: alpha(currentColor, 0.08);
}

.lab-sidebar .annotate-cases button.lab-case-selected,
.lab-sidebar .annotate-cases button.lab-case-selected:hover,
.lab-sidebar .annotate-cases .stage-thumb.lab-case-selected,
.lab-sidebar .annotate-cases .stage-thumb.lab-case-selected:hover {
  background-color: @accent_bg_color;
  color: @accent_fg_color;
  font-weight: 600;
}

.lab-sidebar .annotate-cases .stage-thumb.stage-page-current {
  outline: 2px solid @accent_fg_color;
  outline-offset: -2px;
}

.lab-sidebar .annotate-cases .stage-thumb.stage-drop-before {
  box-shadow: inset 3px 0 0 0 @accent_color;
}

.lab-sidebar .annotate-cases .stage-thumb.stage-drop-after {
  box-shadow: inset -3px 0 0 0 @accent_color;
}

.annotate-case-expander {
  margin-bottom: 4px;
}

.annotate-page-row {
  min-height: 30px;
}

row.annotate-page-row .crop-band-fail {
  color: @error_color;
}

row.annotate-page-row .crop-band-warn {
  color: #9a7200;
}

.dark row.annotate-page-row .crop-band-warn {
  color: #f5d90a;
}

.lab-sidebar list.navigation-sidebar row.annotate-page-row:hover .crop-band-fail,
.lab-sidebar list.navigation-sidebar row.annotate-page-row:selected .crop-band-fail,
.lab-sidebar list.navigation-sidebar row.annotate-page-row:selected:focus .crop-band-fail,
.lab-sidebar list.navigation-sidebar row.annotate-page-row:selected:focus-visible .crop-band-fail {
  color: @error_color;
}

.lab-sidebar list.navigation-sidebar row.annotate-page-row:hover .crop-band-warn,
.lab-sidebar list.navigation-sidebar row.annotate-page-row:selected .crop-band-warn,
.lab-sidebar list.navigation-sidebar row.annotate-page-row:selected:focus .crop-band-warn,
.lab-sidebar list.navigation-sidebar row.annotate-page-row:selected:focus-visible .crop-band-warn {
  color: #9a7200;
}

.dark .lab-sidebar list.navigation-sidebar row.annotate-page-row:hover .crop-band-warn,
.dark .lab-sidebar list.navigation-sidebar row.annotate-page-row:selected .crop-band-warn,
.dark .lab-sidebar list.navigation-sidebar row.annotate-page-row:selected:focus .crop-band-warn,
.dark .lab-sidebar list.navigation-sidebar row.annotate-page-row:selected:focus-visible .crop-band-warn {
  color: #f5d90a;
}

.lab-sidebar .annotate-case-expander.annotate-case-focused {
  background-color: alpha(currentColor, 0.08);
  border-radius: 6px;
  outline: none;
  box-shadow: none;
}

.lab-sidebar .annotate-case-expander,
.lab-sidebar .annotate-case-expander:focus,
.lab-sidebar .annotate-case-expander:focus-visible,
.lab-sidebar .annotate-case-expander:focus-within {
  background-color: transparent;
  outline: none;
  box-shadow: none;
}

.lab-sidebar list.navigation-sidebar row.annotate-page-row:hover,
.lab-sidebar list.navigation-sidebar row.annotate-page-row:selected,
.lab-sidebar list.navigation-sidebar row.annotate-page-row:selected:focus,
.lab-sidebar list.navigation-sidebar row.annotate-page-row:selected:focus-visible {
  background-color: alpha(currentColor, 0.08);
  background-image: none;
  color: @window_fg_color;
  outline: none;
  box-shadow: none;
}

.log-better {
  color: @success_color;
}

.log-worse {
  color: @error_color;
}

.log-summary {
  margin-top: 4px;
}

.log-summary-grid {
  background: transparent;
}

.log-summary-head {
  padding: 6px 8px 8px 8px;
}

.log-summary-group {
  padding-bottom: 2px;
  font-weight: 600;
  opacity: 0.9;
}

/* Border lives on this box (or undimmed data cell) — never on dim-label. */
.log-summary-vrule {
  border-left: 1px solid alpha(@window_fg_color, 0.18);
  padding-left: 12px;
}

.log-summary-vrule > .log-summary-head {
  padding-left: 0;
}

.log-summary-rule {
  margin: 0;
  min-height: 1px;
  background-color: alpha(@window_fg_color, 0.18);
}

.log-summary-cell {
  padding: 4px 8px;
  min-height: 28px;
}

.log-summary-selected {
  background-color: alpha(@accent_bg_color, 0.28);
}

.log-commit-kind {
  font-family: monospace;
  font-weight: 600;
  opacity: 0.8;
}

.log-summary-lead {
  margin-top: 6px;
  font-size: 1.05em;
}

.log-verdict {
  padding: 1px 9px;
  border-radius: 999px;
  font-size: 0.85em;
  font-weight: 700;
}

.log-verdict-keep {
  color: @success_color;
  background-color: alpha(@success_color, 0.15);
}

.log-verdict-revert {
  color: @error_color;
  background-color: alpha(@error_color, 0.15);
}

.log-verdict-baseline {
  background-color: alpha(currentColor, 0.1);
}

.log-band-changes {
  margin-top: 2px;
}

.log-band-pages {
  margin-top: 2px;
}

.log-band-chip {
  padding: 2px 10px;
  border-radius: 999px;
  font-family: monospace;
  font-size: 0.85em;
  font-weight: 700;
}

.log-band-row {
  min-height: 28px;
  padding: 0;
}

.log-band-badge {
  padding: 1px 9px;
  border-radius: 999px;
  font-family: monospace;
  font-size: 0.85em;
  font-weight: 700;
}

.log-band-pass {
  color: @success_color;
  background-color: alpha(@success_color, 0.14);
}

.log-band-warn {
  color: @warning_color;
  background-color: alpha(@warning_color, 0.16);
}

.log-band-fail {
  color: @error_color;
  background-color: alpha(@error_color, 0.14);
}

.log-band-none {
  background-color: alpha(currentColor, 0.08);
  opacity: 0.75;
}
"""
