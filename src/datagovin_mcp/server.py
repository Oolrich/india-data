"""MCP server exposing India's Open Government Data platform (data.gov.in) to LLMs."""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import DataGovInClient, DataGovInError
from .normalize import coverage, dedupe, split_fields, summarize_dataset, to_fields, wide_to_long

INSTRUCTIONS = """\
Tools for India's Open Government Data platform (data.gov.in).
Workflow: search_datasets -> describe_dataset -> get_series (time-series tables) or query_dataset (row lookups).
Many datasets are old: always check 'updated' and 'data_covers' and tell the user how current the data is.
Values are as published by the source ministry; mention the dataset title when citing numbers."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True, idempotent_hint=True)
PAGE_SIZE = 100
MAX_SCAN = int(os.environ.get("DATA_GOV_IN_MAX_SCAN", "500"))
MAX_SERIES_RECORDS = int(os.environ.get("DATA_GOV_IN_MAX_SERIES_RECORDS", "1000"))
WIDE_TABLE_COLUMNS = 25

mcp = MCPServer("data-gov-in", instructions=INSTRUCTIONS)
_client: DataGovInClient | None = None


def get_client() -> DataGovInClient:
    global _client
    if _client is None:
        _client = DataGovInClient()
    return _client


def set_client(client: DataGovInClient | None) -> None:
    """Swap the API client (used by tests)."""
    global _client
    _client = client


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


async def _schema(dataset_id: str) -> dict[str, Any]:
    return await get_client().get_resource(dataset_id, limit=1)


def _column_lookup(fields) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for f in fields:
        lookup[f.name.lower()] = f.id
        lookup[f.id.lower()] = f.id
    return lookup


@mcp.tool(annotations=READ_ONLY)
async def search_datasets(query: str = "", sector: str | None = None, org: str | None = None, limit: int = 10) -> dict:
    """Search the data.gov.in catalog for datasets.

    Args:
        query: Keywords that must all appear in the title, description, ministry or sector, e.g. "gdp quarterly".
        sector: Optional exact sector name, e.g. "Economy", "Finance", "Industries", "Agriculture".
        org: Optional ministry/organisation substring, e.g. "Statistics" or "Petroleum".
        limit: Maximum results to return (1-25).

    Returns compact summaries (id, title, ministry, last update, and the period the data covers).
    Newest datasets are scanned first.
    """
    limit = _clamp(limit, 1, 25)
    terms = query.lower().split()
    org_needle = org.lower() if org else None
    results: list[dict] = []
    scanned, offset, total = 0, 0, 0
    try:
        while scanned < MAX_SCAN and len(results) < limit:
            page = await get_client().list_datasets(sectors=[sector] if sector else [], limit=PAGE_SIZE, offset=offset)
            records = page.get("records") or []
            total = int(page.get("total") or 0)
            for rec in records:
                orgs = " ".join(dedupe(rec.get("org"))).lower()
                haystack = " ".join([str(rec.get("title", "")), str(rec.get("desc", "")), orgs, *dedupe(rec.get("sector"))]).lower()
                if terms and not all(t in haystack for t in terms):
                    continue
                if org_needle and org_needle not in orgs:
                    continue
                results.append(summarize_dataset(rec))
                if len(results) >= limit:
                    break
            scanned += len(records)
            offset += len(records)
            if not records or offset >= total:
                break
    except DataGovInError as exc:
        raise ToolError(str(exc)) from exc

    out: dict[str, Any] = {"results": results, "scanned": scanned, "catalog_total": total}
    if len(results) < limit and scanned < total:
        out["note"] = (
            f"Only the {scanned} most recent of {total} datasets were scanned. "
            "Add a sector filter or different keywords to find older datasets."
        )
    return out


@mcp.tool(annotations=READ_ONLY)
async def describe_dataset(dataset_id: str) -> dict:
    """Show a dataset's schema: its dimension columns, and for wide time-series tables,
    which metrics and periods it covers. Call this before query_dataset or get_series.

    Args:
        dataset_id: The 'id' from search_datasets.
    """
    try:
        data = await _schema(dataset_id)
    except DataGovInError as exc:
        raise ToolError(str(exc)) from exc
    fields = to_fields(data.get("field"))
    dims, period_fields = split_fields(fields)
    out: dict[str, Any] = {
        "id": dataset_id,
        "title": (data.get("title") or "").strip() or None,
        "org": dedupe(data.get("org")),
        "updated": str(data.get("updated_date") or "")[:10] or None,
        "total_rows": int(data.get("total") or 0),
        "columns": [{"id": f.id, "name": f.name, "type": f.type} for f in dims][:60],
    }
    if period_fields:
        out["time_series"] = {
            "period_columns": len(period_fields),
            "metrics": dedupe(p.metric for p in period_fields),
            "covers": coverage(period_fields),
            "hint": "Use get_series to read these columns as a tidy time series.",
        }
    records = data.get("records") or []
    if records:
        out["sample_row"] = {f.name: records[0].get(f.id) for f in dims}
    return out


@mcp.tool(annotations=READ_ONLY)
async def query_dataset(
    dataset_id: str,
    filters: dict[str, str] | None = None,
    columns: list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Fetch rows from a dataset, with exact-match filters and column selection.

    Args:
        dataset_id: The 'id' from search_datasets.
        filters: Column (name or id) -> exact value, e.g. {"State/UT": "Odisha"}.
        columns: Columns (names or ids) to return. Defaults to all, except that very wide
            time-series tables return only their dimension columns (use get_series for those).
        limit: Rows per page (1-100).
        offset: Row offset for pagination; use 'next_offset' from the previous call.
    """
    limit = _clamp(limit, 1, 100)
    offset = max(0, int(offset))
    try:
        schema = await _schema(dataset_id)
        fields = to_fields(schema.get("field"))
        lookup = _column_lookup(fields)
        names = {f.id: f.name for f in fields}

        resolved_filters: dict[str, str] = {}
        for key, value in (filters or {}).items():
            fid = lookup.get(str(key).lower())
            if not fid:
                dims, _ = split_fields(fields)
                raise ToolError(f"Unknown filter column '{key}'. Filterable columns: {[d.name for d in dims]}")
            resolved_filters[fid] = str(value)

        note = None
        if columns:
            selected = []
            for col in columns:
                fid = lookup.get(str(col).lower())
                if not fid:
                    raise ToolError(f"Unknown column '{col}'. Call describe_dataset to see available columns.")
                selected.append(fid)
        elif len(fields) > WIDE_TABLE_COLUMNS:
            dims, period_fields = split_fields(fields)
            selected = [d.id for d in dims]
            note = f"{len(period_fields)} period columns omitted; use get_series or pass 'columns'."
        else:
            selected = [f.id for f in fields if f.id != "document_id"]

        data = await get_client().get_resource(dataset_id, filters=resolved_filters, limit=limit, offset=offset)
    except DataGovInError as exc:
        raise ToolError(str(exc)) from exc

    records = data.get("records") or []
    total = int(data.get("total") or 0)
    out: dict[str, Any] = {
        "rows": [{names.get(fid, fid): rec.get(fid) for fid in selected} for rec in records],
        "total_matching": total,
        "returned": len(records),
    }
    if offset + len(records) < total:
        out["next_offset"] = offset + len(records)
    if note:
        out["note"] = note
    return out


