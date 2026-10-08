"""Settings read from the environment."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    api_host: str
    api_port: int
    worker_heartbeat_seconds: float


def get_settings() -> Settings:
    return Settings(
        api_host=os.environ.get("API_HOST", "127.0.0.1"),
        api_port=int(os.environ.get("PORT", "8000")),
        worker_heartbeat_seconds=float(os.environ.get("WORKER_HEARTBEAT_SECONDS", "30")),
    )
