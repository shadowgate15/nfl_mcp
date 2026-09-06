# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Multi-stage build.
#
# The build stage carries the C toolchain (gcc + its hard dependency binutils)
# needed to compile any dependency that ships only an sdist. binutils alone
# accounts for the large majority of the image scanner's findings (it parses
# many binary formats -> broad CVE surface, and ships as ~8 sub-packages so each
# CVE is reported 8x). By building into an isolated virtualenv and copying ONLY
# that venv into a clean runtime stage, the final image carries no gcc/binutils
# at all -- removing that entire class of findings and shrinking the image.
# ---------------------------------------------------------------------------

# ---- Build stage -----------------------------------------------------------
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# gcc/binutils live ONLY in this stage and never reach the runtime image.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Obtain the uv binary from Astral's official image layer -- no curl, no pip
# bootstrap, keeping this stage's install surface minimal.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Install pinned, reproducible dependencies straight from the committed
# lockfile into a self-contained virtualenv we can copy wholesale to the
# runtime stage, before the rest of the source is copied in (dependencies
# change far less often than app code, so this layer caches well). --locked
# fails the build if uv.lock is out of sync with pyproject.toml instead of
# silently re-resolving. --no-dev excludes the dev/agent-evals dependency
# groups from the shipped image.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Install the project itself into the venv.
COPY . .
RUN uv sync --locked --no-dev

# ---- Runtime stage ---------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Pull the latest Debian security patches, but do NOT install gcc/binutils here
# -- the runtime needs no C toolchain (all deps arrive as prebuilt wheels via
# the copied venv).
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-built virtualenv (all deps + the installed package) and the app
# source (the editable install and PYTHONPATH resolve nfl_mcp from /app).
COPY --from=builder /opt/venv /opt/venv
COPY . .

# Smoke test: fail the build if the copied venv can't import the full app graph
# in this clean, toolchain-free runtime. `import nfl_mcp.server` transitively
# imports the tool registry -> every tool module -> every (compiled) dependency
# (lxml, pydantic-core, ...), so a broken venv copy or a missing wheel is caught
# here rather than at container start. Side-effect-free (no server bind, no DB).
RUN python -c "import nfl_mcp.server"

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

# Expose the port
EXPOSE 9000

# Health check (Python stdlib -- avoids depending on curl in the image)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:9000/health', timeout=5).status == 200 else 1)" || exit 1

# Run the server
CMD ["python", "-m", "nfl_mcp.server"]
