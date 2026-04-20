from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    prusa_base_url: str = Field(..., alias="PRUSA_BASE_URL")
    prusa_username: str = Field(..., alias="PRUSA_USERNAME")
    prusa_password: str = Field(..., alias="PRUSA_PASSWORD")
    prusa_http_timeout: float = Field(60.0, alias="PRUSA_HTTP_TIMEOUT")
    prusa_download_timeout: float = Field(300.0, alias="PRUSA_DOWNLOAD_TIMEOUT")
    prusa_connect_download_enabled: bool = Field(
        False, alias="PRUSA_CONNECT_DOWNLOAD_ENABLED"
    )
    prusa_connect_printer_id: str | None = Field(
        None, alias="PRUSA_CONNECT_PRINTER_ID"
    )
    prusa_connect_team_id: int | None = Field(
        None, alias="PRUSA_CONNECT_TEAM_ID"
    )
    rtsp_url: str = Field(..., alias="RTSP_URL")
    host: str = Field(
        "127.0.0.1",
        alias="HOST",
        description="Bind address: 127.0.0.1 localhost only; use 0.0.0.0 for LAN/Tailscale",
    )
    port: int = Field(8765, alias="PORT")
    ffmpeg_path: str = Field("ffmpeg", alias="FFMPEG_PATH")
    user_settings_path: Path = Field(
        Path("data/user_settings.json"), alias="USER_SETTINGS_PATH"
    )
    metrics_udp_enabled: bool = Field(False, alias="METRICS_UDP_ENABLED")
    metrics_udp_bind: str = Field("0.0.0.0", alias="METRICS_UDP_BIND")
    metrics_udp_port: int = Field(9100, alias="METRICS_UDP_PORT")


def load_env_config() -> EnvConfig:
    return EnvConfig()
