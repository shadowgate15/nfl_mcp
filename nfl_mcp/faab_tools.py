"""
FAAB (Free Agent Acquisition Budget) bid recommendations.

Waiver claims are often where leagues are won or lost, yet most managers bid on
gut feeling. This turns a bid into a data-driven number by combining:

    - the player's real market value      (player_values / FantasyCalc)
    - the marginal upgrade for YOUR roster (value over your current starter at
      that position)
    - league demand                        (no ESPN equivalent to Sleeper's
      trending-adds signal -- frozen at neutral, see demand_label)
    - budget & timing                      (your remaining FAAB, weeks left)

Output is a recommended bid as a percentage of the total FAAB budget (plus an
absolute number when the budget is known), a tier, an aggressive/safe range, and
a transparent breakdown.
"""

from __future__ import annotations

import logging

from .errors import ErrorType, create_error_response, create_success_response, handle_http_errors
from .espn_fantasy_tools import enrich_roster, get_espn_league, get_espn_rosters
from .nfl_enrichment import get_current_nfl_week
from .player_values import get_values_service
from .trade_analyzer_tools import league_format_from_settings

logger = logging.getLogger(__name__)

# Starter slots per position (used to find your replacement-level player).
_STARTER_SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1, "DST": 1}
# Regular-season fantasy weeks (playoffs typically start week 15).
_FANTASY_REGULAR_WEEKS = 14
# Never recommend blowing more than this share of budget on a single player.
_MAX_BID_PCT = 75.0


def _tier(pct: float) -> str:
    if pct >= 30:
        return "must_add"
    if pct >= 15:
        return "strong"
    if pct >= 5:
        return "solid"
    if pct >= 1:
        return "speculative"
    return "hold_or_stream"


