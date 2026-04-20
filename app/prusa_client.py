from typing import Any
from urllib.parse import quote

import requests
from requests.auth import HTTPDigestAuth


class PrusaClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 60.0,
        download_timeout: float = 300.0,
        connect_download_enabled: bool = False,
        connect_printer_id: str | None = None,
        connect_team_id: int | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._auth = HTTPDigestAuth(username, password)
        self._timeout = timeout
        self._download_timeout = download_timeout
        self._connect_download_enabled = connect_download_enabled
        self._connect_printer_id = connect_printer_id.strip() if connect_printer_id else None
        self._connect_team_id = connect_team_id
        self.last_download_debug: str | None = None

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

    def _to_absolute(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        return f"{self.base_url}{path_or_url}"

    def download_print_file_urls(self, job: dict) -> list[str]:
        f = job.get("file")
        if not isinstance(f, dict):
            return []
        out: list[str] = []

        def add(url: str) -> None:
            if url and url not in out:
                out.append(url)

        refs = f.get("refs")
        if isinstance(refs, dict):
            href = refs.get("download")
            if isinstance(href, str) and href.startswith(("/", "http://", "https://")):
                add(href)

        storage = (f.get("path") or "/local").strip("/") or "local"
        short_name = f.get("name")
        display_name = f.get("display_name") or short_name
        dp = (f.get("display_path") or "").strip("/")
        dp_segments = [p for p in dp.split("/") if p]

        def suffix_for(name: str | None) -> str | None:
            if not name:
                return None
            segs = list(dp_segments)
            segs.append(name)
            return "/".join(quote(s, safe="") for s in segs)

        for name in (short_name, display_name):
            sfx = suffix_for(name)
            if not sfx:
                continue
            add(f"/{storage}/{sfx}")
            add(f"/api/files/{storage}/{sfx}/raw")
            add(f"/api/v1/files/{storage}/{sfx}/raw")
            add(f"/api/v1/files/{storage}/{sfx}?download=1")
            add(f"/api/v1/files/{storage}/{sfx}")
        return out

    def download_print_file(self, job: dict) -> tuple[bytes, str] | None:
        self.last_download_debug = None
        f = job.get("file")
        if not isinstance(f, dict):
            self.last_download_debug = "no_job_file_dict"
            return None
        name = str(f.get("display_name") or f.get("name") or "print")
        connect_reason: str | None = None
        if self._connect_download_enabled:
            got = self._download_print_file_via_connect(job, name)
            if got is not None:
                self.last_download_debug = "connect_ok"
                return got
            connect_reason = self.last_download_debug

        fallback_reasons: list[str] = []
        for cand in self.download_print_file_urls(job):
            url = self._to_absolute(cand)
            try:
                r = requests.get(
                    url,
                    auth=self._auth,
                    timeout=self._download_timeout,
                    headers={"Accept": "application/octet-stream"},
                    allow_redirects=True,
                )
            except requests.RequestException:
                fallback_reasons.append("request_exception")
                continue
            if r.status_code != 200:
                fallback_reasons.append(f"http_{r.status_code}")
                continue
            ct = (r.headers.get("content-type") or "").lower()
            if "application/json" in ct:
                fallback_reasons.append("json_metadata")
                continue
            body = r.content
            if not body:
                fallback_reasons.append("empty_body")
                continue
            self.last_download_debug = "fallback_ok"
            return body, name
        parts: list[str] = []
        if connect_reason:
            parts.append(connect_reason)
        if fallback_reasons:
            uniq = ",".join(dict.fromkeys(fallback_reasons))
            parts.append(f"fallback_failed:{uniq}")
        else:
            parts.append("fallback_failed:no_candidates")
        self.last_download_debug = "+".join(parts)
        return None

    def _download_print_file_via_connect(
        self, job: dict, display_name: str
    ) -> tuple[bytes, str] | None:
        """
        Preferred route: Prusa Connect SDK, same approach as reference project.
        Requires:
          - SDK installed (`prusa-connect-sdk-client`)
          - auth set up for PrusaConnect
          - printer/team context (from env or lookup)
          - job hash in current job payload
        """
        try:
            from prusa.connect.client import PrusaConnectClient  # type: ignore[import-not-found]
        except ImportError:
            self.last_download_debug = "connect_import_error"
            return None

        team_id = self._connect_team_id
        printer_id = self._connect_printer_id
        try:
            client = PrusaConnectClient()
        except Exception:
            self.last_download_debug = "connect_client_init_failed"
            return None

        # PrusaLink /api/v1/job may omit hash while printing; prefer SDK printer.job hash when needed.
        job_hash_raw = job.get("hash")
        if (not job_hash_raw) and printer_id:
            try:
                printer = client.printers.get(printer_id)
                pj = getattr(printer, "job", None)
                if isinstance(pj, dict):
                    job_hash_raw = pj.get("hash")
                elif pj is not None:
                    job_hash_raw = getattr(pj, "hash", None)
                if team_id is None and getattr(printer, "team_id", None) is not None:
                    team_id = int(printer.team_id)
            except Exception:
                self.last_download_debug = "connect_printer_lookup_failed"
                return None

        if not job_hash_raw:
            self.last_download_debug = "connect_missing_job_hash"
            return None
        job_hash = str(job_hash_raw).strip()
        if not job_hash:
            self.last_download_debug = "connect_empty_job_hash"
            return None

        if team_id is None and printer_id:
            try:
                printer = client.printers.get(printer_id)
                if getattr(printer, "team_id", None) is not None:
                    team_id = int(printer.team_id)
            except Exception:
                self.last_download_debug = "connect_team_lookup_failed"
                return None
        if team_id is None:
            self.last_download_debug = "connect_missing_team_id"
            return None
        try:
            body = client.download_team_file(team_id, job_hash)
        except Exception:
            self.last_download_debug = "connect_download_exception"
            return None
        if not body:
            self.last_download_debug = "connect_empty_body"
            return None
        self.last_download_debug = "connect_ok"
        return body, display_name
