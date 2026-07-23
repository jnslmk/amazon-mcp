"""MCP server exposing amazon.de product search as tools for LLM agents.

Amazon offers no keyless product-search API for this use case, so this is a
scraper. Amazon fronts its pages with Akamai Bot Manager (a ``bm-verify`` JS
challenge that plain HTTP clients cannot solve, even from residential IPs), so
the scraping is done with a real headless Chromium via Playwright — the same
shape as the sibling kleinanzeigen-mcp. The client library
(:mod:`amazon_mcp.amazon_client`) owns Chromium's lifecycle; this module owns
the two tools, response shaping and the streamable-HTTP transport.

This is ToS-gray: it scrapes a site whose terms discourage automated access,
and it is fragile by nature — Amazon can change its markup or tighten bot
detection at any time. It is intended for personal, low-volume use behind a
single chat agent.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Optional

from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from amazon_mcp.amazon_client import (
    BASE_URL,
    AmazonBrowser,
    BotChallengeError,
    _canonical_product_url,
    _extract_asin,
    build_search_url,
    parse_product,
    parse_search_results,
)

log = logging.getLogger("amazon-mcp")

_browser: AmazonBrowser | None = None


def _require_browser() -> AmazonBrowser:
    if _browser is None:  # pragma: no cover - guarded by the lifespan
        raise RuntimeError("Browser is not running")
    return _browser


@asynccontextmanager
async def lifespan(_: FastMCP) -> AsyncIterator[None]:
    """Start one shared Chromium instance for the process lifetime."""
    global _browser
    _browser = AmazonBrowser()
    await _browser.start()
    try:
        yield
    finally:
        await _browser.close()
        _browser = None


mcp = FastMCP(
    name="amazon",
    version="0.1.0",
    lifespan=lifespan,
    instructions=(
        "Search amazon.de, Germany's Amazon marketplace, for products. Prices "
        "are in EUR and titles are German. Start with `search_amazon` to get a "
        "list of products with their ASINs, then call `get_amazon_product` on "
        "the ASIN (or a full amazon.de URL) of anything worth a closer look to "
        "get the full description, feature bullets, availability and images. "
        "This is read-only product search — it cannot add to cart or buy. It "
        "scrapes the live site, so an occasional empty result can just mean "
        "Amazon's bot detection got in the way; retrying usually helps."
    ),
)


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #


@mcp.tool
async def search_amazon(
    query: Annotated[
        str,
        Field(description="Search keywords, e.g. 'usb c kabel' or 'anker powerbank'"),
    ],
    limit: Annotated[
        int, Field(description="Maximum products to return", ge=1, le=50)
    ] = 10,
    sort: Annotated[
        Optional[str],
        Field(
            description=(
                "Result ordering: 'price_asc' (cheapest first), 'price_desc' "
                "(most expensive first), 'review' (best rated), 'newest', or "
                "'featured'. Omit for Amazon's default relevance ranking."
            )
        ),
    ] = None,
    min_price: Annotated[
        Optional[float],
        Field(description="Minimum price in EUR", ge=0),
    ] = None,
    max_price: Annotated[
        Optional[float],
        Field(description="Maximum price in EUR", ge=0),
    ] = None,
    page: Annotated[
        int, Field(description="Result page number (~50 products per page)", ge=1)
    ] = 1,
) -> dict[str, Any]:
    """Search amazon.de for products by keyword, with optional price and sort.

    Returns product summaries — ASIN, title, price (EUR), rating, review count,
    Prime flag (when Amazon renders one), thumbnail image and canonical URL.
    Pass a product's `asin` to `get_amazon_product` for the full detail record.
    """
    url = build_search_url(
        query=query,
        sort=sort,
        min_price=min_price,
        max_price=max_price,
        page=page,
    )
    try:
        html = await _require_browser().fetch_html(url)
    except BotChallengeError as exc:
        return {
            "query": query,
            "returned": 0,
            "items": [],
            "error": "bot_check",
            "detail": str(exc),
        }

    items = parse_search_results(html, limit=limit)
    return {
        "query": query,
        "page": page,
        "sort": sort,
        "returned": len(items),
        "items": items,
    }


@mcp.tool
async def get_amazon_product(
    product: Annotated[
        str,
        Field(
            description=(
                "An ASIN (e.g. 'B0CXDXP8VR') or a full amazon.de product URL. "
                "ASINs come from `search_amazon`'s `asin` field."
            )
        ),
    ],
) -> dict[str, Any]:
    """Retrieve the full detail record of a single amazon.de product.

    Use after `search_amazon` surfaces something worth a closer look: title,
    price (EUR), availability, rating and review count, feature bullets, all
    product images, brand and the seller when Amazon shows one. Accepts either a
    bare ASIN or a full amazon.de URL.
    """
    asin = _extract_asin(product)
    if not asin:
        raise ValueError(
            "Could not find an ASIN in the input; pass a 10-character ASIN or a "
            "full amazon.de product URL"
        )

    url = _canonical_product_url(asin, product) or f"{BASE_URL}/dp/{asin}"
    try:
        html = await _require_browser().fetch_html(url)
    except BotChallengeError as exc:
        return {"asin": asin, "url": url, "error": "bot_check", "detail": str(exc)}

    return parse_product(html, url)


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    """Container readiness probe: reports whether Chromium actually came up."""
    if _browser is None or not _browser.ready:
        return JSONResponse({"status": "starting"}, status_code=503)
    return JSONResponse({"status": "ok"})


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    transport = os.getenv("MCP_TRANSPORT", "http")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="http",
            host=os.getenv("MCP_HOST", "0.0.0.0"),  # noqa: S104 - containerised
            port=int(os.getenv("MCP_PORT", "8000")),
            path=os.getenv("MCP_PATH", "/mcp"),
        )


if __name__ == "__main__":
    main()
