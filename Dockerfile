FROM node:20-slim

# Install Python, git, gh CLI, and dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    git \
    gh \
    jq \
    curl \
    ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code@latest

# node user already exists in the node:20 base image
# Create workspace and claude config dirs owned by node
RUN mkdir -p /workspace /workspaces /home/node/.claude && \
    chown -R node:node /workspace /workspaces /home/node/.claude

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# Install team-memory (private repo). Pass GITHUB_TOKEN as a build arg:
#   docker build --build-arg GITHUB_TOKEN=$(gh auth token) ...
# The deploy.sh script wires this up automatically.
ARG GITHUB_TOKEN
RUN test -n "$GITHUB_TOKEN" || (echo "ERROR: GITHUB_TOKEN build-arg required to install team-memory"; exit 1) && \
    pip install --no-cache-dir --break-system-packages \
      "git+https://${GITHUB_TOKEN}@github.com/ergodic-ai/team-memory.git@main"

COPY . .
RUN chown -R node:node /app

USER node
ENV HOME=/home/node

EXPOSE 2020

CMD ["python3", "run.py"]
