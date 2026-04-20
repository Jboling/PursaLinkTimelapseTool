"""Layer marker parsing and sdpos -> layer lookup."""

from __future__ import annotations

import bisect
import re

_RE_LAYER_VALUE = re.compile(br"^\s*;\s*LAYER\s*:\s*(-?\d+)\s*$", re.IGNORECASE)
_RE_LAYER_CHANGE = re.compile(br"^\s*;\s*LAYER_CHANGE\b", re.IGNORECASE)


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

