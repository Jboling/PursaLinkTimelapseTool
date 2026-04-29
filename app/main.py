import asyncio
from contextlib import asynccontextmanager
import json
import math
import os
from pathlib import Path
import subprocess
from typing import Any

import mimetypes

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.env_config import load_env_config
from app.folder_picker import pick_folder
from app.photo_video import (
    build_timelapse_mp4,
    filter_by_names,
    list_images,
    resolve_safe_image_file,
)
from app.metrics_state import metrics_state
from app.metrics_udp import (
    parse_buddy_metrics_payload,
    start_metrics_udp_server,
    stop_metrics_udp_server,
)
from app.prusa_client import PrusaClient
from app.snapshot import build_filename, grab_frame_rtsp, resolve_output_path
from app.user_settings import UserSettings, load_user_settings, save_user_settings
from app.worker import runtime, start_worker, stop_worker

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
STATIC_DIR = BASE_DIR / "static"
FAVICON_PATH = STATIC_DIR / "favicon.png"


class UserSettingsPayload(BaseModel):
    snapshot_interval_seconds: float | None = None
    snapshot_interval_ms: float | None = None
    output_dir: str | None = None
    subfolder_by_date: bool | None = None
    subfolder_by_job_id: bool | None = None
    filename_template: str | None = None
    jpeg_quality: int | None = None
    skip_if_unchanged_seconds: float | None = None
    snapshot_mode: str | None = None
    clear_zone_enabled: bool | None = None
    camera_side: str | None = None
    clear_zone_xy_tolerance_mm: float | None = None
    clear_zone_wait_enabled: bool | None = None
    clear_zone_wait_seconds: float | None = None
    snapshots_enabled: bool | None = None
    auto_shutdown_enabled: bool | None = None
    auto_shutdown_minutes: float | None = None


class BrowseFolderPayload(BaseModel):
    """Optional folder to open the dialog in (must exist)."""

    initial_dir: str | None = None
    dialog_title: str | None = None


class PhotoVideoBuildPayload(BaseModel):
    input_dir: str
    output_dir: str
    output_filename: str = "timelapse.mp4"
    fps: float = 24.0
    include_names: list[str] | None = None
    hold_last_seconds: float = 0.0


def _is_localhost(request: Request) -> bool:
    client = request.client
    if not client:
        return False
    host = client.host
    if host in ("127.0.0.1", "::1"):
        return True
    if host.startswith("::ffff:") and host.endswith("127.0.0.1"):
        return True
    return False


def get_env():
    return load_env_config()


def _first_metric(metrics: dict[str, Any], names: tuple[str, ...]) -> int | float | None:
    for name in names:
        val = metrics.get(name)
        if isinstance(val, (int, float)):
            return val
    return None


def _extract_chamber_temps(metrics: dict[str, Any]) -> dict[str, int | float | None]:
    cur = _first_metric(
        metrics,
        (
            "temp_chamber",
            "chamber_temp",
            "chamber_temperature",
            "chamber",
        ),
    )
    tgt = _first_metric(
        metrics,
        (
            "chamber_ttemp",
            "target_chamber",
            "chamber_target",
            "setpoint_chamber",
        ),
    )
    if cur is None:
        for k, v in metrics.items():
            kl = k.lower()
            if "chamber" in kl and ("temp" in kl or kl.endswith("_c")) and isinstance(v, (int, float)):
                cur = v
                break
    if tgt is None:
        for k, v in metrics.items():
            kl = k.lower()
            if "chamber" in kl and ("target" in kl or "setpoint" in kl) and isinstance(v, (int, float)):
                tgt = v
                break
    return {"current": cur, "target": tgt}


def _sanitize_metric_value(val: Any) -> int | float | str | bool | None:
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if not math.isfinite(val):
            return None
        return val
    if isinstance(val, str):
        return val
    return str(val)


def _sanitize_metrics(metrics: dict[str, Any]) -> dict[str, int | float | str | bool | None]:
    out: dict[str, int | float | str | bool | None] = {}
    for key, val in metrics.items():
        out[key] = _sanitize_metric_value(val)
    return out


def settings_path() -> Path:
    return get_env().user_settings_path


def _resolved_output_dir(output_dir: str) -> str:
    return str(Path(output_dir).expanduser().resolve())


