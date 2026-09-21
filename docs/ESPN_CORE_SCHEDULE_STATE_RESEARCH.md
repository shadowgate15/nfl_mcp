# ESPN-core Schedule/State Research: Findings and Recommendation

**Date:** September 8, 2026
**Ticket:** #26 ("Decide whether NFL schedule/state data moves to ESPN-core instead of ESPN Fantasy"), part of the Sleeper-to-ESPN-Fantasy cutover map (#21)

---

## Question

`sos_tools.py` and `weather_tools.py` call `sleeper_tools._fetch_week_schedule`, and
`faab_tools.py`/`playoff_tools.py` call `sleeper_tools.get_nfl_state` (current
season/week) — both league-agnostic NFL data, not tied to any specific fantasy
league.

This repo already has a keyless "ESPN-core" surface (`site.api.espn.com`,
`sports.core.api.espn.com` — see `CONTEXT.md`'s Language section) used in
`nfl_tools.py` for league-agnostic NFL data (teams, news, standings). This
ticket investigates whether ESPN-core already exposes (or can trivially expose)
a week/schedule/current-state equivalent, which would let these four modules
avoid the cookie-gated ESPN Fantasy surface (`ESPN_S2`/`ESPN_SWID`) entirely.

All findings below were verified directly against this repo's source code and
against live ESPN/Sleeper endpoints via `curl`, using the repo's branded
User-Agent (`NFL-MCP-Server/<version> (+https://github.com/gtonic/nfl_mcp)`),
on 2026-09-08.

---

## Findings

### 1. `_fetch_week_schedule` already talks to ESPN-core, not Sleeper — this is a naming/location issue only

`nfl_mcp/sleeper_enrichment.py::_fetch_week_schedule(season, week, force)`
(defined at line 96) is re-exported through `nfl_mcp/sleeper_tools.py`'s import
block (`nfl_mcp/sleeper_tools.py:45`), and is called by:

- `nfl_mcp/sos_tools.py:147` — inside `_gather_opponents`, via
  `sleeper_tools._fetch_week_schedule(season, wk, force=True)`
- `nfl_mcp/weather_tools.py:226` — inside `get_weather_forecast`, via
  `sleeper_tools._fetch_week_schedule(season, week, force=True)`
- `nfl_mcp/sleeper_tools.py:239` — internally, via the re-exported
  `_fetch_week_schedule(season, week_guess)`

Despite the `sleeper_*` module names and the access path through
`sleeper_tools`, this function's network call is 100% ESPN-core:

```
GET https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week={week}&year={season}&seasontype=2
```

(see `nfl_mcp/sleeper_enrichment.py:116`), parsed from
`events[].competitions[].competitors[]` into bidirectional game rows
(`{season, week, team, opponent, is_home, kickoff, raw}`). It has **zero**
dependency on `api.sleeper.app`.

**Conclusion:** for these two call sites, there is nothing to migrate at the
network-call level. The only work is organizational: relocate the function out
of `sleeper_enrichment.py` (and off the `sleeper_tools.py` import path) into an
ESPN-core-owned module, before `sleeper_tools.py` is deleted per #21's hard
cutover plan.

### 2. `get_nfl_state` genuinely calls Sleeper, but callers use only one field

`nfl_mcp/sleeper_tools.py::get_nfl_state()` (defined at line 650) calls:

```
GET https://api.sleeper.app/v1/state/nfl
```

Live response (fetched 2026-09-08):

```json
{"week":1,"leg":1,"season":"2026","season_type":"regular","league_season":"2026","previous_season":"2025","season_start_date":"2026-09-09","display_week":1,"league_create_season":"2026","season_has_scores":true}
```

Call sites:

- `nfl_mcp/faab_tools.py:155-156` — inside the FAAB bid-timing logic:
  ```python
  state = await get_nfl_state()
  wk = state.get("nfl_state", {}).get("week") if state.get("success") else None
  ```
- `nfl_mcp/playoff_tools.py:173-174` — playoff-odds current-week fallback:
  ```python
  state = await get_nfl_state()
  current_week = int(state.get("nfl_state", {}).get("week")) if state.get("success") else None
  ```

Neither call site reads `season_type`, `season`, or any other field from the
Sleeper response — only the integer `week`.

### 3. ESPN-core's own scoreboard endpoint, called with no params, already returns current week + season type/year for free

```
GET https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard
```

(no `week`/`year` query params). This requires the repo's branded User-Agent —
a generic UA gets HTTP 403 from ESPN's WAF, the same behavior already noted in
an existing comment in `nfl_mcp/nfl_tools.py::get_nfl_news`.

Live response fetched 2026-09-08 (pre-Week-1, 2026 season) has top-level keys
`leagues`, `season`, `week`, `events`, `provider`, with:

- `season`: `{"type": 2, "year": 2026}` — `type` uses the same 1=Pre/2=Regular/3=Post
  convention already documented elsewhere in this codebase (e.g.
  `nfl_tools.py::get_nfl_standings`'s `season_type` param docstring).
- `week`: `{"number": 1}`
- `leagues[0].season.type`: `{"id":"2","type":2,"name":"Regular Season","abbreviation":"reg"}`
  (a human-readable variant, redundant with the above)

This single unauthenticated call returns everything `faab_tools.py` /
`playoff_tools.py` currently consume from Sleeper's `get_nfl_state()` (just the
week number), plus season type/year as a bonus, with zero
`ESPN_S2`/`ESPN_SWID` cookies.

### 4. Accepted gap: ESPN-core has no equivalent to Sleeper's `season_type == "off"`

Sleeper's `season_type` can be `"off"` for the deep offseason (roughly
Feb-Aug), a state ESPN's `season.type` int (1/2/3) has no 4th value for.

