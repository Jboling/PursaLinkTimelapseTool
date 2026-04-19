import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.user_settings import UserSettings


def resolve_ffmpeg_exe(configured: str) -> str:
    """
    Return an executable path FFmpeg can run. On Windows, 'ffmpeg' only works if
    it is on PATH for this process — otherwise set FFMPEG_PATH to ffmpeg.exe.
    """
    p = (configured or "ffmpeg").strip()
    candidate = Path(p)
    if candidate.is_file():
        return str(candidate.resolve())
    found = shutil.which(p)
    if found:
        return found
    raise RuntimeError(
        "FFmpeg not found. Install FFmpeg and ensure it is on your system PATH, or set "
        "FFMPEG_PATH in .env to the full path of ffmpeg.exe "
        r"(e.g. C:\ffmpeg\bin\ffmpeg.exe)."
    )


def _safe_token(s: str) -> str:
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_", ".", "+"):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)[:200]


def _format_axis_z_sortable(axis_z: float | None) -> str:
    """
    Zero-padded micrometres (µm from mm) so lexicographic sort matches height order
    (e.g. 002000 before 002800 before 010000). 6 digits → 0–999.999 mm.
    """
    if axis_z is None:
        return "na"
    um = int(round(max(0.0, float(axis_z)) * 1000.0))
    um = min(um, 999_999)
    return f"{um:06d}"


def _format_axis_z_mm_readable(axis_z: float | None) -> str:
    """Human-readable mm string for templates; not sort-safe."""
    if axis_z is None:
        return "na"
    s = f"{float(axis_z):.4f}".rstrip("0").rstrip(".")
    return _safe_token(s) if s else "na"


def build_filename(
    settings: UserSettings,
    printer_state: str,
    job_id: str,
    progress: str,
    job_state: str,
    axis_z: float | None = None,
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    z_sort = _format_axis_z_sortable(axis_z)
    z_mm = _format_axis_z_mm_readable(axis_z)
    base = settings.filename_template.format(
        timestamp=ts,
        printer_state=_safe_token(printer_state),
        job_id=_safe_token(job_id),
        progress=_safe_token(progress),
        job_state=_safe_token(job_state),
        axis_z=z_sort,
        z=z_sort,
        axis_z_mm=z_mm,
        axis_z_sort=z_sort,
    )
    if not base.lower().endswith((".jpg", ".jpeg")):
        base = f"{base}.jpg"
    return base


def resolve_output_path(
    settings: UserSettings,
    filename: str,
    job_id: str | None = None,
) -> Path:
    root = Path(settings.output_dir).expanduser().resolve()
    if settings.subfolder_by_date:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        root = root / day
    if settings.subfolder_by_job_id:
        jid = (job_id or "").strip()
        if not jid:
            jid = "_no_job"
        root = root / _safe_token(jid)
    root.mkdir(parents=True, exist_ok=True)
    return root / filename


def grab_frame_rtsp(ffmpeg_exe: str, rtsp_url: str, dest: Path, jpeg_q: int) -> None:
    exe = resolve_ffmpeg_exe(ffmpeg_exe)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-vframes",
        "1",
        "-q:v",
        str(jpeg_q),
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, timeout=60, capture_output=True)
    except FileNotFoundError as e:
        raise RuntimeError(
            "Could not execute FFmpeg. Install FFmpeg, add it to PATH, or set FFMPEG_PATH "
            r"in .env to the full path of ffmpeg.exe (e.g. C:\ffmpeg\bin\ffmpeg.exe)."
        ) from e
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(err or f"ffmpeg exited with {proc.returncode}")
