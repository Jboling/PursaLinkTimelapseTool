"""Layer marker parsing and sdpos -> layer lookup."""

from __future__ import annotations

import bisect
import re

_RE_LAYER_VALUE = re.compile(br"^\s*;\s*LAYER\s*:\s*(-?\d+)\s*$", re.IGNORECASE)
_RE_LAYER_CHANGE = re.compile(br"^\s*;\s*LAYER_CHANGE\b", re.IGNORECASE)
_RE_Z_COMMENT = re.compile(br"^\s*;\s*Z\s*:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_RE_HEIGHT_COMMENT = re.compile(br"^\s*;\s*HEIGHT\s*:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_RE_G1_Z = re.compile(br"^\s*G[01]\b[^\n;]*?\bZ(-?\d+(?:\.\d+)?)", re.IGNORECASE)


def layer_starts_from_bytes(data: bytes) -> list[tuple[int, int]]:
    """
    Parse text-style G-code bytes and return sorted tuples of:
      (layer_index, sdpos_start_byte)
    """
    out: list[tuple[int, int]] = []
    current_layer = -1
    next_layer = 0
    has_explicit_layer_values = False
    offset = 0
    lines = data.splitlines(keepends=True)
    for ln in lines:
        m = _RE_LAYER_VALUE.match(ln.rstrip(b"\r\n"))
        if m:
            try:
                current_layer = int(m.group(1))
                has_explicit_layer_values = True
                if current_layer + 1 > next_layer:
                    next_layer = current_layer + 1
            except ValueError:
                pass
            offset += len(ln)
            continue
        if _RE_LAYER_CHANGE.match(ln.rstrip(b"\r\n")):
            if has_explicit_layer_values and current_layer >= 0:
                idx = current_layer
            else:
                idx = next_layer
                next_layer += 1
            out.append((idx, offset))
        offset += len(ln)
    if out:
        # Keep first seen start offset for each layer index.
        dedup: dict[int, int] = {}
        for idx, pos in out:
            if idx not in dedup:
                dedup[idx] = pos
        return sorted(dedup.items(), key=lambda x: x[1])
    return []


def layer_at_sdpos(
    layer_starts: list[tuple[int, int]], sdpos: int
) -> tuple[int | None, str | None]:
    if not layer_starts:
        return None, "no_layer_markers"
    if sdpos < 0:
        return None, "sdpos_negative"
    starts = [p for _, p in layer_starts]
    i = bisect.bisect_right(starts, sdpos) - 1
    if i < 0:
        return None, "before_first_layer"
    return layer_starts[i][0], None


def layer_z_heights_from_bytes(data: bytes) -> list[tuple[int, float]]:
    """
    Extract per-layer nominal Z heights from decoded G-code bytes.

    Returns a sorted list of (layer_index, z_mm), picking (in priority order):
      1. `;Z:<val>` comment after a `;LAYER_CHANGE`
      2. accumulated `;HEIGHT:<val>` after a `;LAYER_CHANGE`
      3. first absolute G0/G1 Z-move after the `;LAYER_CHANGE`
    """
    out: list[tuple[int, float]] = []
    current_layer = -1
    next_layer = 0
    has_explicit_layer_values = False
    this_layer_idx: int | None = None
    this_layer_z: float | None = None
    this_layer_accum_height: float = 0.0

    def _commit() -> None:
        nonlocal this_layer_idx, this_layer_z
        if this_layer_idx is not None and this_layer_z is not None:
            out.append((this_layer_idx, float(this_layer_z)))
        this_layer_idx = None
        this_layer_z = None

    for ln in data.splitlines(keepends=True):
        line = ln.rstrip(b"\r\n")
        m = _RE_LAYER_VALUE.match(line)
        if m:
            try:
                current_layer = int(m.group(1))
                has_explicit_layer_values = True
                if current_layer + 1 > next_layer:
                    next_layer = current_layer + 1
            except ValueError:
                pass
            continue
        if _RE_LAYER_CHANGE.match(line):
            _commit()
            if has_explicit_layer_values and current_layer >= 0:
                this_layer_idx = current_layer
            else:
                this_layer_idx = next_layer
                next_layer += 1
            continue
        if this_layer_idx is None or this_layer_z is not None:
            continue
        mz = _RE_Z_COMMENT.match(line)
        if mz:
            try:
                this_layer_z = float(mz.group(1))
            except ValueError:
                pass
            continue
        mh = _RE_HEIGHT_COMMENT.match(line)
        if mh:
            try:
                this_layer_accum_height += float(mh.group(1))
                this_layer_z = this_layer_accum_height
            except ValueError:
                pass
            continue
        mg = _RE_G1_Z.match(line)
        if mg:
            try:
                this_layer_z = float(mg.group(1))
            except ValueError:
                pass
            continue
    _commit()
    if not out:
        return []
    dedup: dict[int, float] = {}
    for idx, z in out:
        if idx not in dedup:
            dedup[idx] = z
    return sorted(dedup.items(), key=lambda item: item[1])


def layer_at_z(
    layer_z_heights: list[tuple[int, float]],
    z: float,
    tolerance: float = 0.08,
) -> tuple[int | None, str | None]:
    """
    Map current Z height to a layer index using the parsed Z-table.

    Picks the largest table z <= (current z + tolerance). During retraction
    lifts (typically ~0.4mm) Z exceeds any layer's nominal Z by more than
    `tolerance`, so we return `(None, "retraction_lift")` to let the caller
    hold the previous index instead of jumping forward.
    """
    if not layer_z_heights:
        return None, "no_layer_z_table"
    zs = [z_val for _, z_val in layer_z_heights]
    target = z + tolerance
    i = bisect.bisect_right(zs, target) - 1
    if i < 0:
        return layer_z_heights[0][0], None
    # Reject if Z is well above the selected layer's nominal Z (retraction lift).
    if z - zs[i] > tolerance:
        return None, "retraction_lift"
    return layer_z_heights[i][0], None

