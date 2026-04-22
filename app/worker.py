import asyncio
import bisect
import math
import os
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from app.bgcode_decode import normalize_print_file_to_text_bytes
from app.env_config import EnvConfig
from app.gcode_cache import GcodeCache, key_from_job
from app.gcode_layers import (
    layer_at_sdpos,
    layer_at_z,
    layer_starts_from_bytes,
    layer_z_heights_from_bytes,
)
from app.metrics_state import metrics_state
from app.prusa_client import PrusaClient
from app.snapshot import build_filename, grab_frame_rtsp, resolve_output_path
from app.user_settings import UserSettings, load_user_settings

GCODE_CACHE_ROOT = Path("cache/gcode")
Z_TOLERANCE = 1e-4
# Require this many consecutive polls at the same axis_z before snapping (filters brief Z blips).
AXIS_Z_STABLE_POLLS = 2


@dataclass
class WorkerState:
    running: bool = False
    last_snapshot_at: Optional[str] = None
    last_snapshot_path: Optional[str] = None
    snapshots_taken: int = 0
    last_error: Optional[str] = None
    last_printer_state: Optional[str] = None
    last_loop_at: Optional[str] = None
    stop_reason: Optional[str] = None


@dataclass
class Runtime:
    task: Optional[asyncio.Task] = None
    state: WorkerState = field(default_factory=WorkerState)
    last_snap_fingerprint: Optional[str] = None
    last_snap_wallclock: Optional[float] = None
    last_polled_axis_z: Optional[float] = None
    job_id_snapped_at_100: Optional[str] = None
    axis_z_prev_poll: Optional[float] = None
    axis_z_same_streak: int = 0
    idle_since_monotonic: Optional[float] = None
    layer_map_job_id: Optional[str] = None
    layer_starts: list[tuple[int, int]] = field(default_factory=list)
    last_snap_layer_idx: Optional[int] = None
    layer_current_index: Optional[int] = None
    layer_total: Optional[int] = None
    layer_map_error: Optional[str] = None
    gcode_download_status: str = "idle"
    gcode_download_job_id: Optional[str] = None
    gcode_download_display_name: Optional[str] = None
    gcode_download_bytes: Optional[int] = None
    gcode_download_layer_markers: Optional[int] = None
    gcode_download_error: Optional[str] = None
    move_xy_points: list[tuple[int, float, float]] = field(default_factory=list)
    pending_layer_idx: Optional[int] = None
    pending_layer_since: Optional[float] = None
    layer_z_heights: list[tuple[int, float]] = field(default_factory=list)
    last_axis_z: Optional[float] = None


