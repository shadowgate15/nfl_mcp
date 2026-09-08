# ESPN Fantasy Football API: Options and Recommendation

## Overview

This document evaluates how `nfl_mcp` could add ESPN *fantasy football* data (league settings,
rosters, matchups, transactions) — a surface this repo does not currently touch — and whether to
roll a thin client of our own or add a third-party dependency.

**Date:** September 6, 2026

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Option A: ESPN's Official Fantasy API](#option-a-espns-official-fantasy-api)
3. [Option B: The Undocumented ESPN Fantasy Endpoints](#option-b-the-undocumented-espn-fantasy-endpoints)
4. [Option C: Third-Party Library (`espn-api`)](#option-c-third-party-library-espn-api)
5. [Rate Limits / Acceptable Use](#rate-limits--acceptable-use)
6. [Recommendation](#recommendation)
7. [References](#references)

---

## Executive Summary

**Key Findings:**

- ESPN has **no official, documented Fantasy Football API** in 2026. Its historic developer
  portal (`developer.espn.com`) is a dead stub that meta-refreshes to a defunct `espn.go.com`
  URL, confirming the old ESPN Developer Center is retired, not merely undiscoverable.
- The fantasy data surface is entirely **undocumented**: `fantasy.espn.com/apis/v3/games/ffl/...`
  now 302-redirects to `lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/...`, a different host
  than the core/site sports API (`site.api.espn.com`) this repo already calls in
  `nfl_mcp/nfl_tools.py`. Same publisher, unrelated and independently-undocumented surface.
- Private leagues require two session cookies, `espn_s2` and `SWID`, sent as request cookies —
  confirmed directly in the primary open-source client's request layer, not just claimed in prose.
- The one actively-maintained third-party Python wrapper, **`cwendt94/espn-api`** (957 stars, MIT
  licensed, latest release `v0.46.0` / March 2026, commits as recent as August 2026), supports
  public and private leagues and both current and historical seasons, and its own real GitHub
  issues document ESPN breaking the API without notice (HTTP 401/403 incidents in 2023 and 2024)
  as well as the library's own fallback-URL workaround for it.
- `espn-api`'s dependency footprint is small (`requests`, `urllib3`, `idna`, version-pinned) but
  it uses `requests` (synchronous), not this project's `httpx` (async) — pulling it in would mean
  running a second, blocking HTTP stack alongside the repo's existing async `httpx` client.
- **Recommendation:** roll our own thin client, following this repo's existing Sleeper/CBS
  pattern, but *port* (not import) `espn-api`'s auth-cookie and endpoint-mapping approach from its
  MIT-licensed source as a reference implementation.

---

## Option A: ESPN's Official Fantasy API

ESPN does not publish a supported, documented Fantasy Football API — for any tier (free, paid, or
partnership) — as of this research.

- `https://developer.espn.com/` returns HTTP 200 but its body is a bare meta-refresh stub:
  `<meta http-equiv="refresh" content="1;url=http://espn.go.com/">` — i.e. the domain exists but
  serves no developer content and forwards to a defunct pre-2013 ESPN branding URL, confirmed by
  fetching the page directly.
- `https://www.espn.com/apis/devcenter/`, the URL path for ESPN's old 2012-era "Developer Center"
  (which TechCrunch covered at launch — [TechCrunch, 2012](https://techcrunch.com/2012/03/05/espn-developer-center-and-apis)),
  now 404s directly.
- This is a *different* surface from the public, undocumented core sports API this repo already
  calls (`site.api.espn.com/apis/site/v2/sports/football/nfl/...` in `nfl_mcp/nfl_tools.py`) —
  that surface is also unofficial/undocumented, just for non-fantasy data (news, teams,
  standings). Neither surface has an ESPN-published spec.

**Conclusion:** there is no official option to evaluate. Any ESPN fantasy integration is
necessarily built on an undocumented API (Option B), either directly or through a third-party
wrapper (Option C).

---

## Option B: The Undocumented ESPN Fantasy Endpoints

Verified directly from `cwendt94/espn-api`'s actual source (`espn_api/requests/constant.py` and
`espn_api/requests/espn_requests.py` in its GitHub repo), not from prose claims:

```python
# espn_api/requests/constant.py
FANTASY_BASE_ENDPOINT = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/'
NEWS_BASE_ENDPOINT = 'https://site.api.espn.com/apis/fantasy/v3/games/'
```

League data is fetched from
`{FANTASY_BASE_ENDPOINT}ffl/seasons/{year}/segments/0/leagues/{league_id}` for 2018+ seasons, and
from a separate `leagueHistory` path for pre-2018 seasons — the client automatically retries the
other URL format on an HTTP 401, per `espn_requests.py`'s `checkRequestStatus`. Empirically,
`https://fantasy.espn.com/apis/v3/games/ffl/...` now 302-redirects to the `lm-api-reads` host
(confirmed with `curl -I` during this research), and hitting `lm-api-reads` directly for a
non-existent league returns a 404 — consistent with the source's documented behavior.

**Auth for private leagues:** confirmed directly in `BaseLeague.__init__`
(`espn_api/base_league.py`):

```python
cookies = None
if espn_s2 and swid:
    cookies = {'espn_s2': espn_s2, 'SWID': swid}
self.espn_request = EspnFantasyRequests(sport=sport, year=year, league_id=league_id, cookies=cookies, ...)
```

and then passed straight through as `requests.get(..., cookies=self.cookies)` in
`espn_requests.py`. So a private league needs exactly the `espn_s2` and `SWID` values a user's
browser session holds, sent as cookies — no separate API key or OAuth flow exists.

**What's available**, per the same source file's request methods: league settings/teams/rosters/
matchups (`get_league`, the `mTeam`/`mRoster`/`mMatchup`/`mSettings`/`mStandings` views), draft
results (`get_league_draft`), transactions/waiver reports (`get_league_offers`), free-agent/player
data (`get_pro_players`, `get_player_card`), and player news (`get_player_news`). Historical
seasons are supported via the alternate `leagueHistory` endpoint for years before 2018.

**Evidence of instability/breakage** — real, closed/open GitHub issues on the same repo, not
speculation:
- [Issue #629, "Doesn't seem to work anymore"](https://github.com/cwendt94/espn-api/issues/629)
  (Jan 2025) — multiple users hit an unhandled failure; the fix in practice was upgrading to a
  newer pinned release (`espn_api==0.38.1`), i.e. ESPN's API shape had moved and the library
  needed a patch to keep up.
- [Issue #586, "ESPN returned an HTTP 403"](https://github.com/cwendt94/espn-api/issues/586)
  (Oct 2024) — a previously-working integration started failing with a bare 403 with no ESPN
  changelog to explain it.
- [Issue #549, "ESPN_2 and SWID Credentials"](https://github.com/cwendt94/espn-api/issues/549) and
  [#527, "Private League 2024 does not work"](https://github.com/cwendt94/espn-api/issues/527) —
  further private-league auth breakage reports.
- The library's own `checkRequestStatus` method contains a hard-coded fallback that swaps between
  the two known URL formats on a 401 — itself evidence, straight from the maintainer's source,
  that ESPN has changed the endpoint shape across seasons and the client had to grow a
  workaround rather than being told about the change in advance.

**Conclusion:** the endpoints work, are well-mapped by an actively-maintained open-source client,
but are unambiguously undocumented, unversioned, and have broken client integrations more than
once with no advance notice — matching this repo's existing experience with CBS's scraped markup
(see `nfl_mcp/cbs_fantasy_tools.py`'s defensive comments about CBS markup drift).

---

## Option C: Third-Party Library (`espn-api`)

[`cwendt94/espn-api`](https://github.com/cwendt94/espn-api) is the primary, actively-maintained
candidate. ([`rbarton65/espnff`](https://github.com/rbarton65/espnff), which `espn-api`'s own
README credits as its inspiration, appears dormant by comparison and was not investigated in
depth for this research.)

| Signal | Finding | Source |
|---|---|---|
| License | MIT, `Copyright (c) 2019 Christian Wendt` | [LICENSE](https://github.com/cwendt94/espn-api/blob/master/LICENSE) |
| Stars / forks | 957 stars, 290 forks | [GitHub API repo metadata](https://api.github.com/repos/cwendt94/espn-api) (fetched live during this research) |
| Latest release | `v0.46.0`, published 2026-03-23; prior releases run back through 2019 at a roughly multi-month cadence | [Releases](https://github.com/cwendt94/espn-api/releases) |
| Latest commit | 2026-08-18 ("chore: Add more test coverage", merge of PR #698) | [Commits](https://github.com/cwendt94/espn-api/commits/master) |
| Open issues | 60 open at time of research | GitHub API repo metadata |
| PyPI | `espn-api` 0.46.0, 90 releases total | [PyPI project page](https://pypi.org/project/espn-api/) |

**Test coverage:** the repo has a real `tests/football/{unit,integration}` split.
`tests/football/unit/test_league.py` mocks HTTP with `requests_mock` against captured JSON
fixtures (`tests/football/unit/data/*.json`) — solid coverage of parsing logic. But
`tests/football/integration/test_league.py` hits the **live** ESPN API with real league IDs and no
mocking (e.g. `League(1234, 2018)`, `League(48153503, 2019)`) — a direct, primary-source
confirmation that testing against this API is exactly as awkward as expected for an undocumented,
unversioned surface: the project's own CI has to hit production ESPN infrastructure to know if it
still works.

**Public + private, current + historical seasons — confirmed from source, not README:**
- Public/private: `BaseLeague.__init__` builds cookies only `if espn_s2 and swid`, otherwise
  passes `cookies=None` — same code path serves both, per `espn_api/base_league.py`.
- Historical seasons: `EspnFantasyRequests.__init__` branches on `year < 2018` to use the
  `leagueHistory` endpoint format vs. the current `seasons/{year}/segments/0/leagues/{id}` format,
  per `espn_api/requests/espn_requests.py`.

**Dependency footprint** (from the repo's own `setup.py` and PyPI metadata):
```
install_requires=['requests>=2.32.4,<2.33.0', 'urllib3>=2.2.3,<2.3.0', 'idna>=3.12,<3.13']
```
Small in count, but all three are pinned to narrow ranges — a maintenance-burden and
possible-conflict risk in its own right — and `requests` is a synchronous HTTP library, whereas
this repo standardizes on async `httpx` throughout (`nfl_mcp/config.py`'s `create_http_client()`,
used by every existing tool module). Depending on `espn-api` would mean running two HTTP client
stacks side by side, and it does not use this repo's retry/circuit-breaker layer
(`nfl_mcp/retry_utils.py`) or its `@handle_http_errors` error-normalization convention
(`nfl_mcp/errors.py`) — those would still need to be hand-wrapped around it either way.

---

## Rate Limits / Acceptable Use

No formal ToS exists for this undocumented API, and this research did **not** find concrete,
citable evidence of ESPN rate-limiting, issuing 429s, or IP-blocking fantasy API consumers — a
targeted search of `cwendt94/espn-api`'s issue tracker for "429" and "rate limit" returned zero
results. The breakage that *is* documented (Option B above) is shape/auth changes (401/403), not
throttling. Absent evidence, this document makes no rate-limit claim beyond: build in the same
conservative default delay/backoff this repo already applies to Sleeper and CBS via
`nfl_mcp/retry_utils.py`, since an undocumented API offers no guarantee either way.

---

## Recommendation

**Roll our own thin client, matching this repo's existing convention — do not add `espn-api` as a
dependency.**

Reasoning, weighed against what was actually found:

1. **Precedent fit.** This repo already hand-rolls thin clients over two other undocumented/
   scraped fantasy-adjacent sources — Sleeper (structured JSON API) and CBS (scraped HTML) — both
   wrapped in the repo's own `@handle_http_errors` / retry-and-circuit-breaker conventions rather
   than pulling in a Sleeper or CBS SDK. ESPN fantasy is the same shape of problem a third time:
   an undocumented API with no ToS and a history of silent breakage. Treating it differently now
   (via a third-party dependency) would be the outlier, not the consistent choice.
2. **Dependency discipline.** `pyproject.toml` already caps `httpx<1` and `pydantic<2.14` as a
   deliberate "guardrail," per its own `[tool.uv]` comment — signaling this project keeps its
   dependency surface small and load-bearing on purpose. `espn-api` would add a second,
   synchronous HTTP stack (`requests`/`urllib3`/`idna`, each itself version-pinned narrowly) that
   this repo's async `httpx`-based tools, retry layer, and error-handling decorator would still
   need to wrap around externally to get parity with every other tool module — most of the
   integration cost survives even after adding the dependency.
3. **The library's real health is good, but doesn't change the calculus.** `espn-api` is
   genuinely active (commits through August 2026, MIT-licensed, 957 stars, real if imperfect test
   coverage) and is a legitimate reference for *how* to talk to these endpoints. But "actively
   maintained" here specifically means "gets patched reactively after ESPN silently breaks it"
   (issues #629, #586, #549, #527) — the same failure mode this project already absorbs directly
   for CBS's markup drift. A dependency doesn't insulate this project from that risk; it just
   moves the fix onto someone else's release cadence, which cuts against the value of adding it.
4. **What to actually take from `espn-api`.** Its source is small, MIT-licensed, and cleanly
   separates concerns worth porting directly rather than importing: the cookie-based auth pattern
   (`{'espn_s2': ..., 'SWID': ...}` as request cookies), the endpoint map
   (`lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{id}`, plus
   the pre-2018 `leagueHistory` fallback), and the `view=` query-param catalog for league data,
   rosters, matchups, and draft results. Porting this logic (with attribution, since MIT requires
   preserving the license/copyright notice for any copied code) into a new
   `nfl_mcp/espn_fantasy_tools.py` — built on `httpx`, wrapped in `@handle_http_errors`, and using
   `retry_utils.py` — gets the concrete implementation value of `espn-api`'s reverse-engineering
   work without a second HTTP stack or an external release dependency.

---

## References

1. ESPN Developer Center dead-redirect stub — fetched live: `https://developer.espn.com/` (2026-09-06)
2. ESPN Developer Center 404 — fetched live: `https://www.espn.com/apis/devcenter/` (2026-09-06)
3. TechCrunch, "ESPN Developer Center and APIs" (2012) — https://techcrunch.com/2012/03/05/espn-developer-center-and-apis
4. `cwendt94/espn-api` GitHub repository — https://github.com/cwendt94/espn-api
5. `espn_api/requests/constant.py` (endpoint hosts) — https://github.com/cwendt94/espn-api/blob/master/espn_api/requests/constant.py
6. `espn_api/requests/espn_requests.py` (request/auth/fallback logic) — https://github.com/cwendt94/espn-api/blob/master/espn_api/requests/espn_requests.py
7. `espn_api/base_league.py` (cookie construction) — https://github.com/cwendt94/espn-api/blob/master/espn_api/base_league.py
8. `espn-api` LICENSE (MIT) — https://github.com/cwendt94/espn-api/blob/master/LICENSE
9. `espn-api` `setup.py` (dependency pins) — https://github.com/cwendt94/espn-api/blob/master/setup.py
10. `espn-api` PyPI project page — https://pypi.org/project/espn-api/
11. GitHub Issue #629, "Doesn't seem to work anymore" — https://github.com/cwendt94/espn-api/issues/629
12. GitHub Issue #586, "ESPN returned an HTTP 403" — https://github.com/cwendt94/espn-api/issues/586
13. GitHub Issue #549, "ESPN_2 and SWID Credentials" — https://github.com/cwendt94/espn-api/issues/549
14. GitHub Issue #527, "Private League 2024 does not work" — https://github.com/cwendt94/espn-api/issues/527
15. `tests/football/integration/test_league.py` (live-API integration tests) — https://github.com/cwendt94/espn-api/blob/master/tests/football/integration/test_league.py
16. `tests/football/unit/test_league.py` (mocked unit tests) — https://github.com/cwendt94/espn-api/blob/master/tests/football/unit/test_league.py
17. This repo's dependency guardrail comment — `pyproject.toml`, `[tool.uv]` section
18. This repo's existing hand-rolled clients — `nfl_mcp/sleeper_tools.py`, `nfl_mcp/cbs_fantasy_tools.py`, `nfl_mcp/config.py`, `nfl_mcp/errors.py`, `nfl_mcp/retry_utils.py`

---

*Document created: September 6, 2026*
*NFL MCP Server Research Initiative*