@handle_http_errors(default_data={"recommendation": None}, operation_name="recommending FAAB bid")
async def recommend_faab_bid(
    league_id: str,
    player_id: str | None = None,
    player_name: str | None = None,
    team_id: int | None = None,
    db=None,
) -> dict:
    """Recommend a FAAB waiver bid for a player (as % of budget, + absolute).

    Args:
        league_id: ESPN league id.
        player_id: ESPN player id of the target (preferred).
        player_name: Player name (fallback lookup).
        team_id: Your ESPN team id — enables roster-need (marginal upgrade)
            weighting. Without it, the bid reflects absolute value + demand only.

    Returns: {recommendation: {bid_pct, bid_absolute, range, tier, reasoning,
              breakdown, ...}, success}
    """
    if not player_id and not player_name:
        return create_error_response("Provide player_id or player_name", ErrorType.VALIDATION,
                                     {"recommendation": None})

    # --- League format + budget context ---
    league_res = await get_espn_league(league_id)
    if not league_res.get("success") or not league_res.get("league"):
        return create_error_response(f"Could not load league: {league_res.get('error')}",
                                     ErrorType.HTTP, {"recommendation": None})
    league = league_res["league"]
    fmt = league_format_from_settings(league)
    settings = league.get("settings", {}) or {}
    acquisition = settings.get("acquisitionSettings", {}) or {}
    total_budget = acquisition.get("acquisitionBudget") or 0
    is_faab = bool(acquisition.get("isUsingAcquisitionBudget")) and total_budget > 0

    # --- Values ---
    service = get_values_service(db)
    values = await service.get_values(fmt["ppr"], fmt["num_qbs"], fmt["num_teams"], fmt["is_dynasty"])
    target = service.lookup(values, player_id=player_id, name=player_name)
    if not target:
        return create_success_response({
            "recommendation": None,
            "is_faab_league": is_faab,
            "message": (f"'{player_id or player_name}' not in the consensus value list "
                        "(deep bench / K / DST) — minimal FAAB (0-1%) or a priority claim."),
        })
    target_value = float(target.get("value") or 0)
    position = (target.get("position") or "").upper()
    max_value = max((float(v.get("value") or 0) for v in values.get("list", [])), default=target_value or 1)

    warnings: list[str] = []

    # --- Marginal upgrade vs your roster ---
    upgrade = target_value
    replacement_value = 0.0
    my_team = None
    if team_id is not None:
        rosters_res = await get_espn_rosters(league_id, detail="full")
        for t in (rosters_res.get("rosters", []) if rosters_res.get("success") else []):
            if t.get("id") == team_id:
                my_team = t
                break
        if my_team is not None:
            entries = (my_team.get("roster") or {}).get("entries") or []
            my_players_enriched = enrich_roster(entries, db)
            my_pos_vals = []
            for p in my_players_enriched:
                if (p.get("position") or "").upper() == position:
                    v = service.lookup(values, player_id=p.get("player_id"), name=p.get("full_name"))
                    if v and v.get("value") is not None:
                        my_pos_vals.append(float(v["value"]))
            my_pos_vals.sort(reverse=True)
            slots = _STARTER_SLOTS.get(position, 2)
            if len(my_pos_vals) >= slots:
                replacement_value = my_pos_vals[slots - 1]  # your last starter at the position
            upgrade = max(0.0, target_value - replacement_value)
            if upgrade <= 0:
                warnings.append(f"You're already strong at {position} — this is depth, not an upgrade")
        else:
            warnings.append(f"Team {team_id} not found; bidding on absolute value only")

    value_score = min(1.0, target_value / max_value) if max_value else 0.0
    upgrade_score = min(1.0, (upgrade / target_value)) if target_value else 0.0

    # --- Demand: ESPN has no trending-players signal (issue #50) — frozen at
    # neutral rather than fabricating a contested-demand level.
    demand_mult = 1.0
    demand_label = "unavailable"

    # --- Timing (weeks left) ---
    timing_mult = 1.0
    weeks_left = None
    try:
        wk = await get_current_nfl_week()
        if wk:
            weeks_left = max(0, _FANTASY_REGULAR_WEEKS - int(wk))
            if weeks_left <= 3:
                timing_mult = 1.2  # spend it before playoffs
                warnings.append("Few weeks left — spend aggressively if contending")
    except Exception:
        pass

    # --- Bid model ---
    base_pct = 100.0 * value_score * (0.5 + 0.5 * upgrade_score)
    bid_pct = round(min(_MAX_BID_PCT, base_pct * demand_mult * timing_mult), 1)
    tier = _tier(bid_pct)

    # Budget context
    remaining_budget = None
    bid_absolute = None
    aggressive_abs = safe_abs = None
    if is_faab:
        used = (my_team.get("transactionCounter", {}) or {}).get("acquisitionBudgetSpent") if my_team else None
        remaining_budget = (total_budget - used) if used is not None else total_budget
        bid_absolute = round(bid_pct / 100.0 * total_budget)
        if remaining_budget is not None:
            bid_absolute = min(bid_absolute, remaining_budget)
            if bid_absolute >= remaining_budget * 0.9 and remaining_budget > 0:
                warnings.append("This would use most of your remaining budget")
        aggressive_abs = min(round(bid_pct * 1.25 / 100.0 * total_budget), remaining_budget or 10**9)
        safe_abs = round(bid_pct * 0.7 / 100.0 * total_budget)
    else:
        warnings.append("Not a FAAB league (waiver priority) — use your claim priority instead of a $ bid")

    reasoning = [
        f"Market value {int(target_value)} ({position} #{target.get('position_rank')})",
        (f"Marginal upgrade for you: +{int(upgrade)} over your replacement ({int(replacement_value)})"
         if team_id is not None else "No roster context — absolute value used"),
        "League demand: unavailable — ESPN has no trending-adds signal, so demand isn't weighted into this bid",
    ]
    if weeks_left is not None:
        reasoning.append(f"{weeks_left} regular-season weeks left")

    return create_success_response({
        "recommendation": {
            "player": target.get("name"),
            "position": position,
            "bid_pct": bid_pct,
            "bid_absolute": bid_absolute,
            "range_pct": {"safe": round(bid_pct * 0.7, 1), "aggressive": round(min(_MAX_BID_PCT, bid_pct * 1.25), 1)},
            "range_absolute": {"safe": safe_abs, "aggressive": aggressive_abs},
            "tier": tier,
            "reasoning": reasoning,
            "warnings": warnings,
            "breakdown": {
                "value_score": round(value_score, 3),
                "upgrade_score": round(upgrade_score, 3),
                "demand_mult": demand_mult,
                "demand_label": demand_label,
                "timing_mult": timing_mult,
                "base_pct": round(base_pct, 1),
            },
        },
        "is_faab_league": is_faab,
        "total_budget": total_budget if is_faab else None,
        "remaining_budget": remaining_budget,
        "message": (
            (f"Bid ~{bid_pct}% "
             + (f"(${bid_absolute} of {total_budget}) " if bid_absolute is not None else "")
             + f"on {target.get('name')} [{tier}]")
            if is_faab else
            (f"Non-FAAB league — use a high waiver-priority claim on {target.get('name')} "
             f"[{tier}] (value {int(target_value)}); the bid_pct below is only a priority heuristic.")
        ),
    })
