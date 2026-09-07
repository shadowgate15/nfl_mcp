# ESPN cookie-pull script: headed-browser login, .env staging output

Private ESPN Fantasy leagues need `espn_s2`/`SWID` session cookies, which only exist after a
real ESPN login — so `scripts/` gains a one-time, user-run Playwright script that launches a
real, non-headless browser at ESPN's login page and lets the user log in by hand. The script
never touches a raw ESPN password and needs no 2FA-specific logic, since ESPN's own login page
handles whatever challenge (2FA, SSO, CAPTCHA) it throws up. It detects completion by polling
the browser context for both cookies to appear, and by prompting the user to press Enter once
logged in — the Enter press is the real trigger, polling is a fallback, both bounded by a
timeout (e.g. 5 minutes) that exits with an error. It writes `ESPN_S2=`/`ESPN_SWID=` into a
repo-root `.env` (already gitignored), merging into any existing `.env` content rather than
overwriting it — a staging file only, since the runtime keeps reading credentials via plain
`os.getenv("ESPN_S2")`/`os.getenv("ESPN_SWID")`, mirroring how `vegas_tools.py` reads
`ODDS_API_KEY`; no dotenv-loader is added to `config.py`. Playwright lives in a new
`dependency-groups.espn` in `pyproject.toml` (not the existing `dev` group), installed
on-demand via `uv sync --group espn`, so the browser-binary download doesn't land on every
contributor's normal `uv sync`.

## Considered Options

- **Scripted credentials**: the script prompts for and submits username/password itself.
  Rejected — the script would have to handle a raw ESPN password and be taught each new
  2FA/CAPTCHA challenge ESPN introduces, where the headed-browser approach gets both for free
  by just being ESPN's own login page.
- **Adding dotenv-loading to `config.py`** so the written `.env` becomes a real, auto-loaded
  config source. Rejected as out of this ticket's scope: it's a runtime behavior change beyond
  the extraction script, and would be its own decision; every other env var in this repo
  (`ODDS_API_KEY` included) is read via plain `os.getenv`, so `.env` stays a staging file the
  user copies/sources from.
- **Folding Playwright into the existing `dev` dependency group.** Rejected — Playwright's
  browser-binary download is real overhead most contributors never need; a dedicated `espn`
  group keeps it opt-in.
