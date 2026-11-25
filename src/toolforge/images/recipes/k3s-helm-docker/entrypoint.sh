#!/bin/bash
# Entrypoint script for k3s-helm-docker runtime image

# Start Docker daemon in the background if not already running
if ! pgrep -x dockerd > /dev/null; then
    echo "Starting Docker daemon..."
    sudo dockerd &
    sleep 5
fi

# Set up k3s configuration if needed
if [ ! -f "$KUBECONFIG" ]; then
    echo "Setting up k3s configuration..."
    mkdir -p "$(dirname "$KUBECONFIG")"
    # Create a minimal kubeconfig for local development
    cat > "$KUBECONFIG" <<EOF
apiVersion: v1
clusters:
- cluster:
    server: http://localhost:8080
  name: local
contexts:
- context:
    cluster: local
    namespace: default
    user: toolforge
  name: local-context
current-context: local-context
kind: Config
preferences: {}
users:
- name: toolforge
EOF
fi

# Install bash completion if available
for cmd in kubectl helm docker; do
    if command -v $cmd > /dev/null && [ -f "/usr/share/bash-completion/completions/$cmd" ]; then
        source "/usr/share/bash-completion/completions/$cmd"
    fi
done

# Start bash or execute the command
if [ $# -eq 0 ]; then
    exec bash
else
    exec "$@"
fi