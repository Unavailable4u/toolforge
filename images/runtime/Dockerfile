# toolforge-runtime:py312
# Pinned Python 3.12 runtime image with software libraries for tool use.
#
# Libraries baked into the image (pinned wheels only):
# - duckdb 1.0.0: in-process SQL engine, read-only queries
# - pyyaml 6.0.1: YAML parsing/serialization
# - python-dateutil 2.9.0.post0: datetime parsing extensions
# - beautifulsoup4 4.12.3: HTML/XML parsing (no network)
# - lxml 5.3.0: fast XML/HTML parser
# - pillow 10.4.0: image processing
# - numpy 1.26.4: numerical arrays
# - gymnasium 0.29.1: reinforcement learning environments (toy-text only)
# - chess 1.10.0: chess board representation and move generation
# - jsonschema 4.22.0: JSON schema validation
# - tabulate 0.9.0: table formatting
# - requests 2.31.0: HTTP client (for http-api recipe)
#
# The image also includes sqlite3 (from Python stdlib) and the standard library.

FROM python:3.12-slim

WORKDIR /workspace

# Install system dependencies needed for some wheels
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libxml2-dev \
        libxslt1-dev \
        libjpeg-dev \
        zlib1g-dev \
        libfreetype6-dev \
        libpng-dev \
        gcc \
        g++ \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for sandbox execution
RUN useradd -m -s /bin/bash toolforge && \
    chown -R toolforge:toolforge /workspace

# Install pinned Python packages
COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt && \
    rm requirements-runtime.txt

# Create input directory (tools receive files under /workspace/inputs/)
RUN mkdir -p /workspace/inputs && chown toolforge:toolforge /workspace/inputs

# Set environment variables
ENV PYTHONPATH=/workspace
ENV PYTHONUNBUFFERED=1

# Switch to non-root user
USER toolforge

# Default command - entrypoint will be overridden by MiniLocker
CMD ["python", "-c", "print('toolforge-runtime:py312 ready')"]