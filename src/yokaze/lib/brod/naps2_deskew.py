"""NAPS2-style Hough deskew angle (port of NAPS2.Sdk Deskewer.GetSkewAngle).

Source: https://github.com/cyanfish/naps2/blob/master/NAPS2.Sdk/Images/Deskewer.cs

The winning cluster is the same as upstream. Upstream then averages every
angle in it, which lets weak lines drag a good peak. The sharpest single
accumulator cell has the opposite fault on ragged text: one offset at the
wrong angle outscores the text, whose votes are spread over a few offsets.
The angle with the most votes in total tracks the text. A total that lands
on 0° is the axis-aligned bed fringe, and there the sharpest cell wins.
A near-tie between the top two angles is the same edge smeared across bins,
not a text mode, so the sharpest cell breaks it.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

ANGLE_MIN = -20.0
ANGLE_MAX = 20.0
ANGLE_STEPS = 201  # 0.2° steps
BEST_MAX_COUNT = 100
BEST_THRESHOLD_INDEX = 9
BEST_THRESHOLD_FACTOR = 0.5
CLUSTER_TARGET_SPREAD = 2.01
IGNORE_EDGE_FRACTION = 0.01
INK_LUMA_MAX = 140  # luma < 140 → ink
# Bins are 0.2°, so this matches the zero bin only.
_ZERO_MODE_DEG = 0.1
# Runner-up this close to the leader is one edge split across bins.
_NEAR_TIE_RATIO = 0.97


def naps2_skew_angle(gray: np.ndarray) -> float:
    """Return skew angle in degrees (NAPS2 GetSkewAngle), or 0.0 if unsure."""
    cluster = _winning_cluster(gray)
    if not cluster:
        return 0.0
    return _reduce_cluster(cluster)


def _reduce_cluster(cluster: list[tuple[float, int]]) -> float:
    """Angle with the most votes, unless that pick is the bed or a near-tie."""
    totals: dict[float, int] = defaultdict(int)
    for angle, count in cluster:
        totals[angle] += count
    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    mode, top = ranked[0]
    if abs(mode) < _ZERO_MODE_DEG:
        return _peak_angle(cluster)
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if top > 0 and runner_up >= top * _NEAR_TIE_RATIO:
        return _peak_angle(cluster)
    return float(mode)


def _winning_cluster(gray: np.ndarray) -> list[tuple[float, int]]:
    """Hough lines in the winning cluster, or empty when the page is unsure."""
    if gray.ndim != 2:
        raise ValueError("naps2_skew_angle expects a 2-D grayscale array")
    h, w = gray.shape
    if h < 8 or w < 8:
        return []

    ink = gray < INK_LUMA_MAX
    y_offset = int(round(h * IGNORE_EDGE_FRACTION))
    y0 = 1 + y_offset
    y1 = h - 1 - y_offset
    if y1 <= y0:
        return []
    region = ink[y0:y1, 1 : w - 1]
    below = ink[y0 + 1 : y1 + 1, 1 : w - 1]
    ys, xs = np.where(region & ~below)
    if ys.size == 0:
        return []
    ys = ys + y0
    xs = xs + 1

    angle_step = (ANGLE_MAX - ANGLE_MIN) / (ANGLE_STEPS - 1)
    angles = ANGLE_MIN + angle_step * np.arange(ANGLE_STEPS, dtype=np.float64)
    sin = np.sin(np.deg2rad(angles)).astype(np.float32)
    cos = np.cos(np.deg2rad(angles)).astype(np.float32)

    d_count = 2 * (w + h)
    scores = np.zeros((d_count, ANGLE_STEPS), dtype=np.int32)
    chunk = 4096
    for start in range(0, ys.size, chunk):
        y = ys[start : start + chunk].astype(np.float32)
        x = xs[start : start + chunk].astype(np.float32)
        d = (y[:, None] * cos[None, :] - x[:, None] * sin[None, :] + w).astype(
            np.int32
        )
        np.clip(d, 0, d_count - 1, out=d)
        for i in range(ANGLE_STEPS):
            np.add.at(scores[:, i], d[:, i], 1)

    best = _best_lines(scores, angle_step)
    if not best:
        return []
    cluster = _cluster_lines(best)
    if len(cluster) < len(best) / 2:
        return []
    return cluster


def _peak_angle(cluster: list[tuple[float, int]]) -> float:
    peak_count = max(count for _angle, count in cluster)
    peak_angles = [angle for angle, count in cluster if count == peak_count]
    return float(sum(peak_angles) / len(peak_angles))


def _best_lines(scores: np.ndarray, angle_step: float) -> list[tuple[float, int]]:
    """Top-scoring (angle, count) lines, filtered like NAPS2 GetAnglesOfBestLines."""
    flat = scores.ravel()
    if flat.size == 0 or int(flat.max()) == 0:
        return []
    n_best = min(BEST_MAX_COUNT, flat.size)
    idx = np.argpartition(flat, -n_best)[-n_best:]
    pairs = sorted(
        (
            (
                ANGLE_MIN + angle_step * (int(i) % ANGLE_STEPS),
                int(flat[i]),
            )
            for i in idx
        ),
        key=lambda ac: ac[1],
        reverse=True,
    )
    threshold = int(
        pairs[min(BEST_THRESHOLD_INDEX, len(pairs) - 1)][1] * BEST_THRESHOLD_FACTOR
    )
    return [(angle, count) for angle, count in pairs if count >= threshold]


def _cluster_lines(
    lines: list[tuple[float, int]],
) -> list[tuple[float, int]]:
    ordered = sorted(lines, key=lambda ac: ac[0])
    n = len(ordered)
    best_i = 0
    best_size = 0
    for i in range(n):
        size = 0
        for j in range(i, n):
            if ordered[j][0] < ordered[i][0] + CLUSTER_TARGET_SPREAD:
                size += 1
            else:
                break
        if size > best_size:
            best_size = size
            best_i = i
    start = ordered[best_i][0]
    return [
        item
        for item in ordered[best_i:]
        if item[0] < start + CLUSTER_TARGET_SPREAD
    ]
