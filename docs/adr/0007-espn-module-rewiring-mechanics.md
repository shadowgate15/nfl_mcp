# Module-rewiring mechanics for the Sleeper-to-ESPN cutover

Every downstream module still calling Sleeper's tool surface is re-pointed at the equivalent
`get_espn_*` tool, following one recurring convention established across this pass:
Sleeper-era `roster_id`/`my_roster_id`/`opponent_roster_id` parameters and fields rename to
`team_id`/`opponent_team_id` (ESPN's `mTeam.id`), a breaking change applied with no back-compat
shim, matching `CONTEXT.md`'s **Team** entry. The rewiring is otherwise scoped module-by-module,
since each pairing has its own shape differences to absorb.

`draft_tools.py`'s `get_draft_board` and `simulate_draft` have no Sleeper dependency and are
unaffected. `recommend_draft_pick(draft_id, ...)` becomes `recommend_draft_pick(league_id,
year=None, ...)`, since ESPN has no standalone draft id — a draft is a sub-resource of league and
season. It sources data from two calls, `get_espn_draft` (picks) plus a settings call (`mSettings`)
for `slots_*`/`teams`/scoring-type, mirroring the two-call pattern `get_espn_transactions` already
uses for `scoringPeriodId` resolution; a call-site adapter translates ESPN's `draftDetail.picks[]`
shape into the Sleeper-shaped pick dicts the existing VBD/analysis logic already consumes, leaving
that analysis code untouched. Per-pick enrichment joins directly against the ESPN-keyed player
cache and FantasyCalc values from the player-identity ADR, since both are already keyed on
`espnId`. The docstring's "live" claim is softened to "best-effort live": ESPN's
`draftDetail.inProgress` field suggests live tracking exists, but update cadence during an active
draft was never independently verified, and is left as a note for the build rather than a blocking
question. `waiver_tools.py`'s three tools swap their Sleeper `get_transactions(league_id,
round=None, week=None)` call for `get_espn_transactions(league_id, week=week,
types=["WAIVER","FREEAGENT"], year=year)`, adopting ESPN's native transaction-type vocabulary
wholesale rather than a Sleeper-vocabulary translation shim, consistent with the cutover's
hard-cutover stance. `WaiverAnalyzer._extract_waiver_transactions` gets a call-site adapter folding
ESPN's per-transaction `items[]` shape into the same flat `adds`/`drops` dict the dedup/re-entry
logic already expects, so that logic needs no changes. Sleeper's `waiver_budget` transfer list has
no ESPN equivalent and is dropped rather than translated, since nothing in scope reads it today.

`handcuff_tools.py` and `streaming_tools.py` swap their `sleeper_tools.get_rosters` calls for
`get_espn_rosters`. `get_handcuff_map`'s `roster_id` parameter renames to `team_id`. The
`rostered: dict[str, int]` matching logic in both modules is structurally unchanged — only the id
source changes — but building it requires walking `roster.entries[]`, which nests the player
object two different ways depending on endpoint (`entry["playerPoolEntry"]["player"]` or a bare
`entry["player"]`); both modules use one new shared extraction helper rather than duplicating that
parsing twice. `streaming_tools.py`'s DST special case — matching a team abbreviation directly as a
pseudo-player-id, a Sleeper convention — is deleted; DST lookups instead go through
`db.get_athletes_by_team(team)` filtered to `position == "DST"`, the same path every other position
already uses, once the player-identity cache's position map covers `defaultPositionId 16 →
"DST"`. `db.get_athletes_by_ids`/`db.get_athletes_by_team` call sites are otherwise unchanged in
both modules, since the `proTeamId`-to-abbreviation join happens once, at cache-population time.

`faab_tools.py` and `trade_analyzer_tools.py` share a rewired `league_format_from_settings`: PPR
reads from `settings.scoringSettings.scoringItems` where `statId == 53` ("Each reception"),
falling back to full PPR (`1.0`) only if that statId is absent; `num_qbs`/`superflex` derive from
`settings.rosterSettings.lineupSlotCounts` against ESPN's confirmed slot-id map; `num_teams` reads
`settings.size` as a direct swap for Sleeper's `total_rosters`; and `is_dynasty` collapses to
`keeperCount > 0`, since ESPN has no field anywhere distinguishing redraft, keeper, and dynasty the
way Sleeper's three-way `settings.type` enum did — a permanent loss of that finer distinction,
confirmed as ESPN's actual ceiling rather than a stopgap. FAAB budget fields (the term itself stays
this repo's tool-facing vocabulary per `CONTEXT.md`'s **FAAB** entry) swap to ESPN's
`settings.acquisitionSettings.isUsingAcquisitionBudget`/`acquisitionBudget` and each team's
`transactionCounter.acquisitionBudgetSpent`. A new shared helper, `enrich_roster_entries(entries,
player_cache)`, sits in `espn_fantasy_tools.py` layered on the roster-entry extraction helper from
the handcuff/streaming rewiring, joining raw ESPN players against the player cache and position map
to reconstruct the enriched player lists both `faab_tools.py` (marginal-upgrade calculations) and
`trade_analyzer_tools.py` (positional-needs calculations) need. `trade_analyzer_tools.py`'s
starter/bench split is fully preserved rather than degraded: a roster entry counts as a starter iff
its `lineupSlotId` is not `20` (bench) or `21` (IR), ESPN's confirmed slot ids.

`opponent_analysis_tools.py` and `playoff_tools.py` share a new `resolve_team_owner_names(rosters,
members)` helper in `espn_fantasy_tools.py`, the one canonical implementation of the owners-id-to-
members-id join the player-identity ADR scoped, replacing each module's own
`sleeper_tools.get_league_users` call and manual lookup dict; only the resolved name is surfaced
(`opponent_name`, `odds[].name`), since neither module consumes co-manager lists today. Both
modules swap `sleeper_tools.get_rosters` for `get_espn_rosters` directly.
`playoff_tools.py`'s season-aggregate reads move from Sleeper's split
`wins`/`losses`/`ties`/`fpts`/`fpts_decimal` fields to ESPN's `team.record.overall` fields, where
`pointsFor` is already one float needing no decimal-split reconstruction; its league-settings reads
move from `sleeper_tools.get_league` to `get_espn_league`, with `playoff_teams` becoming
`settings.scheduleSettings.playoffTeamCount` and `playoff_week_start` becoming
`scheduleSettings.matchupPeriodCount + 1`. `opponent_analysis_tools.py`'s richer per-player needs
(`snap_pct`, `practice_status`, `usage_trend_overall`), beyond what `enrich_roster_entries` covers,
are met by relocating `_enrich_usage_and_opponent` out of the doomed `sleeper_enrichment.py` — the
function's DB lookups are already `player_id`-keyed and id-scheme-agnostic, so the relocation
carries no logic change. Both modules also swap their matchup reads to `get_espn_matchups`;
`playoff_tools.py`'s `_build_remaining_schedule` drops its by-`matchup_id` grouping entirely, since
ESPN's `schedule[]` is already home/away-paired, and simply extracts `(home["teamId"],
away["teamId"])` tuples per entry, skipping any entry missing either key for a bye week.
`opponent_analysis_tools.py`'s `matchup_context.projected_points` degrades to `None` outside
in-progress weeks, an accepted gap: ESPN's nearest equivalent, `totalProjectedPointsLive`, only
exists once a week is underway, with nothing matching Sleeper's always-present `custom_points`.
Both modules' `get_nfl_state()` calls (each only ever reading `.week`) collapse onto one shared
`get_current_nfl_week()` coroutine interface, whose concrete module home is decided below.

A new module, `nfl_enrichment.py`, owns the ESPN-core-only leaf helpers that survive the cutover,
mirroring the existing `sleeper_tools.py`/`sleeper_enrichment.py` public-surface/leaf-helper split
(`nfl_tools.py` is the public-surface analog; this is the leaf-helper analog — internal plumbing
other modules import directly, never registered as MCP tools). It absorbs `_fetch_week_schedule`
and `_fetch_all_team_schedules` unchanged, and the relocated `_enrich_usage_and_opponent`
described above. It also gains the new `get_current_nfl_week() -> int | None` coroutine, which
calls ESPN-core's parameterless scoreboard endpoint and reads `week.number`, replacing both
`faab_tools.py`'s and `playoff_tools.py`'s `get_nfl_state()` calls with a single shared
implementation rather than two copies. `sos_tools.py` and `weather_tools.py` re-point their
`sleeper_tools._fetch_week_schedule` calls at `nfl_enrichment`; `matchup_tools.py` was confirmed to
have zero Sleeper dependency of its own and needs no changes. `nfl_tools.py`'s
`get_current_season_and_week()` is deleted outright rather than migrated, since its only caller —
Sleeper's own `get_trending_players` — is itself deleted under the hard cutover, making it dead
code rather than a migration target.

Two more leaf helpers, `_fetch_injuries()` and `_fetch_practice_reports(season, week)`, remained in
`sleeper_enrichment.py` unaddressed by any of the above and needed their own disposition.
`_fetch_injuries()` is not a relocation candidate: `injury_service.py`'s
`InjuryAggregator.fetch_espn_injuries()` already hits the identical ESPN-core injuries endpoint and
duplicates the same pagination/detail-walk logic, but with concurrent fetching, ETag/delta caching,
and status-certainty-based confidence scoring that `_fetch_injuries()` lacks (it hard-codes a flat
confidence of 60 — precisely the gap `injury_service.py`'s own history fixed). `injury_service.py`
is also the live implementation behind three existing MCP tools, and both write to the same
`player_injuries` table. `_fetch_injuries()` is deleted, and `server.py`'s prefetch loop is
repointed at `injury_service.get_injury_reports(db=nfl_db, ...)` directly.
`_fetch_practice_reports()` has no such supersession — no practice-status concept exists in
`injury_service.py` — so it relocates into `nfl_enrichment.py` as a genuine migration, rewired to
source injury data from `injury_service.get_injury_reports()` in place of the now-deleted
`_fetch_injuries()`. Its DNP/LP/FP derivation logic duplicates a second copy already present in
`_enrich_usage_and_opponent`'s fallback branch — flagged as a non-blocking follow-on cleanup once
both land in the same module, not resolved here.

## Considered Options

- **A Sleeper-vocabulary translation shim** for `waiver_tools.py`'s transaction types, preserving
  Sleeper's enum values at the tool boundary. Rejected: the cutover is a hard cutover with no
  fallback source, so there's no reason to keep translating to a vocabulary nothing downstream
  still speaks; ESPN's native transaction-type strings are used directly.
- **Keeping `streaming_tools.py`'s DST special case** (matching a team abbreviation as a
  pseudo-player-id) instead of unifying it into the position-filtered lookup every other position
  uses. Rejected: it was a Sleeper-specific convention with no ESPN equivalent, and unifying it
  removes a special case rather than reimplementing one against a different id scheme.
- **Relocating `_fetch_injuries()` into `nfl_enrichment.py`** alongside its sibling leaf helpers,
  by default/consistency with `_fetch_practice_reports()`. Rejected once `injury_service.py` was
  confirmed to already supersede it with real correctness and performance improvements — relocating
  a strictly worse duplicate would be a regression, not a migration.
- **A bundled `get_draft_board`-style multi-view call** for `recommend_draft_pick`'s settings needs,
  instead of a second dedicated settings call. Not pursued: the two-call pattern already has a
  precedent in `get_espn_transactions`'s `scoringPeriodId` resolution, and no independent ADR was
  judged necessary for that specific choice since it introduces no new trade-off beyond that
  precedent.
