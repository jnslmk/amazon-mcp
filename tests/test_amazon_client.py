"""Run with: python -m unittest discover -s tests."""

import asyncio
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from amazon_mcp.amazon_client import (
    AmazonHttpClient,
    BotChallengeError,
    PageMismatchError,
    RateLimitError,
    parse_product,
    parse_search_results,
)


class PriceParsingTests(unittest.TestCase):
    def test_search_prices_preserve_decimal_and_grouping_separators(self):
        for text, expected in (
            ("16,99 €", 16.99),
            ("€16.99", 16.99),
            ("1.899,99 €", 1899.99),
            ("€1,899.99", 1899.99),
            ("1\u00a0899,99 €", 1899.99),
            ("€1\u202f899.99", 1899.99),
            ("1.899 €", 1899.0),
            ("€1,899", 1899.0),
            ("19 €", 19.0),
            ("€0.00", 0.0),
        ):
            with self.subTest(text=text):
                html = f"""
                    <div data-component-type="s-search-result" data-asin="B0DLTX9HSS">
                        <h2><span>USB cable</span></h2>
                        <span class="a-price"><span class="a-offscreen">{text}</span></span>
                    </div>
                """
                product = parse_search_results(html, limit=1)[0]
                self.assertEqual(product["price"], expected)
                self.assertEqual(product["currency"], "EUR")


class ProductParsingTests(unittest.TestCase):
    def test_detail_prices_use_product_price_slots(self):
        for slot in (
            "corePrice_feature_div",
            "corePriceDisplay_desktop_feature_div",
            "priceblock_ourprice",
        ):
            for text in ("€16.99", "16,99 €"):
                with self.subTest(slot=slot, text=text):
                    price = (
                        text
                        if slot == "priceblock_ourprice"
                        else f'<span class="a-offscreen">{text}</span>'
                    )
                    html = f'''
                        <div class="a-price"><span class="a-offscreen">€9.12</span></div>
                        <div id="dp">
                            <h1 id="productTitle">USB cable</h1>
                            <div id="{slot}">{price}</div>
                        </div>
                    '''
                    product = parse_product(html, "https://www.amazon.de/dp/B0DLTX9HSS")
                    self.assertEqual(product["price"], 16.99)
                    self.assertEqual(product["title"], "USB cable")

    def test_missing_product_price_does_not_use_recommendations(self):
        html = """
            <div id="dp">
                <h1 id="productTitle">USB cable</h1>
                <div class="recommendations">
                    <span class="a-price"><span class="a-offscreen">€9.12</span></span>
                </div>
            </div>
        """
        product = parse_product(html, "https://www.amazon.de/dp/B0DLTX9HSS")
        self.assertIsNone(product["price"])
        self.assertIsNone(product["currency"])

    def test_incomplete_detail_page_is_rejected(self):
        for title in ("", '<h1 id="productTitle"> </h1>'):
            with self.subTest(title=title):
                html = f"""
                    <div id="dp">
                        {title}
                        <span class="a-price"><span class="a-offscreen">€9.12</span></span>
                    </div>
                """
                with self.assertRaises(PageMismatchError):
                    parse_product(html, "https://www.amazon.de/dp/B0DLTX9HSS")


class HttpSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_errors_never_become_products_and_429_keeps_session_backoff(self):
        card = '<div data-component-type="s-search-result" data-asin="B0DLTX9HSS"></div>'
        challenge = "<script>bm-verify</script>" + card
        detail = """
            <h1 id="productTitle">USB cable</h1>
            <div id="corePrice_feature_div"><span class="a-offscreen">16,99 €</span></div>
            <div id="availability">Auf Lager</div>
        """
        responses = [
            (429, challenge),
            (200, challenge),
            (200, '<h1 id="productTitle"> </h1>'),
            (200, "Keine Ergebnisse"),
            (200, detail),
        ]
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.headers)
                status, body = responses.pop(0)
                payload = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                if status == 429:
                    self.send_header("Retry-After", "3600")
                    self.send_header("Set-Cookie", "session=retained; Path=/")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        client = AmazonHttpClient(max_concurrent=1)
        try:
            with patch.multiple(
                "amazon_mcp.amazon_client",
                PACING_MIN_S=0,
                PACING_MAX_S=0,
                BACKOFF_BASE_S=0,
                RETRY_AFTER_MAX_S=0.04,
            ):
                self.assertFalse(client.ready)
                await client.start()
                self.assertTrue(client.ready)
                with self.assertRaises(RateLimitError) as caught:
                    await client.fetch_html(base + "/s?k=cable")
                self.assertEqual(caught.exception.retry_after, 3600)
                started = time.monotonic()
                with self.assertRaises(BotChallengeError):
                    await client.fetch_html(base + "/s?k=cable")
                self.assertGreaterEqual(time.monotonic() - started, 0.03)
                with self.assertRaises(PageMismatchError):
                    await client.fetch_html(base + "/dp/B0DLTX9HSS")
                empty = await client.fetch_html(base + "/s?k=none")
                self.assertEqual(parse_search_results(empty, limit=1), [])
                html = await client.fetch_html(base + "/dp/B0DLTX9HSS")
                product = parse_product(html, base + "/dp/B0DLTX9HSS")
                self.assertEqual(
                    (product["asin"], product["price"], product["currency"], product["availability"]),
                    ("B0DLTX9HSS", 16.99, "EUR", "Auf Lager"),
                )
                self.assertEqual(len(requests), 5)
                self.assertTrue(all("session=retained" in r.get("Cookie", "") for r in requests[1:]))
                self.assertTrue(all(r.get("Accept-Language") == "de-DE,de;q=0.9,en;q=0.8" for r in requests))
        finally:
            await client.close()
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()
        self.assertFalse(client.ready)


if __name__ == "__main__":
    unittest.main()
