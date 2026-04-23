import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SnapshotMode = Literal["axis_z", "sdpos_layer"]
# Camera position around the bed as a 3x3 grid with the center excluded.
# Values describe where the camera physically sits relative to the bed;
# the "farthest from camera" point per layer is chosen in the opposite direction.
CameraSide = Literal[
    "front_left", "front", "front_right",
    "left", "right",
    "back_left", "back", "back_right",
]


class UserSettings(BaseModel):
    """Persisted settings editable from the web UI."""

    model_config = ConfigDict(extra="ignore")

    snapshot_interval_seconds: float = Field(
        30.0,
        ge=1.0,
        le=86400.0,
        description=(
            "Worker poll interval (seconds). Controls how often the app polls "
            "the printer for state and re-evaluates sdpos for a potential "
            "snapshot. Lower = more responsive layer detection, higher = less "
            "load on the printer."
        ),
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
        description=(
            "When true (sdpos_layer), wait each layer until the toolhead reaches the "
            "point furthest from the camera (computed per layer from parsed XY moves) "
            "before taking the snapshot."
        ),
    )
    camera_side: CameraSide = Field(
        "front",
        description=(
            "Where the camera sits relative to the bed (3x3 grid around the bed, "
            "center excluded). 'Farthest from camera' is picked as the XY point in "
            "the opposite direction: e.g. 'front' -> max Y, 'front_left' -> max X+Y, "
            "'back_right' -> min X+Y."
        ),
    )
    clear_zone_xy_tolerance_mm: float = Field(
        5.0,
        ge=0.1,
        le=50.0,
        description=(
            "Live XY proximity (mm) to the per-layer target that also fires the snap, "
            "in case sdpos advances past the target between polls."
        ),
    )
    clear_zone_wait_enabled: bool = Field(
        True,
        description=(
            "When true, cap the per-layer clear-zone wait at "
            "clear_zone_wait_seconds and force a snapshot at the current "
            "toolhead position on timeout. When false, wait indefinitely for "
            "the sdpos target or the XY tolerance match (recommended only "
            "when sdpos is reliable)."
        ),
    )
    clear_zone_wait_seconds: float = Field(
        30.0,
        ge=0.5,
        le=600.0,
        description=(
            "Max seconds to wait for the dynamic target per layer before forcing a "
            "snapshot at the current toolhead position. Ignored when "
            "clear_zone_wait_enabled is false."
        ),
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
