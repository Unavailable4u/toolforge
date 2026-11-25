# Python development runtime image
# Provides Python 3.12, pip, and common development tools

FROM python:3.12-slim

# Install common development tools and dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    wget \
    jq \
    nano \
    vim \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python tools
RUN pip install --no-cache-dir \
    pipx \
    poetry \
    setuptools \
    wheel \
    pytest \
    pytest-cov \
    black \
    flake8 \
    isort \
    mypy \
    pylint \
    bandit \
    safety \
    pipenv \
    virtualenv

# Install common Python packages for data science and web development
RUN pip install --no-cache-dir \
    numpy \
    pandas \
    matplotlib \
    seaborn \
    scikit-learn \
    jupyter \
    requests \
    flask \
    fastapi \
    sqlalchemy \
    psycopg2-binary \
    pymongo \
    redis \
    celery \
    pytest-asyncio

# Set up working directory
WORKDIR /workspace

# Create non-root user
RUN groupadd -r toolforge && useradd -r -g toolforge -m -s /bin/bash toolforge && \
    chown -R toolforge:toolforge /workspace

USER toolforge
ENV PATH="/home/toolforge/.local/bin:$PATH"

# Set up environment
ENV PYTHONPATH=/workspace:/workspace/src
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

CMD ["python"]