"""Thin ClinicalTrials.gov API v2 client with retry/backoff and polite rate limiting."""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx

from protocol_intelligence.config import get_settings

CDN_DOCS_BASE = "https://cdn.clinicaltrials.gov/large-docs"
STUDY_PAGE_BASE = "https://clinicaltrials.gov/study"


def protocol_doc_url(nct_id: str, filename: str) -> str:
    return f"{CDN_DOCS_BASE}/{nct_id[-2:]}/{nct_id}/{filename}"


def study_page_url(nct_id: str) -> str:
    return f"{STUDY_PAGE_BASE}/{nct_id}"


class ClinicalTrialsClient:
    def __init__(self, base_url: str | None = None, min_interval_s: float = 1.3):
        # API guidance is ~50 requests/min; stay under it.
        self.base_url = (base_url or get_settings().ctgov_api_base).rstrip("/")
        self.min_interval_s = min_interval_s
        self._last_call = 0.0
        self._http = httpx.Client(
            timeout=60, follow_redirects=True,
            headers={"User-Agent": "protocol-intelligence-poc/0.1 (research)"},
        )

    def _get(self, url: str, params: dict | None = None, retries: int = 4) -> httpx.Response:
        for attempt in range(retries + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                r = self._http.get(url, params=params)
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                return r
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if attempt == retries or (status and 400 <= status < 500 and status != 429):
                    raise
                time.sleep(2 ** (attempt + 1))
        raise RuntimeError("unreachable")

    def search(self, params: dict, max_records: int) -> Iterator[dict]:
        """Yield study records across pages until max_records."""
        params = {"pageSize": min(max_records, 200), **params}
        seen = 0
        while True:
            data = self._get(f"{self.base_url}/studies", params).json()
            for study in data.get("studies", []):
                yield study
                seen += 1
                if seen >= max_records:
                    return
            token = data.get("nextPageToken")
            if not token:
                return
            params = {**params, "pageToken": token}

    def get_study(self, nct_id: str) -> dict:
        return self._get(f"{self.base_url}/studies/{nct_id}").json()

    def download(self, url: str) -> bytes:
        return self._get(url).content

    def api_version(self) -> str:
        try:
            return self._get(f"{self.base_url}/version").json().get("apiVersion", "unknown")
        except httpx.HTTPError:
            return "unknown"
