# ESPN Fantasy Tool-Response Payload Size: Findings and Trimming Strategy

## Purpose and scope

Resolves GitHub issue #22 (child of the wayfinder map, issue #21: "Replace Sleeper with ESPN
Fantasy as the sole fantasy data source"). Issue #21 flagged tool-output size as a first-class
migration risk: ESPN's fantasy payloads are known for deeply nested multi-`view` envelopes, full
box-score/lineup detail, and a full ~1700-player pro-player pool — all much heavier than Sleeper's
ID-array-based equivalents. This document measures/estimates that gap for all 10 `get_espn_*`
tools against their Sleeper equivalents, identifies the biggest offenders with source-grounded
reasoning, surveys this repo's existing size/pagination conventions, and proposes a concrete
trimming strategy with a recommended size budget.

**Method and confidence tier.** No live ESPN API access is available in this environment
(`ESPN_S2`/`ESPN_SWID` are unset; live calls were not attempted, per instructions). Estimates
below are grounded in three inputs, cited per claim:
1. `nfl_mcp/espn_fantasy_tools.py` — the actual `view=` params, headers, and client-side
   processing each tool performs (source of truth for *what this repo's code does*, not what ESPN
   could theoretically return).
2. `docs/ESPN_FANTASY_ENDPOINT_CATALOG.md` — a prior research doc (resolves issue #4) giving
   field-level response shapes per view, sourced from `cwendt94/espn-api`'s code/fixtures and live
   probes run 2026-09-07. Cited by its section numbers (`§N`) below.
3. `tests/test_espn_fantasy_tools.py` — fixture payloads used in this repo's own tests. These are
   deliberately minimal (2-3 fields, 1-2 array entries) to keep unit tests focused, so they
   validate *shape*, not *scale* — they are not used below as size proxies, only as shape
   confirmation alongside the catalog.

Byte estimates are reasoned from field counts × array cardinality, not measured against a live
payload — every number below is explicitly a **reasoned estimate**, not a measurement. Token
counts use the common ~4 characters/token heuristic on the estimated JSON string length; this is
an approximation (real tokenizers vary, especially on numeric-heavy JSON) and is noted as such
throughout.

---

## 1. Per-tool size table

Cardinality assumptions used throughout (stated once here, applied per tool below): 10-16 teams
per league, 15-25 roster slots per team, 14-18 regular-season weeks, ~1700 active NFL players
league-pool-wide (per the issue's own framing).

| # | ESPN tool | Sleeper equivalent | Typical size (est.) | Worst case (est.) | Rough tokens (typical / worst) | Offender rank |
|---|---|---|---|---|---|---|
| 1 | `get_espn_league` | `get_league` (`sleeper_tools.py:105`) | ~5-8 KB | ~15 KB | ~1.5K / ~3.8K | Low |
| 2 | `get_espn_rosters` | `get_rosters` (`sleeper_tools.py:139`) | ~250-400 KB | ~1.6 MB | ~70K / ~410K | **High (#2)** |
| 3 | `get_espn_standings` | *(no dedicated Sleeper tool — see §3 notes)* | ~10-20 KB | ~35 KB | ~3K / ~9K | Low |
| 4 | `get_espn_scoreboard` | `get_matchups` (`sleeper_tools.py:415`, final-score subset) | ~15-40 KB | ~90 KB | ~5K / ~23K | Low-Med |
| 5 | `get_espn_matchups` | `get_matchups` (`sleeper_tools.py:415`, full-detail subset) | ~300-500 KB (1 week) | **~4-8 MB (full season, no `week`)** | ~90K / ~1.5M | **Highest (#1)** |
| 6 | `get_espn_draft` | `get_draft_picks` / `get_draft` (`sleeper_tools.py:868`,`953`) | ~30-50 KB | ~70 KB | ~9K / ~18K | Low |
| 7 | `get_espn_transactions` | `get_transactions` (`sleeper_transactions.py:29`) | ~5-30 KB (1 week) | ~200+ KB (unfiltered/busy week) | ~2K-8K / ~55K | Med |
| 8 | `get_espn_players` | `fetch_all_players` (`sleeper_tools.py:995`) | **~600-900 KB** (always full pool) | ~900 KB | ~170K | **High (#3)** |
| 9 | `get_espn_free_agents` | *(no dedicated Sleeper tool — closest analog is `fetch_all_players` minus rostered IDs, computed client-side; not exposed as its own tool)* | ~400 KB-1 MB | **~2-3 MB (no `limit`; see §2.3)** | ~150K / ~700K | **High (#4)** |
| 10 | `get_espn_player_news` | *(no Sleeper equivalent; closest analog is `nfl_tools.get_nfl_news`)* | ~5-20 KB (with `limit`) | ~100+ KB (no `limit`, ESPN default page size unconfirmed) | ~2K-5K / ~25K | Low-Med |

**Ranking rationale, brief**: `get_espn_matchups` (full season, no `week`) is worst because its
cost scales as *weeks × teams × roster-size*, not just *teams × roster-size* — every week
re-embeds a full box score for every team. `get_espn_rosters` and `get_espn_players`/
`get_espn_free_agents` all scale with *teams × roster-size* or the *entire player pool* once, but
only once, not once per week. Full deep-dives in §2.

---

## 2. Deep-dive: the biggest offenders

### 2.1 `get_espn_matchups` — full box-score/lineup detail (rank #1)

Source: `nfl_mcp/espn_fantasy_tools.py:506-548` (`get_espn_matchups`). Requests
`views=["mMatchup", "mScoreboard"]` via `_fetch_espn_league_view` and returns
`league_data.get("schedule", [])` (line 544), optionally filtered to one `week` client-side via
`_filter_schedule_to_week` (line 199) — **but only if the caller passes `week`**. With no `week`
argument, ESPN returns `schedule` for the *entire season* in one response (confirmed shape:
catalog §3, "81 matchup entries for a 2018 14-team, 17-week-plus-playoffs league").

Per catalog §3, each `schedule[]` entry's `home`/`away` side additionally carries
`rosterForCurrentScoringPeriod.entries` for box-score mode — "the same roster-entry shape as §2"
(full `Player` objects, including nested `stats: [...]` arrays with per-`statId` actual and
projected point breakdowns) — plus `pointsByScoringPeriod` and live-game fields. This means the
*same* team's ~15-25-player roster, each with a full nested stat-line object, is serialized **once
per matchup that team plays**, i.e. once per week. For a 16-team, 17-week league, that is
`17 weeks × 16 teams × ~20 players` ≈ 5,440 full player-with-stats objects in one response, versus
`get_espn_rosters`' one-time `16 teams × ~20 players` ≈ 320. This `weeks×teams` multiplier (not
present in `get_espn_rosters`) is why this tool ranks above rosters/players despite sharing the
same per-player object shape.

The lighter sibling, `get_espn_scoreboard` (`espn_fantasy_tools.py:456-498`), requests only
`mMatchupScore` and returns just `{teamId, totalPoints, tiebreak, adjustment}` per side (catalog
§3) — no roster/player detail at all — which is why it ranks far lower despite sharing the same
`schedule[]`-without-`week`-filter unboundedness. The size gap between these two siblings is
itself the strongest existing evidence in this codebase that view selection is the dominant size
lever, not incidental formatting.

Sleeper's `get_matchups` (`sleeper_tools.py:415-587`) is structurally incapable of this blowup: it
is always scoped to one `week` (a required, `LIMITS["week_min"]`/`LIMITS["week_max"]`-validated
parameter, line 419), and its roster contents are bare player-ID arrays
(`m.get("players")`/`m.get("starters")`, lines 501-502) enriched with only
`{player_id, full_name, position, ...}` (lines 508-524) — no per-player stat-line arrays at all.

### 2.2 `get_espn_rosters` (rank #2)

Source: `nfl_mcp/espn_fantasy_tools.py:350-392`. Requests `views=["mRoster", "mTeam"]` and returns
`league_data.get("teams", [])` unfiltered (line 392) — every team, every roster slot, in one call,
with no `limit`/pagination parameter at all in the tool's signature (only `week`/`year`).

Per catalog §2, each `teams[]` entry carries team metadata (record, transactionCounter, etc.) plus
`roster.entries[]`, one per roster slot (15-25 depending on league format), and each entry holds a
full `Player` object: `id, fullName, firstName, lastName, defaultPositionId, eligibleSlots: [int],
proTeamId, injured, injuryStatus, jersey, active, droppable, ownership: {...}, stats: [{seasonId,
scoringPeriodId, statSourceId, statSplitTypeId, stats: {...}, appliedStats: {...}, appliedTotal,
appliedAverage}, ...]`. The `stats[]` array alone can hold multiple entries per player (actual
*and* projected, `statSourceId == 0` vs. non-zero per catalog §2) each containing a
`{<statId>: float}` map of roughly 15-30 keys. This nested stats-per-player structure, repeated
across up to 16 teams × 25 slots (~400 players), is what pushes this tool's worst case toward
1.5-1.6 MB even though it only fetches the *current* roster state once (no week-loop multiplier
the way `get_espn_matchups` has).

Sleeper's `get_rosters` (`sleeper_tools.py:139-372`) returns the same *cardinality* (one entry per
team) but each roster's `players`/`starters` fields are bare Sleeper player-ID strings; the
enrichment step (`enrich_players`, lines 278-305) attaches only `{player_id, full_name, position,
...injury/usage fields}` — no per-week stat-line arrays, no `stats[]`. This is the core structural
gap driving the whole migration's size concern: ESPN embeds full player objects inline wherever
Sleeper would embed a bare ID.

### 2.3 `get_espn_players` — full pro-player pool (rank #3)

Source: `nfl_mcp/espn_fantasy_tools.py:249-287`. Hits the separate `/players` endpoint with
`view=players_wl` and an `x-fantasy-filter: {"filterActive": {"value": true}}` header, returning
**every active player in the ESPN player pool in one response**, with no `limit`, no pagination,
and no field allowlist (line 285: `players = data if isinstance(data, list) else data.get(...)`,
returned as-is). Per catalog §7b this is a bare JSON array, one object per player, at whatever
field set `players_wl` returns (bio/identity fields at minimum: `id, fullName, defaultPositionId,
proTeamId, ...`; the catalog's live probe did not enumerate the full `players_wl` field list, so
the "bio-only, no stats" assumption behind the ~600-900 KB typical estimate carries a real
uncertainty band — if `players_wl` includes even a slimmed `stats[]` array per player, typical
size could be materially higher). At ~1700 active players (the issue's own cardinality framing),
even a lean ~350-500 bytes/player bio-only object totals ~600 KB-900 KB, and this size is
**unconditional** — every call returns the same full pool regardless of what a caller actually
needs, unlike every other tool in this table where size at least varies with league/week
selection.

Sleeper's directly analogous tool, `fetch_all_players` (`sleeper_tools.py:991-1025`), is the
single most relevant piece of prior art in this whole comparison: it explicitly **does not return
the ~5MB raw blob it fetches**. Its own docstring/comment says so outright
(`sleeper_tools.py:987`: `"Player dump caching (large ~5MB) - cache in memory to reduce calls."`),
and the success response deliberately substitutes an empty dict for the real payload
(`sleeper_tools.py:1007,1021`: `"players": {},  # not returning the large blob again intentionally`
/ `"players": {},  # avoid huge payload downstream; signal success"`), surfacing only
`player_count`/`cached`/`ttl_remaining` metadata to the caller. **This is a direct, in-repo
precedent for "never let a full player-pool response reach the LLM tool-call boundary,"** and
`get_espn_players` currently does exactly what this precedent was built to avoid.

### 2.4 `get_espn_free_agents` (rank #4)

Source: `nfl_mcp/espn_fantasy_tools.py:297-342`. Requests `view=kona_player_info` with an
`x-fantasy-filter: {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS"]}}}` header
(line 294) and returns `data.get("players", [])` unfiltered (line 342) — **no `limit`/`size`
filter is ever added to this header or to `extra_params`**, unlike the reference client
`cwendt94/espn-api`'s `free_agents()`, which the catalog (§7a) confirms takes an explicit `limit`
parameter as part of its own `x-fantasy-filter` construction. This repo's implementation omits
that entirely, meaning **the number of free agents returned per call is bounded only by however
many players league-wide currently have FREEAGENT/WAIVERS status and whatever undocumented default
ESPN's server applies with no `limit` in the filter** — an unconfirmed, potentially unbounded
quantity. In a typical 10-16 team league with ~200-400 rostered players out of a ~1700-player
pool, 1300-1500 players could plausibly match the free-agent/waiver filter.

Per catalog §7a, each entry in this response is *heavier* per-player than the `/players` pool
entry in §2.3: `{draftAuctionValue, id, keeperValue, keeperValueFuture, lineupLocked, onTeamId,
ratings, rosterLocked, status, tradeLocked, player: {<full Player fields from §2, i.e. including
stats[]>, plus draftRanksByRankType, lastNewsDate, lastVideoDate, rankings, seasonOutlook,
laterality, stance}}` — i.e. this view carries the same nested `stats[]` array as
`get_espn_rosters` *and* several additional ranking/outlook fields, applied across a potentially
much larger player count than a single league's rosters. This combination (rosters-style per-player
weight × pool-scale cardinality × no size cap in this repo's request) is why it ranks as a top-tier
offender and, unlike `get_espn_players`, its worst case is genuinely open-ended rather than a
fixed ~1700-player ceiling.

---

## 3. Existing repo conventions

### 3.1 `response_validation.py` — not a size-limiting module

Full read of `nfl_mcp/response_validation.py`: this module provides **structural/quality
validation**, not size limiting. `ValidationResult` (lines 14-42) tracks pass/fail plus
warnings; `validate_snap_count_response`, `validate_schedule_response`,
`validate_practice_report_response`, `validate_usage_stats_response` (lines 45-273) each **sample**
only the first 10-20 items of a list/dict (e.g. `sample_size = min(10, len(data))` at line 75;
`games[:20]` at line 140; `reports[:20]` at line 187; `stats[:20]` at line 240) to check for
required-field presence and flag low data-coverage — but this sampling is purely for validation
cost/scope, **the sampled subset is never what gets returned to a caller**; the full, unsampled
`data`/`games`/`reports`/`stats` still flows through untouched. `validate_response_and_log` (lines
275-309) just logs and returns a bool "is this usable" signal. **There is no field allowlisting,
truncation, or byte/token budget enforcement anywhere in this file.**

### 3.2 `param_validator.py` — schema-driven input validation, not output shaping

Full read of `nfl_mcp/param_validator.py`: `validate_params(schema, values)` (lines 28-85) is a
generic parameter-schema validator (type coercion, `required`/`nullable`, `min`/`max` numeric
bounds, `choices` enums, `default` fallback) used throughout `sleeper_tools.py` and
`sleeper_transactions.py` to validate tool **inputs** like `week`, `bracket_type`, `trend_type`,
`lookback_hours`, `limit`. It has no concept of response size or output field shaping at all — it
governs what a caller may *ask for*, not what a tool *returns*.

### 3.3 What the repo DOES already do for output size (the real precedent to build on)

Not a single dedicated "size limiter," but a consistent, repeated pattern of **caller-supplied
`limit` params validated via `LIMITS`/`param_validator`, then applied as a plain Python slice on
the output list**, seen in:
- `get_trending_players` (`sleeper_tools.py:685-861`): `limit` validated against
  `LIMITS["trending_limit_min"/"trending_limit_max"]` (default 25), enforced server-side via the
  Sleeper URL's own `limit` query param.
- `get_league_leaders` (`tool_registry.py:2598-2631`): `limit` validated via `validate_limit(limit,
  1, 100, 25)`, then applied **client-side** regardless of what the underlying call returns:
  `players = (res.get("players") or [])[:limit]` (line 2631).
- `get_espn_player_news` itself (`espn_fantasy_tools.py:731-732`): `feed = feed[:limit]`,
  explicitly enforced client-side "even if ESPN's server ignores the query param" (per its own
  docstring, lines 619-621 referencing this same reasoning for `get_espn_transactions`'s `types`
  filter).
- `cbs_fantasy_tools.py:86`: `for container in news_containers[:limit]`.
- `fetch_all_players` (`sleeper_tools.py:991-1025`): the strongest precedent — **never return the
  large blob at all**, return count/cache metadata instead (§2.3 above).

**Synthesis**: this repo's established idiom for large-collection tools is "validate a `limit`
param via `LIMITS`/`param_validator`, then truncate the returned list to it client-side as a
belt-and-suspenders guarantee regardless of server-side behavior" — plus, for the single
largest-imaginable payload (`fetch_all_players`), "don't return the payload at all, return a
summary." Any ESPN trimming strategy should extend this exact idiom rather than introduce field
allowlisting, cursors, or a new `detail`/`view` flag as a totally novel pattern — though a `detail`
flag is still worth proposing below, since none of the existing `limit`-based precedents handle
"same collection, fewer fields per item," only "same fields, fewer items."

---

## 4. Proposed trimming/size-limiting strategy

### 4.1 Recommended size budget

**Target: ≤50 KB (≈12-15K tokens) typical, hard cap 150 KB (≈35-40K tokens) worst case, per tool
response.** Rationale: this is a common single-tool-call budget heuristic for agentic LLM
workflows — keeping any one tool result to roughly 1-3% of a 128K-token context window leaves room
for multiple tool calls, conversation history, and the model's own reasoning within a single turn,
without one oversized call crowding out everything else. `get_espn_league`, `get_espn_standings`,
`get_espn_draft`, and `get_espn_scoreboard` already fit this budget as currently implemented (§1).
The other six need the changes below to comply.

### 4.2 Concrete changes, mapped to the existing idiom (§3.3)

1. **Add a `limit`/pagination parameter to every unbounded-collection tool**, validated the same
   way `get_trending_players`/`get_league_leaders` already are (`LIMITS` entry +
   `param_validator.validate_params`, then a client-side slice as the guarantee):
   - `get_espn_players`: add `limit`/`offset` (or a cursor), default `limit` ~200-300 players per
     page. Response adds `"total_players": <full pool count>` and a `"has_more": bool"` flag —
     mirroring the "more available" signal called for in the issue — so a caller can page through
     the full pool deliberately rather than receive it all unconditionally.
   - `get_espn_free_agents`: add the `limit` parameter this tool currently omits entirely (§2.4) —
     both applied to ESPN's own `x-fantasy-filter` (to reduce what's fetched, not just what's
     returned) and enforced client-side same as `get_espn_player_news`'s `feed[:limit]` pattern.
     Default ~50, matching the reference client's own convention noted in catalog §7a.
   - `get_espn_matchups`/`get_espn_scoreboard`: **make `week` the recommended default entry point**
     (already optional/supported) and, when `week` is omitted, apply a hard server-response cap
     (truncate `schedule` to the most recent N weeks, or require an explicit `all_weeks=True` opt-in)
     rather than silently returning the full season — this directly targets the `weeks×teams`
     multiplier identified as the #1 offender in §2.1.
   - `get_espn_transactions`: already scopes to one `scoringPeriodId` by default; additionally cap
     `transactions` length with the same `limit`-and-slice idiom for busy weeks.

2. **Add a `detail` (or `view`) flag for `get_espn_rosters` and `get_espn_matchups`** — the one gap
   the existing `limit`-only idiom doesn't cover, since these two tools' size problem is
   *per-item field weight* (full nested `Player.stats[]`, `ownership`, `ratings`, etc.), not just
   item *count*:
   - `detail="summary"` (proposed default): strip each roster/box-score entry down to an
     allowlist — `{playerId, fullName, position/defaultPositionId, proTeamId, lineupSlotId,
     injuryStatus, appliedTotal or totalPoints}` — dropping the full `stats[]` per-statId map,
     `ownership`, `ratings`, `rankings`, `draftRanksByRankType`, etc.
   - `detail="full"`: today's unfiltered passthrough, opt-in only, still subject to the `limit`
     changes above.
   - This is a field allowlist, the one piece of the issue's suggested toolkit with no existing
     in-repo precedent (§3.3) — introduce it narrowly, scoped to these two tools' nested player
     objects specifically, rather than as a repo-wide new abstraction.

3. **Apply `fetch_all_players`'s "don't return the blob" precedent nowhere else as a *default*,
   but keep it available as an explicit escape hatch**: unlike Sleeper's player pool (which every
   downstream tool needs cached locally for ID→name lookups, justifying the aggressive
   cache-and-withhold approach), ESPN's `get_espn_players`/`get_espn_free_agents` responses are the
   *direct* useful payload a caller wants back, not an internal cache-warming step — so full
   withholding isn't the right default here. Pagination (item 1) is the better fit; full
   withholding remains a documented fallback only for a hypothetical future "warm the local
   player-identity cache" tool, not for these two.

4. **Integration point**: implement the `limit`-and-slice and `detail`-allowlist logic as small,
   tool-local helpers inside `espn_fantasy_tools.py` (mirroring `_filter_schedule_to_week` and
   `_standings_sort_key`'s existing pattern of small private module-level functions next to the
   tool that uses them), reusing `LIMITS`/`param_validator.validate_params` for the new `limit`
   params exactly as `sleeper_tools.py` already does — no new validation framework needed.

5. **Surface truncation/pagination state explicitly in the response**, consistent with
   `get_espn_transactions`'s existing `total_transactions` count and `get_espn_player_news`'s
   `total_news` count: add `total_<collection>` (full/matched count) alongside the possibly-shorter
   returned list, plus `has_more`/`next_offset` where pagination applies, so a caller (human or
   LLM) can tell truncation happened rather than mistaking a partial result for the complete one.

### 4.3 Summary of what changes per tool

| Tool | Change |
|---|---|
| `get_espn_rosters` | Add `detail` flag (default `summary`); allowlist fields when summary |
| `get_espn_matchups` | Add `detail` flag; cap weeks returned when `week` omitted; `has_more`/count signal |
| `get_espn_players` | Add `limit`/`offset` pagination; `total_players`/`has_more` |
| `get_espn_free_agents` | Add `limit` (currently missing entirely); apply to `x-fantasy-filter` + client-side slice |
| `get_espn_scoreboard` | Cap weeks returned when `week` omitted (lighter than matchups, lower priority) |
| `get_espn_transactions` | Cap `transactions` list length via existing `limit`-and-slice idiom |
| `get_espn_league`, `get_espn_standings`, `get_espn_draft`, `get_espn_player_news` | No change needed; already within budget (player_news already has `limit`, just needs a sane default when omitted) |

---

## Sources

- `nfl_mcp/espn_fantasy_tools.py` (full read) — every ESPN tool's `view=`/header/slicing logic
  cited by line number above.
- `nfl_mcp/sleeper_tools.py` (full read) — every Sleeper equivalent's logic, including
  `fetch_all_players`'s withholding precedent (lines 987, 1007, 1021).
- `nfl_mcp/sleeper_transactions.py` (read for `get_transactions`/`get_traded_picks`).
- `nfl_mcp/response_validation.py` (full read) — confirmed no size-limiting exists; sampling is
  validation-scoped only.
- `nfl_mcp/param_validator.py` (full read) — confirmed input-validation-only scope.
- `nfl_mcp/tool_registry.py` (`get_league_leaders`, lines 2598-2631) — client-side `limit`-slice
  precedent.
- `nfl_mcp/cbs_fantasy_tools.py:86` — another `limit`-slice precedent.
- `nfl_mcp/config.py` (`LIMITS`/`_get_limits`, lines 485-507; `create_http_client`/
  `get_http_headers`, lines 322+) — confirmed no HTTP-layer size capping exists.
- `docs/ESPN_FANTASY_ENDPOINT_CATALOG.md` §§1-9 — ESPN response shapes/cardinality per view,
  cited by section number throughout §§1-2.
- `tests/test_espn_fantasy_tools.py` (full read) — shape confirmation only, fixtures are
  intentionally minimal and not used as size proxies.
- GitHub issue #21 (`gh issue view 21`) — wayfinder map context, cardinality framing
  ("~10-16 teams", "~1700 active NFL players").
- GitHub issue #22 (`gh issue view 22`) — this ticket's original ask.

---

*Document created: September 8, 2026*
*NFL MCP Server Research Initiative — resolves GitHub issue #22 (child of #21)*
