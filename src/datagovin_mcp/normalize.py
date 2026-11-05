"""Turn data.gov.in's messy, wide tables into something an LLM can reason over.

data.gov.in datasets often store one column per period, e.g.
``Value (Rs. in crore) - 2011-12 - Q1`` ... ``Value (Rs. in crore) - 2022-23 - Q1``,
and naming is inconsistent even inside a single dataset (``2021-22 - Q3`` vs
``2021-22- Q3``, ``Total - As on 31.03.2019`` vs ``Total as on 31.03.2018``).

We parse the human-readable field *names* (not the ids, which are even less
consistent) into ``(metric, period)`` pairs so wide rows can be reshaped into a
tidy ``[{..., metric, period, value}]`` series.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

# Indian fiscal year like 2011-12 (the second part must follow the first).
_FISCAL_YEAR = re.compile(r"(?<!\d)(\d{4})\s*-\s*(\d{2})(?!\d)")
_QUARTER = re.compile(r"\bQ\s*([1-4])\b", re.IGNORECASE)
# Dates like 31.03.2019
_DATE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)")
_AS_ON = re.compile(r"\bas\s+on\b", re.IGNORECASE)
_MISSING = {"", "na", "n.a.", "n/a", "-", "--", "nil", "null", "none", "..."}


@dataclass(frozen=True)
class Period:
    label: str
    sort_key: tuple[int, int, int]


@dataclass(frozen=True)
class PeriodField:
    field_id: str
    metric: str
    period: Period


@dataclass(frozen=True)
class Field:
    id: str
    name: str
    type: str


def to_fields(raw_fields: Iterable[dict[str, Any]] | None) -> list[Field]:
    fields = []
    for f in raw_fields or []:
        fid = str(f.get("id") or f.get("name") or "").strip()
        if not fid:
            continue
        fields.append(Field(id=fid, name=str(f.get("name") or fid).strip(), type=str(f.get("type") or "")))
    return fields


def _clean_metric(text: str) -> str:
    text = _AS_ON.sub(" ", text)
    text = re.sub(r"\s*-\s*(-\s*)+", " - ", text)  # collapse "- -"
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" -–:")
    return text or "value"


def parse_period_field(field_id: str, name: str) -> PeriodField | None:
    """Return the (metric, period) a column represents, or None if it isn't a period column."""
    fy = _FISCAL_YEAR.search(name)
    if fy and (int(fy.group(1)) + 1) % 100 == int(fy.group(2)):
        start = int(fy.group(1))
        rest = name[: fy.start()] + " " + name[fy.end():]
        q = _QUARTER.search(rest)
        if q:
            quarter = int(q.group(1))
            rest = rest[: q.start()] + " " + rest[q.end():]
            period = Period(f"{fy.group(1)}-{fy.group(2)} Q{quarter}", (start, quarter, 0))
        else:
            period = Period(f"{fy.group(1)}-{fy.group(2)}", (start, 0, 0))
        return PeriodField(field_id, _clean_metric(rest), period)

    date = _DATE.search(name)
    if date:
        day, month, year = int(date.group(1)), int(date.group(2)), int(date.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            rest = name[: date.start()] + " " + name[date.end():]
            period = Period(f"{year:04d}-{month:02d}-{day:02d}", (year, month, day))
            return PeriodField(field_id, _clean_metric(rest), period)
    return None


def split_fields(fields: list[Field]) -> tuple[list[Field], list[PeriodField]]:
    """Split a schema into dimension columns (Item, State/UT, ...) and period columns."""
    dims, periods = [], []
    for f in fields:
        if f.id == "document_id":
            continue
        pf = parse_period_field(f.id, f.name)
        if pf:
            periods.append(pf)
        else:
            dims.append(f)
    return dims, periods


def coverage(period_fields: list[PeriodField]) -> dict[str, str] | None:
    if not period_fields:
        return None
    ordered = sorted(period_fields, key=lambda p: p.period.sort_key)
    return {"from": ordered[0].period.label, "to": ordered[-1].period.label}


def to_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text.lower() in _MISSING:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def wide_to_long(
    fields: list[Field],
    records: Iterable[dict[str, Any]],
    *,
    item: str | None = None,
    metric: str | None = None,
) -> list[dict[str, Any]]:
    """Reshape wide period columns into tidy rows, optionally filtered.

    ``item`` matches (case-insensitively, substring) any dimension value, e.g. "GDP"
    or "Odisha". ``metric`` matches the metric name, e.g. "growth".
    """
    dims, period_fields = split_fields(fields)
    if metric:
        needle = metric.lower()
        period_fields = [p for p in period_fields if needle in p.metric.lower()]
    item_needle = item.lower() if item else None

    rows: list[tuple[tuple, dict[str, Any]]] = []
    for rec in records:
        dim_values = {d.name: rec.get(d.id) for d in dims}
        if item_needle and not any(item_needle in str(v).lower() for v in dim_values.values() if v is not None):
            continue
        for pf in period_fields:
            row = {**dim_values, "metric": pf.metric, "period": pf.period.label, "value": to_number(rec.get(pf.field_id))}
            key = (tuple(str(v) for v in dim_values.values()), pf.metric, pf.period.sort_key)
            rows.append((key, row))
    rows.sort(key=lambda kv: kv[0])
    return [row for _, row in rows]


def dedupe(values: Iterable[Any]) -> list[str]:
    seen: dict[str, None] = {}
    for v in values or []:
        if v:
            seen.setdefault(str(v), None)
    return list(seen)


def summarize_dataset(rec: dict[str, Any]) -> dict[str, Any]:
    """A compact, token-cheap summary of one catalog entry (drops the huge field list)."""
    fields = to_fields(rec.get("field"))
    _, period_fields = split_fields(fields)
    summary = {
        "id": rec.get("index_name"),
        "title": (rec.get("title") or "").strip(),
        "org": dedupe(rec.get("org")),
        "sector": dedupe(rec.get("sector")),
        "updated": str(rec.get("updated_date") or "")[:10] or None,
        "columns": len(fields),
    }
    cov = coverage(period_fields)
    if cov:
        summary["data_covers"] = cov
    return summary
