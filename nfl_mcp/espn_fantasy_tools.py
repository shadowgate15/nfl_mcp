"""
ESPN Fantasy tools.

Holds @handle_espn_auth_errors, the shared decorator every ESPN Fantasy tool
stacks under @handle_http_errors to turn missing credentials and 401/403
responses into a distinct, actionable error before the generic HTTP handler
sees them, plus `_fetch_espn_league_view`, the shared helper every
league-scoped tool uses to cross the pre-2018/2018+ URL-and-envelope
boundary (ADR 0003, ADR 0004; docs/ESPN_FANTASY_ENDPOINT_CATALOG.md §9)
without callers ever seeing it:

    @handle_http_errors(...)
    @handle_espn_auth_errors
    async def get_espn_league(...): ...

`get_espn_league` is the first tool built on this foundation; `get_espn_rosters`,
`get_espn_standings`, `get_espn_scoreboard`, `get_espn_matchups`,
`get_espn_draft`, `get_espn_transactions`, `get_espn_players`, and
`get_espn_free_agents` round out the rest of the catalog as siblings in
this module.
"""

import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from typing import Any, NamedTuple

import httpx

from .config import create_http_client, get_http_headers
from .errors import (
    ErrorType,
    create_error_response,
    create_success_response,
    handle_http_errors,
)
from .espn_errors import classify_espn_auth_error

logger = logging.getLogger(__name__)

# Player news lives on ESPN's core-sports host (site.api.espn.com), not the
# fantasy host (lm-api-reads.fantasy.espn.com) every other tool in this module
# targets — confirmed cookie-free and league-independent (ESPN_FANTASY_ENDPOINT_CATALOG.md §8).
_PLAYER_NEWS_URL = "https://site.api.espn.com/apis/fantasy/v3/games/ffl/news/players"

# espn_errors.py deliberately has no dependency on errors.py's ErrorType, so
# it can be called standalone by evals/contracts/checks.py (ADR 0003). This
# maps its plain-string categories onto ErrorType constants only here, at the
# tool-response boundary, matching every other module's create_error_response
# usage.
_CATEGORY_TO_ERROR_TYPE = {
    "expired_cookies": ErrorType.ESPN_EXPIRED_COOKIES,
    "possible_auth_issue": ErrorType.ESPN_POSSIBLE_AUTH_ISSUE,
}


