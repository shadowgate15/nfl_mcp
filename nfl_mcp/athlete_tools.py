"""
Athlete-related MCP tools for the NFL MCP Server.

This module contains MCP tools for fetching, searching, and managing NFL athlete data.
"""

from . import espn_fantasy_tools
from .config import LIMITS, validate_limit
from .errors import create_success_response, handle_database_errors, handle_http_errors

# ESPN `defaultPositionId` -> position abbreviation. Same static-map pattern as
# `coaching_tools.TEAM_ID_MAP`: no existing table to join this against.
# `16 -> "DST"` is relied on by streaming/handcuff DST lookups (ADR 0007).
POSITION_ID_MAP = {
    0: "QB",
    1: "QB",
    2: "RB",
    3: "WR",
    4: "WR",
    5: "WR",
    6: "TE",
    7: "OP",
    8: "DT",
    9: "DE",
    10: "LB",
    11: "DL",
    12: "CB",
    13: "S",
    14: "DB",
    15: "DP",
    16: "DST",
    17: "K",
    18: "P",
    19: "HC",
}


@handle_http_errors(
    default_data={"athletes_count": 0, "last_updated": None},
    operation_name="fetching athletes from ESPN API"
)
async def fetch_athletes(nfl_db) -> dict:
    """
    Fetch the full ESPN pro-player pool and store it in the local database.

    Sources from `espn_fantasy_tools.fetch_all_espn_players`, the unpaginated
    leaf fetch behind `get_espn_players` — since that tool now paginates its
    response (ADR 0006), this cache needs the full pool in one shot rather
    than looping pages. The cache is keyed on ESPN player ids end to end;
    `proTeamId` is resolved to a team abbreviation via the existing `teams`
    table (same numeric id space, no new mapping table), and
    `defaultPositionId` via `POSITION_ID_MAP`.

    Args:
        nfl_db: The NFLDatabase instance to store data in

    Returns:
        A dictionary containing:
        - athletes_count: Number of athletes processed
        - last_updated: Timestamp of the update
        - success: Whether the fetch was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    players = await espn_fantasy_tools.fetch_all_espn_players()

    # `teams.id` is ESPN-core's own string team id (nfl_tools.fetch_teams), and
    # `proTeamId` is the same numeric id space (ADR 0005, live-probed) — cast to
    # str() on both sides of the lookup for the join to line up.
    team_abbrev_by_pro_team_id = {
        team["id"]: team["abbreviation"] for team in nfl_db.get_all_teams()
    }

    athletes_data = {}
    for player in players:
        player_id = player.get("id")
        if player_id is None:
            continue

        pro_team_id = player.get("proTeamId")
        athletes_data[str(player_id)] = {
            "full_name": player.get("fullName", "") or "",
            "first_name": player.get("firstName", "") or "",
            "last_name": player.get("lastName", "") or "",
            "team": team_abbrev_by_pro_team_id.get(str(pro_team_id), ""),
            "position": POSITION_ID_MAP.get(player.get("defaultPositionId"), ""),
            "status": "Active" if player.get("active") else "Inactive",
        }

    # Store in database
    count = nfl_db.upsert_athletes(athletes_data)
    last_updated = nfl_db.get_last_updated()

    return create_success_response({
        "athletes_count": count,
        "last_updated": last_updated
    })


@handle_database_errors(
    default_data={"athlete": None, "found": False},
    operation_name="looking up athlete"
)
def lookup_athlete(nfl_db, athlete_id: str) -> dict:
    """
    Look up an athlete by their ID.

    This tool queries the local database for an athlete with the given ID
    and returns their information including name, team, position, etc.

    Args:
        nfl_db: The NFLDatabase instance to query
        athlete_id: The unique identifier for the athlete

    Returns:
        A dictionary containing:
        - athlete: Athlete information (if found)
        - found: Whether the athlete was found
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    athlete = nfl_db.get_athlete_by_id(athlete_id)

    if athlete:
        return create_success_response({
            "athlete": athlete,
            "found": True
        })
    else:
        return create_success_response({
            "athlete": None,
            "found": False
        })


@handle_database_errors(
    default_data={"athletes": [], "count": 0, "search_term": None},
    operation_name="searching athletes"
)
def search_athletes(nfl_db, name: str, limit: int | None = 10) -> dict:
    """
    Search for athletes by name (partial match supported).

    This tool searches the local database for athletes whose names match
    the given search term, supporting partial matches.

    Args:
        nfl_db: The NFLDatabase instance to query
        name: Name or partial name to search for
        limit: Maximum number of results to return (default: 10)

    Returns:
        A dictionary containing:
        - athletes: List of matching athletes
        - count: Number of athletes found
        - search_term: The search term used
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    # Validate limit using shared validation
    limit = validate_limit(
        limit,
        LIMITS["athletes_search_min"],
        LIMITS["athletes_search_max"],
        LIMITS["athletes_search_default"]
    )

    athletes = nfl_db.search_athletes_by_name(name, limit)

    return create_success_response({
        "athletes": athletes,
        "count": len(athletes),
        "search_term": name
    })


@handle_database_errors(
    default_data={"athletes": [], "count": 0, "team_id": None},
    operation_name="getting athletes by team"
)
def get_athletes_by_team(nfl_db, team_id: str) -> dict:
    """
    Get all athletes for a specific team.

    This tool retrieves all athletes associated with a given team ID
    from the local database.

    Args:
        nfl_db: The NFLDatabase instance to query
        team_id: The team identifier (e.g., "SF", "DAL", "NE")

    Returns:
        A dictionary containing:
        - athletes: List of athletes on the team
        - count: Number of athletes found
        - team_id: The team ID searched for
        - error: Error message (if any)
        - error_type: Type of error (if any)
    """
    athletes = nfl_db.get_athletes_by_team(team_id)

    return create_success_response({
        "athletes": athletes,
        "count": len(athletes),
        "team_id": team_id
    })
