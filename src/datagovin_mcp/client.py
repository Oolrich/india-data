"""Async client for the data.gov.in (OGD India) API with caching and retries."""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any, Sequence

import httpx

BASE_URL = "https://api.data.gov.in"
API_KEY_ENV = "DATA_GOV_IN_API_KEY"
_RESOURCE_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

Params = Sequence[tuple[str, str | int]]


class DataGovInError(Exception):
    """An error safe to show to the model / user (never contains the API key)."""


def validate_resource_id(resource_id: str) -> str:
    resource_id = (resource_id or "").strip()
    if not _RESOURCE_ID.match(resource_id):
        raise DataGovInError(
            f"'{resource_id}' is not a valid dataset id. Use the 'id' returned by search_datasets "
            "(a UUID like 'a56b86d1-f00e-4ee4-ab08-5b5367b3fab2')."
        )
    return resource_id.lower()


class _TTLCache:
    def __init__(self, ttl_seconds: float, max_entries: int = 512):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if time.monotonic() > expires:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: Any, value: Any) -> None:
        if len(self._data) >= self.max_entries:
            self._data.pop(next(iter(self._data)))  # evict oldest insertion
        self._data[key] = (time.monotonic() + self.ttl, value)


class DataGovInClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_ttl: float = 3600,
        timeout: float = 30,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV, "").strip() or None
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._cache = _TTLCache(cache_ttl)
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "datagovin-mcp/0.1"},
        )
        self.requests_made = 0  # handy for tests and debugging

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: Params, *, needs_key: bool) -> dict[str, Any]:
        cache_key = (path, tuple(sorted((k, str(v)) for k, v in params)))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        query: list[tuple[str, str | int]] = [("format", "json"), *params]
        if needs_key:
            if not self.api_key:
                raise DataGovInError(
                    f"No API key configured. Get a free key from your data.gov.in account page "
                    f"and set the {API_KEY_ENV} environment variable."
                )
            query.append(("api-key", self.api_key))

        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            if attempt:
                await asyncio.sleep(self.backoff_base * 2 ** (attempt - 1))
            try:
                self.requests_made += 1
                resp = await self._http.get(path, params=query)
            except httpx.TransportError as exc:
                last_error = f"network error: {type(exc).__name__}"
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code} from data.gov.in"
                continue
            if resp.status_code in (401, 403):
                raise DataGovInError("data.gov.in rejected the API key (HTTP %d)." % resp.status_code)
            if resp.status_code >= 400:
                raise DataGovInError(f"data.gov.in returned HTTP {resp.status_code} for {path}.")
            try:
                data = resp.json()
            except ValueError:
                raise DataGovInError("data.gov.in returned a non-JSON response.") from None
            if str(data.get("status", "ok")).lower() == "error":
                raise DataGovInError(f"data.gov.in error: {str(data.get('message', 'unknown'))[:200]}")
            self._cache.set(cache_key, data)
            return data
        raise DataGovInError(f"data.gov.in is unavailable after {self.max_retries + 1} attempts ({last_error}).")

    async def list_datasets(self, *, sectors: Sequence[str] = (), limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Catalog listing (newest first). Works without an API key."""
        params: list[tuple[str, str | int]] = [
            ("filters[active]", "1"),
            ("notfilters[source]", "visualize.data.gov.in"),
            ("sort[created]", "desc"),
            ("limit", limit),
            ("offset", offset),
        ]
        params += [("filters[sector][]", s) for s in sectors]
        return await self._get("/lists", params, needs_key=False)

    async def get_resource(
        self,
        resource_id: str,
        *,
        filters: dict[str, str] | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Rows (plus schema metadata) of one dataset. Requires an API key."""
        resource_id = validate_resource_id(resource_id)
        params: list[tuple[str, str | int]] = [("limit", limit), ("offset", offset)]
        params += [(f"filters[{k}]", v) for k, v in sorted((filters or {}).items())]
        return await self._get(f"/resource/{resource_id}", params, needs_key=True)
