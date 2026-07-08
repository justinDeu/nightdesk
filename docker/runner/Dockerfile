# nightdesk-runner image — the in-pod entrypoint for the k8s execution target.
#
# Slim: git + node (for the claude CLI) + the nightdesk wheel. No SQLite, no
# FastAPI — the pod is DB-less and talks to the API only over its run token.
# The pod is already the isolation boundary, so there is no bwrap here; the
# claude binary is symlinked to the path the SDK runner expects.
FROM python:3.12-slim

# git for clone; nodejs for the Claude Code CLI; ca-certificates for HTTPS clone.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Pin the Claude Code CLI. The SDK runner launches it via an explicit cli_path
# (worker.sandbox.SANDBOX_CLAUDE_BIN); symlink the installed binary there so the
# in-pod path matches without bwrap mounts.
ARG CLAUDE_VERSION=latest
# /sandbox-bin/claude matches worker.sandbox.SANDBOX_CLAUDE_BIN, the cli_path
# the SDK runner passes to the Claude Agent SDK.
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_VERSION} \
    && mkdir -p /sandbox-bin \
    && ln -sf "$(command -v claude)" /sandbox-bin/claude

# Non-root runtime user with a writable HOME + workspace.
RUN useradd --create-home --uid 10001 nightdesk \
    && mkdir -p /workspace /nightdesk/home \
    && chown -R nightdesk:nightdesk /workspace /nightdesk

# Install nightdesk with the k8s extra so `kubernetes` is present if the runner
# ever needs it; the runner itself is DB/FastAPI-free at import time.
COPY . /src
RUN pip install --no-cache-dir /src

USER nightdesk
ENV NIGHTDESK_POD_WORKDIR=/workspace \
    NIGHTDESK_POD_HOME=/nightdesk/home \
    HOME=/nightdesk/home

ENTRYPOINT ["nightdesk-runner"]
