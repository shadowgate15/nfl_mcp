# ESPN Fantasy Football API: Endpoint-by-Endpoint Catalog

## Purpose and scope

This document is the child of `docs/ESPN_FANTASY_API_RESEARCH.md` (which established *that*
ESPN's fantasy surface is undocumented, hosted at `lm-api-reads.fantasy.espn.com`, and best
understood via `cwendt94/espn-api`). This document goes one level deeper: an endpoint-by-endpoint
catalog of exact URLs, `view=` params, JSON response shapes, and auth-failure signatures, precise
enough to design Pydantic response models and `@handle_http_errors`-style error handling from.

**Method.** Every claim below is sourced from one or more of:
1. `cwendt94/espn-api`'s source on `master` (fetched fresh via `raw.githubusercontent.com` on
   2026-09-07) — file path + line number cited for every quoted snippet.
2. `cwendt94/espn-api`'s own test fixtures (`tests/football/unit/data/*.json`) — real captured
   ESPN response bodies, fetched fresh, fields verified by direct inspection (not by reading the
   parsing code alone).
3. **Live probes** run directly against `lm-api-reads.fantasy.espn.com` and
   `site.api.espn.com` during this research (2026-09-07, via plain `curl`, no cookies unless
   noted). Two *real, currently-live* public league IDs were found and used throughout:
   `league_id=1234` (season 2018) and `league_id=48153503` (season 2019) — both are
   `cwendt94/espn-api`'s own integration-test league IDs
   (`tests/football/integration/test_league.py`, fetched fresh), confirmed still reachable without
   any credentials as of this research. A confirmed **private** league, `league_id=368876`
   (season 2018) — also the maintainer's own integration-test league, used specifically to
   exercise the "wrong/missing credentials" path (`test_private_league` asserts an `Exception`) —
   was used to capture a live 401 body.
4. GitHub issues on `cwendt94/espn-api`, fetched fresh via `gh issue view --json`, quoted verbatim
   including full tracebacks where present.

Every claim below states which of these four it rests on. Where live probing and source/fixtures
agree, both are cited, which is the strongest confidence tier in this document.

---

## 1. League settings (`mSettings` view)

**URL + params.** `GET {FANTASY_BASE_ENDPOINT}ffl/seasons/{year}/segments/0/leagues/{league_id}`
with `view=mSettings` (2018+ seasons). Confirmed both in source and live. Source:
`espn_api/requests/espn_requests.py:106-111` (`get_league`, which actually requests
`mTeam,mRoster,mMatchup,mSettings,mStandings` together in one call — ESPN's fantasy API lets
multiple `view` values be requested in a single GET, repeated as `?view=mTeam&view=mRoster&...`).
Live-probe confirmed the multi-`view` call works: `GET .../leagues/1234?view=mTeam&view=mRoster&view=mMatchup&view=mSettings&view=mStandings` → HTTP 200 (probe run 2026-09-07).

