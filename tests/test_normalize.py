from datagovin_mcp.normalize import parse_period_field, summarize_dataset, to_fields, to_number, wide_to_long

from .conftest import load


def test_quarter_columns_with_inconsistent_separators_parse_the_same():
    a = parse_period_field("value_rs_in_crore___2021_22___q2", "Value (Rs. in crore) - 2021-22 - Q2")
    b = parse_period_field("value_rs_in_crore___2021_22__q3", "Value (Rs. in crore) - 2021-22- Q3")
    assert (a.metric, a.period.label) == ("Value (Rs. in crore)", "2021-22 Q2")
    assert (b.metric, b.period.label) == ("Value (Rs. in crore)", "2021-22 Q3")
    assert a.period.sort_key < b.period.sort_key


def test_yearly_and_as_on_date_columns():
    qty = parse_period_field("_2014_15_qty", "2014-15 QTY")
    assert (qty.metric, qty.period.label) == ("QTY", "2014-15")
    t19 = parse_period_field("total___as_on_31_03_2019", "Total - As on 31.03.2019")
    t18 = parse_period_field("total_as_on_31_03_2018", "Total as on 31.03.2018")
    assert t19.metric == t18.metric == "Total"
    assert (t18.period.label, t19.period.label) == ("2018-03-31", "2019-03-31")


def test_non_period_columns_are_not_parsed():
    assert parse_period_field("item", "Item") is None
    assert parse_period_field("population___million_", "Population - (Million)") is None
    assert parse_period_field("x", "Range 1234-56") is None  # not a real fiscal year


def test_to_number_handles_missing_and_commas():
    assert to_number("1,234.5") == 1234.5
    assert to_number("NA") is None and to_number("") is None and to_number(None) is None


def test_wide_to_long_filters_and_sorts():
    data = load("resource_gdp.json")
    fields = to_fields(data["field"])
    rows = wide_to_long(fields, data["records"], item="gdp", metric="value")
    assert {r["Item"] for r in rows} == {"GDP"}
    periods = [r["period"] for r in rows]
    assert periods[0] == "2020-21 Q1" and periods[-1] == "2022-23 Q1"
    assert len(periods) == 9  # none lost to the double/triple underscore mismatch


def test_summary_is_compact_and_reports_coverage():
    rec = load("lists.json")["records"][0]
    s = summarize_dataset(rec)
    assert "field" not in s
    assert s["data_covers"] == {"from": "2020-21 Q1", "to": "2022-23 Q1"}
    assert s["updated"] == "2023-02-13"
