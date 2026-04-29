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

cat <<'EOF'

Claude Code auth must be done interactively in the REPL — `claude auth login`
non-interactively swallows stdin and blocks paste of the verification code.

Do this instead:

  1. Run:  claude
  2. In the REPL, type:  /login
  3. A URL will be shown. Open it in your browser, authorize.
  4. Copy the verification code from the browser back into the REPL.
  5. Exit the REPL with Ctrl+D once "Logged in" is shown.

Tokens are written to ~/.claude/ and bind-mounted into the auto-agent
container — no rebuild or restart needed.

EOF
