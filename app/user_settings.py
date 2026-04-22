import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SnapshotMode = Literal["axis_z", "sdpos_layer"]


class UserSettings(BaseModel):
    """Persisted settings editable from the web UI."""

    model_config = ConfigDict(extra="ignore")

    snapshot_interval_seconds: float = Field(
        30.0, ge=1.0, le=86400.0, description="Seconds between snapshots"
    )
    output_dir: str = Field(
        "captures", description="Folder for saved images (relative or absolute)"
    )
    subfolder_by_date: bool = Field(
        True, description="Save under YYYY-MM-DD inside output_dir"
    )
    subfolder_by_job_id: bool = Field(
        True,
        description="When job id is known, save under output_dir/date/job_id/ (or output_dir/job_id/ if date subfolder off)",
    )
    filename_template: str = Field(
        "{timestamp}_z{axis_z}_job{job_id}",
        description="Template without extension; tokens: timestamp, printer_state, "
        "job_id, progress, job_state, axis_z/z/axis_z_sort (Z as 6-digit µm, sortable), "
        "axis_z_mm (human-readable mm)",
    )
    jpeg_quality: int = Field(
        2,
        ge=1,
        le=31,
        description="FFmpeg -q:v (2–31, lower is better quality)",
    )
    skip_if_unchanged_seconds: float = Field(
        0.0,
        ge=0.0,
        description="Min seconds between snaps when state unchanged (0 = off)",
    )
    snapshot_mode: SnapshotMode = Field(
        "sdpos_layer",
        description="axis_z: snap on Z change. sdpos_layer: snap once per new layer from UDP sdpos",
    )
    clear_zone_enabled: bool = Field(
        False,
        description="When true (sdpos_layer), wait for toolhead XY to enter clear zone before snapshot.",
    )
    clear_zone_x_min: float = Field(170.0, description="Clear-zone X minimum (mm)")
    clear_zone_x_max: float = Field(260.0, description="Clear-zone X maximum (mm)")
    clear_zone_y_min: float = Field(170.0, description="Clear-zone Y minimum (mm)")
    clear_zone_y_max: float = Field(260.0, description="Clear-zone Y maximum (mm)")
    clear_zone_wait_seconds: float = Field(
        5.0, ge=0.5, le=30.0, description="Max wait for clear-zone before forcing snapshot."
    )
    snapshots_enabled: bool = Field(
        True,
        description="When false, the capture service still runs for monitoring but does not save snapshots.",
    )
    auto_shutdown_enabled: bool = Field(
        True,
        description="When true, the app shuts down after auto_shutdown_minutes of non-PRINTING state.",
    )
    auto_shutdown_minutes: float = Field(
        15.0,
        ge=1.0,
        le=1440.0,
        description="Minutes of non-PRINTING state before auto-shutdown fires (when enabled).",
    )


def default_user_settings() -> UserSettings:
    return UserSettings()


def load_user_settings(path: Path) -> UserSettings:
    if not path.exists():
        s = default_user_settings()
        save_user_settings(path, s)
        return s
    data = json.loads(path.read_text(encoding="utf-8"))
    return UserSettings.model_validate(data)


def save_user_settings(path: Path, settings: UserSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        settings.model_dump_json(indent=2),
        encoding="utf-8",
    )
