# Player identity, owner resolution, and accepted Sleeper-feature gaps in the ESPN cutover

`athlete_tools.py`'s Player-identity cache (names, teams, positions — consumed by
`handcuff_tools.py` and `streaming_tools.py`) moves from Sleeper's `/v1/players/nfl` to ESPN
Fantasy's `get_espn_players` (the full pro-player pool, no cookies required), re-keyed from
Sleeper player ids to ESPN player ids, as part of the broader Sleeper-to-ESPN cutover (map
issue #21). Team resolution (`proTeamId` → abbreviation) needs no new mapping table: live probes
confirmed `proTeamId` is the same numeric id space as the `teams` table this repo already
populates from ESPN-core (`nfl_tools.py`'s `fetch_teams`), so it's a join against existing,
independently-refreshed data. Position resolution (`defaultPositionId` → abbreviation) has no
existing table to join against, so it gets a small static map, same pattern as
`coaching_tools.py`'s `TEAM_ID_MAP`. The `PREFETCH_ATHLETES`/interval refresh mechanism carries
over unchanged — nothing suggests ESPN's pool is more volatile or rate-limited than Sleeper's.

Every other `player_id`-keyed table in `database.py` was checked: `player_practice_status` has no
current callers (dead/future-use, no migration needed); `player_injuries`/`injury_history` are
populated today by `injury_service.py` but already key on ESPN's own athlete id (extracted from
ESPN's `/athletes/{id}` injury-detail URL), not Sleeper's — that system already anticipated this
cutover. **Correction (surfaced by issue #31):** `player_week_stats` and `player_usage_stats` are
*not* dead — `server.py`'s prefetch loop actively writes both, and
`sleeper_enrichment._enrich_usage_and_opponent` actively reads them across four call sites in
`sleeper_tools.py`/`sleeper_transactions.py`. Both need re-keying from Sleeper to ESPN player ids
like every other table in this cache, not a no-op.

`player_values.py` (FantasyCalc integration, feeding `draft_tools.py`, `faab_tools.py`,
`trade_analyzer_tools.py`) is the one real dependency: it documents Sleeper id as its *primary*
join key against FantasyCalc's response, with name+position matching only as a fallback. A live
probe of `api.fantasycalc.com/values/current` showed every player object already carries an
`espnId` field alongside `sleeperId` (e.g. `{"sleeperId":"9221","espnId":"4429795",...}`), so
`player_values.py` swaps its primary join key from `sleeperId` to `espnId` — a like-for-like
exact-id swap, not a demotion to fuzzy matching.

## Owner and member resolution

ESPN's `mTeam` view — already fetched by `get_espn_rosters`/`get_espn_league` — does not carry
resolved Owner identity inline: each `teams[]` entry only has `owners: [memberId, ...]` and
`primaryOwner: memberId`, opaque member-id strings. The human-readable identity (`displayName`,
`firstName`, `lastName`) lives in a separate top-level `members[]` array in the same league
response, requiring a client-side join exactly like `cwendt94/espn-api`'s own `_fetch_teams` does
it. `get_espn_rosters` already requests this response but discards `members` today, so the fix is
a one-line addition to its return shape (`"members": league_data.get("members", [])`) rather than
a new tool or an extra HTTP round-trip; `get_espn_league` only requests `mSettings` and never sees
`members` at all, so it stays out of scope for this join. The owners-id-to-members-id join itself
is implemented once, as a shared helper consumed by the rewired downstream modules (see the
module-rewiring mechanics ADR). Compared to Sleeper, ESPN has no avatar equivalent and no explicit
commissioner/`is_owner` flag on `members[]` — neither is used by today's consumers, which only
ever resolve an owner id to a display name. Conversely, ESPN models co-managers more naturally:
`owners` is natively a list, where Sleeper's `roster.owner_id` was a single id. `CONTEXT.md` gains
an **Owner** entry, canonicalizing ESPN's own `owners`/`primaryOwner` vocabulary over Sleeper's
"user"/"league user" concept.

## Trending players and demand-signal gaps

No ESPN equivalent exists to Sleeper's `GET /v1/players/nfl/trending/{trend_type}`, a cross-league,
server-computed adds/drops *velocity* signal. ESPN's closest adjacent data is
`ownership.percentOwned`/`percentStarted` on the Player object (surfaced via `get_espn_players`/
`get_espn_free_agents`) — a point-in-time ownership *snapshot*, not a change-over-time metric — and
`get_espn_transactions` is strictly single-league scoped with no cross-league aggregate at all.
Building a substitute would require net-new infrastructure (a recurring poller, a snapshot table,
diff/rank logic); no scheduler exists anywhere in this repo today, and the closest schema
precedent (`roster_snapshots`/`transaction_snapshots` in `database.py`) is defined but unwired —
medium-to-high effort for a genuinely new capability, not a config change. The gap is accepted
permanently rather than built: `faab_tools.py`'s demand multiplier, which fed its bid-percentage
calculation directly, freezes `demand_mult` at `1.0` and reports a fixed `demand_label` of
`"unavailable"` (not a fabricated "low"), with `reasoning` stating the gap explicitly. In
`trade_analyzer_tools.py`, whose use of trending data was already informational-only, the
`include_trending` parameter and `is_trending` field are removed entirely rather than left wired to
an always-`False` stub.

## Snap percentage and usage-stat gaps

`sleeper_enrichment.py`'s advanced-enrichment feature (`snap_pct`, `targets_avg`, `routes_avg`,
`rz_touches_avg`, `snap_share_avg`, `usage_trend_overall`, gated by `NFL_MCP_ADVANCED_ENRICH=1`)
partial-accepts across four fields rather than transferring wholesale. Targets, touches, and air
yards carry over at low effort: both ESPN Fantasy's `mRoster` view (statId `58` for targets,
`23`/`41`/`53` for rush attempts/receptions) and nflverse's `player_stats_{season}.csv` — already
fetched by `matchup_tools.py` and asserted by the `nflverse.usage_columns` CI check — carry these
per player per week, with `receiving_air_yards` on the nflverse side requiring no new fetch at all.
Snap percentage and snap share carry over at medium effort: ESPN's fantasy stat taxonomy has no
snap field anywhere, but nflverse's separate `snap_counts_{season}.csv` release
(`offense_snaps`/`offense_pct`) fills the gap — it's keyed by `pfr_player_id` rather than
`gsis_id`/`espn_id`, but nflverse's `players.csv` crosswalk carries all three ids on the same row,
making this a straightforward id-map join rather than fuzzy matching. Red-zone touches are dropped
(or scoped as a separate follow-on ticket if wanted): no source — ESPN, nflverse `player_stats`,
`snap_counts`, or Next Gen Stats receiving — has a pre-aggregated field; it's only derivable from
nflverse's full play-by-play, a meaningfully bigger ETL effort than a column read. Routes run are
dropped permanently with no substitute anywhere, the same permanent-loss shape as the
trending-players gap above; `_enrich_usage_and_opponent` already treats routes as optional, so this
degrades gracefully.

## Schedule and current-week state

`sos_tools.py`/`weather_tools.py`'s weekly-schedule calls and `faab_tools.py`/`playoff_tools.py`'s
current-season/week reads both move to this repo's existing keyless ESPN-core surface
(`site.api.espn.com`) rather than to ESPN Fantasy, for all four modules. `_fetch_week_schedule`
(called via `sleeper_tools.py`, actually defined in `sleeper_enrichment.py`) already hits
ESPN-core's `scoreboard?week=&year=&seasontype=2` endpoint under the hood — zero real dependency on
`api.sleeper.app` despite the module names — so `sos_tools.py`/`weather_tools.py` need only
relocation off the doomed Sleeper-named modules, not a new network call or a response-shape change;
this was independently reconfirmed during the module-rewiring pass that gave `_fetch_week_schedule`
its new home. `faab_tools.py`/`playoff_tools.py` genuinely called Sleeper's `get_nfl_state()`
(`api.sleeper.app/v1/state/nfl`), but only ever read its `week` field — ESPN-core's parameterless
scoreboard endpoint returns `week.number` (plus `season.type`/`season.year` for free) with no
authentication at all, so it replaces that call directly. The net effect is that all four modules
avoid ESPN Fantasy's cookie-gated (`ESPN_S2`/`ESPN_SWID`) auth entirely for what is inherently
league-agnostic NFL data. The one accepted gap: ESPN-core's `season.type` is a 3-value enum
(1=Pre/2=Regular/3=Post) with no equivalent to Sleeper's 4th value, `"off"` (deep offseason) — not
read by any of the four call sites today, so it doesn't block the cutover, but is flagged for any
future season-type-aware logic.

## Considered Options

- **Keep a `sleeper_id` column on the re-keyed cache**, populated by a one-time/refresh-time
  name+position match against Sleeper's player list, solely to keep `player_values.py`'s existing
  `sleeperId` join alive. Rejected once the live FantasyCalc probe showed `espnId` is already in
  the payload — no reason to keep any Sleeper dependency alive, however thin, when an equivalent
  exact-id field already exists on the other side of the join.
- **Hand-maintain a static `proTeamId` → abbreviation map** (mirroring the position-id map).
  Rejected: a live probe cross-referencing ESPN Fantasy's `proTeamId` against this repo's
  ESPN-core `teams` table (by abbreviation, for several known players) confirmed they're the same
  id space, so the existing table already answers this with no new code or data to keep in sync.
- **A brand-new dedicated tool for owner identity**, instead of extending `get_espn_rosters`'s
  return shape. Rejected: strictly more expensive (an extra network call) for no benefit, since
  `members` is already present in the response `get_espn_rosters` fetches.
- **Build a trending-players substitute** (poller + snapshot table + diff/rank logic). Rejected as
  disproportionate effort for a feature that degrades gracefully in one consumer and only partially
  degrades a demand multiplier in the other, with no scheduler infrastructure in this repo to build
  on.
- **Build a red-zone-touches or routes-run substitute.** Rejected: no source publishes either as a
  pre-aggregated field; red-zone touches would require full play-by-play ETL, and routes run has no
  substitute anywhere at any effort level.
- **Route schedule/state reads through ESPN Fantasy instead of ESPN-core.** Rejected: ESPN-core
  already serves everything these four modules read, unauthenticated, so routing through the
  cookie-gated Fantasy surface would add auth risk for a subset of data ESPN-core already provides
  for free.
