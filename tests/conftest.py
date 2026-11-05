import asyncio
import json
from pathlib import Path

import httpx
import pytest

from datagovin_mcp import server
from datagovin_mcp.client import DataGovInClient

FIXTURES = Path(__file__).parent / "fixtures"
RESOURCES = {
    "a56b86d1-f00e-4ee4-ab08-5b5367b3fab2": "resource_gdp.json",
    "84f8814d-288b-4f25-a18e-86e5453ce510": "resource_lpg.json",
}


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FakeAPI:
    """Minimal stand-in for api.data.gov.in that honours limit/offset/filters."""

    def __init__(self):
        self.calls = []
        self.fail_next = 0  # return HTTP 429 this many times first

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.fail_next:
            self.fail_next -= 1
            return httpx.Response(429)
        params = request.url.params
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 10))
        if request.url.path == "/lists":
            data = load("lists.json")
        elif request.url.path.startswith("/resource/"):
            if params.get("api-key") != "test-key":
                return httpx.Response(403)
            name = RESOURCES.get(request.url.path.rsplit("/", 1)[-1])
            if not name:
                return httpx.Response(404)
            data = load(name)
            for key, value in params.multi_items():
                if key.startswith("filters["):
                    col = key[len("filters["):-1]
                    data["records"] = [r for r in data["records"] if str(r.get(col)) == value]
            data["total"] = len(data["records"])
        else:
            return httpx.Response(404)
        data["records"] = data["records"][offset: offset + limit]
        return httpx.Response(200, json=data)


@pytest.fixture
def api():
    fake = FakeAPI()
    client = DataGovInClient(api_key="test-key", transport=httpx.MockTransport(fake), backoff_base=0)
    server.set_client(client)
    yield fake
    asyncio.run(client.aclose())
    server.set_client(None)


def run(coro):
    return asyncio.run(coro)
