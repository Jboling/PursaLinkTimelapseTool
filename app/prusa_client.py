from typing import Any

import requests
from requests.auth import HTTPDigestAuth


class PrusaClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self._auth = HTTPDigestAuth(username, password)
        self._timeout = timeout

    def _get(self, path: str) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        r = requests.get(url, auth=self._auth, timeout=self._timeout)
        if r.status_code == 204:
            return r.status_code, None
        ct = r.headers.get("content-type", "")
        if "application/json" in ct:
            return r.status_code, r.json() if r.text else None
        return r.status_code, r.text

    def version(self) -> dict[str, Any]:
        code, data = self._get("/api/version")
        if code != 200 or not isinstance(data, dict):
            raise RuntimeError(f"version: HTTP {code}")
        return data

    def status(self) -> dict[str, Any]:
        code, data = self._get("/api/v1/status")
        if code != 200 or not isinstance(data, dict):
            raise RuntimeError(f"status: HTTP {code}")
        return data

    def job(self) -> dict[str, Any] | None:
        code, data = self._get("/api/v1/job")
        if code == 204:
            return None
        if code != 200 or not isinstance(data, dict):
            raise RuntimeError(f"job: HTTP {code}")
        return data
