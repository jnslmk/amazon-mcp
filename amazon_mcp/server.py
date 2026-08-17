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


def _coerce_int(
    value: str | int | None, field: str, *, ge: int | None = None
) -> int | None:
    """Coerce the numeric strings LLMs routinely send for int parameters.

    FastMCP validates tool input against the JSON schema before the function
    runs, so a parameter typed ``int`` rejects the string ``"5"`` outright —
    and LibreChat's rejection names no field ("did not match expected
    schema"), so the model cannot see what to fix and can only guess.
    Accepting ``str | int`` in the schema and normalising here keeps the
    model-facing contract lenient while the rest of the module still sees a
    real int. Ported from aliexpress-mcp's ``_coerce_int``.
    """
    if value is None or isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field} must be an integer, got {value!r}") from exc
    else:
        raise ValueError(f"{field} must be an integer, got {value!r}")
    if ge is not None and result is not None and result < ge:
        raise ValueError(f"{field} must be >= {ge}, got {result}")
    return result


def _coerce_float(
    value: str | float | None, field: str, *, ge: float | None = None
) -> float | None:
    """Coerce numeric strings for float parameters (prices). See _coerce_int."""
    if value is None or isinstance(value, (int, float)):
        result = float(value) if value is not None else None
    elif isinstance(value, str) and value.strip():
        try:
            result = float(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field} must be a number, got {value!r}") from exc
    else:
        raise ValueError(f"{field} must be a number, got {value!r}")
    if ge is not None and result is not None and result < ge:
        raise ValueError(f"{field} must be >= {ge}, got {result}")
    return result


def _resolve_limit(limit: str | int | None, max_results: str | int | None) -> int:
    """Accept either name for the result-cap knob, on `search_amazon`.

    Across the six sibling MCP servers behind LibreChat this knob has two
    names — ``max_results`` in geizhals-mcp and baumarkt-mcp, ``limit`` in
    aliexpress-mcp, ebay-mcp and amazon-mcp — and the model sees all of them
    in one conversation, so it reaches for whichever name it used on a
    sibling server a moment ago. ``limit`` stays canonical here; the alias is
    declared in the schema rather than silently swallowed, because a quietly
    ignored unknown key would hand back the default count while the model
    believed it had asked for more. Modelled on kleinanzeigen-mcp's
    ``_resolve_page_count``.
    """
    if limit is not None and max_results is not None:
        resolved = _coerce_int(limit, "limit", ge=1)
        alias = _coerce_int(max_results, "max_results", ge=1)
        if resolved != alias:
            raise ValueError(
                "limit and max_results are two names for the same parameter "
                f"but were given different values ({resolved} and {alias}); "
                "pass limit only"
            )
    elif max_results is not None:
        resolved = _coerce_int(max_results, "max_results", ge=1)
    else:
        resolved = _coerce_int(limit, "limit", ge=1)
    return min(resolved or 10, 50)


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
    version="0.2.0",  # x-release-please-version
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
        str | int | None, Field(description="Maximum products to return")
    ] = None,
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
        str | float | None,
        Field(description="Minimum price in EUR"),
    ] = None,
    max_price: Annotated[
        str | float | None,
        Field(description="Maximum price in EUR"),
    ] = None,
    page: Annotated[
        str | int, Field(description="Result page number (~50 products per page)")
    ] = 1,
    max_results: Annotated[
        str | int | None,
        Field(description="Deprecated alias for `limit`; prefer `limit`"),
    ] = None,
) -> dict[str, Any]:
    """Search amazon.de for products by keyword, with optional price and sort.

    Returns product summaries — ASIN, title, price (EUR), rating, review count,
    Prime flag (when Amazon renders one), thumbnail image and canonical URL.
    Pass a product's `asin` to `get_amazon_product` for the full detail record.
    """
    limit = _resolve_limit(limit, max_results)
    page = _coerce_int(page, "page", ge=1) or 1
    min_price = _coerce_float(min_price, "min_price", ge=0)
    max_price = _coerce_float(max_price, "max_price", ge=0)
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
