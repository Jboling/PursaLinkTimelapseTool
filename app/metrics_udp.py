"""UDP receiver for Buddy metrics (configured via M334 + M331 on printer)."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.metrics_state import metrics_state

log = logging.getLogger(__name__)

_RE_INT = re.compile(r"([A-Za-z0-9_]+)\s+v=(\d+)i\s+(-?\d+)\s*")
_RE_FLOAT = re.compile(r"([A-Za-z0-9_]+)\s+v=([0-9.eE+-]+)\s+(-?\d+)\s*")
_RE_SDPOS_INFLUX = re.compile(r"(?:^|[\s,])sdpos=(\d+)i?(?=\s|,|\n|$)", re.MULTILINE)
_RE_METRIC_KV_LINE = re.compile(
    r"^([A-Za-z0-9_]+)\s+([^ \n]+(?:,[^ \n]+)*)\s+(-?\d+)\s*$", re.MULTILINE
)


def _parse_kv_value(raw: str) -> int | float | None:
    s = raw.strip()
    if not s:
        return None
    if s.endswith("i"):
        try:
            return int(s[:-1])
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_buddy_metrics_payload(text: str) -> dict[str, int | float]:
    out: dict[str, int | float] = {}
    for m in _RE_INT.finditer(text):
        out[m.group(1)] = int(m.group(2))
    for m in _RE_FLOAT.finditer(text):
        name = m.group(1)
        if name in out:
            continue
        try:
            out[name] = float(m.group(2))
        except ValueError:
            continue
    for m in _RE_METRIC_KV_LINE.finditer(text):
        name = m.group(1)
        fields = m.group(2).split(",")
        for field in fields:
            if "=" not in field:
                continue
            key, raw_val = field.split("=", 1)
            if key != "v":
                continue
            parsed = _parse_kv_value(raw_val)
            if parsed is not None:
                out[name] = parsed
            break
    if "sdpos" not in out:
        last_sdpos: int | None = None
        for m in _RE_SDPOS_INFLUX.finditer(text):
            last_sdpos = int(m.group(1))
        if last_sdpos is not None:
            out["sdpos"] = last_sdpos
    return out


class _MetricsUDPProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr: Any) -> None:
        text = data.decode("utf-8", errors="replace")
        parsed = parse_buddy_metrics_payload(text)
        if not parsed:
            log.debug("UDP metrics: no parse from %s (%d bytes)", addr, len(data))
        metrics_state.record_packet(data, parsed)


_transport: asyncio.DatagramTransport | None = None
_protocol: asyncio.DatagramProtocol | None = None


async def start_metrics_udp_server(host: str, port: int) -> None:
    global _transport, _protocol
    if _transport is not None:
        return
    loop = asyncio.get_running_loop()
    _transport, _protocol = await loop.create_datagram_endpoint(
        _MetricsUDPProtocol,
        local_addr=(host, port),
    )
    log.info("Buddy metrics UDP listening on %s:%s", host, port)


async def stop_metrics_udp_server() -> None:
    global _transport, _protocol
    if _transport is not None:
        _transport.close()
        _transport = None
        _protocol = None

