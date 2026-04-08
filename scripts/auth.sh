#!/bin/bash
# One-time auth: logs into Claude Code on the HOST machine.
#
# The docker-compose.yml bind-mounts ~/.claude into the container,
# so tokens authenticated here are automatically available inside Docker.
#
# Usage: ./scripts/auth.sh
#   Run this on the machine where docker compose will run (locally or via SSH).

set -e

if ! command -v claude &>/dev/null; then
    echo "Claude Code CLI not found. Installing..."
    npm install -g @anthropic-ai/claude-code@latest
fi

echo ""
echo "Starting Claude Code auth..."
echo "A URL will appear — open it in your browser to authorize."
echo ""

claude auth login

echo ""
echo "Auth complete. Tokens are stored in ~/.claude/"
echo "The container will pick them up via bind mount — no further setup needed."
