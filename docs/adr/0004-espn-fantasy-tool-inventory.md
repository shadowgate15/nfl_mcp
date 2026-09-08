# ESPN fantasy tool inventory

`nfl_mcp/espn_fantasy_tools.py` exposes ten flat, CBS-style tools, one per ESPN fantasy endpoint category from the endpoint catalog (`docs/ESPN_FANTASY_ENDPOINT_CATALOG.md`, ticket #4): `get_espn_league`, `get_espn_rosters`, `get_espn_standings`, `get_espn_scoreboard`, `get_espn_matchups`, `get_espn_draft`, `get_espn_transactions`, `get_espn_players`, `get_espn_free_agents`, `get_espn_player_news`. All league-scoped tools take `year: int | None` to transparently span the 2018 `leagueHistory` boundary internally — the URL-format switch and array-vs-object envelope unwrap stay inside the tool, never surfaced to callers, matching the catalog's finding that the inner schema is otherwise unchanged across that boundary. Every tool but `get_espn_player_news` stacks `@handle_espn_auth_errors` inside `@handle_http_errors` per ADR 0003; `get_espn_player_news` is the one endpoint the catalog confirmed needs no `ESPN_S2`/`ESPN_SWID` cookies at all (different host, `site.api.espn.com`), so it omits a preflight check that could never fire.

## Signatures

| Tool | Signature | Catalog category | Auth |
|---|---|---|---|
| `get_espn_league` | `(league_id: str, year: int \| None = None) -> dict` | §1 League settings (`mSettings`) | yes |
| `get_espn_rosters` | `(league_id: str, week: int \| None = None, year: int \| None = None) -> dict` | §2 Rosters (`mRoster`+`mTeam`) | yes |
| `get_espn_standings` | `(league_id: str, year: int \| None = None) -> dict` | §4 Standings (derived) | yes |
| `get_espn_scoreboard` | `(league_id: str, week: int \| None = None, year: int \| None = None) -> dict` | §3 Matchups, scores only (`mMatchupScore`) | yes |
| `get_espn_matchups` | `(league_id: str, week: int \| None = None, year: int \| None = None) -> dict` | §3 Matchups, full box score (`mMatchup`+`mScoreboard`) | yes |
| `get_espn_draft` | `(league_id: str, year: int \| None = None) -> dict` | §5 Draft results (`mDraftDetail`) | yes |
| `get_espn_transactions` | `(league_id: str, week: int \| None = None, types: list[str] \| None = None, year: int \| None = None) -> dict` | §6 Transactions/waivers (`mTransactions2`) | yes |
| `get_espn_players` | `(year: int \| None = None) -> dict` | §7b Full pro-player pool (`/players`) | no |
| `get_espn_free_agents` | `(league_id: str, week: int \| None = None, year: int \| None = None) -> dict` | §7a League free agents (`kona_player_info`) | yes |
| `get_espn_player_news` | `(player_id: int \| None = None, limit: int \| None = None) -> dict` | §8 Player news (`site.api.espn.com`) | no |

`league_id: str` matches `sleeper_tools.py`'s convention. `get_espn_rosters` and `get_espn_matchups` are plural, not singular, matching this repo's existing convention in `sleeper_tools.py` (`get_rosters`, `get_matchups`) for a tool that returns every team's/every matchup's data for a league+week rather than one entity.

## Judgment calls resolved

- **Full pro-player pool vs. league-scoped free agents: two tools, not one.** `get_espn_players` (bare-array `/players`, no cookies) and `get_espn_free_agents` (`kona_player_info`, cookie-gated) have different auth requirements and different inputs, which outweighs ADR 0003's envelope normalization already unifying their *output* shape.
- **Standings gets its own thin tool despite no dedicated ESPN endpoint.** `get_espn_standings` replicates `espn-api`'s client-side sort/tiebreak logic once, in one place, rather than pushing that logic onto every caller of `get_espn_rosters`.
- **Matchups split into two tools, not one with a detail flag.** `get_espn_scoreboard` (final scores only) and `get_espn_matchups` (full box-score/lineup detail) are different-enough use cases and wire cost — mirrors `espn-api`'s own two separate methods (`scoreboard()` vs. `box_scores()`) rather than a single `detail: bool` parameter.
- **`get_espn_league` returns settings/metadata only, not ESPN's bundled multi-view payload.** Keeps a clean 1:1 mapping between catalog categories and tools; no field is reachable through two different tools.

## Considered Options

- **Bundled `get_espn_league`** returning the full multi-view payload (settings + teams + rosters + matchups + standings), mirroring `espn-api`'s own `get_league()` default. Rejected: makes roster/standings data reachable two ways, which ADR 0003 never addressed, and breaks the clean category-to-tool mapping.
- **One `get_espn_matchups(detail: bool)`** instead of two tools. Rejected during grilling: a quick score check and a full lineup/box-score analysis are different-enough calls to earn separate tools.
- **Applying `@handle_espn_auth_errors` to `get_espn_player_news` anyway**, for decorator-stack consistency across the whole module. Rejected: the endpoint-catalog research confirmed this endpoint needs no cookies at all; a preflight that can never fire is dead code posing as a real check.
