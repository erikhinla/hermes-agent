# =============================================================================
# Hermes Agent — Docker Image
# Based on: erikhinla/hermes-agent (fork of NousResearch/hermes-agent)
# Python 3.11 + uv for fast dependency management
# =============================================================================

FROM python:3.11-slim-bookworm

# ── System dependencies ──────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ffmpeg \
    build-essential \
    libssl-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Install uv (fast Python package manager) ────────────────────────────────
RUN curl -fsSL https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# ── Set working directory ────────────────────────────────────────────────────
WORKDIR /app

# ── Clone your fork ──────────────────────────────────────────────────────────
# Using ARG so you can override branch at build time:
#   docker build --build-arg HERMES_BRANCH=main .
ARG HERMES_REPO=https://github.com/erikhinla/hermes-agent.git
ARG HERMES_BRANCH=main

RUN git clone --depth 1 --branch ${HERMES_BRANCH} ${HERMES_REPO} .

# ── Initialize submodules (if any) ──────────────────────────────────────────
RUN git submodule update --init --recursive || true

# ── Install Python dependencies via uv ──────────────────────────────────────
RUN uv pip install --system -e ".[modal]" 2>/dev/null || uv pip install --system -e .

# ── Copy .env template (user mounts real .env at runtime) ───────────────────
RUN cp .env.example .env.template 2>/dev/null || true

# ── Create directories for persistent data ──────────────────────────────────
RUN mkdir -p /root/.hermes /workspace

# ── Expose gateway port ──────────────────────────────────────────────────────
EXPOSE 50090

# ── Healthcheck ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:50090/health 2>/dev/null || python3 -c "import hermes_constants" 2>/dev/null || exit 1

# ── Default: run gateway mode so Portainer can hit it via HTTP ───────────────
# Override with: docker run ... hermes (for interactive CLI)
CMD ["python3", "run_agent.py", "--gateway", "--port", "50090", "--host", "0.0.0.0"]
