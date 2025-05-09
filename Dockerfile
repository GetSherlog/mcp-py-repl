FROM python:3.12-slim-bookworm AS base

WORKDIR /app

# Install necessary packages for adding Docker repo, Docker CLI, and Node.js
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    apt-transport-https \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    && rm -rf /var/lib/apt/lists/*

# Add Docker's official GPG key and set up the repository
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc && \
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
      $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
      tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update

# Install Docker CLI (docker-ce-cli)
RUN apt-get install -y --no-install-recommends docker-ce-cli && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Install Node.js and npm (which includes npx)
# Using NodeSource repository for a specific LTS version (e.g., 20.x)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Verify npx installation (optional, but good for debugging)
RUN npx -v

# --- UV Stage ---
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv && \
    uv pip install -e .
RUN . .venv/bin/activate && \
    python -c "import sherlog_repl" || { echo "Package import failed"; exit 1; }

# --- Final Stage ---
FROM base
WORKDIR /app

# Copy application code and venv from UV stage
COPY --from=uv /app/.venv /app/.venv
COPY --from=uv /usr/local/bin/uv /usr/local/bin/uv
COPY src/ ./src/

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"

ENTRYPOINT ["python", "-m", "sherlog_repl"]