_RE_MOVE = re.compile(br"^\s*G0?1\b", re.IGNORECASE)
_RE_X = re.compile(br"\bX(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_RE_Y = re.compile(br"\bY(-?\d+(?:\.\d+)?)", re.IGNORECASE)


def _job_progress_value(job: dict | None) -> float | None:
    if not job:
        return None
    p = job.get("progress")
    if p is None:
        return None
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def _fingerprint(status: dict, job: dict | None) -> str:
    p = status.get("printer") or {}
    parts = [
        str(p.get("state", "")),
        str(p.get("temp_nozzle", "")),
        str(p.get("temp_bed", "")),
    ]
    if job:
        parts.extend(
            [
                str(job.get("id", "")),
                str(job.get("state", "")),
                str(job.get("progress", "")),
            ]
        )
    return "|".join(parts)


def _clear_layer_progress(rt: Runtime) -> None:
    rt.layer_map_job_id = None
    rt.layer_starts = []
    rt.last_snap_layer_idx = None
    rt.layer_current_index = None
    rt.layer_total = None
    rt.layer_map_error = None
    rt.gcode_download_status = "idle"
    rt.gcode_download_job_id = None
    rt.gcode_download_display_name = None
    rt.gcode_download_bytes = None
    rt.gcode_download_layer_markers = None
    rt.gcode_download_error = None
    rt.move_xy_points = []
    rt.pending_layer_idx = None
    rt.pending_layer_since = None
    rt.layer_z_heights = []
    rt.last_axis_z = None


def _extract_xy_points(data: bytes) -> list[tuple[int, float, float]]:
    """
    Build a sparse stream of (sdpos_offset, x, y) from G0/G1 lines.
    Coordinates are interpreted as absolute positions (typical sliced output).
    """
    out: list[tuple[int, float, float]] = []
    cur_x: float | None = None
    cur_y: float | None = None
    offset = 0
    for ln in data.splitlines(keepends=True):
        line = ln.rstrip(b"\r\n")
        if _RE_MOVE.match(line):
            mx = _RE_X.search(line)
            my = _RE_Y.search(line)
            if mx is not None:
                try:
                    cur_x = float(mx.group(1))
                except ValueError:
                    pass
            if my is not None:
                try:
                    cur_y = float(my.group(1))
                except ValueError:
                    pass
            if cur_x is not None and cur_y is not None:
                out.append((offset, cur_x, cur_y))
        offset += len(ln)
    return out


def _xy_at_sdpos(points: list[tuple[int, float, float]], sdpos: int) -> tuple[float, float] | None:
    if not points:
        return None
    offsets = [p[0] for p in points]
    i = bisect.bisect_right(offsets, sdpos) - 1
    if i < 0:
        return None
    _, x, y = points[i]
    return x, y


def _xy_in_clear_zone(settings: UserSettings, x: float, y: float) -> bool:
    x0 = min(settings.clear_zone_x_min, settings.clear_zone_x_max)
    x1 = max(settings.clear_zone_x_min, settings.clear_zone_x_max)
    y0 = min(settings.clear_zone_y_min, settings.clear_zone_y_max)
    y1 = max(settings.clear_zone_y_min, settings.clear_zone_y_max)
    return x0 <= x <= x1 and y0 <= y <= y1


async def _ensure_layer_map(job: dict, rt: Runtime, client: PrusaClient) -> None:
    job_id = str(job.get("id", "")) if job else ""
    if not job_id:
        return
    if rt.layer_map_job_id == job_id and rt.layer_starts:
        return

    rt.layer_map_job_id = job_id
    rt.last_snap_layer_idx = None
    rt.layer_starts = []
    rt.gcode_download_status = "downloading"
    rt.gcode_download_job_id = job_id
    rt.gcode_download_error = None

    key = key_from_job(job)
    cache = GcodeCache(GCODE_CACHE_ROOT)

    if key is not None:
        hit = await asyncio.to_thread(cache.get, key)
        if hit is not None:
            data, meta = hit
            name = str(meta.get("content_name") or key.display_name)
            try:
                normalized = normalize_print_file_to_text_bytes(data, name)
            except Exception as e:
                rt.gcode_download_status = "failed"
                rt.gcode_download_error = f"bgcode_decode_failed:{e!s}"
                rt.state.last_error = (
                    "sdpos_layer: cached BGCODE could not be decoded. "
                    "Install pybgcode or use plain .gcode."
                )
                return
            starts = layer_starts_from_bytes(normalized)
            if starts:
                rt.layer_starts = starts
                rt.move_xy_points = _extract_xy_points(normalized)
                rt.layer_z_heights = layer_z_heights_from_bytes(normalized)
                rt.layer_total = max(i for i, _ in starts) + 1
                rt.gcode_download_status = "success"
                rt.gcode_download_display_name = name
                rt.gcode_download_bytes = len(data)
                rt.gcode_download_layer_markers = len(starts)
                rt.gcode_download_error = f"cache_hit:{meta.get('source', 'unknown')}"
                return

    got = await asyncio.to_thread(client.download_print_file, job)
    if not got:
        rt.gcode_download_status = "failed"
        detail = client.last_download_debug or "unknown"
        rt.gcode_download_error = f"download_failed:{detail}"
        rt.state.last_error = (
            "sdpos_layer: could not download current print file. "
            "Firmware may block active file reads; keep a cached copy for this job."
        )
        return

    data, name = got
    try:
        normalized = normalize_print_file_to_text_bytes(data, name)
    except Exception as e:
        rt.gcode_download_status = "failed"
        rt.gcode_download_display_name = name
        rt.gcode_download_bytes = len(data)
        rt.gcode_download_error = f"bgcode_decode_failed:{e!s}"
        rt.state.last_error = (
            "sdpos_layer: BGCODE decode failed. "
            "Install pybgcode to support .bgcode files."
        )
        return
    starts = layer_starts_from_bytes(normalized)
    if not starts:
        rt.gcode_download_status = "failed"
        rt.gcode_download_display_name = name
        rt.gcode_download_bytes = len(data)
        rt.gcode_download_error = "no_layer_markers_in_file"
        rt.state.last_error = (
            "sdpos_layer: no layer markers found in downloaded file."
        )
        return

    rt.layer_starts = starts
    rt.move_xy_points = _extract_xy_points(normalized)
    rt.layer_z_heights = layer_z_heights_from_bytes(normalized)
    rt.layer_total = max(i for i, _ in starts) + 1
    rt.gcode_download_status = "success"
    rt.gcode_download_display_name = name
    rt.gcode_download_bytes = len(data)
    rt.gcode_download_layer_markers = len(starts)
    rt.gcode_download_error = None
    rt.state.last_error = None

    if key is not None:
        try:
            await asyncio.to_thread(
                cache.put, key, data, content_name=name, source="prusalink_download"
            )
        except OSError:
            pass


def _update_layer_progress(rt: Runtime, status: dict | None = None) -> None:
    if status is not None:
        printer = status.get("printer") or {}
        z_raw = printer.get("axis_z")
        try:
            if z_raw is not None:
                rt.last_axis_z = float(z_raw)
        except (TypeError, ValueError):
            pass

    if not rt.layer_starts:
        rt.layer_current_index = None
        rt.layer_map_error = None
        return

    # Primary: axis_z -> parsed Z-table. Handles both .gcode and .bgcode.
    if rt.layer_z_heights and rt.last_axis_z is not None:
        idx, err = layer_at_z(rt.layer_z_heights, rt.last_axis_z)
        if idx is None:
            # Z-hop above all layers: hold the last known layer.
            rt.layer_map_error = err
            return
        rt.layer_current_index = idx
        rt.layer_map_error = None
        return

    # Fallback: sdpos-based lookup (works for plain .gcode).
    sdpos = metrics_state.sdpos
    if sdpos is None:
        rt.layer_current_index = None
        rt.layer_map_error = "waiting_axis_z_or_sdpos"
        return
    idx, err = layer_at_sdpos(rt.layer_starts, sdpos)
    rt.layer_current_index = idx
    rt.layer_map_error = err


async def _try_snap_sdpos_layer(
    env: EnvConfig,
    settings: UserSettings,
    status: dict,
    job: dict | None,
    printer_state: str,
    axis_z: float | None,
    rt: Runtime,
) -> bool:
    if not job or not rt.layer_starts:
        return False
    sdpos = metrics_state.sdpos
    if sdpos is None:
        rt.state.last_error = (
            "sdpos_layer: waiting for UDP sdpos. Configure M334 <host> <port> then M331 sdpos."
        )
        return False

    layer_idx, err = layer_at_sdpos(rt.layer_starts, sdpos)
    if layer_idx is None:
        rt.state.last_error = f"sdpos_layer: {err or 'unknown'} (sdpos={sdpos})"
        return False
    if rt.last_snap_layer_idx is not None and layer_idx <= rt.last_snap_layer_idx:
        return False

    now_ts = time.monotonic()
    if settings.clear_zone_enabled:
        if rt.pending_layer_idx != layer_idx:
            rt.pending_layer_idx = layer_idx
            rt.pending_layer_since = now_ts
        xy = _xy_at_sdpos(rt.move_xy_points, sdpos)
        in_zone = False
        if xy is not None:
            in_zone = _xy_in_clear_zone(settings, xy[0], xy[1])
        waited = (
            (now_ts - rt.pending_layer_since)
            if rt.pending_layer_since is not None
            else 0.0
        )
        if (not in_zone) and waited < settings.clear_zone_wait_seconds:
            if xy is None:
                rt.state.last_error = "sdpos_layer: waiting clear-zone (no XY yet)"
            else:
                rt.state.last_error = (
                    f"sdpos_layer: waiting clear-zone (x={xy[0]:.1f} y={xy[1]:.1f})"
                )
            return False

    job_id = str(job.get("id", ""))
    progress = str(job.get("progress", ""))
    job_state = str(job.get("state", ""))
    fp = _fingerprint(status, job) + f"|layer:{layer_idx}"
    if settings.skip_if_unchanged_seconds > 0 and rt.last_snap_fingerprint == fp:
        if rt.last_snap_wallclock is not None:
            elapsed = now_ts - rt.last_snap_wallclock
            if elapsed < settings.skip_if_unchanged_seconds:
                return False

    filename = build_filename(
        settings, printer_state, job_id, progress, job_state, axis_z=axis_z
    )
    dest = resolve_output_path(settings, filename, job_id)

    def snap():
        grab_frame_rtsp(
            env.ffmpeg_path,
            env.rtsp_url,
            dest,
            settings.jpeg_quality,
        )

    await asyncio.to_thread(snap)

    rt.last_snap_layer_idx = layer_idx
    rt.pending_layer_idx = None
    rt.pending_layer_since = None
    rt.last_snap_fingerprint = fp
    rt.last_snap_wallclock = now_ts
    rt.state.last_snapshot_at = datetime.now(timezone.utc).isoformat()
    rt.state.last_snapshot_path = str(dest)
    rt.state.snapshots_taken += 1
    rt.state.last_error = None
    return True


def _shutdown_entire_process() -> None:
    """Exit the whole process (uvicorn web UI + capture worker)."""
    os._exit(0)


async def _run_loop(
    env: EnvConfig,
    get_settings: Callable[[], UserSettings],
) -> None:
    client = PrusaClient(
        env.prusa_base_url,
        env.prusa_username,
        env.prusa_password,
        timeout=env.prusa_http_timeout,
        download_timeout=env.prusa_download_timeout,
        connect_download_enabled=env.prusa_connect_download_enabled,
        connect_printer_id=env.prusa_connect_printer_id,
        connect_team_id=env.prusa_connect_team_id,
    )
    rt = runtime

    while rt.state.running:
        rt.state.last_loop_at = datetime.now(timezone.utc).isoformat()
        try:
            settings = get_settings()

            def fetch():
                return client.status(), client.job()

            status, job = await asyncio.to_thread(fetch)
            printer = status.get("printer") or {}
            printer_state = str(printer.get("state", "UNKNOWN"))
            rt.state.last_printer_state = printer_state

            if printer_state == "PRINTING":
                rt.idle_since_monotonic = None
            elif settings.auto_shutdown_enabled:
                idle_limit = max(60.0, float(settings.auto_shutdown_minutes) * 60.0)
                now_m = time.monotonic()
                if rt.idle_since_monotonic is None:
                    rt.idle_since_monotonic = now_m
                elif now_m - rt.idle_since_monotonic >= idle_limit:
                    minutes = settings.auto_shutdown_minutes
                    rt.state.stop_reason = (
                        f"Stopped automatically: printer not printing for {minutes:g} minutes. "
                        "Shutting down the app."
                    )
                    rt.state.running = False
                    _shutdown_entire_process()
            else:
                rt.idle_since_monotonic = None

            # Snapshots only while PrusaLink reports printer.state == PRINTING
            if printer_state != "PRINTING":
                rt.last_polled_axis_z = None
                rt.job_id_snapped_at_100 = None
                rt.axis_z_prev_poll = None
                rt.axis_z_same_streak = 0
                _clear_layer_progress(rt)
                await asyncio.sleep(max(1.0, settings.snapshot_interval_seconds))
                continue

            if settings.snapshot_mode == "sdpos_layer":
                if job:
                    await _ensure_layer_map(job, rt, client)
                if rt.layer_starts:
                    _update_layer_progress(rt, status)
                    if settings.snapshots_enabled:
                        z_raw_sd = printer.get("axis_z")
                        try:
                            z_for_name = float(z_raw_sd) if z_raw_sd is not None else None
                        except (TypeError, ValueError):
                            z_for_name = None
                        snapped = await _try_snap_sdpos_layer(
                            env, settings, status, job, printer_state, z_for_name, rt
                        )
                        if snapped:
                            await asyncio.sleep(max(1.0, settings.snapshot_interval_seconds))
                            continue

            if not settings.snapshots_enabled:
                await asyncio.sleep(max(1.0, settings.snapshot_interval_seconds))
                continue

            z_raw = printer.get("axis_z")
            try:
                z = float(z_raw) if z_raw is not None else None
            except (TypeError, ValueError):
                z = None

            # One snap per Z change: skip if axis_z matches the last committed Z (after snap)
            if z is None:
                await asyncio.sleep(max(1.0, settings.snapshot_interval_seconds))
                continue

            if rt.axis_z_prev_poll is None:
                rt.axis_z_same_streak = 1
            elif math.isclose(z, rt.axis_z_prev_poll, rel_tol=0.0, abs_tol=Z_TOLERANCE):
                rt.axis_z_same_streak += 1
            else:
                rt.axis_z_same_streak = 1

            try:
                if rt.last_polled_axis_z is not None and math.isclose(
                    z, rt.last_polled_axis_z, rel_tol=0.0, abs_tol=Z_TOLERANCE
                ):
                    await asyncio.sleep(max(1.0, settings.snapshot_interval_seconds))
                    continue

                # New Z vs last snap: require N consecutive polls at this height (retraction blips)
                if rt.axis_z_same_streak < AXIS_Z_STABLE_POLLS:
                    await asyncio.sleep(max(1.0, settings.snapshot_interval_seconds))
                    continue

                job_id = str(job.get("id", "")) if job else ""
                progress = str(job.get("progress", "")) if job else ""
                job_state = str(job.get("state", "")) if job else ""
                progress_val = _job_progress_value(job)

                if (
                    progress_val is not None
                    and progress_val >= 100.0
                    and job_id
                    and rt.job_id_snapped_at_100 == job_id
                ):
                    rt.last_polled_axis_z = z
                    await asyncio.sleep(max(1.0, settings.snapshot_interval_seconds))
                    continue

                fp = _fingerprint(status, job)
                now_ts = time.monotonic()
                if settings.skip_if_unchanged_seconds > 0 and rt.last_snap_fingerprint == fp:
                    if rt.last_snap_wallclock is not None:
                        elapsed = now_ts - rt.last_snap_wallclock
                        if elapsed < settings.skip_if_unchanged_seconds:
                            wait = settings.skip_if_unchanged_seconds - elapsed
                            rt.last_polled_axis_z = z
                            await asyncio.sleep(
                                min(max(0.5, wait), settings.snapshot_interval_seconds)
                            )
                            continue

                filename = build_filename(
                    settings,
                    printer_state,
                    job_id,
                    progress,
                    job_state,
                    axis_z=z,
                )
                dest = resolve_output_path(settings, filename, job_id)

                def snap():
                    grab_frame_rtsp(
                        env.ffmpeg_path,
                        env.rtsp_url,
                        dest,
                        settings.jpeg_quality,
                    )

                await asyncio.to_thread(snap)

                rt.last_polled_axis_z = z
                if (
                    progress_val is not None
                    and progress_val >= 100.0
                    and job_id
                ):
                    rt.job_id_snapped_at_100 = job_id
                rt.last_snap_fingerprint = fp
                rt.last_snap_wallclock = now_ts
                rt.state.last_snapshot_at = datetime.now(timezone.utc).isoformat()
                rt.state.last_snapshot_path = str(dest)
                rt.state.snapshots_taken += 1
                rt.state.last_error = None
            finally:
                rt.axis_z_prev_poll = z

        except asyncio.CancelledError:
            raise
        except Exception as e:
            rt.state.last_error = f"{e!s}\n{traceback.format_exc()}"

        settings = get_settings()
        await asyncio.sleep(max(1.0, settings.snapshot_interval_seconds))


runtime = Runtime()


async def start_worker(
    env: EnvConfig,
    settings_path: Path,
) -> tuple[bool, str]:
    if runtime.state.running:
        return False, "Already running"
    runtime.state.running = True
    runtime.state.last_error = None
    runtime.state.stop_reason = None
    runtime.idle_since_monotonic = None
    _clear_layer_progress(runtime)

    def get_settings():
        return load_user_settings(settings_path)

    runtime.task = asyncio.create_task(_run_loop(env, get_settings))
    return True, "Started"


async def stop_worker() -> tuple[bool, str]:
    if not runtime.state.running:
        return False, "Not running"
    runtime.state.running = False
    runtime.state.stop_reason = None
    if runtime.task:
        runtime.task.cancel()
        try:
            await runtime.task
        except asyncio.CancelledError:
            pass
        runtime.task = None
    _clear_layer_progress(runtime)
    return True, "Stopped"
