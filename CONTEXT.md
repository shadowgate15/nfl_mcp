# NFL MCP

An MCP server that turns real NFL and fantasy football data — league rosters, matchups,
projections, news, injuries, Vegas lines — into tools an AI assistant can call directly.

## Language

**ESPN-core**:
The public, keyless ESPN surface (`site.api.espn.com`) already used for league-agnostic
NFL data — teams, news, standings, schedules, coaching, injuries.
_Avoid_: "ESPN API" alone (ambiguous with ESPN Fantasy)

**ESPN Fantasy**:
ESPN's cookie-authenticated fantasy football surface (`lm-api-reads.fantasy.espn.com`),
used to read a specific private or public fantasy league — rosters, matchups, standings,
draft results, transactions. A distinct host, distinct auth (`ESPN_S2`/`ESPN_SWID`
cookies), and distinct purpose from ESPN-core; the two are never conflated.
_Avoid_: "ESPN API" alone (ambiguous with ESPN-core)

**FAAB**:
Free Agent Acquisition Budget — the blind-bid waiver system this repo's fantasy tools expose
(`recommend_faab_bid`, `is_faab_league`). A fantasy-football-community term, not Sleeper-specific;
it stays this repo's user-facing vocabulary across the Sleeper-to-ESPN cutover (map issue #21)
even though ESPN's own API calls the underlying league setting `acquisitionSettings`/
`acquisitionBudget` — that's ESPN's internal wire-field naming, not a term to adopt in tool names
or responses.
_Avoid_: "acquisition budget" as the external/tool-facing term (fine as an internal reference to
ESPN's own field names in code/comments)

**Owner**:
The human (or humans, for co-owned teams) who control a Team, identified by ESPN's member id
(`teams[].owners`/`primaryOwner`, resolved against the top-level `members[]` array — see
`resolve_team_owner_names` in `espn_fantasy_tools.py`). Canonical term as this repo cuts over
from Sleeper's "user"/"league user" concept to ESPN's, matching ESPN's own per-team vocabulary
(`owners`/`primaryOwner`) over the array's own name (`members`).
_Avoid_: User / league user (the prior, Sleeper-era term; being phased out with the Sleeper
cutover)

**Player**:
An individual NFL athlete, identified by ESPN's numeric player id. Canonical term as
this repo cuts its player-identity source over from Sleeper to ESPN Fantasy, matching
ESPN's own vocabulary (catalog docs, ADRs 0001-0004, every `Player` object field name).
_Avoid_: Athlete (the prior, Sleeper-era term; being phased out with the Sleeper cutover)

**Team**:
A fantasy-league roster-ownership unit, identified by ESPN's numeric `id` on `mTeam`
(`team_id` in code). Canonical term as this repo cuts over from Sleeper's roster-ownership
concept to ESPN Fantasy's, matching ESPN's own vocabulary (`mTeam`/`mRoster` views).
_Avoid_: Roster / roster_id (the prior, Sleeper-era term; being phased out with the Sleeper
cutover — note "roster" still separately means a team's set of rostered players, e.g.
`get_espn_rosters`, and stays in use in that sense)
