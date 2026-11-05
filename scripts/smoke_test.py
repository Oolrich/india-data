"""Live check against api.data.gov.in. Run: DATA_GOV_IN_API_KEY=... python scripts/smoke_test.py"""

import asyncio
import json

from datagovin_mcp import server


def show(label, data):
    text = json.dumps(data, indent=2, ensure_ascii=False)
    print(f"\n=== {label} ===\n{text[:1500]}{' ...' if len(text) > 1500 else ''}")


async def main():
    found = await server.search_datasets(query="gdp quarterly", sector="Economy", limit=3)
    show("search_datasets", found)
    if not found["results"]:
        return
    dataset_id = found["results"][0]["id"]
    show("describe_dataset", await server.describe_dataset(dataset_id))
    show("get_series", await server.get_series(dataset_id, item="GDP", metric="growth", max_rows=8))
    await server.get_client().aclose()


if __name__ == "__main__":
    asyncio.run(main())
