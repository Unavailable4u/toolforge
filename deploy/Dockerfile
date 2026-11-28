# The ToolForge control plane: the MCP server and the dashboard run from this one image (docker-compose.yml).
# It is also the image MiniLocker's egress proxy runs from (TOOLFORGE_EGRESS_PROXY_IMAGE): the proxy is a module of the minilocker
# package, which is on PYTHONPATH here (and TOOLFORGE_MINILOCKER_PATH: an installed toolforge cannot find a submodule next to its source), so no file from this container is ever bind-mounted into another one.
# Build context: the repository root, with the MiniLocker submodule checked out (git submodule update --init).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp TOOLFORGE_HOME=/data TOOLFORGE_MINILOCKER_PATH=/app/third_party/minilocker/backend \
    PYTHONPATH=/app/third_party/minilocker/backend

RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data /runs /app && chown app:app /data /runs
WORKDIR /app

# MiniLocker has no pyproject.toml: its backend/ directory is put on PYTHONPATH. Only the `docker` package is needed from its requirements, and the
# `sandbox` extra below installs it; MiniLocker's web and test requirements are deliberately not installed (docs/guide/03-minilocker-integration.md).
COPY third_party/minilocker/backend/minilocker ./third_party/minilocker/backend/minilocker

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[sandbox]" \
 && chmod -R a+rX /app/third_party   # the egress proxy container runs this code as an unprivileged user

USER 10001:10001
EXPOSE 8765 8787
CMD ["toolforge", "serve", "--transport", "http"]
