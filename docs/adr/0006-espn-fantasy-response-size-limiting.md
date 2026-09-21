# ESPN Fantasy response-size budget and trimming strategy

ESPN Fantasy's raw payloads are bigger and more deeply nested than the Sleeper responses they
replace, and no size budget existed before this cutover. Reasoned estimates (no live ESPN access
during this research) rank the ten `get_espn_*` tools by worst-case response size:
`get_espn_matchups` in full-season/box-score mode is the top offender at roughly 4-8 MB, since
every week re-embeds a full nested-stats roster for both sides of every matchup; `get_espn_rosters`
follows at roughly 1.6 MB, driven by full `Player.stats[]` per roster slot across up to ~400
players; `get_espn_players` sits at roughly 600-900 KB because it always returns the entire
~1,700-player pool unconditionally, with no pagination; and `get_espn_free_agents` is effectively
unbounded at roughly 2-3 MB worst case, since the tool never sets a `limit` on its
`x-fantasy-filter`.

This repo already has a working idiom for exactly this problem, just not applied consistently:
`get_trending_players`, `get_league_leaders`, and `get_espn_player_news`'s own `feed[:limit]` all
validate a `limit` parameter and slice client-side, and `fetch_all_players` goes further by never
returning its ~5 MB blob to a caller at all. `response_validation.py` only does structural/quality
sampling and has no size-limiting behavior; `param_validator.py` is input-validation only. The
trimming strategy for this cutover extends the existing limit-and-slice idiom rather than inventing
a new mechanism: `get_espn_players` gains pagination and `get_espn_free_agents` gains the `limit`
parameter it currently lacks entirely; `get_espn_matchups`/`get_espn_scoreboard` cap the number of
weeks returned when `week` is omitted; and a new `detail` (`summary`/`full`) flag with a field
allowlist is added to `get_espn_rosters`/`get_espn_matchups` to cut per-player nested-stats
weight — the one gap the existing count-based idiom doesn't cover on its own, since a roster or
matchup can be within limit on entity count while still carrying heavy per-player stat blocks.
Every trimming change surfaces `total_*`/`has_more` counts so truncation is visible to callers,
matching `get_espn_transactions`'s existing `total_transactions` pattern rather than silently
dropping data.

The recommended budget is a typical response of 50 KB or less (roughly 12-15K tokens), with a hard
cap of 150 KB (roughly 35-40K tokens) per tool response.
