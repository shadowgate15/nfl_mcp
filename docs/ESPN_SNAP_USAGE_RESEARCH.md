# ESPN/nflverse Snap%/Usage-Stats Substitute Research

## Overview

This document investigates GitHub issue #31 (part of the wayfinder map, issue #21, for a hard
cutover from Sleeper to ESPN Fantasy as this repo's sole fantasy data source):
`sleeper_enrichment.py`'s `_fetch_week_player_snaps`/`_fetch_weekly_usage_stats` (gated by
`NFL_MCP_ADVANCED_ENRICH=1`) pull per-player offensive snap %, targets, routes, and red-zone
touches from Sleeper's `api.sleeper.app/v1/stats/nfl/regular/{season}/{week}` endpoint, powering
AGENT.md's documented "Advanced Enrichment" feature. Unlike issue #25's trending-players research
(pure investigation, decision deferred), this ticket asks for an explicit recommendation:
accept the gap, drop the feature, or build a substitute — per field, with effort estimates.

**Date:** September 9, 2026

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [What the Sleeper Feature Currently Computes](#what-the-sleeper-feature-currently-computes)
3. [How Consumers Use It](#how-consumers-use-it)
4. [Does ESPN Fantasy Expose an Equivalent?](#does-espn-fantasy-expose-an-equivalent)
5. [Does nflverse Publish an Equivalent?](#does-nflverse-publish-an-equivalent)
6. [Field-by-Field Substitution Table](#field-by-field-substitution-table)
7. [A Correction to ADR 0005](#a-correction-to-adr-0005)
8. [Recommendation](#recommendation)

---

## Executive Summary

- **Targets and touches (targets/carries/receptions): substitutable today, low effort.** Both
  ESPN Fantasy (live-probed) and nflverse's already-fetched `player_stats` CSV carry real
  per-player, per-week targets and rushing/receiving volume. ESPN Fantasy's data is reachable
  through tools this repo has already shipped (`get_espn_rosters`, `get_espn_matchups`); nflverse's
  is already being downloaded by `matchup_tools.py` for defense rankings and asserted by the
  `nflverse.usage_columns` CI check.
- **Snap %/snap share: substitutable, medium effort.** ESPN Fantasy's stat taxonomy has **no**
  snap-count field anywhere (confirmed against the full stat-ID mapping and a live probe — see
  below). nflverse, however, publishes a **separate** `snap_counts_{season}.csv` release with
  `offense_snaps`/`offense_pct` per player per week — live-downloaded and confirmed during this
  research. It's keyed by Pro-Football-Reference id (`pfr_player_id`), not the `gsis_id` used in
  `player_stats` or the `espn_id` this repo is cutting player identity over to (ADR 0005) — but
  nflverse also publishes a `players.csv` crosswalk containing `gsis_id`, `pfr_id`, and `espn_id`
  in the same row, live-confirmed to exist, which makes the join a straightforward id-map lookup,
  not a name-matching problem.
- **Red-zone touches: no direct per-player-week field anywhere checked; derivable only from raw
  play-by-play, higher effort.** Neither ESPN Fantasy's stat taxonomy, nflverse's `player_stats`,
  nflverse's `snap_counts`, nor nflverse's Next Gen Stats receiving file has a red-zone-touches
  column. nflverse's full play-by-play release (`pbp/play_by_play_{season}.csv.gz`) does have the
  raw ingredients (`yardline_100`, `rusher_id`, `receiver_id`, `play_type`, `week`) to derive it,
  but that means downloading and aggregating full per-play data (tens of megabytes, ~45k rows/
  season) instead of reading a pre-aggregated column — a materially bigger lift than the other
  fields.
- **Routes run: no free public source found anywhere.** Not in ESPN Fantasy's stat taxonomy, not
  in nflverse's `player_stats`, not in nflverse's Next Gen Stats receiving file, not derivable
  from play-by-play (routes require charting data on every offensive snap, not just the plays
  where a given player was targeted — PFF/Sports Info Solutions proprietary data). This mirrors
  issue #25's finding for trending velocity: some fields simply have no free public substitute.
- **Recommendation: partial-accept.** Build substitutes for snap%/snap-share and targets/touches
  (low-to-medium effort, no new subsystem needed); drop routes_run (no substitute exists); drop or
  separately scope red-zone touches (technically buildable but a meaningfully larger, separate
  effort — a play-by-play aggregation pipeline, not a field swap). See
  [Recommendation](#recommendation) for the per-field breakdown.
- **Bonus finding:** ADR 0005 stated `player_week_stats`/`player_usage_stats` have "no current
  callers" and need no migration work. That's incorrect — `server.py`'s prefetch loop
  (`NFL_MCP_PREFETCH=1` + `NFL_MCP_ADVANCED_ENRICH=1`) actively writes both tables every cycle, and
  `sleeper_enrichment._enrich_usage_and_opponent` actively reads them on every roster/matchup/
  transaction enrichment call. See [A Correction to ADR 0005](#a-correction-to-adr-0005).

---

## What the Sleeper Feature Currently Computes

`nfl_mcp/sleeper_enrichment.py` implements four pieces, all gated by
`ADVANCED_ENRICH_ENABLED = os.getenv("NFL_MCP_ADVANCED_ENRICH") == "1"` (line 22):

- **`_fetch_week_player_snaps(season, week)`** (lines 24-94) hits
  `https://api.sleeper.app/v1/stats/nfl/regular/{season}/{week}` and extracts, per player:
  `snaps_offense` (`snaps`/`off_snp`/`off_snaps`/`offense_snaps`), `snaps_team_offense`
  (`team_snaps`/`tm_off_snp`/`off_team_snaps`/`team_snp`), and `snap_pct`
  (`snap_pct`/`off_snp_pct`/`off_snap_pct`). Rows feed `nfl_db.upsert_player_week_stats`, writing
  the `player_week_stats` table (`player_id, season, week, snaps_offense, snaps_team_offense,
  snap_pct`, `nfl_mcp/database.py:386-396`).
- **`_fetch_weekly_usage_stats(season, week)`** (lines 523-709) hits the *same* Sleeper endpoint
  and extracts, per player: `targets` (`rec_tgt`/`targets`), `routes`
  (`routes_run`/`routes`/`rec_routes`/`pass_routes`/`receiving_routes`), `rz_touches` (summed from
  `rec_tgt_rz`+`rush_att_rz` variants, or estimated from `rec_td+rush_td` if no explicit RZ field
  exists — line 606-615), `touches` (`rush_att + rec`), `air_yards`
  (`rec_air_yds`/`air_yards`), and `snap_share` (several field-name variants, or computed from
  `off_snp/team_snp` if no percentage field exists). Rows feed `nfl_db.upsert_usage_stats`, writing
  `player_usage_stats` (`player_id, season, week, targets, routes, rz_touches, touches, air_yards,
  snap_share`, `nfl_mcp/database.py:445-457`). Falls back to an empty list if Sleeper's endpoint
  fails — the docstring's mention of an "ESPN fallback" (line 691-695) is dead code: it always
  returns `[]` with a `TODO`-style comment, never actually calls ESPN.
- **`_calculate_usage_trend(weekly_data, metric)`** (lines 754-794) is pure local math — no
  network call. Given a list of week-dicts ordered most-recent-first, it compares the most recent
  value against the average of prior weeks and buckets the % change into `"up"`/`"down"`/`"flat"`
  (>15% / <-15% / else). This logic is source-agnostic: it works on whatever `targets`/`routes`/
  `snap_share` values are stored per week, regardless of where they came from.
- **`_enrich_usage_and_opponent(nfl_db, athlete, season, week)`** (lines 796-1031) is the
  aggregation point. For non-DEF positions it reads `nfl_db.get_player_snap_pct` (falling back to
  the prior week, then to `_estimate_snap_pct` from depth-chart rank if no cached data exists) to
  set `snap_pct`/`snap_pct_source`/`snap_pct_week`. For WR/RB/TE only, it reads
  `nfl_db.get_usage_last_n_weeks` (3-week rolling averages: `targets_avg`, `routes_avg`,
  `rz_touches_avg`, `snap_share_avg`, `weeks_sample`) and `nfl_db.get_usage_weekly_breakdown`, then
  calls `_calculate_usage_trend` three times (targets/routes/snap_share) to produce
  `usage_trend: {targets, routes, snap_share}` and `usage_trend_overall` (targets trend preferred,
  falling back to snap or routes trend). It also stacks in injury/practice-status/matchup/Vegas
  enrichment unrelated to this ticket's scope.

These four map directly onto AGENT.md's documented "Advanced Enrichment" feature list
(`AGENT.md:240-266`): snap percentages, usage metrics (targets_avg/routes_avg/rz_touches_avg/
snap_share_avg), and usage trends (targets/routes/snap_share/usage_trend_overall).

---

## How Consumers Use It

`_enrich_usage_and_opponent` is called from three files, all gated behind
`NFL_MCP_ADVANCED_ENRICH=1` upstream (the fetchers return `[]` when disabled, so the DB tables stay
empty and every read returns `None`/falls back to depth-chart estimation):

- **`sleeper_tools.py:299`** — inside `get_rosters`' extended-enrichment block, per roster player.
- **`sleeper_tools.py:523`** — inside `get_matchups`' extended-enrichment block, per matchup
  player (`players`/`starters` lists).
- **`sleeper_tools.py:834`** — a third call site (trending-players enrichment, per issue #25's
  research).
- **`sleeper_transactions.py:172`** — inside transaction enrichment, applied to both `adds` and
  `drops` player ids per transaction.

All four call sites use the *same* helper with the *same* field names, so a substitute only needs
to be implemented once, inside `_enrich_usage_and_opponent` (or its DB-read dependencies) — no
per-consumer logic changes.

---

## Does ESPN Fantasy Expose an Equivalent?

**Partially — targets and touches yes; snap counts, red-zone touches, and routes: no.**

### Live evidence: ESPN's per-player stat taxonomy has no snap/red-zone/route stat

`docs/ESPN_FANTASY_ENDPOINT_CATALOG.md:127-130` documents that each roster-entry `player` object
carries `stats: [{seasonId, scoringPeriodId, statSourceId, statSplitTypeId, stats: {<statId
str>: float}, ...}]`, where `statSourceId == 0` is actual (not projected) stats. The catalog does
not enumerate what the numeric `statId` keys mean, so this research went to the primary source:
`cwendt94/espn-api`'s `PLAYER_STATS_MAP`/`SETTINGS_SCORING_FORMAT_MAP`
(`espn_api/football/constant.py`, fetched fresh from `raw.githubusercontent.com` during this
research). That mapping is exhaustive across passing (0-22), rushing (23-40), receiving (41-61),
misc (62-73), kicking (74-88), defense (89-136), punting (138-154), and coaching (155-206) stat
categories — **no snap-count, snap-%, red-zone, or "routes run" stat id exists anywhere in the
map**.

This research then live-probed ESPN Fantasy directly (no cookies needed for this public,
2018-season league already used as a fixture in the endpoint catalog):

```
GET https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2018/segments/0/leagues/1234
    ?view=mRoster&view=mTeam&scoringPeriodId=1
```

Real week-1 actual-stat blocks (`statSourceId == 0, scoringPeriodId == 1`) across the league's
rosters confirmed **statId `"58"` (`receivingTargets`) is populated with real values** — e.g.
Michael Thomas: 17 targets, 16 receptions; Zach Ertz: 10 targets, 5 receptions; Kareem Hunt: 1
target. Rushing attempts (statId `"23"`) and receptions (statId `"41"`/`"53"`) are populated the
same way — i.e., **touches (targets + carries + receptions) are directly available, per player,
per week, from data this repo's `get_espn_rosters`/`get_espn_matchups` tools already fetch** (they
request `mRoster`/`mMatchup` and don't currently need any new endpoint or auth).

Cross-checking the *complete* set of `statId` keys actually observed across all players in that
week's real-stats blocks (`0`-`139`, `143`, `144`, `147`, `154`-`157`, `174`, `205`, `206`, `210`)
against the full source-code mapping confirms every observed key maps to a passing/rushing/
receiving/kicking/defense/punting/coaching stat — **no unexplained or snap/red-zone/route-shaped
key appears in the live data**, closing the loop between "the taxonomy doesn't define it" and "the
real payload doesn't return it."

### What this means for the four Sleeper fields

| Sleeper field | ESPN Fantasy equivalent |
|---|---|
| `targets` | **Yes** — statId `58` (`receivingTargets`), confirmed live with real values. |
| `touches` (targets+carries+receptions) | **Yes** — statId `23` (rushing attempts) + `41`/`53` (receptions), confirmed live. |
| `snap_pct`/`snap_share` | **No** — absent from the full stat-id taxonomy and from live data. |
| `rz_touches` | **No** — no red-zone-scoped stat id exists (ESPN's red-zone-adjacent ids, e.g. `37`/`38`/`56`/`57`, are yardage-milestone bonuses like "100-199 yard rushing game," not red-zone touch counts). |
| `routes` | **No** — not a fantasy-scoring stat category at all. |

---

## Does nflverse Publish an Equivalent?

**Yes for targets/touches (already in use), yes for snap%/share (a separate, unused-by-this-repo
release), no direct field for red-zone touches or routes.**

### `player_stats` — already fetched by this repo, already CI-checked

`nfl_mcp/matchup_tools.py:17-22` already fetches
`https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_
{season}.csv` for defense rankings, and `evals/backtest/data.py:22-25` fetches the sibling
`player_stats/player_stats_{season}.csv` release for the backtest. Both are asserted live by the
`nflverse.usage_columns` CI check (`evals/contracts/checks.py:93-105`), which today only asserts
`fantasy_points_ppr` and `touches` (`targets + carries`) exist. This research downloaded the real
2024 `player_stats_{season}.csv` (1.7 MB, live) and confirmed its full column set, including
`targets`, `carries`, `receptions`, `receiving_air_yards`, `target_share`, `air_yards_share`,
`wopr` — **targets and touches (and air_yards, which Sleeper's usage stats also track) are already
present** in a file this repo already downloads for another purpose. **No snap, red-zone, or route
column exists in this file.**

### `snap_counts` — a separate nflverse release, not currently used by this repo

This research downloaded
`https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_{season}.csv`
(2.4 MB, live, 2024 season) and confirmed its columns:

```
game_id,pfr_game_id,season,game_type,week,player,pfr_player_id,position,team,opponent,
offense_snaps,offense_pct,defense_snaps,defense_pct,st_snaps,st_pct
```

`offense_snaps` and `offense_pct` (0-1 scale, e.g. `1` for a 100%-snap offensive lineman) are a
direct, per-player, per-week analog to Sleeper's `snaps_offense`/`snap_pct`. **The join key is the
complication**: this file keys on `pfr_player_id` (Pro-Football-Reference id, e.g. `DawkDi00`),
not the `gsis_id`/`player_id` that `player_stats` uses, and not the `espn_id` this repo is cutting
player identity over to (ADR 0005).

nflverse solves exactly this with a public crosswalk file. This research downloaded
`https://github.com/nflverse/nflverse-data/releases/download/players/players.csv` (7.3 MB, live)
and confirmed its header includes `gsis_id`, `pfr_id`, and `espn_id` columns on the same row (along
with `nfl_id`, `pff_id`, `otc_id`, `esb_id`, etc.) — i.e., **joining `player_stats` (by `gsis_id`)
+ `snap_counts` (by `pfr_id`) + this repo's ESPN-id-keyed player identity is a straightforward
three-file id-map join, not a name-matching or fuzzy-match problem.**

### Red-zone touches — no per-player-week field; derivable only from raw play-by-play

No red-zone-touches column exists in `player_stats`, `snap_counts`, or nflverse's Next Gen Stats
receiving file (`nextgen_stats/ngs_receiving.csv.gz`, downloaded and inspected during this
research — columns are `avg_cushion`, `avg_separation`, `avg_intended_air_yards`, `catch_
percentage`, `avg_yac`, etc. — no red-zone field). nflverse does publish full play-by-play
(`pbp/play_by_play_{season}.csv.gz`, ~19 MB/season, downloaded and inspected) with `yardline_100`,
`rusher_id`, `receiver_id`, `play_type`, and `week` columns — enough to derive red-zone touches by
filtering plays to `yardline_100 <= 20` and counting rush attempts + targets per player per week.
This is **technically possible but a materially different scale of work**: instead of reading one
pre-aggregated column from a file this repo already downloads, it means downloading and parsing
full per-play data (order of 45,000 rows/season) and writing new aggregation logic — closer to a
small new ETL step than a field-name swap.

### Routes run — not published anywhere checked

Routes run (how many pass plays a receiver was on the field and ran a route, whether targeted or
not) requires charting every offensive snap, not just plays with a recorded target or carry.
Neither `player_stats`, `snap_counts`, nor the Next Gen Stats receiving file has this, and
play-by-play alone cannot derive it either (a receiver who ran a route but wasn't targeted leaves
no row keyed to them in `pbp` for that play). This is proprietary charting data (the kind PFF/
Sports Info Solutions sell) with no free public source found in either ESPN's or nflverse's
surfaces — a genuine, unfillable gap, structurally identical to issue #25's finding that Sleeper's
trending-velocity signal has no ESPN analog.

---

## Field-by-Field Substitution Table

| Sleeper field | ESPN Fantasy? | nflverse? | Substitute source | Effort |
|---|---|---|---|---|
| `targets` (raw + `targets_avg`) | Yes (statId 58) | Yes (`player_stats.targets`) | Either — nflverse is simpler since it's already fetched in this repo for defense rankings | **Low** |
| `touches` | Yes (statIds 23/41/53) | Yes (`player_stats.targets+carries`, matches existing CI-asserted `touches` definition exactly) | nflverse `player_stats` (zero new fetch) | **Low** |
| `snap_pct`/`snap_share`/`snap_share_avg` | **No** | Yes (`snap_counts.offense_pct`) | nflverse `snap_counts`, joined via nflverse `players.csv` crosswalk (`pfr_id`↔`gsis_id`/`espn_id`) | **Medium** — new CSV fetch + new join, but same fetch-a-CSV pattern already used twice in this repo |
| `rz_touches`/`rz_touches_avg` | No | No pre-aggregated field; derivable from `pbp` | nflverse play-by-play aggregation | **Medium-high** — new ETL over raw per-play data, not a column read |
| `routes`/`routes_avg` | No | No | **None found** | N/A — no substitute exists |
| `usage_trend_overall`/`usage_trend.*` | N/A (local math) | N/A (local math) | Unaffected — `_calculate_usage_trend` operates on whatever's stored per week; degrades only for the `routes` trend component if `routes` is dropped (targets/snap_share trends keep working) | **None** (already source-agnostic) |
| `air_yards` (not in AGENT.md's list but tracked internally) | Not checked (not requested by ticket) | Yes (`player_stats.receiving_air_yards`) | nflverse `player_stats` (zero new fetch) | **Low** |

---

## A Correction to ADR 0005

ADR 0005 (`docs/adr/0005-player-identity-cache-cutover-to-espn.md:15-17`) states: "Every other
`player_id`-keyed table in `database.py` was checked: `player_week_stats`, `player_usage_stats`,
and `player_practice_status` have no current callers (dead/future-use, no migration needed)."

This research found that's incorrect for `player_week_stats` and `player_usage_stats` (this
ticket didn't investigate `player_practice_status`):

- `nfl_mcp/server.py`'s prefetch loop (`NFL_MCP_PREFETCH=1`, gated further by
  `NFL_MCP_ADVANCED_ENRICH=1`) actively calls `_fetch_week_player_snaps`/
  `_fetch_weekly_usage_stats` every cycle and writes both tables via
  `nfl_db.upsert_player_week_stats`/`nfl_db.upsert_usage_stats` (`server.py:233`, `server.py:
  325-327`).
- `sleeper_enrichment._enrich_usage_and_opponent` actively reads both tables on every
  roster/matchup/transaction enrichment call via `nfl_db.get_player_snap_pct`,
  `nfl_db.get_usage_last_n_weeks`, and `nfl_db.get_usage_weekly_breakdown` — called from four
  sites across `sleeper_tools.py` and `sleeper_transactions.py` (see
  [How Consumers Use It](#how-consumers-use-it)).

Both tables are real, live-written, live-read tables — not dead code. Whatever cutover work
follows from this ticket's recommendation needs to account for that (e.g., if `player_id` in these
tables re-keys from Sleeper ids to ESPN ids as part of the broader identity cutover, these two
tables need the same migration treatment as any other `player_id`-keyed table, contrary to ADR
0005's current text).

---

## Recommendation

**Partial-accept: build substitutes for targets/touches/snap%, drop routes, defer/drop red-zone
touches.**

1. **Targets, touches, air_yards — build, low effort.** Swap Sleeper's per-week stats endpoint for
   nflverse's `player_stats_{season}.csv` (already fetched by `matchup_tools.py`, already
   CI-asserted). No new infrastructure — this is the same file, same fetch pattern, already
   proven reliable in this codebase. The rolling-3-week-average and trend logic
   (`get_usage_last_n_weeks`/`_calculate_usage_trend`) needs no changes; it just needs its input
   rows populated from a new source instead of Sleeper.

2. **Snap %/snap share — build, medium effort.** Add a fetch for nflverse's
   `snap_counts_{season}.csv` (a new file, but the same "download a public nflverse CSV" pattern
   this repo already has twice) and join it to player identity via nflverse's `players.csv`
   crosswalk (`pfr_id` ↔ `gsis_id`/`espn_id`) — a one-time or cached lookup table, not a live
   per-request join. This is more than a field rename (new file, new join key) but is bounded,
   well-understood work with no new subsystem (no poller, no new scheduler, unlike issue #25's
   trending-players gap) — closer to "one more CSV in the same shape as the ones already handled"
   than to "build new infrastructure."

3. **Red-zone touches — drop, or scope separately if truly wanted.** No pre-aggregated source
   exists anywhere checked (ESPN Fantasy, nflverse `player_stats`, nflverse `snap_counts`,
   nflverse NGS receiving). It's derivable from nflverse's full play-by-play data, but that's a
   meaningfully bigger, separate effort (new ETL over ~45k rows/season of raw play data, not a
   column read) that shouldn't block shipping the targets/touches/snap% substitutes above. If
   red-zone touches are considered a must-have, it should be tracked as its own follow-on ticket
   rather than bundled into this cutover.

4. **Routes run — drop; no substitute exists.** Neither ESPN nor nflverse's free public surfaces
   publish anything route-shaped, and it isn't derivable from data either source does publish.
   This is a genuine feature loss, structurally identical to issue #25's finding for Sleeper's
   trending-velocity signal. The consuming code already treats `routes`/`routes_avg`/the routes
   component of `usage_trend` as optional (`_enrich_usage_and_opponent` only sets these fields
   `if not None`, and `usage_trend_overall` already falls back to `targets_trend or snap_trend or
   routes_trend` when routes data is absent) — so dropping it degrades gracefully rather than
   requiring new null-handling code.

Net effect: three of AGENT.md's four documented "Advanced Enrichment" usage metrics (targets,
snap%, and the touches/air_yards it's built from) survive the Sleeper-to-ESPN/nflverse cutover
with bounded, well-scoped work; the fourth (routes) is a permanent loss with no available
substitute; red-zone touches is a judgment call best deferred to its own ticket given the size
mismatch between it and the other three fields.