**Response shape** (live probe on `league_id=1234`, season 2018, and cross-checked against
`espn_api/base_settings.py` and `espn_api/football/settings.py`'s field access patterns):

Top-level response is a single JSON object (not array) for 2018+ seasons, with a `settings` key:
```
settings: {
  acquisitionSettings: { isUsingAcquisitionBudget: bool, acquisitionBudget: int,
                          acquisitionLimit: int|null, matchupAcquisitionLimit,
                          matchupLimitPerScoringPeriod, minimumBid: int,
                          waiverProcessDays: [str], waiverProcessHour: int }
  draftSettings: { keeperCount: int, ... }
  financeSettings: { ... }
  isAutoReactivate: bool
  isCustomizable: bool
  isPublic: bool
  name: str
  restrictionType: str
  rosterSettings: { lineupSlotCounts: {<slotId str>: int, ...}, ... }
  scheduleSettings: { matchupPeriodCount: int, matchupPeriods: {<periodId>: [scoringPeriodIds]},
                       playoffTeamCount: int, playoffSeedingRule: str,
                       playoffMatchupPeriodLength: int, divisions: [{id, name}, ...] }
  scoringSettings: { matchupTieRule: str, playoffMatchupTieRule: str, scoringType: str,
                      scoringEnhancementType: str|null, scoringItems: [{statId: int,
                      points: float, pointsOverrides: {"16": float}}, ...] }
  size: int
  tradeSettings: { vetoVotesRequired: int, deadlineDate: int(epoch ms)|absent, revisionHours: int }
}
```
Field access confirmed directly against `BaseSettings.__init__`
(`espn_api/base_league.py` imports it; class body is in `espn_api/base_settings.py`, fetched
fresh — every attribute is a literal `data[...]`/`data.get(...)` chain against these exact key
names) and `Settings.__init__` in `espn_api/football/settings.py` (fetched fresh — layers
`scoring_format` from `scoringSettings.scoringItems` and `position_slot_counts` from
`rosterSettings.lineupSlotCounts`, zipped positionally against `POSITION_MAP`
(`espn_api/football/constant.py`) — i.e. **slot-count keys are matched to position labels by
list order, not by the numeric slot ID itself**, a latent fragility worth flagging for our own
model design). Live probe's actual `settings` top-level keys observed:
`['acquisitionSettings', 'draftSettings', 'financeSettings', 'isAutoReactivate',
'isCustomizable', 'isPublic', 'name', 'restrictionType', 'rosterSettings', 'scheduleSettings',
'scoringSettings', 'size', 'tradeSettings']` — matches the source's expectations exactly, no
drift found.

**Auth-failure signature.** See the top-level summary — `mSettings` shares the same
`checkRequestStatus` path as every other `league_get`-based view (401/404/other, per
`espn_api/requests/espn_requests.py:38-74`); it is not an independently-gated view.

---

## 2. Rosters (`mRoster` / `mTeam` views)

**URL + params.** Same league endpoint, `view=mRoster` (and `mTeam` for team metadata — ESPN
splits "team info" and "roster contents" into separate view flags even though both live under
`data['teams'][n]`). Source: `get_league()` requests both together
(`espn_api/requests/espn_requests.py:106-111`); `League.load_roster_week()`
(`espn_api/football/league.py:497-511`) requests `mRoster` alone with an added
`scoringPeriodId` param to pull a specific week's roster — i.e. **roster contents are
week-scoped**, not just current-state, confirmed directly from this second, narrower call.

**Response shape** (live probe, `league_id=1234`/2018, `team[0]`):
```
teams: [{
  abbrev: str, currentProjectedRank: int, divisionId: int, draftDayProjectedRank: int,
  eliminated: bool, eliminationMatchupPeriod: int, id: int, isActive: bool,
  isTransactionLocked: bool, name: str, owners: [str (member id)],
  playoffSeed: int, points: float, pointsAdjusted: float, pointsDelta: float,
  primaryOwner: str, rankCalculatedFinal: int|null, rankFinal: int|null,
  record: { overall: { wins, losses, ties, pointsFor, pointsAgainst,
                        streakLength, streakType } },
  roster: { appliedStatTotal: float, entries: [ <roster entry>, ... ],
            tradeReservedEntries: [...] },
  transactionCounter: { acquisitions, acquisitionBudgetSpent, drops, trades, moveToIR },
  waiverRank: int
}]
```
Each `roster.entries[]` item (per `Team._fetch_roster` in `espn_api/football/team.py:1074-1080`
and `Player.__init__` in `espn_api/football/player.py:929-1001`, both fetched fresh) is a
"roster entry" object holding `lineupSlotId: int` plus either a nested `playerPoolEntry.player`
or a bare `player` object — **the source explicitly handles both shapes**:
`player = data['playerPoolEntry']['player'] if 'playerPoolEntry' in data else data['player']`
(`espn_api/football/player.py:961`) — meaning different views/endpoints return the player payload
at different nesting depths, and any model we design must tolerate both. The `player` object
itself carries `id, fullName, firstName, lastName, defaultPositionId, eligibleSlots: [int],
proTeamId: int, injured: bool, injuryStatus: str, jersey: str, active: bool, droppable: bool,
ownership: {percentOwned, percentStarted, ...}, stats: [ {seasonId, scoringPeriodId,
statSourceId, statSplitTypeId, stats: {<statId str>: float}, appliedStats: {<statId str>: float},
appliedTotal: float, appliedAverage: float}, ... ]`. `statSourceId == 0` = actual stats,
non-zero = projected (`espn_api/football/player.py:983-991`, comment-verified).

**Auth-failure signature.** Same shared `league_get` path — no roster-specific auth gate found in
source or via probing.

---

## 3. Matchups (`mMatchup` / `mScoreboard` views)

**URL + params.** `view=mMatchup&view=mScoreboard` together for box-score detail
(`League.box_scores`, `espn_api/football/league.py:697-736`, which also adds a
`x-fantasy-filter` header: `{"schedule":{"filterMatchupPeriodIds":{"value":[matchup_period]}}}`
to scope the response to one matchup period rather than the whole season). A lighter
`view=mMatchupScore` alone powers `League.scoreboard()`
(`espn_api/football/league.py:675-695`) — final scores only, no per-player lineup detail.

**Response shape**, `data['schedule']` (live probe, `mMatchup&mScoreboard` on `league_id=1234`;
also directly confirmed against fixture `league_matchupScore_2018.json`, top-level keys
`['draftDetail','gameId','id','schedule','scoringPeriodId','seasonId','segmentId','status']`,
81 matchup entries for a 2018 14-team, 17-week-plus-playoffs league):
```
schedule: [{
  id: int, matchupPeriodId: int, playoffTierType: str ("NONE" for regular season),
  winner: "HOME"|"AWAY"|"TIE"|"UNDECIDED",
  home: { teamId: int, totalPoints: float, tiebreak: float, adjustment: float },
  away: { teamId: int, totalPoints: float, tiebreak: float, adjustment: float }
}]
```
Live probe with the fuller `mMatchup`+`mScoreboard` combo on `league_id=1234` additionally
surfaces `gamesPlayed` and `pointsByScoringPeriod: {<periodId str>: float}` per side — a
week-by-week point breakdown not present in the leaner `mMatchupScore`-only shape, confirming
the two views genuinely differ in payload depth despite both populating `schedule`. For box
scores specifically, each side additionally carries `rosterForCurrentScoringPeriod.entries`
(the same roster-entry shape as §2) plus `totalPointsLive`/`totalProjectedPointsLive` when the
week is in progress — `BoxScore._get_team_data` in `espn_api/football/box_score.py:169-183`
branches on the presence of `totalPointsLive` specifically to detect a live/in-progress week
vs. a settled one. A missing `home` or `away` key means a bye week — `Matchup._fetch_matchup_info`
(`espn_api/football/matchup.py:915-922`) and `BoxScore._get_team_data`
(`espn_api/football/box_score.py:169-171`) both explicitly guard `if team not in data`. **This
guard exists because ESPN really does omit the key** — confirmed by GitHub Issue #527 (see §9 and
the auth-failure summary) where an *older*, less-defensive version of the sibling basketball
client crashed with `KeyError: 'home'` on exactly this case (fixed in `v0.34.2`, per maintainer
`cwendt94`'s comment, 2024-02-21).

**Auth-failure signature.** Shared `league_get` path.

---

## 4. Standings (`mStandings` view / derived, not a dedicated response shape)

**URL + params.** `get_league()` requests `mStandings` as one of its five combined views
(`espn_api/requests/espn_requests.py:106-111`), but **there is no standalone `standings` key in
the JSON response** — confirmed by inspecting the live-probe response's top-level keys
(`['draftDetail','gameId','id','members','schedule','scoringPeriodId','seasonId','segmentId',
'settings','status','teams']` — no `standings` key present even with `mStandings` requested).
`mStandings` instead **augments fields already inside each `teams[]` entry** — `playoffSeed`,
`rankCalculatedFinal`, `rankFinal`, `currentProjectedRank`, `draftDayProjectedRank` (all present
in the §2 shape) are the standings-relevant fields ESPN adds when `mStandings` is included.

**Derivation logic.** `League.standings()` (`espn_api/football/league.py:513-515`) is a pure
client-side sort: `sorted(self.teams, key=lambda x: x.final_standing if x.final_standing != 0
else x.standing, reverse=False)`, where `final_standing = data.get('rankFinal') or
data.get('rankCalculatedFinal')` and `standing = data['playoffSeed']`
(`espn_api/football/team.py:1054-1055`). A separate, much heavier `standings_weekly()`
(`espn_api/football/league.py:517-624`) recomputes a mid-season projected order from raw
win/loss/points-for/points-against arithmetic plus a configurable tiebreaker hierarchy
(`TOTAL_POINTS_SCORED` / `H2H_RECORD` / `INTRA_DIVISION_RECORD`, keyed off
`settings.playoff_seed_tie_rule` from §1) — **this is entirely client-side computation over
already-fetched data, not a distinct ESPN endpoint or response shape.** Anything we build that
wants "standings" needs to either replicate this sort/tiebreaker logic or accept ESPN's
`playoffSeed` ordering as a simpler approximation.

**Auth-failure signature.** N/A as its own endpoint — inherits `mTeam`/`league_get`'s.

---

## 5. Draft results (`mDraftDetail` view)

**URL + params.** `view=mDraftDetail`, via `get_league_draft()`
(`espn_api/requests/espn_requests.py:132-137`).

**Response shape** (live probe, `league_id=1234`/2018, confirmed identical field set against
fixture `league_draft_2015.json`, a pre-2018 array-wrapped fixture — see §9):
```
draftDetail: {
  completeDate: int (epoch ms), drafted: bool, inProgress: bool,
  picks: [{
    autoDraftTypeId: int, bidAmount: int, id: int, keeper: bool, lineupSlotId: int,
    memberId: str, nominatingTeamId: int, overallPickNumber: int,
    owningTeamIds: [int], playerId: int, reservedForKeeper: bool,
    roundId: int, roundPickNumber: int, teamId: int, tradeLocked: bool
  }]
}
```
Live probe on `league_id=1234` returned 128 picks (16 rounds × 8+ teams); fixture
`league_draft_2015.json` returned the identical key set with 128 picks too — no shape drift
found between the pre-2018 and 2018+ formats for this view (source-level confirmation: `BaseLeague._fetch_draft` in `espn_api/base_league.py:53-64` reads `data.get('draftDetail',
{}).get('drafted')`/`.get('picks', [])` uniformly, with no year-conditional branching — the only
place year matters for drafts is the outer envelope, §9). **Undrafted-league edge case**: if
`draftDetail.drafted` is false/absent, `_fetch_draft` returns immediately with an empty
`self.draft` — confirmed directly in source (`espn_api/base_league.py:56-57`), i.e. this is a
normal "no data yet" case, not an error.

**Auth-failure signature.** Shared `league_get` path.

---

## 6. Transactions / waiver reports (`mTransactions2` view)

**URL + params.** `view=mTransactions2` plus a required `scoringPeriodId` param and an
`x-fantasy-filter` header restricting `transactions.filterType` to specific transaction type
strings (e.g. `["WAIVER","WAIVER_ERROR"]` for `get_league_offers`, or a caller-supplied set for
`League.transactions()`, which validates the set against `TRANSACTION_TYPES` from
`espn_api/football/constant.py` and raises a plain `Exception('Invalid transaction type')` if it
contains anything outside that set — client-side validation, not an ESPN-side error). Source:
`espn_api/requests/espn_requests.py:158-169` (`get_league_offers`) and
`espn_api/football/league.py:819-840` (`transactions()`).

**Response shape** (live probe, `league_id=1234`/2018, `scoringPeriodId=3`,
filter=`WAIVER,WAIVER_ERROR`):
```
transactions: [{
  bidAmount: int, executionType: str ("PROCESS"), id: str (uuid),
  isActingAsTeamOwner: bool, isLeagueManager: bool, isPending: bool,
  items: [{ fromLineupSlotId: int, fromTeamId: int, isKeeper: bool,
            overallPickNumber: int, playerId: int, toLineupSlotId: int,
            toTeamId: int, type: "ADD"|"DROP"|... }],
  processDate: int (epoch ms)|absent, proposedDate: int (epoch ms),
  rating: int, scoringPeriodId: int, status: str ("EXECUTED"|"CANCELED"|
    "FAILED_INVALIDPLAYERSOURCE"|"FAILED_AUCTIONBUDGETEXCEEDED"|
    "FAILED_POSITIONLIMIT"|"FAILED_ROSTERLOCK"|"FAILED_PLAYERALREADYDROPPED"|
    "FAILED_ROSTERLIMIT"|"PENDING"),
  teamActions: {<teamId str>: "INVOLVED"}, teamId: int, type: str ("WAIVER"|...)
}]
```
The `status` enum's full member list is confirmed from `Offer.__init__`
(`espn_api/base_offer.py`, fetched fresh) which maps each value to a human label — this is the
authoritative enumeration since it's the *only* place in the client that branches on every known
`status` value. A missing `processDate` (present in live probe's `txn0`, but the source treats it
as optional: `self.date = data.get('processDate'); if not self.date: self.date =
data.get('proposedDate')`, `espn_api/football/transaction.py`) means the transaction hasn't been
finalized/processed yet — client must fall back to `proposedDate`. `League.transactions()`
additionally does `if 'transactions' not in data: raise Exception('No transactions found')`
(`espn_api/football/league.py:836-837`) — **note this is indistinguishable from a real ESPN 200
response that simply has no transactions for the requested period/filter**, since the source
raises on missing key regardless of whether that means "empty result" or something else; our own
error handling should not conflate this with an auth or network failure.

**Auth-failure signature.** Shared `league_get` path.

---

## 7. Free-agent / player data (`kona_player_info` view, and the separate `/players` endpoint)

Two distinct endpoints exist under this heading — conflating them was a risk worth flagging
explicitly:

**7a. In-league free agents — `kona_player_info` view**, scoped to one league's roster/waiver
state. `espn_api/football/league.py:759-788` (`free_agents()`): same league URL,
`view=kona_player_info&scoringPeriodId={week}`, with an `x-fantasy-filter` header restricting by
`filterStatus: ["FREEAGENT","WAIVERS"]`, `filterSlotIds`, `limit`, and sort keys. **Guarded by a
hard `year` check in source**: `if self.year < 2019: raise Exception('Cant use free agents before
2019')` (`espn_api/football/league.py:763-764`) — this is a client-side guard, not an ESPN-side
constraint the API itself enforces (confirmed: nothing in `checkRequestStatus` special-cases this
view). Live probe (`league_id=1234`, season 2018 — deliberately probing *without* the header, and
despite the source's post-2019-only guard) still returned **HTTP 200** with a normal
`{"players": [...]}` body — i.e. the *live ESPN endpoint itself* does not enforce this cutoff;
the `espn-api` library's 2019 floor is a client-side policy choice (likely because free-agent
*ownership%* fields were unreliable pre-2019), not evidence of the endpoint rejecting older
seasons. Response shape (matches fixture `league_free_agents_2018.json`,
top-level `{"players": [...]}`, 50 entries in that fixture, capped at the requested `limit`):
```
players: [{
  draftAuctionValue: int, id: int, keeperValue: int, keeperValueFuture: int,
  lineupLocked: bool, onTeamId: int, ratings: {...}, rosterLocked: bool,
  status: str, tradeLocked: bool,
  player: { <same Player fields as §2, plus> draftRanksByRankType: {...},
            lastNewsDate: int, lastVideoDate: int, rankings: {...},
            seasonOutlook: str, laterality: str, stance: str }
}]
```
(`onTeamId` distinguishes rostered-elsewhere vs. true free agent — `0` typically means unowned,
confirmed by cross-reading `Player.onTeamId = json_parsing(data, 'onTeamId')` in
`espn_api/football/player.py:940`, though the exact sentinel value wasn't independently confirmed
live for an unowned player in this research.)

**7b. Full pro-player pool — separate `/players` endpoint**, not league-scoped in the same way:
`GET {FANTASY_BASE_ENDPOINT}ffl/seasons/{year}/players?view=players_wl`, with an
`x-fantasy-filter: {"filterActive": {"value": true}}` header
(`get_pro_players`, `espn_api/requests/espn_requests.py:122-129`). Live probe (year 2024, **no**
`x-fantasy-filter` header, no league_id in the path at all) returned HTTP 200 with a bare **JSON
array at the top level** (not `{"players": [...]}` — confirmed directly:
`[{"defaultPositionId":2,...,"fullName":"Israel Abanikanda",...}, ...]`), i.e. **this endpoint's
response envelope differs from every league-scoped `kona_player_info`/`mRoster` response**, which
wrap player lists in an object. Any Pydantic model for this endpoint needs a bare-list root, not
an object with a `players` field. `BaseLeague._fetch_players()`
(`espn_api/base_league.py:92-99`) consumes it as `for player in data: self.player_map[player['id']]
= player['fullName']` — directly confirming the bare-array assumption in source, matching the
live probe.

**Auth-failure signature (both 7a and 7b).** 7a shares `league_get`'s path (401/404/other). 7b
uses the plain `get()` method (`espn_api/requests/espn_requests.py:89-96`), which calls the same
`checkRequestStatus` but **without the 401-triggered URL-fallback retry** — that retry logic is
specific to `league_get()` (`espn_api/requests/espn_requests.py:76-87`), not `get()`. So a 401 on
this endpoint raises `ESPNAccessDenied` immediately with no leagueHistory-format retry attempt.

---

## 8. Player news (`site.api.espn.com/apis/fantasy/v3/games/ffl/news/players`)

**URL + params.** Entirely different host from every other endpoint in this catalog:
`NEWS_BASE_ENDPOINT = 'https://site.api.espn.com/apis/fantasy/v3/games/'`
(`espn_api/requests/constant.py`), assembled as `{NEWS_BASE_ENDPOINT}ffl/news/players` in
`EspnFantasyRequests.__init__` (`espn_api/requests/espn_requests.py:27`), called via
`news_get(params={'playerId': playerId})` (`get_player_news`,
`espn_api/requests/espn_requests.py:184-187`). This host matches the repo's *existing*
`site.api.espn.com` core-sports surface (already used in `nfl_mcp/nfl_tools.py`, per the parent
research doc) — i.e. **player news is the one fantasy-adjacent view that does NOT live on
`lm-api-reads.fantasy.espn.com`**, and may not need cookie auth at all for that reason.

**Response shape** (live probe, `playerId=3139477` [Patrick Mahomes] — no cookies, no league
context whatsoever, since this endpoint isn't league-scoped):
```
news: {
  timestamp: str (ISO 8601), resultsOffset: int, status: "success",
  resultsLimit: int, resultsCount: int,
  feed: [{
    id: int, nowId: str, contentKey: str, dataSourceIdentifier: str,
    publishedkey: str, type: str ("Story"), feedDisplayType: str,
    headline: str, description: str, title: str, ... (truncated in probe)
  }]
}
```
Confirmed live HTTP 200, `Content-Type: application/json;charset=UTF-8` (note: capital `UTF-8`,
differing in casing from the fantasy-host responses' `charset=utf-8` — a superficial but real
difference confirming these are genuinely different backend services, not just different paths on
one service). **`news_get()` notably does NOT call `checkRequestStatus` at all**
(`espn_api/requests/espn_requests.py:98-104`) — confirmed directly by reading the method body: it
does `r = requests.get(...)` then goes straight to `return r.json()` with no status-code check
whatsoever. This means the client library has **no auth-failure handling on this endpoint at
all** — a non-200 response would either raise an uncaught `requests.exceptions.JSONDecodeError`
(if the error body isn't JSON) or silently return an ESPN error-shaped JSON body as if it were
news data. This is a real gap worth designing around explicitly rather than porting as-is.

**Auth-failure signature.** None implemented in the reference client (see above) — must be
designed fresh if we build this endpoint, since we cannot lean on `espn-api`'s handling here.

---

## 9. Pre-2018 historical seasons (`leagueHistory` endpoint)

**URL format.** `GET {FANTASY_BASE_ENDPOINT}ffl/leagueHistory/{league_id}?seasonId={year}` for
`year < 2018` (`espn_api/requests/espn_requests.py:31-36`, the exact branch condition is
`if year < 2018`). Every other `view=` catalogued above (§1-6) applies identically on this
endpoint — same query params, same views — only the base path and the year-gating differ.

**Response shape differs by ONE crucial wrapper layer, confirmed both from source and by direct
inspection of two real fixtures:**
- `league_get()` in `espn_api/requests/espn_requests.py:76-87` ends with:
  `return response[0] if isinstance(response, list) else response` — i.e. the source explicitly
  anticipates the `leagueHistory` endpoint returning a **JSON array** (of season-snapshots, one
  per season the league has existed, most likely — this research did not confirm whether
  multi-element responses occur when a league spans several pre-2018 seasons under one ID, since
  no live multi-season pre-2018 league was available to probe) while the 2018+ endpoint returns a
  bare object.
- **Directly confirmed by byte-inspecting the raw fixture files** (not just reading the source):
  `tests/football/unit/data/league_2015_data.json` begins `[\n    {\n        "draftDetail": ...`
  — a JSON array — while `tests/football/unit/data/league_2018_data.json` begins
  `{"draftDetail":{"drafted":true,...` — a bare object. Both fixtures otherwise use the identical
  inner field set (both are `league_id=368876`, the maintainer's own private test league, per
  both fixtures' `"id": 368876` field) — **the inner league-object schema is otherwise unchanged
  across the 2018 boundary**; only the outer array-vs-object envelope differs. This was also
  confirmed independently for the draft view: `league_draft_2015.json` (pre-2018) is an
  array-of-one wrapping the identical `draftDetail` shape found in the live 2018+ probe (§5).

**The 401-triggered URL-format fallback** (this is the single most safety-critical piece of logic
in the whole client, and is entirely undocumented by ESPN): `checkRequestStatus`
(`espn_api/requests/espn_requests.py:38-74`) — on a 401, it **swaps which of the two URL formats
is in use** (`leagueHistory` ↔ `seasons/{year}/segments/0/leagues/{id}`) and retries once,
regardless of which `year` originally selected the format:
```python
if status == 401:
    original_endpoint = self.LEAGUE_ENDPOINT
    if "/leagueHistory/" in self.LEAGUE_ENDPOINT:
        base_endpoint = self.LEAGUE_ENDPOINT.split("/leagueHistory/")[0]
        self.LEAGUE_ENDPOINT = f"{base_endpoint}/seasons/{self.year}/segments/0/leagues/{self.league_id}"
    else:
        base_endpoint = self.LEAGUE_ENDPOINT.split(f"/seasons/")[0]
        self.LEAGUE_ENDPOINT = f"{base_endpoint}/leagueHistory/{self.league_id}?seasonId={self.year}"
    r = requests.get(self.LEAGUE_ENDPOINT + extend, params=params, headers=headers, cookies=self.cookies)
    if r.status_code == 200:
        return r.json()
    self.LEAGUE_ENDPOINT = original_endpoint  # restore on double-failure
    if not self.cookies or 'espn_s2' not in self.cookies or 'SWID' not in self.cookies:
        raise ESPNAccessDenied("espn_s2 and swid are required")
    raise ESPNAccessDenied(f"League {self.league_id} cannot be accessed with the provided credentials")
```
(`espn_api/requests/espn_requests.py:38-65`, quoted verbatim). **This means a bare 401 is not
immediately fatal** in this client — it always costs one extra round-trip against the *other*
URL format before failing. The comment "older season data is stored at a different endpoint"
(`espn_api/requests/espn_requests.py:32`) plus this fallback together imply ESPN's own backend
has, at some point, silently served *some* seasons under *both* URL shapes inconsistently — the
maintainer built the retry defensively rather than because of one documented incident, and this
research found no GitHub issue that pins an exact date/season where this actually triggered in
the wild. Also note: once the request retried and both endpoint formats have now failed, the code
explicitly restores `self.LEAGUE_ENDPOINT` back to the original format
(`espn_api/requests/espn_requests.py:64`, `original_endpoint`) before raising — so a
double-failure doesn't leave the client's internal state corrupted for subsequent calls on the
same instance. **A single 401 that succeeds on retry is silently swallowed** — the caller gets a
200-shaped result with no signal that a fallback occurred, which matters if we want our own
client to expose that as an observability signal (e.g. a log line) rather than hiding it
entirely as `espn-api` does.

**Live verification of the array/object envelope claim**: live-probed
`leagueHistory/1234?seasonId=2015` and `leagueHistory/48153503?seasonId=2017` — both returned
HTTP 404 (neither league existed in those years; `league_id=1234` and `48153503` are both
`espn-api`'s modern, 2018+/2019+ test leagues, so this is an expected negative result, not
evidence against the array-envelope claim, which rests on the fixture-file inspection above
instead).

**Auth-failure signature.** As above — 401 triggers one extra round-trip on the *other* URL
format before any exception is raised; the same `ESPNAccessDenied`/`ESPNInvalidLeague`/
`ESPNUnknownError` taxonomy from the summary below applies to whichever attempt ultimately fails.

---

## Auth-failure signature summary

This section synthesizes across every probe and source read above. **Confidence tiers matter
here**: some of this is live-verified against real ESPN infrastructure during this research
(highest confidence); some is verified only from `cwendt94/espn-api`'s source and GitHub issues
(the client's *interpretation* of ESPN's behavior, which could itself be stale or imprecise).

### What was live-verified directly against `lm-api-reads.fantasy.espn.com` (2026-09-07)

| Case | Probe | HTTP status | Body |
|---|---|---|---|
| Private league, no cookies | `GET .../seasons/2018/segments/0/leagues/368876?view=mTeam` | **401** | `{"messages":["You are not authorized to view this League."],"details":[{"message":"You are not authorized to view this League.","shortMessage":"You are not authorized to view this League.","resolution":null,"type":"AUTH_LEAGUE_NOT_VISIBLE","metaData":null}]}` |
| Private league, garbage/invalid cookies (same values `espn-api`'s own `test_private_league` uses to force the fallback path) | same URL, `Cookie: espn_s2=AEF1234...; SWID={D0C25A4C-...}` | **401**, byte-identical body | same `AUTH_LEAGUE_NOT_VISIBLE` body |
| Nonexistent league (small int, passes ESPN's own ID validation) | `GET .../seasons/2018/segments/0/leagues/2?view=mTeam` (matches `espn-api`'s own `test_unknown_league`, which asserts `League(2, 2018)` raises) | **404** | `{"messages":["Not Found"],"details":[{"message":"Not Found","shortMessage":"Not Found","resolution":null,"type":"GENERAL_NOT_FOUND","metaData":null}]}` |
| League ID that fails ESPN's own integer parsing (too large) | `GET .../seasons/2023/segments/0/leagues/999999999999?view=mTeam` | **400** | `{"messages":["Invalid parameter for 'leagueId'."],"cause":"For input string: \"999999999999\""}` |
| Valid public league, no cookies at all | `GET .../seasons/2018/segments/0/leagues/1234?view=mTeam` and `.../seasons/2019/segments/0/leagues/48153503?view=mTeam` | **200** | normal league JSON |
| Valid public league, garbage cookies present | same as above + bad `Cookie` header | **200**, unaffected | cookies are simply ignored for a public league |

**The single most useful distinguishing signal**: the 400/401/404 bodies all share one envelope —
`{"messages": [str, ...], "details": [{"message", "shortMessage", "resolution", "type",
"metaData"}]}` — **except the 400, which has no `details` array at all** (`{"messages": [...],
"cause": str}` instead). The `details[0].type` field is the cleanest machine-readable
discriminator ESPN provides: `AUTH_LEAGUE_NOT_VISIBLE` for 401, `GENERAL_NOT_FOUND` for 404. Any
error-handling design for this project should key off `details[0].type` when present, falling
back to HTTP status only when it's absent (the 400 case).

**Important nuance confirmed live**: a 401 on a private league is **identical whether no cookies
were sent at all, or wrong/garbage cookies were sent** — ESPN's 401 body does not distinguish
"you didn't try" from "you tried and failed." This means our own client **cannot** tell "this
league needs `espn_s2`/`SWID` and none were supplied" apart from "the supplied `espn_s2`/`SWID`
are present but wrong/expired" from the response alone — that distinction has to be made
client-side, before the request, by checking whether credentials were configured at all (exactly
what `espn_api/requests/espn_requests.py:69` does: `if not self.cookies or 'espn_s2' not in
self.cookies or 'SWID' not in self.cookies: raise ESPNAccessDenied("espn_s2 and swid are
required")` — a client-side branch on *whether we sent cookies*, not on anything ESPN's response
body says).

### What is sourced from `cwendt94/espn-api`'s error-handling logic (not independently
live-verified for every branch, since a live 403 could not be deliberately reproduced in this
research — see caveat below)

`checkRequestStatus` (`espn_api/requests/espn_requests.py:38-74`) recognizes exactly three
outcomes:
- **401** → after the retry-other-URL-format attempt fails too (§9) → `ESPNAccessDenied`, with
  two distinct messages depending on whether cookies were even present: `"espn_s2 and swid are
  required"` (no cookies configured) vs. `f"League {league_id} cannot be accessed with the
  provided credentials"` (cookies were configured but still failed).
- **404** → `ESPNInvalidLeague(f"League {league_id} does not exist")` — live-verified above.
- **Everything else that isn't 200** (this explicitly includes 403, and would include 500/502/503
  etc.) → the generic `ESPNUnknownError(f"ESPN returned an HTTP {status}")`.

**403 is deliberately unhandled as a distinct case — this is the most important and most
surprising finding in this research.** GitHub Issues #629 ("Doesn't seem to work anymore", Jan
2025) and #586 ("ESPN returned an HTTP 403", Oct 2024) both quote this exact generic error text
verbatim in their tracebacks — confirmed by fetching both issues' full bodies (`gh issue view
--json`):
- Issue #629's reporter (`nzylakffa`) states explicitly: *"this doesn't have anything to do with
  SWID or S2. My project searches for public leagues so that info has never been needed"* — i.e.
  **a 403 was hit on a public league with zero cookies configured at all.** The traceback shows
  `ESPNUnknownError: ESPN returned an HTTP 403` from an older client version (line numbers in the
  traceback don't match current `master` — `checkRequestStatus` used to be a free function taking
  `cookies`/`league_id` as explicit params, not a method with the retry logic shown in §9 — so
  this incident predates the current 401-retry design and its exact resolution mechanism can't be
  fully reconstructed from the issue alone).
- Issue #586's reporter (`sweppner`) states they *"confirmed [they] have the correct espn keys"*
  — i.e. **a 403 was also hit on what the user asserts is a private league with correct,
  previously-working credentials.**
- Neither issue's resolution thread identifies a root cause beyond "upgrade the pinned version"
  (#629) or is left unresolved with no answer (#586, a later commenter `gporceng` asks "What was
  the solution here?" with no reply).

**Synthesis**: 403 appears to be orthogonal to cookie validity entirely — it was reported on both
a public league (no cookies possible) and an ostensibly-correctly-authenticated private league.
The most plausible read (not independently confirmed — this research could not force a live 403,
see caveat) is that 403 in this API is more consistent with an edge/WAF-layer block (bot
detection, missing browser-like headers, rate-limiting, or a transient ESPN-side incident) than
with the credential-specific 401. **Design implication for our own client: treat 403 as
`ESPNUnknownError`-equivalent (retryable/transient), never assume it means "bad cookies,"** and
do not silently prompt a user to re-authenticate on 403 the way one reasonably would on 401.

**Separately, Issue #549** ("ESPN_2 and SWID Credentials", Jul 2024, fetched fresh with full
comment thread) documents the *correctly-diagnosed* 401 case: reporter had genuinely malformed
credentials. Maintainer-adjacent contributors' fixes, quoted directly: SWID "works with or
without curly braces" but include them anyway; both `espn_s2` AND `swid` are required together;
one user (`Ujjwal-N`) reported success only after **URL-decoding** their `espn_s2` value (cookie
values pasted directly from a browser's DevTools cookie inspector are sometimes URL-encoded and
must be decoded before use) — a concrete, previously-undocumented gotcha for credential handling
that our own client's cookie-input validation/normalization should account for.

**Issue #527** ("Private League 2024 does not work", Feb 2024, basketball not football, fetched
fresh) is **not an auth-failure case at all** — it's a `KeyError: 'home'` crash from a 200
response missing the `home` key on a bye-week matchup (same shape gap independently confirmed for
football in §3's `Matchup`/`BoxScore` guards). Included here only to make explicit that not every
exception this client raises is auth-related — some are plain response-shape gaps that a naive
model would misattribute to an auth failure if not distinguished carefully (e.g. don't let a
`KeyError` inside our own parsing code surface to the user as "check your credentials").

### Caveat on what this research did NOT verify live

A genuine 403 could not be deliberately reproduced against `lm-api-reads.fantasy.espn.com` during
this research — every live probe against both public and private leagues (with valid, garbage, or
no cookies) returned 200, 401, 404, or 400, never 403. The 403 characterization above rests
entirely on the two GitHub issues' user-pasted tracebacks (tier 4 sourcing), not on a
directly-observed ESPN response body for a 403 case, so **we don't actually know what a 403's
JSON body looks like** (whether it follows the same `{"messages","details"}` envelope as 401/404,
or is empty/HTML/plain-text — issues #629 and #586 both only show the *client's* wrapped exception
message, not the raw HTTP response body ESPN sent). If accurate 403-body handling matters for the
follow-on error-handling ticket, that specific gap would need either a live reproduction (e.g.
deliberately triggering rate-limiting/bot-detection) or accepting the ambiguity and treating 403
as an opaque, retry-worthy failure with no assumed body shape.

---

## Sources

1. `espn_api/requests/constant.py` — https://github.com/cwendt94/espn-api/blob/master/espn_api/requests/constant.py (fetched fresh, 2026-09-07)
2. `espn_api/requests/espn_requests.py` — https://github.com/cwendt94/espn-api/blob/master/espn_api/requests/espn_requests.py (fetched fresh, 2026-09-07; line numbers per that fetch)
3. `espn_api/base_league.py` — https://github.com/cwendt94/espn-api/blob/master/espn_api/base_league.py
4. `espn_api/base_settings.py`, `espn_api/base_offer.py`, `espn_api/base_pick.py`
5. `espn_api/football/{league,team,player,matchup,box_score,box_player,settings,transaction,activity,utils,constant}.py`
6. Fixtures (all fetched fresh, 2026-09-07): `tests/football/unit/data/league_2015_data.json`,
   `league_2018_data.json`, `league_draft_2015.json`, `league_draft_2018.json`,
   `league_matchupScore_2018.json`, `league_2019_playerCard.json`,
   `league_recent_activity_2019.json`, `league_free_agents_2018.json`
7. `tests/football/integration/test_league.py` — source of the real league IDs (`1234`,
   `48153503`, `368876`) used for live probing in this research
8. Live probes against `lm-api-reads.fantasy.espn.com` and `site.api.espn.com`, run 2026-09-07,
   no cookies unless explicitly noted
9. GitHub Issues (fetched fresh via `gh issue view --json`, 2026-09-07): [#629](https://github.com/cwendt94/espn-api/issues/629), [#586](https://github.com/cwendt94/espn-api/issues/586), [#549](https://github.com/cwendt94/espn-api/issues/549), [#527](https://github.com/cwendt94/espn-api/issues/527)
10. `docs/ESPN_FANTASY_API_RESEARCH.md` (this repo) — parent research doc, cited only where this
    document builds on it directly (endpoint host, cookie mechanism, `leagueHistory` existence)

---

*Document created: September 7, 2026*
*NFL MCP Server Research Initiative — resolves GitHub issue #4 (child of #3)*
