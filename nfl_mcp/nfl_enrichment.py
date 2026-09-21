"""ESPN-core-only leaf-helper module (see ADR 0007).

Internal plumbing other modules import directly, never registered as MCP
tools. Everything here is a pure ESPN-core call or an id-scheme-agnostic DB
lookup, relocated unchanged from the now-deleted Sleeper-era leaf-helper
module during the Sleeper-to-ESPN cutover (issue #52).
"""
import logging
import os
from datetime import UTC, datetime

from .config import (
    DEFAULT_TIMEOUT,
    create_http_client,
)

logger = logging.getLogger(__name__)


ADVANCED_ENRICH_ENABLED = os.getenv("NFL_MCP_ADVANCED_ENRICH") == "1"


async def _fetch_week_schedule(season: int, week: int, force: bool = False):
    """Fetch weekly schedule from ESPN scoreboard API (best-effort).

    Returns list of bidirectional game rows for upsert_schedule_games.
    If advanced enrichment disabled or failure occurs, returns empty list.

    Args:
        season: NFL season year.
        week: Regular-season week (1-18).
        force: Fetch even when NFL_MCP_ADVANCED_ENRICH is off. Used by callers
            (e.g. strength-of-schedule) for which the schedule *is* the product,
            not opportunistic enrichment.

    Uses retry logic with exponential backoff and circuit breaker pattern.
    Includes response validation to ensure data quality.
    """
    if not ADVANCED_ENRICH_ENABLED and not force:
        logger.debug("[Fetch Schedule] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []

    logger.info(f"[Fetch Schedule] Starting fetch for season={season}, week={week}")

    async def _fetch():
        # Regular season scoreboard: seasontype=2
        url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week={week}&year={season}&seasontype=2"

        async with create_http_client() as client:
            resp = await client.get(url, timeout=DEFAULT_TIMEOUT)
            if resp.status_code != 200:
                logger.warning(f"[Fetch Schedule] ESPN API returned status {resp.status_code}")
                return []
            data = resp.json() or {}
            events = data.get("events") or []

            logger.debug(f"[Fetch Schedule] Received {len(events)} events from ESPN")
            games = []
            for ev in events:
                comps = ev.get("competitions") or []
                kickoff = ev.get("date")
                for comp in comps:
                    competitors = comp.get("competitors") or []
                    if len(competitors) != 2:
                        continue
                    home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
                    away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[-1])
                    h_abbr = (home.get("team") or {}).get("abbreviation")
                    a_abbr = (away.get("team") or {}).get("abbreviation")
                    if not h_abbr or not a_abbr:
                        continue
                    games.append({"season": season, "week": week, "team": h_abbr, "opponent": a_abbr, "is_home": 1, "kickoff": kickoff, "raw": ev})
                    games.append({"season": season, "week": week, "team": a_abbr, "opponent": h_abbr, "is_home": 0, "kickoff": kickoff, "raw": ev})

            # Validate response
            from .response_validation import validate_response_and_log, validate_schedule_response
            if not validate_response_and_log(games, validate_schedule_response, "Schedule", allow_partial=True):
                logger.error("[Fetch Schedule] Response validation failed, returning empty list")
                return []

            logger.info(f"[Fetch Schedule] Successfully fetched {len(games)} game records ({len(events)} events, season={season}, week={week})")
            return games

    try:
        from .retry_utils import CircuitBreakerError, retry_with_backoff
        # Use retry with circuit breaker for schedule fetches
        return await retry_with_backoff(
            _fetch,
            circuit_breaker_name="espn_schedule"
        )
    except CircuitBreakerError as e:
        logger.warning(f"[Fetch Schedule] Circuit breaker open: {e}")
        return []
    except Exception as e:
        logger.error(f"[Fetch Schedule] Failed for season={season}, week={week}: {e}", exc_info=True)
        return []

