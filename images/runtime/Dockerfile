# The image tool sandboxes run in (docs/runtime-image.md). Build it with `toolforge images build`.
# It stays on this machine: it is tagged toolforge-runtime:py312 and never pushed.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="toolforge-runtime" \
      org.opencontainers.image.description="Python 3.12 with a fixed set of pinned libraries for ToolForge tools"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Wheels only, no dependency resolution, exact versions: what is installed is exactly the list in requirements.txt.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Sandboxes start containers from this image as an unprivileged user with a read-only root filesystem; nothing here needs to be
# writable at run time.
