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

**Player**:
An individual NFL athlete, identified by ESPN's numeric player id. Canonical term as
this repo cuts its player-identity source over from Sleeper to ESPN Fantasy, matching
ESPN's own vocabulary (catalog docs, ADRs 0001-0004, every `Player` object field name).
_Avoid_: Athlete (the prior, Sleeper-era term; being phased out with the Sleeper cutover)
