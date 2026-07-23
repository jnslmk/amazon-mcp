FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

# Install Playwright and its Chromium build ahead of the source copy: a
# ~150 MB browser download plus system libs should not be redone every time
# this project's own code changes. Pin the pip version so the browser build it
# downloads matches the runtime it installs.
RUN pip install "playwright>=1.45" \
    && playwright install --with-deps chromium

# Now the package itself. Chromium is the whole cost here — the MCP layer is
# tiny — so this layer stays cheap to rebuild.
COPY pyproject.toml README.md ./
COPY amazon_mcp ./amazon_mcp
RUN pip install .

# Chromium lives in PLAYWRIGHT_BROWSERS_PATH, which root just wrote to; hand it
# to the unprivileged user the container actually runs as.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin mcp \
    && chown -R mcp:mcp /opt/playwright
USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status == 200 else 1)"

CMD ["amazon-mcp"]