def _user_desktop_dir() -> str:
    """Best-effort path to the current user's Desktop folder."""
    candidates = [
        Path.home() / "Desktop",
        Path.home() / "OneDrive" / "Desktop",
    ]
    for p in candidates:
        if p.is_dir():
            return str(p)
    return str(Path.home())


@asynccontextmanager
async def lifespan(app: FastAPI):
    env = load_env_config()
    if env.metrics_udp_enabled:
        await start_metrics_udp_server(env.metrics_udp_bind, env.metrics_udp_port)
    yield
    await stop_metrics_udp_server()


app = FastAPI(
    title="PrusaLink Snapshot Companion",
    version="0.1.0",
    lifespan=lifespan,
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Browsers request /favicon.ico by default; serve the PNG (modern browsers
    # accept PNG content regardless of the .ico extension).
    if FAVICON_PATH.is_file():
        return FileResponse(FAVICON_PATH, media_type="image/png")
    raise HTTPException(status_code=404, detail="favicon not found")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
        },
    )


@app.get("/tools/photo-video", response_class=HTMLResponse)
async def photo_video_tool(request: Request):
    return templates.TemplateResponse(
        "photo_video.html",
        {"request": request},
    )


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/env")
async def api_env():
    """Non-secret connection hints for the UI."""
    env = get_env()
    return {
        "prusa_base_url": env.prusa_base_url,
        "prusa_http_timeout": env.prusa_http_timeout,
        "prusa_download_timeout": env.prusa_download_timeout,
        "prusa_connect_download_enabled": env.prusa_connect_download_enabled,
        "prusa_connect_printer_id": env.prusa_connect_printer_id,
        "prusa_connect_team_id": env.prusa_connect_team_id,
        "rtsp_url": env.rtsp_url,
        "ffmpeg_path": env.ffmpeg_path,
        "settings_path": str(env.user_settings_path),
        "metrics_udp_enabled": env.metrics_udp_enabled,
        "metrics_udp_bind": env.metrics_udp_bind,
        "metrics_udp_port": env.metrics_udp_port,
        "user_desktop": _user_desktop_dir(),
    }


@app.get("/api/metrics/sdpos")
async def api_metrics_sdpos():
    snap = metrics_state.snapshot()
    raw_metrics = snap.get("metrics") or {}
    metrics = _sanitize_metrics(raw_metrics)
    chamber = _extract_chamber_temps(metrics)
    hint = None
    if snap.get("sdpos") is None:
        hint = (
            "No sdpos seen yet. Configure printer metrics: "
            "M334 <host-ip> <port> then M331 sdpos."
        )
    out = {
        "sdpos": snap.get("sdpos"),
        "sdpos_source": snap.get("sdpos_source"),
        "metrics": metrics,
        "chamber_temp": chamber["current"],
        "chamber_target": chamber["target"],
        "packets_total": snap.get("packets_total"),
        "bytes_total": snap.get("bytes_total"),
        "last_payload_preview": snap.get("last_payload_preview"),
    }
    if hint:
        out["hint"] = hint
    return out


@app.get("/api/metrics/raw")
async def api_metrics_raw():
    snap = metrics_state.snapshot()
    raw = snap.get("last_payload_raw") or ""
    parsed_json = None
    parse_error = None
    json_parse_attempted = False
    parsed_metrics: dict[str, int | float | str | bool] = {}
    if raw:
        stripped = raw.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            json_parse_attempted = True
            try:
                parsed_json = json.loads(stripped)
            except Exception as e:
                parse_error = str(e)
        else:
            # Some emitters prepend syslog metadata before JSON payload.
            # If so, try parsing from the first JSON token.
            idx_obj = raw.find("{")
            idx_arr = raw.find("[")
            starts = [i for i in (idx_obj, idx_arr) if i >= 0]
            if starts:
                json_parse_attempted = True
                start = min(starts)
                try:
                    parsed_json = json.loads(raw[start:])
                except Exception as e:
                    parse_error = str(e)
        parsed_metrics = parse_buddy_metrics_payload(raw)
    return {
        "raw_payload": raw,
        "parsed_json": parsed_json,
        "parsed_metrics": parsed_metrics,
        "json_parse_attempted": json_parse_attempted,
        "json_parse_error": parse_error,
        "packets_total": snap.get("packets_total"),
        "bytes_total": snap.get("bytes_total"),
    }


