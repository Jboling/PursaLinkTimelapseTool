"""Thread-safe store for last Buddy UDP metrics (sdpos and friends)."""

from __future__ import annotations

import threading
import time
from typing import Any


class MetricsState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.packets_total = 0
        self.bytes_total = 0
        self.last_packet_at: float | None = None
        self.last_payload_preview: str = ""
        self.last_payload_raw: str = ""
        self.metrics: dict[str, int | float | str | bool] = {}
        self.sdpos: int | None = None
        self.sdpos_source: str | None = None

    def record_packet(self, raw: bytes, parsed: dict[str, int | float | str | bool]) -> None:
        now = time.monotonic()
        with self._lock:
            self.packets_total += 1
            self.bytes_total += len(raw)
            self.last_packet_at = now
            decoded = raw.decode("utf-8", errors="replace")
            self.last_payload_raw = decoded
            self.last_payload_preview = decoded
            self.metrics.update(parsed)
            if "sdpos" in parsed:
                val = parsed.get("sdpos")
                if isinstance(val, int):
                    self.sdpos = val
                    self.sdpos_source = "sdpos"
                elif isinstance(val, float):
                    self.sdpos = int(val)
                    self.sdpos_source = "sdpos"
            elif "ftch_sdpos" in parsed:
                val = parsed.get("ftch_sdpos")
                if isinstance(val, int):
                    self.sdpos = val
                    self.sdpos_source = "ftch_sdpos"
                elif isinstance(val, float):
                    self.sdpos = int(val)
                    self.sdpos_source = "ftch_sdpos"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "packets_total": self.packets_total,
                "bytes_total": self.bytes_total,
                "last_packet_at_monotonic": self.last_packet_at,
                "sdpos": self.sdpos,
                "sdpos_source": self.sdpos_source,
                "metrics": dict(self.metrics),
                "last_payload_raw": self.last_payload_raw,
                "last_payload_preview": self.last_payload_preview,
            }


metrics_state = MetricsState()

