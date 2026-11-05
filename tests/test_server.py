import httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from datagovin_mcp import server
from datagovin_mcp.client import DataGovInClient

from .conftest import run

GDP = "a56b86d1-f00e-4ee4-ab08-5b5367b3fab2"
LPG = "84f8814d-288b-4f25-a18e-86e5453ce510"


def test_search_matches_all_terms(api):
    out = run(server.search_datasets(query="gdp quarterly"))
    assert [r["id"] for r in out["results"]] == [GDP]
    assert run(server.search_datasets(org="petroleum"))["results"][0]["id"] == LPG


def test_describe_summarises_wide_table(api):
    out = run(server.describe_dataset(GDP))
    assert [c["name"] for c in out["columns"]] == ["Item"]
    assert out["time_series"]["metrics"] == ["Value (Rs. in crore)", "Growth Rate (%)"]
    assert out["time_series"]["covers"]["to"] == "2022-23 Q1"


def test_query_with_filter_by_column_name(api):
    out = run(server.query_dataset(LPG, filters={"State/UT": "Odisha"}))
    assert out["total_matching"] == 1
    assert out["rows"][0]["State/UT"] == "Odisha"


def test_query_rejects_unknown_column(api):
    with pytest.raises(ToolError, match="Unknown filter column"):
        run(server.query_dataset(LPG, filters={"District": "Puri"}))


def test_series_skips_missing_values(api):
    out = run(server.get_series(GDP, item="GDP", metric="growth"))
    assert out["rows"] == [{"Item": "GDP", "metric": "Growth Rate (%)", "period": "2021-22 Q2", "value": 8.4}]
    assert out["missing_values_skipped"] == 1


def test_series_on_non_time_series_dataset_is_rejected(api, monkeypatch):
    # LPG has 'as on' date columns, so it *is* a series; check a truly flat table via fake schema
    async def flat(*a, **k):
        return {"field": [{"id": "country", "name": "Country", "type": "keyword"}], "records": [], "total": 0}
    monkeypatch.setattr(server.get_client(), "get_resource", flat)
    with pytest.raises(ToolError, match="no year/quarter columns"):
        run(server.get_series(GDP))


def test_invalid_id_is_rejected_before_any_request(api):
    with pytest.raises(ToolError, match="not a valid dataset id"):
        run(server.describe_dataset("../../lists"))
    assert api.calls == []


def test_responses_are_cached(api):
    run(server.describe_dataset(GDP))
    run(server.describe_dataset(GDP))
    assert len(api.calls) == 1


def test_retries_on_rate_limit(api):
    api.fail_next = 2
    assert run(server.describe_dataset(GDP))["total_rows"] == 2
    assert len(api.calls) == 3


def test_missing_api_key_gives_helpful_error(api):
    server.set_client(DataGovInClient(api_key="", transport=httpx.MockTransport(api)))
    with pytest.raises(ToolError, match="DATA_GOV_IN_API_KEY"):
        run(server.describe_dataset(GDP))


def test_end_to_end_over_mcp(api):
    async def go():
        async with Client(server.mcp) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert tools == {"search_datasets", "describe_dataset", "query_dataset", "get_series"}
            result = await client.call_tool("search_datasets", {"query": "lpg"})
            assert not result.is_error
            return result
    result = run(go())
    assert LPG in str(result.content)
