# amazon-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an LLM search and
inspect [amazon.de](https://www.amazon.de) products. Read-only product search:
it searches and reads product pages, it does not add to cart or buy.

It speaks **streamable-HTTP** (`:8000/mcp`) plus a `/healthz` probe, so it runs
as a self-hosted container behind an MCP client such as LibreChat — the same
shape as its siblings [ebay-mcp](https://github.com/jnslmk/ebay-mcp) and
[kleinanzeigen-mcp](https://github.com/jnslmk/kleinanzeigen-mcp).

## Why a scraper (and why a headless browser)

Amazon has **no keyless product-search API** for this use case — the Product
Advertising API (PA-API) requires Amazon Associate sales eligibility and is
being wound down. So this is a scraper, and like Kleinanzeigen it is inherently
**ToS-gray and fragile**: Amazon can change its markup or tighten bot detection
at any time.

Plain HTTP does **not** work: amazon.de fronts its pages with Akamai Bot
Manager, which serves a `bm-verify` JavaScript challenge to non-browser clients
— observed even from residential IPs. Solving it needs a real JS engine, so the
scraping is done with a **headless Chromium** driven by Playwright, exactly like
the sibling kleinanzeigen-mcp. Datacenter IPs (e.g. a VPS) are much more likely
to be blocked outright than residential ones.

## Tools

| Tool | What it does |
|------|--------------|
| `search_amazon` | Search by keyword with optional price range and sort. Returns product summaries + ASINs. |
| `get_amazon_product` | Full record for one ASIN (or amazon.de URL): title, price, availability, rating, feature bullets, images, seller. |

`search_amazon` sort accepts `price_asc`, `price_desc`, `review`, `newest`,
`featured`. Prices are in EUR; titles are German (amazon.de).

## Configuration

No credentials. Everything is optional tuning (see `.env.example`):

| Env var | Default | Meaning |
|---------|---------|---------|
| `MCP_TRANSPORT` | `http` | `http` (streamable-HTTP) or `stdio` |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `0.0.0.0` / `8000` / `/mcp` | HTTP bind |
| `LOG_LEVEL` | `INFO` | Logging level |
| `AMZ_MAX_CONTEXTS` | `4` | Upper bound on the semaphore (one Chromium context is shared) |
| `AMZ_MAX_CONCURRENT` | `2` | Max concurrent page loads |
| `AMZ_NAV_TIMEOUT_MS` | `45000` | Per-navigation timeout |
| `AMZ_PACING_MIN_S` / `AMZ_PACING_MAX_S` | `1.5` / `4.0` | Jittered delay between navigation starts |
| `AMZ_BACKOFF_BASE_S` / `AMZ_BACKOFF_MAX_S` | `2.0` / `120.0` | Exponential backoff after blocks/429s (base / cap, seconds) |
| `AMZ_RETRY_AFTER_MAX_S` | `180.0` | Ceiling for an honoured `Retry-After` value |

Fetch failures never read as empty results: a bot challenge, an HTTP 429 or
unrecognised markup raise a typed MCP tool error (`BotChallengeError`,
`RateLimitError`, `PageMismatchError`) instead of returning zero products.

## Run

### Docker (recommended)

```bash
docker run --rm -p 8000:8000 --shm-size=512m \
  ghcr.io/jnslmk/amazon-mcp:latest
# MCP endpoint: http://localhost:8000/mcp   health: http://localhost:8000/healthz
```

`--shm-size=512m` matters: Chromium maps its renderer heap into `/dev/shm` and
crashes on Docker's 64 MB default.

### Local (stdio, for desktop MCP clients)

```bash
python3 -m venv .venv && .venv/bin/pip install .
.venv/bin/playwright install chromium
MCP_TRANSPORT=stdio .venv/bin/amazon-mcp
```

Quick client-free check of the scraper:

```bash
.venv/bin/python -m amazon_mcp.amazon_client "usb c kabel"
.venv/bin/python -m amazon_mcp.amazon_client product B0CXDXP8VR
```

## License

MIT. See [LICENSE](LICENSE).