ESPN-core's parameterless "current" scoreboard query's exact behavior during
the deep offseason (does it return stale prior-postseason data, or default to
next season's preseason?) was **not verified live in this session** — today's
date (2026-09-08) sits just before Week 1 of the 2026 season, so true "off"
conditions couldn't be tested against the live endpoint.

This exact ESPN-core ambiguity is already known and worked around elsewhere in
this codebase: `nfl_tools.py::get_team_schedule` forces `seasontype=2` in its
URL construction specifically "because ESPN otherwise defaults to preseason
when the regular season hasn't started, returning only the 3-game preseason
slate" (see the comment directly above the URL construction there,
`nfl_mcp/nfl_tools.py:846-848`).

Since neither `faab_tools.py` nor `playoff_tools.py` currently reads
`season_type` at all (see Finding 2), this gap does **not** block migrating
those two modules as they exist today. It should be recorded as an explicit,
accepted gap rather than silently assumed covered — relevant only if
future season-type-aware logic is added on top of these call sites.

### 5. Adjacent finding (footnote — not this ticket's focus)

`nfl_mcp/nfl_tools.py::get_current_season_and_week()` (defined at line 1194,
an ESPN-core-hosted helper) itself calls `sleeper_tools.get_nfl_state()`
internally (import at `nfl_mcp/nfl_tools.py:1201`), and is in turn imported
back into `sleeper_tools.py` (`nfl_mcp/sleeper_tools.py:794`, inside
`get_trending_players`'s enrichment path). This is a small circular
cross-module dependency between `nfl_tools.py` and `sleeper_tools.py`,
unrelated to the four modules this ticket is about, but worth flagging as
adjacent cleanup for whoever eventually deletes `sleeper_tools.py` under #21 —
`get_current_season_and_week()` will need its own internal call re-pointed at
ESPN-core's parameterless scoreboard (Finding 3) at that time, independent of
this ticket's four named modules.

---

## Recommendation

**Move both concerns (schedule data and current week/season-type) to
ESPN-core for all four ticket-named modules (`sos_tools.py`, `weather_tools.py`,
`faab_tools.py`, `playoff_tools.py`). Do not adopt ESPN Fantasy for this.**

- **`sos_tools.py` / `weather_tools.py`:** no network-call change needed —
  `_fetch_week_schedule` already hits ESPN-core (Finding 1). The only work is
  relocating the function out of `sleeper_enrichment.py` and off the
  `sleeper_tools.py` import path into an ESPN-core-owned module, before
  `sleeper_tools.py` is deleted.
- **`faab_tools.py` / `playoff_tools.py`:** replace the
  `sleeper_tools.get_nfl_state()` call with a call to ESPN-core's
  parameterless scoreboard endpoint (Finding 3), reading `week.number` (and
  optionally `season.type` / `season.year` if season-type-aware logic is ever
  added). No ESPN Fantasy auth needed.

**Net effect:** all four modules can avoid ESPN Fantasy's cookie-gated auth
entirely, since this is league-agnostic NFL data with no dependency on a
specific fantasy league. ESPN-core covers it end-to-end except for the
accepted `season_type == "off"` gap (Finding 4), which is currently unused by
any of the four call sites.

This resolves and unblocks the relevant bullets in #21's "Not yet specified"
section — re-pointing `sos_tools.py`/`weather_tools.py`'s schedule calls, and
the schedule/state portion of `faab_tools.py`/`playoff_tools.py`.

---

## Accepted Gap (summary)

ESPN-core's `season.type` field is a 3-value enum (1=Pre, 2=Regular, 3=Post)
with no equivalent to Sleeper's 4th `season_type` value, `"off"` (deep
offseason). Not currently read by any of the four call sites in scope for this
ticket, so it does not block migration as designed above. Should be
re-evaluated if season-type-aware logic is added to `faab_tools.py` or
`playoff_tools.py` in the future — at that point, the parameterless scoreboard
query's real offseason behavior (untested here, since this research was
conducted just before Week 1 of the 2026 season) will need to be verified
live.