@app.get("/api/printer/status")
async def printer_status():
    env = get_env()
    try:

        def fetch():
            c = PrusaClient(
                env.prusa_base_url,
                env.prusa_username,
                env.prusa_password,
                timeout=env.prusa_http_timeout,
                download_timeout=env.prusa_download_timeout,
                connect_download_enabled=env.prusa_connect_download_enabled,
                connect_printer_id=env.prusa_connect_printer_id,
                connect_team_id=env.prusa_connect_team_id,
            )
            return c.status(), c.job()

        status, job = await asyncio.to_thread(fetch)
    except Exception as e:
        raise HTTPException(502, detail=str(e)) from e
    return {"status": status, "job": job}


@app.get("/api/settings")
async def get_settings():
    s = load_user_settings(settings_path())
    d = s.model_dump()
    d["snapshot_interval_ms"] = round(float(s.snapshot_interval_seconds) * 1000.0, 3)
    d["output_dir_absolute"] = _resolved_output_dir(s.output_dir)
    return d


@app.get("/api/settings/resolve-output-dir")
async def resolve_output_dir(path: str = Query("", description="Folder path to resolve to absolute")):
    p = path.strip()
    if not p:
        return {"absolute": ""}
    try:
        return {"absolute": _resolved_output_dir(p)}
    except Exception as e:
        raise HTTPException(400, detail=str(e)) from e


@app.post("/api/folder/browse")
async def browse_folder(
    request: Request, body: BrowseFolderPayload = BrowseFolderPayload()
):
    """
    Open the native folder picker (Windows / cross-platform via Tk).
    Only allowed when the HTTP client is localhost (this machine).
    """
    if not _is_localhost(request):
        raise HTTPException(
            status_code=403,
            detail="Folder browse is only available when using the app on this PC (127.0.0.1).",
        )
    try:
        path = await asyncio.to_thread(
            pick_folder, body.initial_dir, body.dialog_title
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not open folder dialog: {e}",
        ) from e
    if path is None:
        return {"cancelled": True}
    return {"path": path}


@app.post("/api/folder/open-snapshot-dir")
async def open_snapshot_dir(request: Request):
    """
    Open current job snapshot folder in OS file explorer.
    Localhost only for safety.
    """
    if not _is_localhost(request):
        raise HTTPException(
            status_code=403,
            detail="Opening folders is only available when using the app on this PC (127.0.0.1).",
        )

    env = get_env()
    s = load_user_settings(settings_path())
    try:
        def fetch_job():
            c = PrusaClient(
                env.prusa_base_url,
                env.prusa_username,
                env.prusa_password,
                timeout=env.prusa_http_timeout,
                download_timeout=env.prusa_download_timeout,
                connect_download_enabled=env.prusa_connect_download_enabled,
                connect_printer_id=env.prusa_connect_printer_id,
                connect_team_id=env.prusa_connect_team_id,
            )
            return c.job()

        job = await asyncio.to_thread(fetch_job)
    except Exception as e:
        raise HTTPException(502, detail=f"Could not fetch current job: {e}") from e

    job_id = str((job or {}).get("id", "")).strip()
    if not job_id:
        raise HTTPException(
            status_code=400,
            detail="No current job id found. Start/refresh an active print, then try again.",
        )

    # Use the same folder logic snapshots use (date/job settings), but no file creation.
    target_dir = resolve_output_path(s, "_open_folder_probe.jpg", job_id).parent

    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", str(target_dir)])
        else:
            raise RuntimeError("Open folder is currently implemented for Windows only.")
    except Exception as e:
        raise HTTPException(500, detail=f"Could not open folder: {e}") from e

    return {"ok": True, "path": str(target_dir), "job_id": job_id}


@app.get("/api/tools/photo-video/resolve")
async def photo_video_resolve(
    input_dir: str = Query("", description="Folder containing images"),
    output_dir: str = Query("", description="Folder for the output video"),
):
    """Return absolute paths and image count for validation."""
    inp = input_dir.strip()
    out = output_dir.strip()
    if not inp or not out:
        raise HTTPException(400, detail="input_dir and output_dir are required")
    try:
        in_abs = Path(inp).expanduser().resolve()
        out_abs = Path(out).expanduser().resolve()
    except Exception as e:
        raise HTTPException(400, detail=str(e)) from e
    if not in_abs.is_dir():
        raise HTTPException(400, detail=f"Input is not a directory: {in_abs}")
    try:
        images = list_images(in_abs)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    return {
        "input_absolute": str(in_abs),
        "output_absolute": str(out_abs),
        "image_count": len(images),
    }


