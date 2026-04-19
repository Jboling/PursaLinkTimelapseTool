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


def load_env_config() -> EnvConfig:
    return EnvConfig()
