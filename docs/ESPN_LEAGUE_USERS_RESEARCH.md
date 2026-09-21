# ESPN Equivalent for Sleeper's "League Users" (Owner/Co-Manager) Concept: Findings

**Date:** September 8, 2026
**Ticket:** #24 ("Investigate the ESPN equivalent for Sleeper's 'league users' (owner/co-manager)
concept"), part of the Sleeper-to-ESPN-Fantasy cutover map (#21)

---

## Question

`opponent_analysis_tools.py` and `playoff_tools.py` call `sleeper_tools.get_league_users` —
Sleeper's concept of the humans (display names, avatars, co-owners) attached to each roster in a
league. Does ESPN's `mTeam` view (already used by `get_espn_rosters`/`get_espn_league`) carry
owner/manager identity data inline on each team object? Is it already exposed through an existing
`get_espn_*` tool's return shape, or does surfacing it require a new tool / a changed output? If
ESPN's data is thinner than Sleeper's, document exactly what's lost.

---

## 1. What Sleeper's `get_league_users` actually gives callers today

`nfl_mcp/sleeper_tools.py:379-412` (`get_league_users`) is a thin pass-through of
`GET https://api.sleeper.app/v1/league/{league_id}/users` — it returns Sleeper's raw user objects
verbatim under `users`, plus a `count`. Sleeper's user objects carry (per Sleeper's public API and
this repo's own consumption of the fields): `user_id`, `display_name`, `username`, `avatar` (an
avatar-hash string used to build an image URL), `is_owner` (league-creator flag), and a `metadata`
dict that can include `team_name` and other per-user league customization.

This repo's own two call sites use only a narrow slice of that shape:

- `nfl_mcp/opponent_analysis_tools.py:441-449` — fetches `get_league_users`, then for the
  opponent's roster looks up `user.get("user_id") == roster.get("owner_id")` and takes
  `user.get("display_name") or user.get("username")` as `opponent_name`. Nothing else from the
  user object is used (no avatar, no metadata).
- `nfl_mcp/playoff_tools.py:134-138` — same join pattern: builds a `user_id -> name` map via
  `u.get("display_name") or u.get("metadata", {}).get("team_name")`, then maps
  `roster.owner_id -> name` for display in playoff-odds output. Also uses only
  `user_id`/`display_name`/`metadata.team_name`.

So in practice, both consumers need exactly one thing from "league users": **a mapping from a
roster's owner-identifier(s) to a human-readable name**, nothing richer (no avatars, no
`is_owner`, no per-user settings consumed anywhere in this repo).

---

## 2. What ESPN's `mTeam` view actually returns, and where the identity data lives

`nfl_mcp/espn_fantasy_tools.py:350-392` (`get_espn_rosters`) requests `mRoster`+`mTeam` and
returns `league_data.get("teams", [])` — the raw ESPN `teams[]` array, unmodified
(confirmed directly in the implementation and in `tests/test_espn_fantasy_tools.py:560,594`,
which assert `result["rosters"] == league_object["teams"]` byte-for-byte). `get_espn_league`
(`nfl_mcp/espn_fantasy_tools.py:209-240`) requests only `mSettings` and never touches `teams` or
owner data at all.

`docs/ESPN_FANTASY_ENDPOINT_CATALOG.md` §2 (line 107, 109) documents each `teams[]` entry as
carrying `owners: [str (member id)]` and `primaryOwner: str` — but explicitly annotates these as
**member IDs**, not names. The catalog separately notes (§4, line 183) that the raw ESPN league
response has a top-level `members` key alongside `teams`, but — and this is the gap this ticket
exists to close — **the catalog never documents what's inside `members[]`**, and no tool in
`espn_fantasy_tools.py` reads, joins, or returns it. `get_espn_rosters` discards `members` because
it only spreads `league_data.get("teams", [])`; `get_espn_league` never requests a view that
would return `members` at all (only `mSettings`).

### 2.1 Confirming `members[]`'s shape against ESPN's own source library

