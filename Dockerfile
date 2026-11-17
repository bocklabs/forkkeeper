# Debian slim variant; digest updates are configured in renovate.json.
FROM ghcr.io/astral-sh/uv@sha256:9b7790faef7a3f2b9ba8f2f7c48b959c1359fe2bd51c44804b6ca61766fbacdf

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home forkkeeper

WORKDIR /app
COPY .python-version pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
COPY container/entrypoint.sh ./container/entrypoint.sh
RUN uv python install \
    && uv sync --locked --no-dev --no-editable \
    && chmod 755 /app/container/entrypoint.sh

USER 10001:10001
ENTRYPOINT ["/app/container/entrypoint.sh"]
CMD ["--mode", "direct"]
