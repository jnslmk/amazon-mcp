# amazon-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an LLM search and
inspect [amazon.de](https://www.amazon.de) products. Read-only product search:
it searches and reads product pages, it does not add to cart or buy.

It speaks **streamable-HTTP** (`:8000/mcp`) plus a `/healthz` probe, so it runs
as a self-hosted container behind an MCP client such as LibreChat — the same
shape as its siblings [ebay-mcp](https://github.com/jnslmk/ebay-mcp) and
[kleinanzeigen-mcp](https://github.com/jnslmk/kleinanzeigen-mcp).

## Why a scraper (and why curl_cffi)

Amazon has **no keyless product-search API** for this use case — the Product
Advertising API (PA-API) requires Amazon Associate sales eligibility and is
being wound down. So this is a scraper, and like Kleinanzeigen it is inherently
**ToS-gray and fragile**: Amazon can change its markup or tighten bot detection
at any time.

The default transport is **browser-free**: `curl_cffi.requests.AsyncSession`
uses a Chrome HTTP/TLS profile and an explicit German language header. A real
search followed by a result-derived product detail succeeded with this profile
where plain stdlib HTTP received `bm-verify`. One shared session retains cookies
and connections; no JavaScript engine, browser download or fallback is used.
This is not a guarantee of access: Amazon can still block an IP or change its
markup, and such responses fail explicitly.

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
| `AMZ_MAX_CONCURRENT` | `2` | Max concurrent HTTP requests (and connection-pool handles) |
| `AMZ_REQUEST_TIMEOUT_MS` | `45000` | Complete HTTP request wall budget, including redirects/body, after concurrency/pacing queues |
| `AMZ_USER_AGENT` | Chrome profile default | Optional User-Agent override; German `Accept-Language` stays explicit |
| `AMZ_PACING_MIN_S` / `AMZ_PACING_MAX_S` | `1.5` / `4.0` | Jittered delay between request starts |
| `AMZ_BACKOFF_BASE_S` / `AMZ_BACKOFF_MAX_S` | `2.0` / `120.0` | Exponential backoff after blocks/429s/page mismatches (base / cap, seconds) |
| `AMZ_RETRY_AFTER_MAX_S` | `180.0` | Ceiling for an honoured `Retry-After` value |

Fetch failures never read as empty results: a bot challenge, an HTTP 429 or
unrecognised markup raise a typed MCP tool error (`BotChallengeError`,
`RateLimitError`, `PageMismatchError`) instead of returning zero products.

There are no automatic retries. Blocks, rate limits and page mismatches stretch
the shared pacer before the next call, honouring numeric `Retry-After` up to its
cap. Other HTTP/transport errors also fail, never becoming products.
`/healthz` reports local HTTP-session readiness only, not Amazon reachability.

Migration: `AMZ_MAX_CONTEXTS` is removed; set `AMZ_MAX_CONCURRENT` directly to
the desired request limit. `AMZ_NAV_TIMEOUT_MS` is replaced by
`AMZ_REQUEST_TIMEOUT_MS`. Browser shared-memory/resource allowances are no
longer required.

## Run

### Docker (recommended)

```bash
docker run --rm -p 8000:8000 \
  ghcr.io/jnslmk/amazon-mcp:latest
# MCP endpoint: http://localhost:8000/mcp   health: http://localhost:8000/healthz
```

### Local (stdio, for desktop MCP clients)

```bash
python3 -m venv .venv && .venv/bin/pip install .
MCP_TRANSPORT=stdio .venv/bin/amazon-mcp
```

Quick client-free check of the scraper:

```bash
.venv/bin/python -m amazon_mcp.amazon_client "usb c kabel"
.venv/bin/python -m amazon_mcp.amazon_client product B0CXDXP8VR
```

Focused offline regressions (real loopback HTTP session, no Amazon requests):

```bash
python -m unittest discover -s tests
```

## License

MIT. See [LICENSE](LICENSE).
