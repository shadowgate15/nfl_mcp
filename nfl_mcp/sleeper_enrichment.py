"""Sleeper enrichment & data-fetch layer (split out of sleeper_tools.py).

Best-effort fetchers (snaps, weekly usage) reading Sleeper's Sleeper-id-keyed
API. These are LEAF helpers — the public Sleeper tools call into them, not the
reverse — so extracting them is cycle-free. Re-exported from ``sleeper_tools``
for backward compatibility.

The ESPN-core-only leaf helpers that used to live here (schedule fetchers,
usage/opponent enrichment, practice reports, current-week lookup) moved to
``nfl_enrichment.py`` under ADR 0007's Sleeper-to-ESPN cutover.
``_fetch_injuries()`` was deleted outright rather than relocated:
``injury_service.InjuryAggregator`` already covers the identical ESPN-core
injuries endpoint with concurrent fetching, ETag/delta caching, and real
confidence scoring.
"""
import logging
import os

from .config import (
    DEFAULT_TIMEOUT,
    create_http_client,
    get_http_headers,
)

logger = logging.getLogger(__name__)


ADVANCED_ENRICH_ENABLED = os.getenv("NFL_MCP_ADVANCED_ENRICH") == "1"

async def _fetch_week_player_snaps(season: int, week: int):
    """Fetch player snap stats (best-effort) from Sleeper weekly stats endpoint.

    Returns list of dicts for upsert_player_week_stats. If advanced enrichment disabled
    or network/API issues occur, returns empty list.

    Uses retry logic with exponential backoff and circuit breaker pattern.
    Includes response validation to ensure data quality.
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug("[Fetch Snaps] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []

    logger.info(f"[Fetch Snaps] Starting fetch for season={season}, week={week}")

    async def _fetch():
        headers = get_http_headers("sleeper_week_stats")
        url = f"https://api.sleeper.app/v1/stats/nfl/regular/{season}/{week}"

        async with create_http_client() as client:
            resp = await client.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
            if resp.status_code != 200:
                logger.warning(f"[Fetch Snaps] API returned status {resp.status_code}")
                return []
            data = resp.json() or {}
            if not isinstance(data, dict):
                logger.warning("[Fetch Snaps] Invalid data format (not dict)")
                return []

            # Validate response
            from .response_validation import validate_response_and_log, validate_snap_count_response
            if not validate_response_and_log(data, validate_snap_count_response, "Snaps", allow_partial=True):
                logger.error("[Fetch Snaps] Response validation failed, returning empty list")
                return []

            logger.debug(f"[Fetch Snaps] Received data for {len(data)} players")
            rows = []
            for pid, stats in list(data.items())[:5000]:  # cap for safety
                if not isinstance(stats, dict):
                    continue
                # Attempt to extract snaps & snap_pct fields (naming may vary)
                # Sleeper uses 'off_snp' (not 'off_snaps'), so check both variations
                snaps = stats.get("snaps") or stats.get("off_snp") or stats.get("off_snaps") or stats.get("offense_snaps")
                team_snaps = stats.get("team_snaps") or stats.get("tm_off_snp") or stats.get("off_team_snaps") or stats.get("team_snp")
                snap_pct = stats.get("snap_pct") or stats.get("off_snp_pct") or stats.get("off_snap_pct")
                rows.append({
                    "player_id": str(pid),
                    "season": season,
                    "week": week,
                    "snaps_offense": snaps,
                    "snaps_team_offense": team_snaps,
                    "snap_pct": snap_pct,
                    "raw": stats
                })

            logger.info(f"[Fetch Snaps] Successfully fetched {len(rows)} snap records (season={season}, week={week})")
            return rows

    try:
        from .retry_utils import CircuitBreakerError, retry_with_backoff
        # Use retry with circuit breaker for snap fetches
        return await retry_with_backoff(
            _fetch,
            circuit_breaker_name="sleeper_snaps"
        )
    except CircuitBreakerError as e:
        logger.warning(f"[Fetch Snaps] Circuit breaker open: {e}")
        return []
    except Exception as e:
        logger.error(f"[Fetch Snaps] Failed for season={season}, week={week}: {e}", exc_info=True)
        return []

async def _fetch_weekly_usage_stats(season: int, week: int):
    """Fetch weekly usage statistics (targets, routes, RZ touches) from available sources.

    Returns list of dicts for upsert_usage_stats.
    Attempts Sleeper stats first, falls back to ESPN if needed.
    Uses retry logic with exponential backoff and circuit breaker pattern.
    Includes response validation to ensure data quality.
    """
    if not ADVANCED_ENRICH_ENABLED:
        logger.debug("[Fetch Usage] Skipped: NFL_MCP_ADVANCED_ENRICH not enabled")
        return []

    logger.info(f"[Fetch Usage] Starting fetch for season={season}, week={week}")

    async def _fetch():
        # Try Sleeper weekly stats endpoint first
        headers = get_http_headers("sleeper_week_stats")
        url = f"https://api.sleeper.app/v1/stats/nfl/regular/{season}/{week}"

        async with create_http_client() as client:
            resp = await client.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json() or {}
                if isinstance(data, dict):
                    logger.debug(f"[Fetch Usage] Received data for {len(data)} players")
                    stats = []
                    for pid, player_stats in list(data.items())[:3000]:  # cap
                        if not isinstance(player_stats, dict):
                            continue
                        # Extract usage fields (naming varies by API)
                        # Use explicit None checks to handle 0 values correctly
                        targets = player_stats.get("rec_tgt")
                        if targets is None:
                            targets = player_stats.get("targets")

                        # Routes should only be actual routes run, not snap count
                        # Try multiple possible field names for routes data
                        routes = player_stats.get("routes_run")
                        routes_field_used = None
                        if routes is not None:
                            routes_field_used = "routes_run"
                        elif (routes := player_stats.get("routes")) is not None:
                            routes_field_used = "routes"
                        elif (routes := player_stats.get("rec_routes")) is not None:
                            routes_field_used = "rec_routes"
                        elif (routes := player_stats.get("pass_routes")) is not None:
                            routes_field_used = "pass_routes"
                        elif (routes := player_stats.get("receiving_routes")) is not None:
                            routes_field_used = "receiving_routes"

                        # Log diagnostic info for routes field detection (sample first 5 players)
                        if len(stats) < 5:
                            if routes is not None:
                                logger.debug(f"[Fetch Usage] Player {pid}: routes={routes} from field '{routes_field_used}'")
                            else:
                                # Check what fields ARE available for this player
                                available_fields = list(player_stats.keys())[:10]  # Sample fields
                                logger.debug(f"[Fetch Usage] Player {pid}: routes=None, available fields: {available_fields}")

                        # Calculate RZ touches from multiple sources
                        # Try multiple field names for better API compatibility
                        # Use explicit None checks to preserve 0 values
                        rz_tgt = player_stats.get("rec_tgt_rz")
                        if rz_tgt is None:
                            rz_tgt = player_stats.get("rec_targets_rz")
                        if rz_tgt is None:
                            rz_tgt = player_stats.get("redzone_targets")
                        if rz_tgt is None:
                            rz_tgt = 0

                        rz_rush = player_stats.get("rush_att_rz")
                        if rz_rush is None:
                            rz_rush = player_stats.get("rush_attempts_rz")
                        if rz_rush is None:
                            rz_rush = player_stats.get("redzone_rushes")
                        if rz_rush is None:
                            rz_rush = player_stats.get("redzone_rush_attempts")
                        if rz_rush is None:
                            rz_rush = 0

                        rz_touches = rz_tgt + rz_rush

                        # If no explicit RZ data, estimate from TDs (TDs often happen in RZ)
                        if rz_touches == 0:
                            rec_td = player_stats.get("rec_td", 0)
                            rush_td = player_stats.get("rush_td", 0)
                            td_total = rec_td + rush_td

                            if td_total > 0:
                                rz_touches = td_total
                            else:
                                # Truly 0 or data missing
                                pass

                        # Calculate total touches
                        rush_att = player_stats.get("rush_att", 0)
                        receptions = player_stats.get("rec", 0)
                        touches = rush_att + receptions

                        # Air yards - preserve 0 values
                        air_yards = player_stats.get("rec_air_yds")
                        if air_yards is None:
                            air_yards = player_stats.get("air_yards")

                        # Get snap percentage - try multiple field names and calculation methods
                        # Use explicit None checks to preserve 0 values
                        snap_share = player_stats.get("snap_pct")
                        if snap_share is None:
                            snap_share = player_stats.get("off_snp_pct")
                        if snap_share is None:
                            snap_share = player_stats.get("snap_share")
                        if snap_share is None:
                            snap_share = player_stats.get("snap_percentage")
                        if snap_share is None:
                            snap_share = player_stats.get("snaps_pct")

                        # Calculate from absolute snaps if percentage not provided
                        if snap_share is None:
                            off_snp = player_stats.get("off_snp")
                            team_snp = player_stats.get("team_snp")
                            if team_snp is None:
                                team_snp = player_stats.get("tm_off_snp")

                            if off_snp is not None and team_snp is not None and team_snp > 0:
                                snap_share = round((off_snp / team_snp) * 100, 1)
                            else:
                                pass

                        # Only include if at least one usage metric present
                        if any([targets, routes, rz_touches, touches]):
                            stats.append({
                                "player_id": str(pid),
                                "season": season,
                                "week": week,
                                "targets": targets,
                                "routes": routes,
                                "rz_touches": rz_touches,
                                "touches": touches,
                                "air_yards": air_yards,
                                "snap_share": snap_share
                            })

                    if stats:
                        # Validate response
                        from .response_validation import (
                            validate_response_and_log,
                            validate_usage_stats_response,
                        )
                        if not validate_response_and_log(stats, validate_usage_stats_response, "Usage", allow_partial=True):
                            logger.error("[Fetch Usage] Response validation failed, returning empty list")
                            return []

                        # Log diagnostic summary about routes data availability
                        routes_available = sum(1 for s in stats if s.get("routes") is not None)
                        routes_zero = sum(1 for s in stats if s.get("routes") == 0)
                        routes_none = sum(1 for s in stats if s.get("routes") is None)
                        logger.info(
                            f"[Fetch Usage] Successfully fetched {len(stats)} usage records "
                            f"(season={season}, week={week}). "
                            f"Routes data: {routes_available} with data "
                            f"({routes_zero} with 0, {routes_none} with None)"
                        )
                        return stats
                    else:
                        logger.warning("[Fetch Usage] No valid usage stats found in response")
            else:
                logger.warning(f"[Fetch Usage] Sleeper API returned status {resp.status_code}")

        # Fallback: ESPN (limited coverage, best-effort)
        # Note: ESPN player stats API may require iterating by position or fetching league leaders
        # For simplicity, return empty list (can be extended later)
        logger.warning(f"[Fetch Usage] No usage stats available from any source for season={season}, week={week}")
        return []

    try:
        from .retry_utils import CircuitBreakerError, retry_with_backoff
        # Use retry with circuit breaker for usage fetches
        return await retry_with_backoff(
            _fetch,
            circuit_breaker_name="sleeper_usage"
        )
    except CircuitBreakerError as e:
        logger.warning(f"[Fetch Usage] Circuit breaker open: {e}")
        return []
    except Exception as e:
        logger.error(f"[Fetch Usage] Failed for season={season}, week={week}: {e}", exc_info=True)
        return []

