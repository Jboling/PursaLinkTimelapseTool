import asyncio
import math
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from app.env_config import EnvConfig
from app.prusa_client import PrusaClient
from app.snapshot import build_filename, grab_frame_rtsp, resolve_output_path
from app.user_settings import UserSettings, load_user_settings

Z_TOLERANCE = 1e-4
# Require this many consecutive polls at the same axis_z before snapping (filters brief Z blips).
AXIS_Z_STABLE_POLLS = 2
# Stop capture service after this many seconds with printer.state != PRINTING
IDLE_STOP_SECONDS = 15 * 60


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


def _shutdown_entire_process() -> None:
    """Exit the whole process (uvicorn web UI + capture worker)."""
    os._exit(0)


async def _run_loop(
    env: EnvConfig,
    get_settings: Callable[[], UserSettings],
) -> None:
    client = PrusaClient(
        env.prusa_base_url, env.prusa_username, env.prusa_password
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
            else:
                now_m = time.monotonic()
                if rt.idle_since_monotonic is None:
                    rt.idle_since_monotonic = now_m
                elif now_m - rt.idle_since_monotonic >= IDLE_STOP_SECONDS:
                    rt.state.stop_reason = (
                        "Stopped automatically: printer not printing for 15 minutes. "
                        "Shutting down the app."
                    )
                    rt.state.running = False
                    _shutdown_entire_process()

            # Snapshots only while PrusaLink reports printer.state == PRINTING
            if printer_state != "PRINTING":
                rt.last_polled_axis_z = None
                rt.job_id_snapped_at_100 = None
                rt.axis_z_prev_poll = None
                rt.axis_z_same_streak = 0
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
    return True, "Stopped"
