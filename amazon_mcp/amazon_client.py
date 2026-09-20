"""Amazon.de scraper. Standalone Python, no MCP dependency.

Amazon has no keyless product-search API for this use case (PA-API needs
Associate sales eligibility and is being deprecated), so this is a scraper.
Amazon fronts its pages with Akamai Bot Manager, which serves a ``bm-verify``
JavaScript challenge to plain HTTP clients — even from residential IPs — so a
real headless browser is required. This module owns one shared Chromium
instance (via Playwright) and drives amazon.de directly.

Kept framework-free so it can also be driven from the CLI
(``python -m amazon_mcp.amazon_client "<query>"``) for debugging inside the
image, mirroring the sibling ebay-mcp / kleinanzeigen-mcp clients.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from typing import Any, Optional
from urllib.parse import quote_plus, urlencode

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

log = logging.getLogger("amazon-mcp")

BASE_URL = "https://www.amazon.de"

# One long-lived Chromium context (a browser profile that keeps consent and
# session cookies across fetches) is created at start; pages are created per
# fetch and everything is bounded by a semaphore, with navigation starts
# additionally spaced by the pacer below. A chat agent scrapes one request at
# a time, so the defaults are deliberately lean and the deployment can raise
# them.
MAX_CONTEXTS = int(os.getenv("AMZ_MAX_CONTEXTS", "4"))
MAX_CONCURRENT = int(os.getenv("AMZ_MAX_CONCURRENT", "2"))
NAV_TIMEOUT_MS = int(os.getenv("AMZ_NAV_TIMEOUT_MS", "45000"))

# Per-origin pacing: navigation starts are spaced by a jittered delay, and a
# bot challenge or HTTP 429 stretches the spacing with bounded exponential
# backoff (honouring Retry-After). Stdlib only: random + time on the loop.
PACING_MIN_S = float(os.getenv("AMZ_PACING_MIN_S", "1.5"))
PACING_MAX_S = float(os.getenv("AMZ_PACING_MAX_S", "4.0"))
BACKOFF_BASE_S = float(os.getenv("AMZ_BACKOFF_BASE_S", "2.0"))
BACKOFF_MAX_S = float(os.getenv("AMZ_BACKOFF_MAX_S", "120.0"))

# Let the real Chromium identity stand: the version in the UA then always
# matches the engine actually running, instead of a pinned string that drifts
# stale (the old default claimed Chrome/125 long after Playwright moved on).
# Chromium also sends its own current Accept header on navigations, so none is
# pinned here. Operators can still override the UA via AMZ_USER_AGENT if
# amazon.de tightens detection. Accept-Language stays pinned because amazon.de
# localises on it.
_USER_AGENT = os.getenv("AMZ_USER_AGENT") or None
_EXTRA_HEADERS = {
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

# Amazon's sort keys (the ``s=`` query param).
_SORT_MAP = {
    "price_asc": "price-asc-rank",
    "price_desc": "price-desc-rank",
    "review": "review-rank",
    "newest": "date-desc-rank",
    "featured": "relevanceblender",
    "relevance": "relevanceblender",
}


class BotChallengeError(RuntimeError):
    """Raised when amazon.de serves a robot/captcha page instead of content.

    The whole point of the E2E test: a datacenter IP (e.g. the VPS) is far more
    likely to see this than a residential one.
    """


class RateLimitError(RuntimeError):
    """Raised when amazon.de answers a navigation with HTTP 429.

    ``retry_after`` carries the server-supplied Retry-After in seconds when
    the response exposes one, so the pacer can honour it; ``None`` otherwise.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PageMismatchError(RuntimeError):
    """Raised when a fetched page matches no known amazon.de layout.

    Neither a recognised block page nor a positively recognised results /
    product / empty-results page — the markup drifted or a new challenge
    shipped. Raised instead of silently parsing the page into zero results.
    """


