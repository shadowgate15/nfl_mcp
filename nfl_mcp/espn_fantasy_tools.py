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

`get_espn_league` is the first tool built on this foundation; `get_espn_rosters`
and `get_espn_standings` are next. Later tickets add the rest of the catalog
(matchups, draft, transactions, free agents) as siblings in this module.
"""

import logging
import os
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from typing import Any

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
        extra_params: Additional query params appended after `view`/`seasonId`
            (e.g. `scoringPeriodId` to scope a roster fetch to one week —
            docs/ESPN_FANTASY_ENDPOINT_CATALOG.md §2).

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

    response = await client.get(
        url,
        params=params,
        headers=get_http_headers("espn_fantasy"),
        cookies=_espn_auth_cookies(),
    )
    response.raise_for_status()
    data = response.json()

    return data[0] if isinstance(data, list) else data


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