async def _fetch_all_team_schedules(season: int):
    """Fetch full season schedules for all 32 NFL teams from ESPN Team Schedule API.

    This prefetches complete schedules (all weeks) for every team to warm the cache.
    Useful for startup/initial cache population.

    Args:
        season: Season year (e.g., 2026)

    Returns:
        List of game dicts for upsert_schedule_games (bidirectional rows)
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug("[Fetch All Schedules] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []

    # All 32 NFL team abbreviations
    nfl_teams = [
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
        "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
        "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
        "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WSH"
    ]

    logger.info(f"[Fetch All Schedules] Starting fetch for {len(nfl_teams)} teams (season={season})")

    all_games = []
    successful_teams = 0
    failed_teams = []

    async with create_http_client() as client:
        for team_abbr in nfl_teams:
            try:
                url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_abbr}/schedule?season={season}"
                resp = await client.get(url, timeout=DEFAULT_TIMEOUT)

                if resp.status_code != 200:
                    logger.warning(f"[Fetch All Schedules] Team {team_abbr}: ESPN API returned status {resp.status_code}")
                    failed_teams.append(team_abbr)
                    continue

                data = resp.json() or {}
                events = data.get("events", [])

                team_games = []
                for event in events:
                    # Extract week and kickoff
                    week_info = event.get("week", {})
                    week = week_info.get("number") if week_info else None
                    kickoff = event.get("date")

                    # Extract competitions
                    competitions = event.get("competitions", [])
                    if not competitions:
                        continue

                    competition = competitions[0]
                    competitors = competition.get("competitors", [])

                    if len(competitors) != 2:
                        continue

                    # Find home and away teams
                    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
                    away = next((c for c in competitors if c.get("homeAway") == "away"), None)

                    if not home or not away:
                        continue

                    h_abbr = (home.get("team") or {}).get("abbreviation")
                    a_abbr = (away.get("team") or {}).get("abbreviation")

                    if not h_abbr or not a_abbr or not week:
                        continue

                    # Create bidirectional game records
                    team_games.append({
                        "season": season,
                        "week": week,
                        "team": h_abbr,
                        "opponent": a_abbr,
                        "is_home": 1,
                        "kickoff": kickoff,
                        "raw": event
                    })
                    team_games.append({
                        "season": season,
                        "week": week,
                        "team": a_abbr,
                        "opponent": h_abbr,
                        "is_home": 0,
                        "kickoff": kickoff,
                        "raw": event
                    })

                all_games.extend(team_games)
                successful_teams += 1
                logger.debug(f"[Fetch All Schedules] Team {team_abbr}: {len(team_games)} game records ({len(events)} events)")

            except Exception as e:
                logger.warning(f"[Fetch All Schedules] Team {team_abbr}: Failed - {e}")
                failed_teams.append(team_abbr)

    logger.info(
        f"[Fetch All Schedules] Completed: {successful_teams}/{len(nfl_teams)} teams successful, "
        f"{len(all_games)} total game records fetched"
    )

    if failed_teams:
        logger.warning(f"[Fetch All Schedules] Failed teams: {', '.join(failed_teams)}")

    return all_games


async def get_current_nfl_week() -> int | None:
    """Fetch the current NFL week from ESPN-core's parameterless scoreboard endpoint.

    Calling the scoreboard endpoint with no week/year params returns ESPN's
    notion of "now", including a top-level `week.number` field. This is the
    one shared implementation behind callers that previously each ran their
    own `get_nfl_state()` call just to read `.week`.

    Uses retry logic with exponential backoff and circuit breaker pattern.

    Returns:
        Current NFL week number, or None on failure.
    """
    logger.info("[Current Week] Fetching current NFL week from ESPN scoreboard")

    async def _fetch():
        url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

        async with create_http_client() as client:
            resp = await client.get(url, timeout=DEFAULT_TIMEOUT)
            if resp.status_code != 200:
                logger.warning(f"[Current Week] ESPN API returned status {resp.status_code}")
                return None
            data = resp.json() or {}
            week_number = (data.get("week") or {}).get("number")
            if week_number is None:
                logger.warning("[Current Week] Response missing week.number")
                return None
            logger.info(f"[Current Week] Current NFL week: {week_number}")
            return int(week_number)

    try:
        from .retry_utils import CircuitBreakerError, retry_with_backoff
        return await retry_with_backoff(
            _fetch,
            circuit_breaker_name="espn_scoreboard_current_week"
        )
    except CircuitBreakerError as e:
        logger.warning(f"[Current Week] Circuit breaker open: {e}")
        return None
    except Exception as e:
        logger.error(f"[Current Week] Failed: {e}", exc_info=True)
        return None


async def _fetch_practice_reports(season: int, week: int):
    """Fetch practice status reports (DNP/LP/FP) from ESPN injury data.

    Returns list of dicts with keys: player_id, date, status, source.

    Note: This derives practice status from injury reports (DNP/Limited/Full),
    sourced from injury_service.get_injury_reports() — the concurrent,
    confidence-scored aggregator that also backs the live injury MCP tools —
    rather than a standalone practice-report endpoint.
    Uses retry logic with exponential backoff and circuit breaker pattern.
    Includes response validation to ensure data quality.
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug("[Fetch Practice] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []

    logger.info(f"[Fetch Practice] Starting fetch for season={season}, week={week}")

    async def _fetch():
        # Use injury reports as source for practice status
        # Practice status is often reflected in injury reports (DNP/Limited/Full)
        from .injury_service import get_injury_reports
        injuries = await get_injury_reports()

        if not injuries:
            logger.warning("[Fetch Practice] No injury data available to extract practice status")
            return []

        # Convert injury status to practice status format
        practice_reports = []
        now = datetime.now(UTC).isoformat()

        for inj in injuries:
            status = (inj.get('injury_status') or '').upper()

            # Map injury status to practice participation
            practice_status = None
            if 'OUT' in status or 'RESERVE' in status or 'PUP' in status:
                practice_status = 'DNP'  # Did Not Participate
            elif 'DOUBTFUL' in status or 'LIMITED' in status:
                practice_status = 'LP'   # Limited Participation
            elif 'QUESTIONABLE' in status:
                practice_status = 'LP'   # Usually limited
            elif 'PROBABLE' in status or 'FULL' in status:
                practice_status = 'FP'   # Full Participation

            if practice_status:
                practice_reports.append({
                    'player_id': inj.get('player_id'),
                    'date': inj.get('date_reported', now[:10]),  # YYYY-MM-DD
                    'status': practice_status,
                    'source': 'espn_injuries'
                })

        # Validate response
        from .response_validation import (
            validate_practice_report_response,
            validate_response_and_log,
        )
        if not validate_response_and_log(practice_reports, validate_practice_report_response, "Practice", allow_partial=True):
            logger.error("[Fetch Practice] Response validation failed, returning empty list")
            return []

        logger.info(f"[Fetch Practice] Extracted {len(practice_reports)} practice status records from {len(injuries)} injuries")
        return practice_reports

    try:
        from .retry_utils import CircuitBreakerError, retry_with_backoff
        # Use retry with circuit breaker for practice fetches
        return await retry_with_backoff(
            _fetch,
            circuit_breaker_name="espn_practice"
        )
    except CircuitBreakerError as e:
        logger.warning(f"[Fetch Practice] Circuit breaker open: {e}")
        return []
    except Exception as e:
        logger.error(f"[Fetch Practice] Failed for season={season}, week={week}: {e}", exc_info=True)
        return []


def _estimate_snap_pct(depth_rank: int | None, position: str | None = None) -> float | None:
    """Estimate snap percentage based on depth chart and position.

    Different positions have different snap count patterns:
    - QBs: Starters play 95%+, backups rarely play
    - RBs: Heavy rotation/committees, starters ~55%
    - WRs: Top receivers play 85%+, backups 50%
    - TEs: Varies by blocking role, starters ~65%

    Args:
        depth_rank: Depth chart position (1=starter, 2=backup, 3=third string, etc.)
        position: Player position (QB, RB, WR, TE, etc.)

    Returns:
        Estimated snap percentage or None if cannot estimate
    """
    if depth_rank is None:
        return None

    # Position-specific estimates for starters
    if depth_rank == 1:
        position_estimates = {
            "QB": 95.0,  # QBs rarely rotate unless blowout
            "RB": 55.0,  # RBs often in committees
            "WR": 85.0,  # #1 WRs play most snaps
            "TE": 65.0,  # TEs vary by blocking role
        }
        return position_estimates.get(position, 70.0)  # Default 70% for unknown positions

    # Backups (depth 2)
    elif depth_rank == 2:
        position_estimates = {
            "QB": 5.0,   # Backup QBs rarely see the field
            "RB": 35.0,  # Backup RBs get carries in rotation
            "WR": 50.0,  # #2 WRs get decent playing time
            "TE": 40.0,  # Backup TEs mostly situational
        }
        return position_estimates.get(position, 45.0)

    # Third string or lower
    else:
        return 15.0  # Limited snaps for depth pieces regardless of position

def _calculate_usage_trend(weekly_data: list[dict], metric: str) -> str | None:
    """Calculate trend direction for a usage metric over recent weeks.

    Args:
        weekly_data: List of week dicts ordered by week DESC (most recent first)
        metric: Name of the metric to analyze (targets, routes, rz_touches, snap_share)

    Returns:
        "up" if trending upward, "down" if trending downward, "flat" if stable, None if insufficient data
    """
    if not weekly_data or len(weekly_data) < 2:
        return None

    # Extract values for the metric (ignore None values)
    values = []
    for week_data in weekly_data:
        val = week_data.get(metric)
        if val is not None:
            values.append(float(val))

    if len(values) < 2:
        return None

    # Compare most recent week vs average of prior weeks
    most_recent = values[0]
    prior_avg = sum(values[1:]) / len(values[1:])

    # Calculate percentage change
    if prior_avg == 0:
        # If prior average is 0, any positive value is "up"
        return "up" if most_recent > 0 else "flat"

    pct_change = ((most_recent - prior_avg) / prior_avg) * 100

    # Threshold for significant change: 15%
    if pct_change > 15:
        return "up"
    elif pct_change < -15:
        return "down"
    else:
        return "flat"

def _enrich_usage_and_opponent(nfl_db, athlete: dict, season: int | None, week: int | None) -> dict:
    """Add snap_pct/opponent fields to a base enrichment object (mutates and returns)."""
    if not athlete:
        return {}

    enriched_additions: dict = {}
    position = athlete.get("position")
    player_id = athlete.get("id") or athlete.get("player_id")
    player_name = athlete.get("full_name") or athlete.get("name") or f"Player-{player_id}"

    logger.debug(f"[Enrichment] Processing {player_name} (id={player_id}, pos={position}, season={season}, week={week})")

    # Snap pct (non-DEF) - try current week, fallback to previous week
    if season and week and position not in (None, "DEF") and hasattr(nfl_db, 'get_player_snap_pct'):
        row = nfl_db.get_player_snap_pct(player_id, season, week)
        snap_week_used = week

        # If current week has no data, try previous week (games may not have been played yet)
        if (not row or row.get("snap_pct") is None) and week > 1:
            row = nfl_db.get_player_snap_pct(player_id, season, week - 1)
            snap_week_used = week - 1
            logger.debug(f"[Enrichment] {player_name}: Current week {week} has no snaps, trying week {week - 1}")

        if row and row.get("snap_pct") is not None:
            enriched_additions["snap_pct"] = row.get("snap_pct")
            enriched_additions["snap_pct_source"] = "cached"
            enriched_additions["snap_pct_week"] = snap_week_used  # Track which week was used
            logger.debug(f"[Enrichment] {player_name}: snap_pct={row.get('snap_pct')}% (cached from week {snap_week_used})")
        else:
            depth_rank = None
            raw_field = athlete.get("raw")
            if isinstance(raw_field, dict):
                depth_rank = raw_field.get("depth_chart_order")
            est = _estimate_snap_pct(depth_rank, position)  # Pass position for better estimates
            if est is not None:
                enriched_additions["snap_pct"] = est
                enriched_additions["snap_pct_source"] = "estimated"
                logger.debug(f"[Enrichment] {player_name}: snap_pct={est}% (estimated from depth={depth_rank}, pos={position})")

    # Opponent for ALL positions (all positions use team_id)
    if season and week and hasattr(nfl_db, 'get_opponent'):
        # All positions use team_id (database only stores team_id, not team)
        team_key = athlete.get("team_id")

        if team_key:
            opponent = nfl_db.get_opponent(season, week, team_key)
            if opponent:
                enriched_additions["opponent"] = opponent
                enriched_additions["opponent_source"] = "cached"
                logger.debug(f"[Enrichment] {player_name} ({position}): opponent={opponent} (cached)")

    # Injury status - all positions
    if player_id and hasattr(nfl_db, 'get_player_injury_from_cache'):
        injury = nfl_db.get_player_injury_from_cache(player_id, max_age_hours=None)  # Adaptive TTL
        if injury:
            age_hours = (datetime.now(UTC) - datetime.fromisoformat(injury["updated_at"])).total_seconds() / 3600
            enriched_additions["injury_status"] = injury["injury_status"]
            enriched_additions["injury_type"] = injury.get("injury_type")
            enriched_additions["injury_description"] = injury.get("injury_description")
            enriched_additions["injury_date"] = injury.get("date_reported")
            enriched_additions["injury_age_hours"] = round(age_hours, 1)
            enriched_additions["injury_stale"] = age_hours > 12
            # New fields from injury service
            enriched_additions["injury_severity"] = injury.get("severity")
            enriched_additions["injury_confidence"] = injury.get("confidence", 50)
            enriched_additions["injury_sources"] = injury.get("sources", ["ESPN"])
            enriched_additions["injury_game_status"] = injury.get("game_status")
            logger.debug(f"[Enrichment] {player_name}: injury_status={injury['injury_status']} severity={injury.get('severity')} confidence={injury.get('confidence')} (age={round(age_hours, 1)}h)")

    # Practice status (DNP/LP/FP) - all positions
    # Always try to provide a practice_status value
    practice_status_set = False

    if player_id and hasattr(nfl_db, 'get_latest_practice_status'):
        practice = nfl_db.get_latest_practice_status(player_id, max_age_hours=72)
        if practice:
            age_hours = (datetime.now(UTC) - datetime.fromisoformat(practice["updated_at"])).total_seconds() / 3600
            enriched_additions["practice_status"] = practice["status"]
            enriched_additions["practice_status_date"] = practice["date"]
            enriched_additions["practice_status_age_hours"] = round(age_hours, 1)
            enriched_additions["practice_status_stale"] = age_hours > 72
            enriched_additions["practice_status_source"] = "cached"
            logger.debug(f"[Enrichment] {player_name}: practice_status={practice['status']} (age={round(age_hours, 1)}h)")
            practice_status_set = True

    # If no cached practice status, derive from injury or default to FP
    if not practice_status_set:
        injury_status = enriched_additions.get("injury_status", "").upper()
        if injury_status:
            # Derive practice status from injury status
            if 'OUT' in injury_status or 'RESERVE' in injury_status or 'PUP' in injury_status:
                derived_status = 'DNP'  # Did Not Participate
            elif 'DOUBTFUL' in injury_status or 'LIMITED' in injury_status:
                derived_status = 'LP'   # Limited Participation
            elif 'QUESTIONABLE' in injury_status:
                derived_status = 'LP'   # Usually limited
            elif 'PROBABLE' in injury_status or 'FULL' in injury_status:
                derived_status = 'FP'   # Full Participation
            else:
                derived_status = 'FP'   # Default to full if injury status is unclear

            enriched_additions["practice_status"] = derived_status
            enriched_additions["practice_status_source"] = "derived_from_injury"
            logger.debug(f"[Enrichment] {player_name}: practice_status={derived_status} (derived from injury_status={injury_status})")
        else:
            # No injury, no practice status -> assume healthy and fully practicing
            enriched_additions["practice_status"] = "FP"
            enriched_additions["practice_status_source"] = "default_healthy"
            logger.debug(f"[Enrichment] {player_name}: practice_status=FP (default - no injury)")

    # Usage stats (targets, routes, RZ touches) - offensive skill positions
    if season and week and position in ("WR", "RB", "TE") and hasattr(nfl_db, 'get_usage_last_n_weeks'):
        usage = nfl_db.get_usage_last_n_weeks(player_id, season, week, n=3)
        if usage:
            enriched_additions["usage_last_3_weeks"] = {
                "targets_avg": round(usage["targets_avg"], 1) if usage["targets_avg"] is not None else None,
                "routes_avg": round(usage["routes_avg"], 1) if usage["routes_avg"] is not None else None,
                "rz_touches_avg": round(usage["rz_touches_avg"], 1) if usage["rz_touches_avg"] is not None else None,
                "snap_share_avg": round(usage["snap_share_avg"], 1) if usage["snap_share_avg"] is not None else None,
                "weeks_sample": usage["weeks_sample"]
            }
            enriched_additions["usage_source"] = "sleeper"
            logger.debug(
                f"[Enrichment] {player_name}: usage_last_3wks="
                f"tgt={usage['targets_avg'] or 0:.1f}, routes={usage['routes_avg'] or 0:.1f}, "
                f"rz={usage['rz_touches_avg'] or 0:.1f} (n={usage['weeks_sample']})"
            )

            # Add trend calculation if we have weekly breakdown
            if hasattr(nfl_db, 'get_usage_weekly_breakdown'):
                weekly_breakdown = nfl_db.get_usage_weekly_breakdown(player_id, season, week, n=3)
                if weekly_breakdown and len(weekly_breakdown) >= 2:
                    # Calculate trends for key metrics
                    targets_trend = _calculate_usage_trend(weekly_breakdown, "targets")
                    routes_trend = _calculate_usage_trend(weekly_breakdown, "routes")
                    snap_trend = _calculate_usage_trend(weekly_breakdown, "snap_share")

                    # Add trend to enrichment if at least one metric has a trend
                    if targets_trend or routes_trend or snap_trend:
                        enriched_additions["usage_trend"] = {
                            "targets": targets_trend,
                            "routes": routes_trend,
                            "snap_share": snap_trend
                        }
                        # Overall trend (prioritize targets for skill positions)
                        overall_trend = targets_trend or snap_trend or routes_trend
                        if overall_trend:
                            enriched_additions["usage_trend_overall"] = overall_trend
                            logger.debug(f"[Enrichment] {player_name}: usage_trend={overall_trend}")

    # Matchup difficulty analysis - QB, RB, WR, TE only
    opponent = enriched_additions.get("opponent")
    if opponent and position in ("QB", "RB", "WR", "TE"):
        try:
            from .matchup_tools import get_defense_analyzer
            analyzer = get_defense_analyzer()

            # Get matchup difficulty (synchronous - uses cached rankings)
            matchup = analyzer.get_matchup_difficulty(position, opponent)

            if matchup and not matchup.get("is_fallback", True):
                enriched_additions["matchup_rank"] = matchup.get("rank")
                enriched_additions["matchup_tier"] = matchup.get("matchup_tier")
                enriched_additions["matchup_indicator"] = matchup.get("tier_indicator")
                enriched_additions["matchup_recommendation"] = matchup.get("recommendation")
                enriched_additions["defense_pts_allowed_avg"] = matchup.get("points_allowed_avg")
                logger.debug(
                    f"[Enrichment] {player_name}: matchup vs {opponent} = "
                    f"{matchup.get('matchup_tier')} (#{matchup.get('rank')})"
                )
            else:
                # Use fallback data but still add basic matchup info
                enriched_additions["matchup_rank"] = matchup.get("rank", 16)
                enriched_additions["matchup_tier"] = matchup.get("matchup_tier", "neutral")
                enriched_additions["matchup_indicator"] = matchup.get("tier_indicator", "🟡")
                enriched_additions["matchup_source"] = "fallback"
                logger.debug(f"[Enrichment] {player_name}: matchup vs {opponent} = neutral (fallback)")
        except Exception as e:
            logger.debug(f"[Enrichment] {player_name}: matchup analysis failed: {e}")

    # Vegas lines game environment analysis - QB, RB, WR, TE only
    team = athlete.get("team")
    if team and position in ("QB", "RB", "WR", "TE"):
        try:
            from .vegas_tools import get_vegas_analyzer
            vegas = get_vegas_analyzer()

            # Get game lines for the team (synchronous - uses cached lines)
            game = vegas.get_game_lines(team)

            if game and not game.get("is_fallback", True):
                # Determine if home or away
                team_norm = vegas._normalize_team(team)
                is_home = game.get("home_team") == team_norm

                # Get team-specific implied total
                implied_total = game.get("home_implied_total") if is_home else game.get("away_implied_total")
                spread = game.get("home_spread") if is_home else game.get("away_spread", 0)

                # Add Vegas data
                enriched_additions["game_total"] = game.get("total")
                enriched_additions["implied_team_total"] = implied_total
                enriched_additions["spread"] = spread

                # Game environment
                env = game.get("game_environment", {})
                enriched_additions["game_environment"] = env.get("tier", "average")
                enriched_additions["game_environment_indicator"] = env.get("indicator", "➡️")

                # Position-specific boost indicator
                if position == "QB":
                    enriched_additions["vegas_boost"] = env.get("qb_boost", "0%")
                elif position in ("WR", "TE"):
                    enriched_additions["vegas_boost"] = env.get("pass_catchers_boost", "0%")
                elif position == "RB":
                    enriched_additions["vegas_boost"] = env.get("rb_boost", "0%")

                logger.debug(
                    f"[Enrichment] {player_name}: Vegas O/U={game.get('total')}, "
                    f"implied={implied_total}, env={env.get('tier')}"
                )
            else:
                # Fallback - still provide basic neutral data
                enriched_additions["game_total"] = 45.0
                enriched_additions["implied_team_total"] = 22.5
                enriched_additions["game_environment"] = "average"
                enriched_additions["game_environment_indicator"] = "➡️"
                enriched_additions["vegas_source"] = "fallback"
                logger.debug(f"[Enrichment] {player_name}: Vegas data unavailable (fallback)")
        except Exception as e:
            logger.debug(f"[Enrichment] {player_name}: Vegas analysis failed: {e}")

    if enriched_additions:
        logger.info(f"[Enrichment] {player_name}: Added {len(enriched_additions)} enrichment fields")

    return enriched_additions