@app.get("/api/tools/photo-video/images")
async def photo_video_images(input_dir: str = Query("", description="Folder containing images")):
    """List image basenames in list_images order."""
    inp = input_dir.strip()
    if not inp:
        raise HTTPException(400, detail="input_dir is required")
    try:
        in_abs = Path(inp).expanduser().resolve()
    except Exception as e:
        raise HTTPException(400, detail=str(e)) from e
    if not in_abs.is_dir():
        raise HTTPException(400, detail=f"Not a directory: {in_abs}")
    try:
        images = list_images(in_abs)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    return {
        "input_absolute": str(in_abs),
        "names": [p.name for p in images],
        "count": len(images),
    }


@app.get("/api/tools/photo-video/thumbnail")
async def photo_video_thumbnail(
    input_dir: str = Query("", description="Folder containing images"),
    name: str = Query("", description="File basename only"),
):
    inp = input_dir.strip()
    if not inp or not name.strip():
        raise HTTPException(400, detail="input_dir and name are required")
    try:
        in_abs = Path(inp).expanduser().resolve()
    except Exception as e:
        raise HTTPException(400, detail=str(e)) from e
    if not in_abs.is_dir():
        raise HTTPException(400, detail=f"Not a directory: {in_abs}")
    path = resolve_safe_image_file(in_abs, name)
    if path is None:
        raise HTTPException(404, detail="Image not found")
    mt, _ = mimetypes.guess_type(str(path))
    media = mt or "application/octet-stream"
    return FileResponse(path, media_type=media)


@app.post("/api/tools/photo-video/build")
async def photo_video_build(body: PhotoVideoBuildPayload):
    env = get_env()
    inp = body.input_dir.strip()
    out_dir = body.output_dir.strip()
    if not inp or not out_dir:
        raise HTTPException(400, detail="input_dir and output_dir are required")
    name = body.output_filename.strip() or "timelapse.mp4"
    if not name.lower().endswith(".mp4"):
        name += ".mp4"
    fps = float(body.fps)
    if fps < 0.1 or fps > 120:
        raise HTTPException(400, detail="fps must be between 0.1 and 120")
    hold_last = float(body.hold_last_seconds)
    if hold_last < 0 or hold_last > 600:
        raise HTTPException(
            400, detail="hold_last_seconds must be between 0 and 600"
        )

    try:
        in_abs = Path(inp).expanduser().resolve()
        out_abs = Path(out_dir).expanduser().resolve()
    except Exception as e:
        raise HTTPException(400, detail=str(e)) from e

    if not in_abs.is_dir():
        raise HTTPException(400, detail=f"Input is not a directory: {in_abs}")

    all_imgs = list_images(in_abs)
    if not all_imgs:
        raise HTTPException(
            400,
            detail="No supported images found (.jpg, .jpeg, .png, .webp, .bmp, .tif)",
        )

    if body.include_names is not None:
        if len(body.include_names) == 0:
            raise HTTPException(
                400,
                detail="include_names is empty; select at least one frame.",
            )
        try:
            images = filter_by_names(all_imgs, body.include_names)
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e
    else:
        images = all_imgs

    dest = out_abs / name

    def run():
        return build_timelapse_mp4(
            env.ffmpeg_path, images, dest, fps, hold_last_seconds=hold_last
        )

    try:
        log = await asyncio.to_thread(run)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(500, detail=str(e)) from e

    return {
        "ok": True,
        "video_path": str(dest.resolve()),
        "frame_count": len(images),
        "fps": fps,
        "hold_last_seconds": hold_last,
        "ffmpeg_log_tail": log[-4000:] if log else "",
    }


@app.put("/api/settings")
async def put_settings(body: UserSettingsPayload):
    path = settings_path()
    cur = load_user_settings(path)
    data = cur.model_dump()
    incoming = body.model_dump(exclude_none=True)
    if "snapshot_interval_ms" in incoming:
        ms = float(incoming.pop("snapshot_interval_ms"))
        incoming["snapshot_interval_seconds"] = ms / 1000.0
    for k, v in incoming.items():
        data[k] = v
    updated = UserSettings.model_validate(data)
    save_user_settings(path, updated)
    d = updated.model_dump()
    d["snapshot_interval_ms"] = round(float(updated.snapshot_interval_seconds) * 1000.0, 3)
    d["output_dir_absolute"] = _resolved_output_dir(updated.output_dir)
    return d


