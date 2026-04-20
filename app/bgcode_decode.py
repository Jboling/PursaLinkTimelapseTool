"""BGCODE -> text G-code conversion helpers (reference-style)."""

from __future__ import annotations

import os
import tempfile
import time

_BGCODE_MAGIC = b"GCDE"


def is_bgcode_bytes(data: bytes, name: str | None = None) -> bool:
    if data[:4] == _BGCODE_MAGIC:
        return True
    if name and name.lower().endswith(".bgcode"):
        return True
    return False


def convert_bgcode_to_gcode_like_prusa_marlin(bgcode_bytes: bytes) -> str:
    """
    Extract only G-code blocks from a BGCODE blob using pybgcode.
    Mirrors the reference project approach.
    """
    try:
        import pybgcode as bg  # type: ignore[import-not-found]
        from pybgcode._bgcode import (  # type: ignore[import-not-found]
            GCodeBlock,
            skip_block_content,
        )
    except ImportError as e:
        raise RuntimeError(
            "BGCODE decode requires 'pybgcode'. Install it to enable .bgcode support."
        ) from e

    gcode_text: list[str] = []
    # On Windows, auto-cleanup of TemporaryDirectory can race with pybgcode's native file handle.
    # Use explicit file lifecycle + retry cleanup to avoid intermittent WinError 32.
    fd, job_path = tempfile.mkstemp(prefix="prusalink_", suffix=".bgcode")
    out_fd, out_path = tempfile.mkstemp(prefix="prusalink_", suffix=".gcode")
    fp = None
    out_fp = None
    block_parse_failed = False
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(bgcode_bytes)
        os.close(out_fd)

        fp = bg.open(job_path, "r")
        if fp is None:
            raise RuntimeError("Failed to open BGCODE temp file.")
        file_header = bg.FileHeader()
        file_header.read(fp)
        block_header = bg.BlockHeader()

        res = bg.read_next_block_header(fp, file_header, block_header)
        while res == bg.EResult.Success:
            if block_header.type == bg.EBlockType.GCode.value:
                gcode_block = GCodeBlock()
                res = gcode_block.read_data(fp, file_header, block_header)
                if res != bg.EResult.Success:
                    block_parse_failed = True
                    break
                gcode_text.append(gcode_block.raw_data)
            else:
                skip_block_content(fp, file_header, block_header)
            res = bg.read_next_block_header(fp, file_header, block_header)

        if block_parse_failed:
            # Fallback path: ask libbgcode to convert the whole file to ASCII text.
            in_fp = bg.open(job_path, "rb")
            out_fp = bg.open(out_path, "w")
            if in_fp is None or out_fp is None:
                raise RuntimeError("Failed to open BGCODE converter file handles.")
            conv_res = bg.from_binary_to_ascii(in_fp, out_fp, True)
            bg.close(in_fp)
            bg.close(out_fp)
            out_fp = None
            if conv_res != bg.EResult.Success:
                raise RuntimeError("Failed to convert BGCODE to ASCII.")
            with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read()
            if not txt.strip():
                raise RuntimeError("Converted BGCODE text is empty.")
            return txt
    finally:
        if fp is not None:
            try:
                bg.close(fp)
            except Exception:
                pass
        if out_fp is not None:
            try:
                bg.close(out_fp)
            except Exception:
                pass
        # Give native layer a brief moment to release the file handle before unlinking.
        for i in range(6):
            try:
                os.unlink(job_path)
                break
            except OSError:
                if i == 5:
                    pass
                else:
                    time.sleep(0.05 * (i + 1))
        for i in range(6):
            try:
                os.unlink(out_path)
                break
            except OSError:
                if i == 5:
                    pass
                else:
                    time.sleep(0.05 * (i + 1))
    return "".join(gcode_text)


def normalize_print_file_to_text_bytes(data: bytes, name: str | None = None) -> bytes:
    """
    Return UTF-8 encoded text G-code bytes for both plain G-code and BGCODE.
    """
    if is_bgcode_bytes(data, name):
        text = convert_bgcode_to_gcode_like_prusa_marlin(data)
        return text.encode("utf-8", errors="replace")
    return data

