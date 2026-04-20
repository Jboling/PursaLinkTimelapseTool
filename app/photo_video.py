"""Assemble ordered image files from a directory into an MP4 via ffmpeg concat."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
)


def list_images(input_dir: Path) -> list[Path]:
    if not input_dir.is_dir():
        raise ValueError(f"Not a directory: {input_dir}")
    out: list[Path] = []
    for p in input_dir.iterdir():
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
            out.append(p)
    out.sort(key=lambda x: x.name.lower())
    return out


def filter_by_names(all_images: list[Path], include_names: list[str]) -> list[Path]:
    """Keep list_images order; only include paths whose name is in include_names."""
    if not include_names:
        raise ValueError("include_names must not be empty")
    have = {p.name for p in all_images}
    missing = [n for n in include_names if n not in have]
    if missing:
        show = ", ".join(missing[:12])
        more = "…" if len(missing) > 12 else ""
        raise ValueError(f"Unknown image name(s): {show}{more}")
    want = frozenset(include_names)
    return [p for p in all_images if p.name in want]


def resolve_safe_image_file(input_dir: Path, name: str) -> Path | None:
    """Basename only; resolved path must stay under input_dir."""
    n = name.strip()
    if not n or n != Path(n).name:
        return None
    if "/" in n or "\\" in n:
        return None
    base = input_dir.resolve()
    candidate = (base / n).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    return candidate


def _concat_path_for_ffmpeg(path: Path) -> str:
    """Single-quoted path segment for ffconcat; use forward slashes."""
    s = str(path.resolve()).replace("\\", "/")
    return s.replace("'", "'\\''")


def build_timelapse_mp4(
    ffmpeg_bin: str,
    images: list[Path],
    output_mp4: Path,
    fps: float,
    hold_last_seconds: float = 0.0,
) -> str:
    """
    Run ffmpeg. Returns stderr+stdout combined text on success.
    Raises RuntimeError on failure.

    hold_last_seconds: extra time (beyond 1/fps) to show the final image.
    """
    if fps <= 0 or fps > 120:
        raise ValueError("fps must be between 0 and 120")
    if hold_last_seconds < 0 or hold_last_seconds > 600:
        raise ValueError("hold_last_seconds must be between 0 and 600")
    if not images:
        raise ValueError("No images")
    output_mp4.parent.mkdir(parents=True, exist_ok=True)

    frame_dur = 1.0 / fps
    hold = max(0.0, float(hold_last_seconds))
    last_q = _concat_path_for_ffmpeg(images[-1])

    # ffconcat layout: each image once with its duration, plus a trailing duplicate of
    # the final image so the concat demuxer honors the last `duration`. We intentionally
    # write the last duration as (frame_dur + hold) and we rely on `-t` below to clip
    # the output to the exact expected total duration. Different ffmpeg builds handle
    # the trailing-duplicate case differently (some apply the last `duration` to both
    # entries, producing ~2x the hold; others without the trailing entry drop the
    # duration entirely). Clamping with `-t` makes the result deterministic either way.
    lines = ["ffconcat version 1.0"]
    for p in images[:-1]:
        lines.append(f"file '{_concat_path_for_ffmpeg(p)}'")
        lines.append(f"duration {frame_dur:.6f}")
    lines.append(f"file '{last_q}'")
    lines.append(f"duration {frame_dur + hold:.6f}")
    lines.append(f"file '{last_q}'")

    total_duration = len(images) * frame_dur + hold

    fd, list_path = tempfile.mkstemp(suffix=".ffconcat", text=True)
    os.close(fd)
    try:
        Path(list_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        # -vf fps + -fps_mode cfr materialize the hold as duplicated frames at the target
        # fps (otherwise a long still with extended PTS is silently truncated on output).
        # -t clamps to the exact intended total duration so the trailing-duplicate quirk
        # of the concat demuxer cannot bleed extra time into the final clip.
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_path,
            "-vf",
            f"fps={fps:g}",
            "-fps_mode",
            "cfr",
            "-t",
            f"{total_duration:.6f}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_mp4),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise RuntimeError(out.strip() or f"ffmpeg exited {proc.returncode}")
        return out.strip()
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass
