# Contributing

Thanks for helping improve the NFL MCP Server!

## Development setup

- Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) (CI runs the suite on 3.11, 3.12, and 3.13)

```bash
uv sync --group dev
```

## Running things

```bash
# Tests (with coverage)
uv run pytest tests/ -q --cov=nfl_mcp --cov-report=term-missing

# Server (HTTP transport on :9000, health at /health)
uv run python -m nfl_mcp.server

# Docker
docker build -t nfl-mcp .
docker run --rm -p 9000:9000 nfl-mcp
```

## Pull requests

- Branch off `main` and keep each PR focused.
- Add or update tests — the suite must stay green. `main` is protected and
  requires the **Tests (3.11)** and **Tests (3.12)** checks to pass before merge.
- CI builds the Docker image on every PR (validates the Dockerfile); on `main`
  and `v*` tags it also publishes the image to GHCR.
- Conventional-style commit prefixes are appreciated: `feat:`, `fix:`, `chore:`,
  `docs:`, `ci:`, `test:`.

## Releases

- Bump the version in `pyproject.toml`.
- Create a git tag `vX.Y.Z` (e.g. via `gh release create vX.Y.Z --generate-notes`).
  The tag triggers CI to publish `ghcr.io/gtonic/nfl_mcp:X.Y.Z` (and `X.Y`).

## Reporting security issues

Please do **not** open public issues for vulnerabilities — see [SECURITY.md](SECURITY.md).