Per `docs/ESPN_FANTASY_API_RESEARCH.md`/`ESPN_FANTASY_ENDPOINT_CATALOG.md`'s own methodology
(treat `cwendt94/espn-api`'s source and fixtures as the authoritative secondary reference for
ESPN's undocumented API), the join logic is in
`espn_api/base_league.py::_fetch_teams` (fetched fresh from
`raw.githubusercontent.com/cwendt94/espn-api/master/espn_api/base_league.py`, 2026-09-08):

```python
def _fetch_teams(self, data, TeamClass, pro_schedule=None):
    self.teams = []
    teams = data['teams']
    schedule = data['schedule']
    seasonId = data['seasonId']
    members = data.get('members', [])
    ...
    for team in teams:
        roster = team_roster[team['id']]
        owners = [member for member in members if member.get('id') in team.get('owners', [])]
        self.teams.append(TeamClass(team, roster=roster, schedule=schedule, year=seasonId,
                                     owners=owners, pro_schedule=pro_schedule))
```

This confirms: ESPN's raw `teams[].owners` is genuinely just a list of opaque member-ID strings.
`espn-api` resolves them to human-readable identity **client-side**, by cross-referencing against
the separate top-level `members[]` array before ever constructing its `Team` object — i.e. ESPN
does not embed rich owner identity inline on the team object; a join against `members` is
mandatory to get anything readable.

`Team.__init__` (`espn_api/football/team.py`) simply stores whatever `owners` it's handed
(`self.owners = kwargs.get('owners', [])`) — it has no independent enrichment logic; all the
enrichment happens in the base-league join above.

The shape of a `members[]` entry, confirmed directly from `cwendt94/espn-api`'s own real captured
fixture (`tests/football/unit/data/league_2018_data.json`, fetched fresh from
`raw.githubusercontent.com/cwendt94/espn-api/master/...`, 2026-09-08 — top-level keys
`['draftDetail','gameId','id','members','schedule','scoringPeriodId','seasonId','segmentId',
'settings','status','teams']`, 10 `members` entries):

```json
{
  "displayName": "fergheg",
  "firstName": "hrthr",
  "id": "{1234-5678-9101}",
  "lastName": "nvbn",
  "notificationSettings": []
}
```

And the corresponding `teams[0]` entry in the same fixture:

```json
{ "id": 1, "owners": ["{6863-6934-3455}"], "primaryOwner": "{6863-6934-3455}" }
```

So a `teams[].owners` entry is a member `id` string (SWID-like format) that must be looked up in
the sibling top-level `members[]` array to get `displayName`/`firstName`/`lastName`. No `avatar`,
no `username` distinct from `displayName`, and no explicit "co-owner" flag beyond `owners` simply
being a list (multiple entries = co-managers) with `primaryOwner` marking which one is primary.

---

## 3. Direct comparison to Sleeper

| Concept | Sleeper (`get_league_users`) | ESPN (`mTeam`/`mRoster` + top-level `members`) |
|---|---|---|
| Per-roster owner ID(s) | `roster.owner_id` (single) via `get_rosters` | `team.owners: [id, ...]` (list — natively supports co-managers) |
| Primary owner marker | N/A (Sleeper has no primary/co distinction beyond `owner_id`) | `team.primaryOwner: id` |
| Human-readable name | `user.display_name` / `user.username` | `member.displayName` / `member.firstName` + `member.lastName` (join required) |
| Per-user team nickname | `user.metadata.team_name` | Not on `members`; team-level `name`/`abbrev` already lives on the `teams[]` entry itself (`ESPN_FANTASY_ENDPOINT_CATALOG.md` §2, line 105) |
| Avatar / profile image | `user.avatar` (avatar-hash string) | **Not present** anywhere in `members[]` or `teams[]` per the confirmed fixture shape and `espn-api`'s own field list |
| League-creator/commissioner flag | `user.is_owner` | Not observed in `members[]` (only `notificationSettings`, empty in the fixture) — ESPN's league-manager concept isn't captured here |
| Co-manager support | Not modeled (Sleeper's `owner_id` is single) | Modeled natively — `owners` is already a list per team |

**Net: ESPN's data is thinner on identity metadata (no avatar, no explicit commissioner/owner flag)
but richer on the one structural thing Sleeper doesn't model at all — co-managers are a first-class
list (`owners[]`) rather than a single `owner_id`.** For the two things this repo's callers
actually consume today (name-for-a-roster, nothing else), ESPN's `members[].displayName` is a
direct, sufficient replacement for Sleeper's `display_name`.

---

## 4. Is this already exposed by an existing `get_espn_*` tool, or does it need new/changed work?

**Not exposed today.** `get_espn_rosters` returns only `league_data.get("teams", [])`
(`nfl_mcp/espn_fantasy_tools.py:392`) — it silently drops `members` even when ESPN includes it in
the same response, because `_fetch_espn_league_view` returns the whole `league_data` dict but the
tool only ever reads `.get("teams", [])` off it. `get_espn_league` never requests a view that
would return `members` at all.

Two ways to close the gap, both requiring a code change (not just a docs/consumption change):

1. **Extend `get_espn_rosters`'s return shape.** Since `mTeam`+`mRoster` responses already include
   `members` at the top level of `league_data` (confirmed by the fixture and the `espn-api` join
   logic above — `members` is populated whenever ESPN returns team data, not gated behind a
   specific view flag), `get_espn_rosters` could add one line —
   `return create_success_response({"rosters": league_data.get("teams", []), "members": league_data.get("members", [])})`
   — to surface the raw `members[]` array alongside `rosters`, and let callers (the rewired
   `opponent_analysis_tools.py`/`playoff_tools.py`) do the same `owners`-id → `members`-id join
   `espn-api` does client-side. This is the smaller, more consistent-with-existing-conventions
   change (matches this module's existing "return ESPN's shape as-is" pattern used by every other
   tool in the file).
2. **A new dedicated tool** (e.g. `get_espn_league_members`) that fetches `mTeam` and returns just
   `members`, mirroring `get_league_users`'s dedicated-endpoint shape more literally. This adds a
   second network round-trip that tool 1 above avoids (since `get_espn_rosters` already fetches
   `mTeam`, which already carries `members` in the same response).

Option 1 is the lower-effort path and avoids an extra HTTP call; it only requires the rewiring
ticket for `opponent_analysis_tools.py`/`playoff_tools.py` to perform the `owners: [id] ->
members[].displayName` join themselves (a few lines, mirroring the join `espn-api` already does in
`base_league.py`), replacing the current `owner_id -> display_name` dict lookup with an
`id -> displayName` dict lookup keyed off `members` instead of `users`.

---

## 5. Summary of gaps vs. Sleeper (for the downstream rewiring ticket)

- **No avatar equivalent.** If any future caller wants a Sleeper-style avatar, there is none in
  ESPN's `members[]`/`teams[]` shape observed here. (Today's two consumers don't use avatars, so
  this is not a blocker for the rewiring ticket, just a known gap.)
- **No explicit `is_owner`/commissioner flag** on `members[]` (only `notificationSettings`, which
  wasn't populated in the reference fixture). Neither current consumer uses this from Sleeper
  either, so not a blocker.
- **Co-managers are actually better modeled by ESPN** — `owners` is natively a list, whereas
  Sleeper's `roster.owner_id` is a single ID with no first-class co-owner concept in this repo's
  usage.
- **A join is required either way** — ESPN never embeds resolved names inline on `teams[]`; the
  rewiring ticket must replicate the `owners`-id → `members`-id lookup (a few lines, not a new
  dependency) wherever `opponent_analysis_tools.py`/`playoff_tools.py` currently resolve
  `owner_id -> display_name` via Sleeper's `get_league_users`.

---

## Sources

1. This repo: `nfl_mcp/sleeper_tools.py:379-412`, `nfl_mcp/opponent_analysis_tools.py:441-449`,
   `nfl_mcp/playoff_tools.py:134-138`, `nfl_mcp/espn_fantasy_tools.py:209-240,350-392`,
   `docs/ESPN_FANTASY_ENDPOINT_CATALOG.md` §2 (lines 92-134) and §4 (lines 178-202),
   `tests/test_espn_fantasy_tools.py:535-629`.
2. `cwendt94/espn-api` source, fetched fresh from `raw.githubusercontent.com` 2026-09-08:
   `espn_api/base_league.py` (`_fetch_teams`), `espn_api/football/team.py` (`Team.__init__`).
3. `cwendt94/espn-api`'s own real captured test fixture
   `tests/football/unit/data/league_2018_data.json`, fetched fresh from
   `raw.githubusercontent.com/cwendt94/espn-api/master/...` 2026-09-08 — inspected directly for
   the `members[]` and `teams[].owners`/`primaryOwner` field shapes quoted in §2.1 above.
