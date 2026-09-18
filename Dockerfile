FROM python:3.12-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    BOLETIN_MCP_HOME=/tmp/boletin-oficial-mcp \
    MCP_TRANSPORT=http

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip install -e . \
    && playwright install --with-deps chromium

EXPOSE 8080

CMD ["python", "-m", "boletin_oficial_mcp.server"]
