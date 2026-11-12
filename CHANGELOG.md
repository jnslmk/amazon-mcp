# Changelog

## Unreleased

### Bug Fixes

* **scraper:** parse German and English price separators without inflating cents
* **scraper:** require a product title and read only product-specific price slots, never recommendation prices
* **scraper:** expose incomplete product pages as typed MCP tool errors
* **scraper:** replace Playwright/Chromium with one shared curl_cffi Chrome-profile async HTTP session, preserving German locale, pacing, typed failures and bounded 429 backoff
* **scraper:** bound the complete HTTP request and classify status, headers and body from the same response before parsing
* **container:** remove browser downloads/system dependencies; retain nonroot operation and report local HTTP-session readiness
* **config:** remove `AMZ_MAX_CONTEXTS`; tune `AMZ_MAX_CONCURRENT` directly and replace `AMZ_NAV_TIMEOUT_MS` with `AMZ_REQUEST_TIMEOUT_MS`

Run the parser and real loopback HTTP-session regression with `python -m unittest discover -s tests`.

## [0.1.2](https://github.com/jnslmk/amazon-mcp/compare/v0.1.1...v0.1.2) (2026-09-20)


### Bug Fixes

* **scraper:** close amazon retry and context gaps ([877ff53](https://github.com/jnslmk/amazon-mcp/commit/877ff532ac2e2a52cc2775aa09290573c11edf14))
* **scraper:** harden amazon fetch and block handling ([767258c](https://github.com/jnslmk/amazon-mcp/commit/767258cdae6ab301a7f44c478a7f06d39e76c827))


### Documentation

* **scraper:** document page mismatch backoff ([f6b1b7b](https://github.com/jnslmk/amazon-mcp/commit/f6b1b7bd7d2a95a5e7ebf60f925c2ecd51d78675))