class AmazonBrowser:
    """Owns one shared Chromium instance for the process lifetime."""

    def __init__(
        self, max_contexts: int = MAX_CONTEXTS, max_concurrent: int = MAX_CONCURRENT
    ) -> None:
        self._max_contexts = max_contexts
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        # Bound concurrent navigations with one gate; the long-lived context
        # keeps consent/session cookies across fetches.
        self._sem = asyncio.Semaphore(min(max_concurrent, max_contexts))
        # Shared pacer state: the next permitted navigation start and the
        # current backoff, keyed to this process's single scraped origin.
        self._pace_lock = asyncio.Lock()
        self._next_ok = 0.0
        self._backoff_until = 0.0
        self._backoff_level = 0

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        # --no-sandbox: the container runs as an unprivileged uid with all caps
        # dropped, where Chromium's setuid sandbox cannot initialise.
        # dev-shm is deliberately left enabled — the compose file gives the
        # container a 512 MB /dev/shm so Chromium's renderer heap does not crash
        # on Docker's 64 MB default.
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
            ],
        )
        context_kwargs: dict[str, Any] = {
            "locale": "de-DE",
            "extra_http_headers": _EXTRA_HEADERS,
            "viewport": {"width": 1366, "height": 900},
        }
        if _USER_AGENT:
            context_kwargs["user_agent"] = _USER_AGENT
        self._context = await self._browser.new_context(**context_kwargs)
        log.info(
            "Chromium ready (max_contexts=%s, max_concurrent=%s)",
            self._max_contexts,
            self._sem._value,  # noqa: SLF001 - informational log only
        )

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    @property
    def ready(self) -> bool:
        return self._context is not None

    async def fetch_html(self, url: str) -> str:
        """Load ``url`` in the shared long-lived context and return the HTML.

        Navigation starts are paced across all callers; a bot challenge or a
        429 stretches the spacing with bounded exponential backoff. Raises
        :class:`BotChallengeError` / :class:`RateLimitError` /
        :class:`PageMismatchError` instead of returning an unusable page.
        """
        context = self._context
        if context is None:
            raise RuntimeError("Browser is not running")

        async with self._sem:
            await self._pace()
            page = await context.new_page()
            try:
                html = await self._load(page, url)
            except (BotChallengeError, RateLimitError) as exc:
                self._back_off(getattr(exc, "retry_after", None))
                raise
            else:
                self._backoff_level = 0
                return html
            finally:
                await page.close()

    async def _pace(self) -> None:
        """Space navigation starts across all callers (per-origin pacing).

        The pacing lock is held while sleeping so concurrent callers queue
        behind the claimed slot instead of all waking at once; the navigation
        itself runs after the lock is released.
        """
        async with self._pace_lock:
            delay = max(
                self._next_ok - time.monotonic(),
                self._backoff_until - time.monotonic(),
                0.0,
            )
            if delay > 0:
                log.debug("pacing: waiting %.1fs before next navigation", delay)
                await asyncio.sleep(delay)
            self._next_ok = time.monotonic() + random.uniform(
                PACING_MIN_S, PACING_MAX_S
            )

    def _back_off(self, retry_after: Optional[float]) -> None:
        """Stretch the pacer after a block or 429.

        Bounded exponential backoff over consecutive failures, never shorter
        than a server-supplied Retry-After.
        """
        self._backoff_level += 1
        delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2**self._backoff_level)
        if retry_after is not None:
            delay = max(delay, retry_after)
        self._backoff_until = max(self._backoff_until, time.monotonic() + delay)
        log.warning(
            "amazon.de fetch failed; backing off %.0fs (level=%d, retry_after=%s)",
            delay,
            self._backoff_level,
            retry_after,
        )

    async def _load(self, page: Page, url: str) -> str:
        page.set_default_timeout(NAV_TIMEOUT_MS)
        response = await page.goto(
            url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
        )

        if response is not None and response.status == 429:
            # A rate-limited body is never content: surface it (with any
            # Retry-After) so the pacer can back off before the next try.
            raise RateLimitError(
                "amazon.de rate-limited the request (HTTP 429)",
                _parse_retry_after(response.headers),
            )

        # Best-effort: dismiss the cookie-consent interstitial if it appears.
        for sel in ("#sp-cc-accept", 'input[name="accept"]'):
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click(timeout=2000)
                    break
            except Exception:  # noqa: BLE001 - consent is optional
                pass

        is_search = "/s?" in url or "/s/" in url
        selector = (
            '[data-component-type="s-search-result"]'
            if is_search
            else "#productTitle, #dp"
        )
        try:
            await page.wait_for_selector(selector, timeout=15000)
        except Exception:  # noqa: BLE001 - classified below, never swallowed
            pass
        else:
            # Positively recognised page: safe to hand to the parsers.
            return await page.content()

        # The selector never appeared. Classify what actually arrived instead
        # of parsing it blindly — a silent zero-result parse hides both bot
        # challenges and markup drift.
        html = await page.content()
        self._raise_if_blocked(html)
        if is_search and self._looks_like_empty_search(html):
            return html
        raise PageMismatchError(
            f"amazon.de page at {url} never showed {selector!r} and matches no "
            "known layout (results, product page, empty results or block "
            "page) — the markup likely changed; refusing to parse it as zero "
            "results"
        )

    @staticmethod
    def _looks_like_empty_search(html: str) -> bool:
        """Recognise amazon.de's legitimate zero-results page.

        A search with no matches renders no result cards at all, so a missing
        selector alone cannot tell "no products" from markup drift.
        """
        low = html.lower()
        return (
            "keine ergebnisse" in low
            or "no results for" in low
            or 'data-component-type="s-no-result"' in low
        )

    @staticmethod
    def _raise_if_blocked(html: str) -> None:
        low = html.lower()
        markers = (
            "api-services-support@amazon",
            "geben sie die zeichen ein",  # "enter the characters" (captcha)
            "enter the characters you see below",
            "zur bestätigung, dass sie kein roboter sind",
        )
        has_content = "data-asin" in low or 'id="producttitle"' in low
        if not has_content and any(m in low for m in markers):
            raise BotChallengeError(
                "amazon.de returned a robot-check / captcha page instead of "
                "results (likely IP-based bot detection)"
            )
        if not has_content and "bm-verify" in low:
            raise BotChallengeError(
                "amazon.de returned an unsolved bm-verify JS challenge"
            )


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #


