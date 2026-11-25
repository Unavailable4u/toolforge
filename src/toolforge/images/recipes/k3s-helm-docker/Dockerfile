# k3s-helm-docker runtime image
# Provides k3s (lightweight Kubernetes), Helm, Docker, kubectl, and related tools
# For running Kubernetes tools safely in ToolForge

FROM ubuntu:22.04

# Install base packages and avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    software-properties-common \
    git \
    jq \
    unzip \
    wget \
    nano \
    vim \
    && rm -rf /var/lib/apt/lists/*

# Install Docker
RUN mkdir -m 0755 -p /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
        $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && apt-get install -y --no-install-recommends \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin \
        && rm -rf /var/lib/apt/lists/*

# Install kubectl
RUN curl -fsSL https://dl.k8s.io/release/v1.28.0/bin/linux/amd64/kubectl -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl

# Install Helm
RUN curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# Install k3s
RUN curl -sfL https://get.k3s.io | INSTALL_K3S_SKIP_START=true INSTALL_K3S_SKIP_ENABLE=true sh - && \
    # Create k3s config directory
    mkdir -p /etc/rancher/k3s

# Install additional useful tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    yamllint \
    bash-completion \
    tree \
    htop \
    net-tools \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Set up working directory
WORKDIR /workspace

# Set up non-root user for safer operation
RUN groupadd -r toolforge && useradd -r -g toolforge -m -s /bin/bash toolforge && \
    # Allow toolforge user to use docker without root
    usermod -aG docker toolforge && \
    # Create workspace directory
    mkdir -p /home/toolforge/.kube && \
    chown -R toolforge:toolforge /home/toolforge

# Copy entrypoint script
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Switch to non-root user
USER toolforge
ENV HOME=/home/toolforge

# Set up environment
ENV KUBECONFIG=/home/toolforge/.kube/config
ENV PATH=/usr/local/bin:/usr/bin:/bin:/var/lib/rancher/k3s/bin:$PATH

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD which docker && which kubectl && which helm

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]