@mcp.tool(annotations=READ_ONLY)
async def get_series(dataset_id: str, item: str | None = None, metric: str | None = None, max_rows: int = 300) -> dict:
    """Read a wide time-series table (one column per year/quarter) as tidy rows:
    [{<dimension columns>, metric, period, value}], sorted by period.

    Args:
        dataset_id: The 'id' from search_datasets.
        item: Optional case-insensitive filter on any dimension value, e.g. "GDP" or "Odisha".
        metric: Optional case-insensitive filter on the metric name, e.g. "growth" or "value".
        max_rows: Maximum rows to return (1-1000).
    """
    max_rows = _clamp(max_rows, 1, 1000)
    client = get_client()
    try:
        first = await client.get_resource(dataset_id, limit=PAGE_SIZE, offset=0)
        records = list(first.get("records") or [])
        total = int(first.get("total") or len(records))
        while len(records) < min(total, MAX_SERIES_RECORDS):
            page = await client.get_resource(dataset_id, limit=PAGE_SIZE, offset=len(records))
            batch = page.get("records") or []
            if not batch:
                break
            records.extend(batch)
    except DataGovInError as exc:
        raise ToolError(str(exc)) from exc

    fields = to_fields(first.get("field"))
    dims, period_fields = split_fields(fields)
    if not period_fields:
        raise ToolError("This dataset has no year/quarter columns. Use query_dataset instead.")

    all_rows = wide_to_long(fields, records, item=item, metric=metric)
    rows = [r for r in all_rows if r["value"] is not None]
    out: dict[str, Any] = {
        "title": (first.get("title") or "").strip() or None,
        "updated": str(first.get("updated_date") or "")[:10] or None,
        "covers": coverage(period_fields),
        "metrics": dedupe(p.metric for p in period_fields),
        "rows": rows[:max_rows],
        "returned": min(len(rows), max_rows),
        "missing_values_skipped": len(all_rows) - len(rows),
    }
    if len(rows) > max_rows:
        out["truncated"] = True
        if dims:
            out["available_items"] = dedupe(rec.get(dims[0].id) for rec in records)[:50]
            out["hint"] = "Narrow with 'item' and/or 'metric' to get the rows you need."
    if len(records) < total:
        out["note"] = f"Read the first {len(records)} of {total} rows."
    if not rows and item:
        out["available_items"] = dedupe(rec.get(dims[0].id) for rec in records)[:50] if dims else []
    return out


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
