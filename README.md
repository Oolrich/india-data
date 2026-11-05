# datagovin-mcp

An [MCP](https://modelcontextprotocol.io) server that lets LLMs like Claude search and query
**India's Open Government Data platform ([data.gov.in](https://data.gov.in))**: GDP, trade,
industry, agriculture and hundreds of thousands of other datasets published by Indian ministries.

> *"How did India's quarterly GDP growth move through COVID?"*
> Claude searches the catalog, finds the MoSPI dataset, pulls the series and answers
> with real numbers, citing the source dataset and how current it is.

## Tools

| Tool | What it does |
|---|---|
| `search_datasets` | Keyword search over the catalog, with sector and ministry filters. Returns compact summaries with the period each dataset covers. |
| `describe_dataset` | A dataset's schema: dimension columns, and for wide tables, which metrics and periods it contains. |
| `query_dataset` | Rows with exact-match filters (by column name or id), column selection and pagination. |
| `get_series` | Reshapes wide year/quarter tables into tidy `{metric, period, value}` rows. |

All tools are marked read-only.

## Why this is more than an API wrapper

data.gov.in data is published as-is by each ministry, and it is messy in ways that trip up LLMs:

- **Wide tables.** The quarterly GDP dataset has ~150 columns (`value_rs_in_crore___2011_12___q1`, …).
  `get_series` parses the column *names* into `(metric, period)` pairs and returns a sorted series.
- **Inconsistent naming within one dataset.** The same dataset mixes `___q2` and `__q3` ids and
  `2021-22 - Q2` vs `2021-22- Q3` names; another mixes `Total - As on 31.03.2019` with
  `Total as on 31.03.2018`. The normalizer treats these as the same metric, so no periods get silently dropped.
- **Token bloat.** Catalog responses include every column of every dataset. Search results are trimmed
  to summaries, and wide tables return only dimension columns unless asked.
- **Stale data.** Many datasets stop years ago. Every summary reports the last update date and the
  period covered, and the server instructions ask the model to tell the user how current the data is.
- **Reliability and safety.** Responses are cached (TTL), HTTP 429/5xx errors are retried with exponential backoff,
  dataset ids are validated as UUIDs before any request, and the API key is never echoed in errors.

## Setup

1. Get a free API key: register at data.gov.in and copy the key from your **My Account** page.
2. Install with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/Oolrich/datagovin-mcp && cd datagovin-mcp
uv sync --extra dev
```

3. Check it works against the live API:

```bash
export DATA_GOV_IN_API_KEY=your-key
uv run python scripts/smoke_test.py
```

### Use with Claude Desktop

Add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), then restart Claude:

```json
{
  "mcpServers": {
    "data-gov-in": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/datagovin-mcp", "run", "datagovin-mcp"],
      "env": { "DATA_GOV_IN_API_KEY": "your-key" }
    }
  }
}
```

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATA_GOV_IN_API_KEY` | (required for data) | Your data.gov.in key. Catalog search works without it. |
| `DATA_GOV_IN_MAX_SCAN` | `500` | How many catalog entries one search scans, newest first. |
| `DATA_GOV_IN_MAX_SERIES_RECORDS` | `1000` | Row cap when building a series. |

## Development

```bash
uv run pytest
```

Tests run offline against a fake API built from real catalog responses, and they include an end-to-end
test over the MCP protocol.

## Limitations

- Search scans the newest catalog entries and matches keywords locally, because the catalog API has no
  full-text search. Use `sector` to reach older datasets.
- Filters are exact-match, as supported by the upstream API.
- Data is exactly as published by the source ministry. Check the dataset title and update date before relying on it.

Inspired by the [datagovindia](https://github.com/econabhishek/datagovindia) R package, rebuilt for LLM agents.

## License

MIT