def _parse_retry_after(headers: Any) -> Optional[float]:
    """Extract a Retry-After delay in seconds; None when absent or a date."""
    raw = (headers or {}).get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form — rare; exponential backoff covers it


def _parse_price(text: Optional[str]) -> Optional[float]:
    """Parse a German-formatted price string like '1.898,99 €' -> 1898.99."""
    if not text:
        return None
    m = re.search(r"(\d[\d.\s]*,\d{2}|\d[\d.\s]*)", text)
    if not m:
        return None
    raw = m.group(1).replace(" ", "")
    # German grouping: '.' thousands, ',' decimal.
    raw = raw.replace(".", "").replace(",", ".")
    try:
        return round(float(raw), 2)
    except ValueError:
        return None


def _parse_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _abs_url(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return BASE_URL + href
    return href


def _canonical_product_url(asin: Optional[str], fallback: Optional[str]) -> Optional[str]:
    if asin:
        return f"{BASE_URL}/dp/{asin}"
    return _abs_url(fallback)


# --------------------------------------------------------------------------- #
# URL building
# --------------------------------------------------------------------------- #


def build_search_url(
    query: str,
    sort: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    page: int = 1,
) -> str:
    params: dict[str, Any] = {"k": query}
    if sort:
        key = _SORT_MAP.get(sort.lower().replace("-", "_"))
        if key:
            params["s"] = key
    if page and page > 1:
        params["page"] = page
    # Price filter: amazon's p_36 refinement works in the smallest currency
    # unit (cents). Range form: p_36:<low>-<high>, either side optional.
    if min_price is not None or max_price is not None:
        low = "" if min_price is None else str(int(round(min_price * 100)))
        high = "" if max_price is None else str(int(round(max_price * 100)))
        params["rh"] = f"p_36:{low}-{high}"
    return f"{BASE_URL}/s?" + urlencode(params, quote_via=quote_plus)


# --------------------------------------------------------------------------- #
# parsing: search results
# --------------------------------------------------------------------------- #


def parse_search_results(html: str, limit: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []

    for card in soup.select('[data-component-type="s-search-result"]'):
        asin = card.get("data-asin") or None
        if not asin:
            continue

        # Title: the h2 heading over the result card.
        title_el = card.select_one("h2 span") or card.select_one("h2 a span")
        title = title_el.get_text(strip=True) if title_el else None
        if not title:
            continue

        # Price: the offscreen full price string, else assemble from parts.
        price_el = card.select_one(".a-price .a-offscreen")
        price_text = price_el.get_text(strip=True) if price_el else None
        if not price_text:
            whole = card.select_one(".a-price-whole")
            if whole:
                price_text = whole.get_text(strip=True)
        price = _parse_price(price_text)

        # Rating, e.g. "4,6 von 5 Sternen" — the star-icon alt text.
        rating = None
        review_count = None
        for aria_el in card.select("[aria-label]"):
            label = aria_el.get("aria-label") or ""
            if rating is None:
                rm = re.search(r"(\d[.,]\d)\s+von\s+5|(\d[.,]\d)\s+out of 5", label)
                if rm:
                    rating = float((rm.group(1) or rm.group(2)).replace(",", "."))
            if review_count is None:
                cm = re.match(r"^([\d.\s]+)\s+(?:Bewertung|rating|review)", label)
                if cm:
                    review_count = _parse_int(cm.group(1))
        if rating is None:
            alt = card.select_one(".a-icon-alt")
            if alt:
                rm = re.search(r"(\d[.,]\d)", alt.get_text())
                if rm:
                    rating = float(rm.group(1).replace(",", "."))

        img_el = card.select_one("img.s-image")
        image = img_el.get("src") if img_el else None

        link_el = card.select_one("h2 a") or card.select_one("a.a-link-normal.s-no-outline")
        url = _canonical_product_url(asin, link_el.get("href") if link_el else None)

        # Prime badge, when Amazon renders one (it often shows a delivery line
        # instead). Best-effort: an explicit Prime icon or aria-label.
        prime = bool(
            card.select_one(
                "i.a-icon-prime, [aria-label*='Prime'], [class*='prime'], "
                "[aria-label*='PRIME']"
            )
        )

        items.append(
            {
                "asin": asin,
                "title": title,
                "price": price,
                "currency": "EUR" if price is not None else None,
                "rating": rating,
                "review_count": review_count,
                "prime": prime,
                "image": image,
                "url": url,
            }
        )
        if len(items) >= limit:
            break

    return items


# --------------------------------------------------------------------------- #
# parsing: product detail
# --------------------------------------------------------------------------- #


def _extract_asin(product: str) -> Optional[str]:
    """Pull an ASIN out of a bare id or an amazon.de URL."""
    product = product.strip()
    if re.fullmatch(r"[A-Z0-9]{10}", product):
        return product
    m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", product)
    if m:
        return m.group(1)
    m = re.search(r"[/?&](?:asin|ASIN)=([A-Z0-9]{10})", product)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Z0-9]{10})\b", product)
    return m.group(1) if m else None