def handle_espn_auth_errors(func: Callable) -> Callable:
    """
    Decorator that turns ESPN credential/auth failures into distinct errors.

    Before calling the wrapped function, short-circuits with a
    "credentials not configured" error if ESPN_S2/ESPN_SWID aren't both set
    (ESPN's 401 body is byte-identical whether zero cookies or wrong cookies
    were sent, so only the client can tell these cases apart). After the
    call, catches httpx.HTTPStatusError and classifies 401/403 via
    classify_espn_auth_error; any other status is re-raised for the outer
    @handle_http_errors to handle.

    Args:
        func: The async tool function to wrap.

    Returns:
        Wrapped async function.
    """
    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if not (os.getenv("ESPN_S2") and os.getenv("ESPN_SWID")):
            return create_error_response(
                "ESPN credentials are not configured — set ESPN_S2 and ESPN_SWID.",
                ErrorType.ESPN_CREDENTIALS_NOT_CONFIGURED,
            )

        try:
            return await func(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (401, 403):
                raise
            classification = classify_espn_auth_error(e.response)
            return create_error_response(
                classification.message,
                _CATEGORY_TO_ERROR_TYPE.get(classification.category, classification.category),
            )

    return wrapper


# ESPN's fantasy API is undocumented; this base and the pre-2018 boundary
# below are sourced from live probes and cwendt94/espn-api's source — see
# docs/ESPN_FANTASY_ENDPOINT_CATALOG.md.
FANTASY_BASE_ENDPOINT = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/"

# ESPN switches both URL shape and envelope shape at this season boundary:
# docs/ESPN_FANTASY_ENDPOINT_CATALOG.md §9.
_LEAGUE_HISTORY_BOUNDARY_YEAR = 2018


def _resolve_year(year: int | None) -> int:
    """Every league-scoped tool defaults `year` to the current year the same way (ADR 0004)."""
    return year if year is not None else datetime.now().year


def _espn_auth_cookies() -> dict[str, str]:
    """
    ESPN_S2/ESPN_SWID as request cookies, under the names ESPN expects.

    Callers stack this under @handle_espn_auth_errors, which already
    guarantees both env vars are set before the wrapped function runs.
    """
    return {"espn_s2": os.environ["ESPN_S2"], "SWID": os.environ["ESPN_SWID"]}


async def _fetch_espn_league_view(
    client: httpx.AsyncClient,
    league_id: str,
    year: int,
    views: list[str],
    extra_params: list[tuple[str, str | int | float | bool | None]] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Fetch one or more `view=` slices of the ESPN league endpoint.

    Shared by every league-scoped ESPN Fantasy tool (ADR 0004): resolves
    `year` to either the 2018+ direct-season URL or the pre-2018
    `leagueHistory` URL, and unwraps the pre-2018 array-wrap (`[{...}]`) vs.
    2018+ object-wrap (`{...}`) envelope difference (ADR 0003) — callers get
    the same shape either way, regardless of which boundary `year` falls on.

    Args:
        client: An open httpx.AsyncClient (caller owns its lifecycle).
        league_id: The ESPN league ID.
        year: The season year; selects which URL format applies.
        views: `view=` query values to request (ESPN allows repeating this
            query param to request multiple views in one call).
        extra_params: Additional query params a specific view needs (e.g.
            `scoringPeriodId` to scope a roster fetch to one week, or for
            `mTransactions2`/`get_espn_free_agents`), appended after the
            `view`/`seasonId` params this helper always sets.
        extra_headers: Additional request headers (e.g. `x-fantasy-filter`
            for matchup-period scoping or free-agent status), merged over
            the base ESPN headers.

    Returns:
        The inner league object, whichever envelope ESPN actually sent.
    """
    is_pre_boundary = year < _LEAGUE_HISTORY_BOUNDARY_YEAR
    if is_pre_boundary:
        url = f"{FANTASY_BASE_ENDPOINT}ffl/leagueHistory/{league_id}"
    else:
        url = f"{FANTASY_BASE_ENDPOINT}ffl/seasons/{year}/segments/0/leagues/{league_id}"

    params: list[tuple[str, str | int | float | bool | None]] = [
        ("view", view) for view in views
    ]
    if is_pre_boundary:
        params.append(("seasonId", str(year)))
    if extra_params:
        params.extend(extra_params)

    headers = get_http_headers("espn_fantasy")
    if extra_headers:
        headers = {**headers, **extra_headers}

    response = await client.get(
        url,
        params=params,
        headers=headers,
        cookies=_espn_auth_cookies(),
    )
    response.raise_for_status()
    data = response.json()

    return data[0] if isinstance(data, list) else data


def _matchup_period_filter_header(week: int) -> dict[str, str]:
    """
    Build the `x-fantasy-filter` header that scopes a schedule request to
    one matchup period, mirroring `espn-api`'s `box_scores()`
    (docs/ESPN_FANTASY_ENDPOINT_CATALOG.md §3).
    """
    filters = {"schedule": {"filterMatchupPeriodIds": {"value": [week]}}}
    return {"x-fantasy-filter": json.dumps(filters)}


def _filter_schedule_to_week(schedule: list[dict[str, Any]], week: int) -> list[dict[str, Any]]:
    """Keep only the schedule entries for one matchup period (week)."""
    return [matchup for matchup in schedule if matchup.get("matchupPeriodId") == week]


# ADR 0006's 150 KB hard response-size cap, applied per tool response.
_RESPONSE_SIZE_HARD_CAP_BYTES = 150 * 1024


class PaginatedPage(NamedTuple):
    """A sliced-and-size-capped page, as returned by `_paginate_bounded`."""
    items: list[dict[str, Any]]
    total: int
    has_more: bool


def _paginate_bounded(items: list[dict[str, Any]], limit: int, offset: int) -> PaginatedPage:
    """
    Slice ``items[offset:offset+limit]`` for get_espn_players/get_espn_free_agents
    pagination (ADR 0006), then shrink the page further if its JSON-serialized size
    would still exceed the 150 KB hard cap. This guarantees the byte budget holds
    regardless of how heavy an individual ESPN player/free-agent object turns out to
    be, rather than depending on a precomputed "safe" limit ceiling tuned to an
    unconfirmed per-item byte estimate (no live ESPN access was available when this
    budget was designed; see docs/ESPN_FANTASY_RESPONSE_SIZE_RESEARCH.md).

    Returns:
        A PaginatedPage(items, total, has_more) where `total` is the pre-slice item
        count and `has_more` is True whenever items remain past this page, whether
        because the caller's limit/offset didn't reach the end or because
        size-capping trimmed items the caller otherwise asked for.
    """
    total = len(items)
    page = items[offset:offset + limit]

    if page and len(json.dumps(page, default=str)) > _RESPONSE_SIZE_HARD_CAP_BYTES:
        # Binary search the largest prefix of `page` that fits the cap, rather
        # than re-serializing the whole (shrinking) page once per popped item.
        lo, hi = 0, len(page)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(json.dumps(page[:mid], default=str)) <= _RESPONSE_SIZE_HARD_CAP_BYTES:
                lo = mid
            else:
                hi = mid - 1
        page = page[:lo]

    has_more = offset + len(page) < total
    return PaginatedPage(page, total, has_more)


@handle_http_errors(
    default_data={"league": None},
    operation_name="fetching ESPN league settings",
)
@handle_espn_auth_errors
async def get_espn_league(league_id: str, year: int | None = None) -> dict:
    """
    Get an ESPN fantasy league's settings and metadata.

    Requests only the `mSettings` view, so the response never carries
    teams/rosters/matchups/standings — a clean 1:1 mapping between catalog
    categories and tools (ADR 0004). Transparently spans the 2018
    leagueHistory boundary; callers never need to know which URL format or
    envelope shape ESPN actually used underneath.

    Args:
        league_id: The ESPN league ID.
        year: Season year; defaults to the current year if omitted.

    Returns:
        A dictionary containing:
        - league: League settings/metadata as ESPN returns them
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    resolved_year = _resolve_year(year)

    async with create_http_client() as client:
        league_data = await _fetch_espn_league_view(
            client,
            league_id=league_id,
            year=resolved_year,
            views=["mSettings"],
        )

    return create_success_response({"league": league_data})


# `players_wl` is the `/players` endpoint's own view param (catalog §7b) —
# distinct from the `mSettings`/`kona_player_info` view names the
# league-scoped endpoint uses.
_ACTIVE_PLAYERS_FILTER_HEADER = json.dumps({"filterActive": {"value": True}})


@handle_http_errors(
    default_data={"players": []},
    operation_name="fetching ESPN pro player pool",
)
async def get_espn_players(year: int | None = None, limit: int = 25, offset: int = 0) -> dict:
    """
    Get a page of the ESPN pro-player pool for a season, unscoped to any league.

    Hits the separate `/players` endpoint (catalog §7b) — not the
    league-scoped `kona_player_info` view `get_espn_free_agents` uses — so it
    takes no `league_id` and needs no `ESPN_S2`/`ESPN_SWID` cookies, and
    deliberately does not stack `@handle_espn_auth_errors` (ADR 0004). ESPN's
    raw response here is a bare JSON array (unlike every league-scoped
    players view, which wraps in `{"players": [...]}`); this tool normalizes
    both shapes to the same `{"players": [...]}` output (ADR 0003).

    The full ~1,700-player pool is always fetched (this endpoint has no
    server-side limit/offset of its own), but only `items[offset:offset+limit]`
    is returned, extending the limit-and-slice idiom `get_trending_players`/
    `get_league_leaders`/`get_espn_player_news` already use rather than
    inventing a new pagination mechanism (ADR 0006).

    Args:
        year: Season year; defaults to the current year if omitted.
        limit: Max players to return in this page (defaults to 25).
        offset: Players to skip before this page (defaults to 0).

    Returns:
        A dictionary containing:
        - players: This page of the pro-player pool
        - total_players: Total players in the full pool (before slicing)
        - has_more: Whether players remain beyond this page
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    resolved_year = _resolve_year(year)
    url = f"{FANTASY_BASE_ENDPOINT}ffl/seasons/{resolved_year}/players"

    headers = {**get_http_headers("espn_fantasy"), "x-fantasy-filter": _ACTIVE_PLAYERS_FILTER_HEADER}

    async with create_http_client() as client:
        response = await client.get(url, params=[("view", "players_wl")], headers=headers)
        response.raise_for_status()
        data = response.json()

    players = data if isinstance(data, list) else data.get("players", [])
    page, total_players, has_more = _paginate_bounded(players, limit, offset)

    return create_success_response({
        "players": page,
        "total_players": total_players,
        "has_more": has_more,
    })


# Restricts the league-scoped `kona_player_info` view to free-agent/waiver
# players server-side (catalog §7a) — without it, the view returns every
# rostered-or-not player in the league, not just the ones actually
# available to add.
_FREE_AGENT_FILTER_HEADER = json.dumps({"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS"]}}})


@handle_http_errors(
    default_data={"players": []},
    operation_name="fetching ESPN league free agents",
)
@handle_espn_auth_errors
async def get_espn_free_agents(
    league_id: str,
    week: int | None = None,
    year: int | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict:
    """
    Get a page of free-agent and waiver-available players for an ESPN fantasy league.

    Requests the `kona_player_info` view (catalog §7a), restricted
    server-side to FREEAGENT/WAIVERS status via an `x-fantasy-filter`
    header. Reuses `_fetch_espn_league_view` (ADR 0004), so this
    transparently spans the 2018 leagueHistory boundary the same way
    `get_espn_league` does.

    This tool previously fetched and returned every matching free
    agent/waiver player unbounded (~2-3 MB worst case). It now gains the
    `limit`/`offset` pagination `get_espn_players` already has, applying the
    same slice-and-cap idiom (ADR 0006): `items[offset:offset+limit]`,
    further trimmed if needed to stay under the 150 KB hard response-size cap.

    Args:
        league_id: The ESPN league ID.
        week: Scoring period (week) to scope free agency to. Omitted
            entirely from the request when None, letting ESPN apply its own
            current-period default.
        year: Season year; defaults to the current year if omitted.
        limit: Max free agents to return in this page (defaults to 25).
        offset: Free agents to skip before this page (defaults to 0).

    Returns:
        A dictionary containing:
        - players: This page of free-agent/waiver player entries
        - total_free_agents: Total matching players (before slicing)
        - has_more: Whether players remain beyond this page
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    resolved_year = _resolve_year(year)

    extra_params: list[tuple[str, str | int | float | bool | None]] = []
    if week is not None:
        extra_params.append(("scoringPeriodId", week))

    async with create_http_client() as client:
        data = await _fetch_espn_league_view(
            client,
            league_id=league_id,
            year=resolved_year,
            views=["kona_player_info"],
            extra_params=extra_params,
            extra_headers={"x-fantasy-filter": _FREE_AGENT_FILTER_HEADER},
        )

    players = data.get("players", [])
    page, total_free_agents, has_more = _paginate_bounded(players, limit, offset)

    return create_success_response({
        "players": page,
        "total_free_agents": total_free_agents,
        "has_more": has_more,
    })


@handle_http_errors(
    default_data={"rosters": []},
    operation_name="fetching ESPN league rosters",
)
@handle_espn_auth_errors
async def get_espn_rosters(
    league_id: str, week: int | None = None, year: int | None = None
) -> dict:
    """
    Get every team's roster for an ESPN fantasy league.

    Requests `mRoster` (roster contents) and `mTeam` (team metadata) together,
    matching `mRoster`+`mTeam` in the catalog's §2 — ESPN splits "team info"
    and "roster contents" into separate view flags even though both end up
    nested under the same `teams[]` entries. Roster contents are week-scoped:
    passing `week` adds `scoringPeriodId` to pull that week's roster instead
    of the current one (docs/ESPN_FANTASY_ENDPOINT_CATALOG.md §2). Transparently
    spans the 2018 leagueHistory boundary via the shared helper.

    Args:
        league_id: The ESPN league ID.
        week: Optional week (scoring period) to scope the roster to; defaults
            to the current roster state if omitted.
        year: Season year; defaults to the current year if omitted.

    Returns:
        A dictionary containing:
        - rosters: One entry per team (team metadata + `roster.entries`), as
          ESPN returns them
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    resolved_year = _resolve_year(year)
    extra_params: list[tuple[str, str | int | float | bool | None]] | None = (
        [("scoringPeriodId", week)] if week is not None else None
    )

    async with create_http_client() as client:
        league_data = await _fetch_espn_league_view(
            client,
            league_id=league_id,
            year=resolved_year,
            views=["mRoster", "mTeam"],
            extra_params=extra_params,
        )

    return create_success_response({"rosters": league_data.get("teams", [])})


def _standings_sort_key(team: dict[str, Any]) -> int:
    """
    Based on `espn-api`'s `League.standings()` sort key (`league.py:513-515`,
    `team.py:1054-1055`, catalog §4): a team's final rank if ESPN has computed
    one, falling back to its (projected) playoff seed otherwise. Diverges from
    that source in one place — `rankFinal`/`rankCalculatedFinal` are both
    nullable, and the cited source doesn't guard that, so this adds an
    explicit `or 0` fallback rather than risking `None` reaching `sorted()`.
    """
    final_standing = team.get("rankFinal") or team.get("rankCalculatedFinal") or 0
    return final_standing if final_standing != 0 else team.get("playoffSeed", 0)


@handle_http_errors(
    default_data={"standings": []},
    operation_name="fetching ESPN league standings",
)
@handle_espn_auth_errors
async def get_espn_standings(league_id: str, year: int | None = None) -> dict:
    """
    Get an ESPN fantasy league's standings.

    ESPN has no dedicated standings endpoint (docs/ESPN_FANTASY_ENDPOINT_CATALOG.md
    §4): `mStandings` only augments fields already inside each `teams[]` entry
    (`playoffSeed`, `rankFinal`, `rankCalculatedFinal`, ...). This tool requests
    `mStandings`+`mTeam` and replicates `espn-api`'s client-side
    `League.standings()` sort once, here, so callers of `get_espn_rosters`
    never need to reimplement it (ADR 0004).

    Args:
        league_id: The ESPN league ID.
        year: Season year; defaults to the current year if omitted.

    Returns:
        A dictionary containing:
        - standings: Teams sorted by final/projected rank (best first), as
          ESPN returns them
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    resolved_year = _resolve_year(year)

    async with create_http_client() as client:
        league_data = await _fetch_espn_league_view(
            client,
            league_id=league_id,
            year=resolved_year,
            views=["mStandings", "mTeam"],
        )

    standings = sorted(league_data.get("teams", []), key=_standings_sort_key)

    return create_success_response({"standings": standings})


@handle_http_errors(
    default_data={"scoreboard": []},
    operation_name="fetching ESPN scoreboard",
)
@handle_espn_auth_errors
async def get_espn_scoreboard(league_id: str, week: int | None = None, year: int | None = None) -> dict:
    """
    Get final scores for an ESPN fantasy league's matchups.

    Requests only the `mMatchupScore` view — final scores only, no
    per-player lineup/box-score detail (ADR 0004; see get_espn_matchups for
    the full box-score tool covering the same catalog category,
    docs/ESPN_FANTASY_ENDPOINT_CATALOG.md §3). When `week` is given, the
    returned schedule is filtered client-side to that matchup period,
    mirroring `espn-api`'s own `scoreboard()` (catalog §3), which filters
    the same way rather than via ESPN's `x-fantasy-filter` header — unlike
    `box_scores()`, the catalog never confirms that header scopes this
    lighter view. Transparently spans the 2018 leagueHistory boundary via
    the shared helper.

    Args:
        league_id: The ESPN league ID.
        week: Matchup period (week) to filter to; omit for the full
            season's schedule.
        year: Season year; defaults to the current year if omitted.

    Returns:
        A dictionary containing:
        - scoreboard: List of matchup score entries (ESPN's `schedule` array)
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    resolved_year = _resolve_year(year)

    async with create_http_client() as client:
        league_data = await _fetch_espn_league_view(
            client,
            league_id=league_id,
            year=resolved_year,
            views=["mMatchupScore"],
        )

    schedule = league_data.get("schedule", [])
    if week is not None:
        schedule = _filter_schedule_to_week(schedule, week)

    return create_success_response({"scoreboard": schedule})


@handle_http_errors(
    default_data={"matchups": []},
    operation_name="fetching ESPN matchups",
)
@handle_espn_auth_errors
async def get_espn_matchups(league_id: str, week: int | None = None, year: int | None = None) -> dict:
    """
    Get full box-score/lineup detail for an ESPN fantasy league's matchups.

    Requests `mMatchup`+`mScoreboard` together — each matchup includes
    per-side lineup/box-score detail, not just final scores (ADR 0004; see
    get_espn_scoreboard for the lighter scores-only tool covering the same
    catalog category, docs/ESPN_FANTASY_ENDPOINT_CATALOG.md §3). When
    `week` is given, scopes the request to that matchup period via ESPN's
    `x-fantasy-filter` header and also filters the returned schedule
    client-side. Transparently spans the 2018 leagueHistory boundary via
    the shared helper.

    Args:
        league_id: The ESPN league ID.
        week: Matchup period (week) to filter to; omit for the full
            season's schedule.
        year: Season year; defaults to the current year if omitted.

    Returns:
        A dictionary containing:
        - matchups: List of matchup entries with full box-score/lineup detail
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    resolved_year = _resolve_year(year)
    extra_headers = _matchup_period_filter_header(week) if week is not None else None

    async with create_http_client() as client:
        league_data = await _fetch_espn_league_view(
            client,
            league_id=league_id,
            year=resolved_year,
            views=["mMatchup", "mScoreboard"],
            extra_headers=extra_headers,
        )

    schedule = league_data.get("schedule", [])
    if week is not None:
        schedule = _filter_schedule_to_week(schedule, week)

    return create_success_response({"matchups": schedule})


@handle_http_errors(
    default_data={"draft": None},
    operation_name="fetching ESPN draft results",
)
@handle_espn_auth_errors
async def get_espn_draft(league_id: str, year: int | None = None) -> dict:
    """
    Get an ESPN fantasy league's draft results.

    Requests only the `mDraftDetail` view. Transparently spans the 2018
    leagueHistory boundary; callers never need to know which URL format or
    envelope shape ESPN actually used underneath. Undrafted leagues are not
    an error: `draft.draftDetail.drafted` is simply false and `picks` empty
    (docs/ESPN_FANTASY_ENDPOINT_CATALOG.md §5).

    Args:
        league_id: The ESPN league ID.
        year: Season year; defaults to the current year if omitted.

    Returns:
        A dictionary containing:
        - draft: Draft results as ESPN returns them
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    resolved_year = _resolve_year(year)

    async with create_http_client() as client:
        draft_data = await _fetch_espn_league_view(
            client,
            league_id=league_id,
            year=resolved_year,
            views=["mDraftDetail"],
        )

    return create_success_response({"draft": draft_data})


@handle_http_errors(
    default_data={"transactions": [], "total_transactions": 0},
    operation_name="fetching ESPN transactions",
)
@handle_espn_auth_errors
async def get_espn_transactions(
    league_id: str,
    week: int | None = None,
    types: list[str] | None = None,
    year: int | None = None,
) -> dict:
    """
    Get an ESPN fantasy league's transaction/waiver activity log.

    Requests the `mTransactions2` view, which the catalog documents as
    needing a *required* `scoringPeriodId` param (docs/ESPN_FANTASY_ENDPOINT_CATALOG.md
    §6) — the reference client (`espn_api`) never omits it either, always
    resolving `self.scoringPeriodId` (a top-level field on any league fetch,
    not specific to `mTransactions2`) before requesting transactions when the
    caller doesn't supply one. This mirrors that: `week` is forwarded as
    `scoringPeriodId` directly when given; when omitted, one extra lightweight
    `_fetch_espn_league_view` call resolves the league's current
    `scoringPeriodId` first, same as `espn_api`'s own two-step resolution. A
    response with no `transactions` key means no transactions matched — the
    catalog notes this is indistinguishable from "empty result" at the wire
    level, so it's treated as an empty list here, not an error. `types`
    filters the returned list client-side by each transaction's `type` field
    (e.g. "WAIVER", "TRADE"): the catalog only confirms an `x-fantasy-filter`
    header *exists* for server-side filtering, not its exact JSON shape, so
    filtering here guarantees the contract regardless of what ESPN's server
    does — same reasoning as `get_espn_player_news`'s client-side `limit`
    enforcement. Transparently spans the 2018 leagueHistory boundary via
    `_fetch_espn_league_view`.

    Args:
        league_id: The ESPN league ID.
        week: Scoring period to fetch transactions for; the league's current
            scoring period is resolved and used if omitted.
        types: Optional list of transaction type strings to filter to (e.g.
            ["WAIVER", "TRADE"]). Unfiltered if omitted.
        year: Season year; defaults to the current year if omitted.

    Returns:
        A dictionary containing:
        - transactions: List of transaction objects, as ESPN returns them,
          optionally filtered by `types`
        - total_transactions: Number of transactions returned
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    resolved_year = _resolve_year(year)

    async with create_http_client() as client:
        scoring_period_id = week
        if scoring_period_id is None:
            current_league_data = await _fetch_espn_league_view(
                client,
                league_id=league_id,
                year=resolved_year,
                views=["mSettings"],
            )
            scoring_period_id = current_league_data.get("scoringPeriodId")

        extra_params: list[tuple[str, str | int | float | bool | None]] = []
        if scoring_period_id is not None:
            extra_params.append(("scoringPeriodId", str(scoring_period_id)))

        league_data = await _fetch_espn_league_view(
            client,
            league_id=league_id,
            year=resolved_year,
            views=["mTransactions2"],
            extra_params=extra_params or None,
        )

    transactions = league_data.get("transactions", [])
    if types is not None:
        types_set = set(types)
        transactions = [t for t in transactions if t.get("type") in types_set]

    return create_success_response({
        "transactions": transactions,
        "total_transactions": len(transactions),
    })


@handle_http_errors(
    default_data={"news": [], "total_news": 0},
    operation_name="fetching ESPN player news",
)
async def get_espn_player_news(player_id: int | None = None, limit: int | None = None) -> dict:
    """
    Fetch the latest ESPN fantasy player news, optionally filtered to one player.

    Unlike every other tool in this module, this endpoint lives on ESPN's
    core-sports host (`site.api.espn.com`) rather than the fantasy host, is not
    league-scoped, and needs no `ESPN_S2`/`ESPN_SWID` cookies at all — so it
    deliberately does not stack `@handle_espn_auth_errors` (ADR 0004).

    Args:
        player_id: Optional ESPN player ID to filter news to a single player.
        limit: Optional max number of news items to return.

    Returns:
        A dictionary containing:
        - news: List of news feed items, as ESPN returns them
        - total_news: Number of news items returned
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    params: dict[str, int] = {}
    if player_id is not None:
        params["playerId"] = player_id
    if limit is not None:
        params["limit"] = limit

    headers = get_http_headers("espn_player_news")

    async with create_http_client() as client:
        response = await client.get(_PLAYER_NEWS_URL, headers=headers, params=params)

        # Same site.api.espn.com host as nfl_tools.get_nfl_news, which hits this
        # exact WAF quirk: a branded User-Agent is intermittently rejected with
        # 403, and retrying with httpx's default User-Agent is accepted.
        if response.status_code == 403:
            logger.warning(
                "ESPN player news returned 403 for branded User-Agent; "
                "retrying with default User-Agent"
            )
            response = await client.get(_PLAYER_NEWS_URL, params=params)

        response.raise_for_status()

        data = response.json()
        feed = data.get("news", {}).get("feed", [])

        # The catalog's reference client only documents `playerId` as a request
        # param (§8) — `limit` is sent best-effort but not confirmed honored
        # server-side, so it's also enforced here to guarantee the contract.
        if limit is not None:
            feed = feed[:limit]

        return create_success_response({
            "news": feed,
            "total_news": len(feed),
        })