@app.get("/api/service")
async def service_state():
    st = runtime.state
    rt = runtime
    s = load_user_settings(settings_path())
    return {
        "running": st.running,
        "snapshots_enabled": s.snapshots_enabled,
        "auto_shutdown_enabled": s.auto_shutdown_enabled,
        "auto_shutdown_minutes": s.auto_shutdown_minutes,
        "last_snapshot_at": st.last_snapshot_at,
        "last_snapshot_path": st.last_snapshot_path,
        "snapshots_taken": st.snapshots_taken,
        "last_error": st.last_error,
        "last_printer_state": st.last_printer_state,
        "last_loop_at": st.last_loop_at,
        "stop_reason": st.stop_reason,
        "gcode_download": {
            "status": rt.gcode_download_status,
            "job_id": rt.gcode_download_job_id,
            "display_name": rt.gcode_download_display_name,
            "bytes": rt.gcode_download_bytes,
            "layer_markers": rt.gcode_download_layer_markers,
            "error": rt.gcode_download_error,
        },
        "layer_progress": {
            "current_index": rt.layer_current_index,
            "current_display": rt.layer_current_index + 1 if rt.layer_current_index is not None else None,
            "total": rt.layer_total,
            "map_error": rt.layer_map_error,
            "last_snapped_index": rt.last_snap_layer_idx,
            "axis_z": rt.last_axis_z,
            "z_table_size": len(rt.layer_z_heights),
        },
        "layer_snap_target": _current_layer_snap_target(rt, s),
    }


def _current_layer_snap_target(rt, s: UserSettings) -> dict | None:
    """Surface the current layer's dynamic-clear-zone target for the UI."""
    if rt.layer_current_index is None:
        return None
    slot = rt.layer_xy_extents.get(rt.layer_current_index)
    if not slot:
        return None
    pt = slot.get(s.camera_side)
    if pt is None:
        return None
    sdpos, x, y = pt
    return {
        "layer_index": rt.layer_current_index,
        "camera_side": s.camera_side,
        "x": x,
        "y": y,
        "sdpos": sdpos,
    }


@app.post("/api/service/start")
async def service_start():
    env = get_env()
    ok, msg = await start_worker(env, settings_path())
    if not ok:
        raise HTTPException(400, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/service/stop")
async def service_stop():
    ok, msg = await stop_worker()
    if not ok:
        raise HTTPException(400, detail=msg)
    return {"ok": True, "message": msg}


@app.post("/api/snapshot/test")
async def snapshot_test():
    """Capture one frame using current settings and RTSP URL from .env."""
    env = get_env()
    s = load_user_settings(settings_path())
    try:

        def fetch():
            c = PrusaClient(
                env.prusa_base_url,
                env.prusa_username,
                env.prusa_password,
                timeout=env.prusa_http_timeout,
                download_timeout=env.prusa_download_timeout,
                connect_download_enabled=env.prusa_connect_download_enabled,
                connect_printer_id=env.prusa_connect_printer_id,
                connect_team_id=env.prusa_connect_team_id,
            )
            return c.status(), c.job()

        status, job = await asyncio.to_thread(fetch)
        printer = status.get("printer") or {}
        printer_state = str(printer.get("state", "UNKNOWN"))
        job_id = str(job.get("id", "")) if job else ""
        progress = str(job.get("progress", "")) if job else ""
        job_state = str(job.get("state", "")) if job else ""
        z_raw = printer.get("axis_z")
        try:
            z_test = float(z_raw) if z_raw is not None else None
        except (TypeError, ValueError):
            z_test = None
        name = build_filename(
            s, printer_state, job_id, progress, job_state, axis_z=z_test
        )
        dest = resolve_output_path(s, name, job_id)

        def snap():
            grab_frame_rtsp(env.ffmpeg_path, env.rtsp_url, dest, s.jpeg_quality)

        await asyncio.to_thread(snap)
    except Exception as e:
        raise HTTPException(500, detail=str(e)) from e
    return {"ok": True, "path": str(dest)}


def main():
    import uvicorn

    env = load_env_config()
    uvicorn.run(
        "app.main:app",
        host=env.host,
        port=env.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
