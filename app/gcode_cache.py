"""Simple local cache for print files keyed by job-file metadata."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JobFileKey:
    display_name: str
    m_timestamp: int | None
    size: int | None

    def fingerprint(self) -> str:
        parts = [self.display_name.strip()]
        if self.m_timestamp is not None:
            parts.append(f"t{int(self.m_timestamp)}")
        if self.size is not None:
            parts.append(f"s{int(self.size)}")
        return "|".join(parts)

    def hash(self) -> str:
        return hashlib.sha1(self.fingerprint().encode("utf-8")).hexdigest()


def key_from_job(job: dict | None) -> JobFileKey | None:
    if not isinstance(job, dict):
        return None
    f = job.get("file")
    if not isinstance(f, dict):
        return None
    display_name = str(f.get("display_name") or f.get("name") or "").strip()
    if not display_name:
        return None
    try:
        m_ts = int(f.get("m_timestamp")) if f.get("m_timestamp") is not None else None
    except (TypeError, ValueError):
        m_ts = None
    try:
        size = int(f.get("size")) if f.get("size") is not None else None
    except (TypeError, ValueError):
        size = None
    return JobFileKey(display_name=display_name, m_timestamp=m_ts, size=size)


class GcodeCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def ensure_dir(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, key: JobFileKey) -> tuple[Path, Path]:
        h = key.hash()
        return self.root / f"{h}.bin", self.root / f"{h}.json"

    def get(self, key: JobFileKey) -> tuple[bytes, dict] | None:
        bin_p, meta_p = self._paths(key)
        if not bin_p.exists():
            return None
        try:
            data = bin_p.read_bytes()
        except OSError:
            return None
        meta: dict = {}
        if meta_p.exists():
            try:
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        return data, meta

    def put(
        self, key: JobFileKey, data: bytes, *, content_name: str, source: str
    ) -> dict:
        self.ensure_dir()
        bin_p, meta_p = self._paths(key)
        bin_p.write_bytes(data)
        meta = {
            "display_name": key.display_name,
            "m_timestamp": key.m_timestamp,
            "size": key.size,
            "content_name": content_name,
            "source": source,
            "stored_at": int(time.time()),
            "byte_length": len(data),
            "hash": key.hash(),
        }
        meta_p.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