def parse_product(html: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    def text(sel: str) -> Optional[str]:
        el = soup.select_one(sel)
        return el.get_text(strip=True) if el else None

    title = text("#productTitle")

    price_text = (
        text("#corePrice_feature_div .a-offscreen")
        or text("#corePriceDisplay_desktop_feature_div .a-offscreen")
        or text("#priceblock_ourprice")
        or text(".a-price .a-offscreen")
    )
    price = _parse_price(price_text)

    availability = text("#availability span") or text("#availability")

    rating = None
    rating_el = soup.select_one("#acrPopover .a-icon-alt") or soup.select_one(
        "#averageCustomerReviews .a-icon-alt"
    )
    if rating_el:
        rm = re.search(r"(\d[.,]\d)", rating_el.get_text())
        if rm:
            rating = float(rm.group(1).replace(",", "."))

    review_count = _parse_int(text("#acrCustomerReviewText"))

    # Feature bullets.
    features = [
        li.get_text(strip=True)
        for li in soup.select("#feature-bullets ul li span.a-list-item")
        if li.get_text(strip=True)
    ]

    # Images: the main image plus any hi-res variants encoded in the JSON blob
    # on the landing image element.
    images: list[str] = []
    main_img = soup.select_one("#landingImage, #imgBlkFront, img#main-image")
    if main_img:
        hires = main_img.get("data-old-hires")
        src = main_img.get("src")
        dyn = main_img.get("data-a-dynamic-image")
        if dyn:
            for u in re.findall(r'"(https?://[^"]+)"', dyn):
                if u not in images:
                    images.append(u)
        for candidate in (hires, src):
            if candidate and candidate not in images:
                images.append(candidate)

    seller = (
        text("#sellerProfileTriggerId")
        or text("#merchant-info a")
        or text("#merchant-info")
        or text("#bylineInfo")
    )

    brand = text("#bylineInfo")

    asin = _extract_asin(url)

    return {
        "asin": asin,
        "title": title,
        "price": price,
        "currency": "EUR" if price is not None else None,
        "availability": availability,
        "rating": rating,
        "review_count": review_count,
        "brand": brand,
        "features": features,
        "images": images,
        "seller": seller,
        "url": _canonical_product_url(asin, url),
    }


# --------------------------------------------------------------------------- #
# CLI (debugging inside the image)
# --------------------------------------------------------------------------- #


async def _cli() -> None:
    import sys

    browser = AmazonBrowser()
    await browser.start()
    try:
        if len(sys.argv) > 2 and sys.argv[1] == "product":
            html = await browser.fetch_html(
                _canonical_product_url(_extract_asin(sys.argv[2]), sys.argv[2])
            )
            data = parse_product(html, sys.argv[2])
            print(f"Title: {(data.get('title') or '')[:120]}")
            print(f"Price: {data.get('price')} {data.get('currency')}")
            print(f"Rating: {data.get('rating')} ({data.get('review_count')} reviews)")
            print(f"URL: {data.get('url')}")
        else:
            q = sys.argv[1] if len(sys.argv) > 1 else "usb c kabel"
            html = await browser.fetch_html(build_search_url(q))
            items = parse_search_results(html, limit=5)
            print(f"[amazon.de] query={q!r} returned {len(items)}\n")
            for it in items:
                print(f"- {it['asin']}  {(it['title'] or '')[:70]}")
                print(f"  {it['price']} {it['currency']}  rating={it['rating']}")
                print(f"  {it['url']}\n")
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(_cli())
