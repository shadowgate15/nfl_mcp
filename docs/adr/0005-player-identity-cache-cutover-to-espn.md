# Player-identity cache cutover to ESPN

`athlete_tools.py`'s player-identity cache (names, teams, positions — consumed by
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

Every other `player_id`-keyed table in `database.py` was checked: `player_week_stats`,
`player_usage_stats`, and `player_practice_status` have no current callers (dead/future-use, no
migration needed); `player_injuries`/`injury_history` are populated today by `injury_service.py`
but already key on ESPN's own athlete id (extracted from ESPN's `/athletes/{id}` injury-detail
URL), not Sleeper's — that system already anticipated this cutover.

`player_values.py` (FantasyCalc integration, feeding `draft_tools.py`, `faab_tools.py`,
`trade_analyzer_tools.py`) is the one real dependency: it documents Sleeper id as its *primary*
join key against FantasyCalc's response, with name+position matching only as a fallback. A live
probe of `api.fantasycalc.com/values/current` showed every player object already carries an
`espnId` field alongside `sleeperId` (e.g. `{"sleeperId":"9221","espnId":"4429795",...}`), so
`player_values.py` swaps its primary join key from `sleeperId` to `espnId` — a like-for-like
exact-id swap, not a demotion to fuzzy matching.

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
