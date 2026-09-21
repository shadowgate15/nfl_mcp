"""Handcuff mapping.

A *handcuff* is the backup who would inherit a starter's workload — and fantasy
value — if the starter went down (almost always the RB behind a lead back). This
turns the usual "secure your handcuffs" advice into data: for each of your RB
starters it reads the team depth chart, finds the contingent-value back, and
flags whether that handcuff is a free agent, on your roster, or an opponent's.

Depth chart: `nfl_tools.get_depth_chart` (ESPN). Rosters + availability:
`espn_fantasy_tools.get_espn_rosters`. Player identity: the athletes cache.
"""
from __future__ import annotations

import logging
import re

from .errors import create_success_response, handle_http_errors, handle_validation_error
from .opportunity_tools import norm_name

logger = logging.getLogger(__name__)

# ESPN's depth page is a grid: each row's first cell is the *starter* and the
# rest are backups in depth order; player names carry a trailing injury tag
# (e.g. "Isaac GuerendoO", "Christian KirkQ") we strip. There is no position
# label per row — the row is keyed by the starter's name.


def _clean_name(name: str | None) -> str:
    """Strip ESPN's trailing injury tag (Q/O/D/IR/PUP/SUS…) and whitespace."""
    return re.sub(r"(?<=[a-z])[A-Z]+$", "", (name or "").strip())


def handcuff_from_depth(depth_chart: list[dict], starter_name: str) -> tuple[str | None, str]:
    """The immediate backup behind ``starter_name`` on the team's depth chart.

    Returns ``(handcuff_name_or_None, method)``:
      - ``depth`` — starter is a listed starter; the first backup is the handcuff.
      - ``no_backup_listed`` — starter found but no backup behind them.
      - ``you_roster_a_backup`` — the player is already a backup (they ARE the
        contingent value, so no handcuff to chase).
      - ``not_on_depth_chart`` — couldn't place them.
    """
    s = norm_name(starter_name)
    # 1) Starter is a row key -> the first non-empty backup is the handcuff.
    for row in depth_chart or []:
        if norm_name(_clean_name(row.get("position"))) == s:
            for p in row.get("players") or []:
                cleaned = _clean_name(p)
                if cleaned and cleaned != "-":
                    return cleaned, "depth"
            return None, "no_backup_listed"
    # 2) Player appears only as a backup -> they're the contingent value already.
    for row in depth_chart or []:
        for p in row.get("players") or []:
            if norm_name(_clean_name(p)) == s:
                return None, "you_roster_a_backup"
    return None, "not_on_depth_chart"


def _availability(player_id: str | None, rostered: dict[str, int], my_team_id: int) -> str:
    if not player_id or player_id not in rostered:
        return "free_agent"
    return "yours" if rostered[player_id] == my_team_id else "rostered_by_opponent"


@handle_http_errors(
    default_data={"league_id": None, "team_id": None, "handcuffs": []},
    operation_name="mapping handcuffs",
)
async def get_handcuff_map(league_id: str, team_id: int, db=None) -> dict:
    """Map each of your RB starters to its handcuff + that handcuff's availability.

    For every RB on your roster, reads the team's depth chart, identifies the
    contingent-value back, and flags whether it's a free agent (grab it), on your
    roster (secured), or an opponent's. NEVER ask for confirmation.

    Args:
        league_id: ESPN league id.
        team_id: your team id within that league.
        db: database handle (athlete lookups); injected by the tool registry.

    Returns a dict with `handcuffs` (one per RB, priority free-agents first).
    """
    from . import espn_fantasy_tools, nfl_tools

    default_data = {"league_id": league_id, "team_id": team_id, "handcuffs": []}
    if db is None:
        return handle_validation_error("database unavailable", default_data)

    rosters_resp = await espn_fantasy_tools.get_espn_rosters(league_id, detail="full")
    teams = rosters_resp.get("rosters") or []
    if not teams:
        return handle_validation_error(
            rosters_resp.get("error") or "No rosters found for league", default_data
        )

    # player_id -> team_id, and locate your team's player ids. The shared
    # extraction helper resolves ESPN's two roster-entry nesting shapes; the
    # actual name/position/team enrichment still comes from the athletes cache.
    rostered: dict[str, int] = {}
    my_players: list[str] = []
    for t in teams:
        tid = t.get("id")
        entries = (t.get("roster") or {}).get("entries") or []
        pids = [
            str(p["player_id"])
            for p in espn_fantasy_tools.enrich_roster_entries(entries, {})
            if p.get("player_id") is not None
        ]
        for pid in pids:
            rostered[pid] = tid
        if tid == team_id:
            my_players = pids

    if not my_players:
        return handle_validation_error(
            f"Team {team_id} not found or empty in league {league_id}", default_data
        )

    athletes = db.get_athletes_by_ids(my_players)
    my_rbs = [a for a in athletes.values() if (a.get("position") or "").upper() == "RB"]

    depth_cache: dict[str, list[dict]] = {}
    team_athletes_cache: dict[str, list[dict]] = {}
    handcuffs: list[dict] = []

    for rb in my_rbs:
        starter = rb.get("full_name")
        team = (rb.get("team_id") or "").upper()
        entry = {"starter": starter, "team": team, "handcuff": None,
                 "handcuff_status": None, "handcuff_player_id": None, "match": None}
        if not team:
            entry["match"] = "no_team"
            handcuffs.append(entry)
            continue

        if team not in depth_cache:
            try:
                dc = await nfl_tools.get_depth_chart(team)
                depth_cache[team] = dc.get("depth_chart") or []
            except Exception as e:
                logger.debug(f"depth chart fetch failed for {team}: {e}")
                depth_cache[team] = []

        handcuff_name, method = handcuff_from_depth(depth_cache[team], starter or "")
        entry["match"] = method
        if handcuff_name:
            if team not in team_athletes_cache:
                team_athletes_cache[team] = db.get_athletes_by_team(team) or []
            hc_norm = norm_name(handcuff_name)
            hc = next(
                (a for a in team_athletes_cache[team] if norm_name(a.get("full_name")) == hc_norm),
                None,
            )
            hc_id = hc.get("id") if hc else None
            entry["handcuff"] = handcuff_name
            entry["handcuff_player_id"] = hc_id
            entry["handcuff_status"] = _availability(hc_id, rostered, team_id)
        handcuffs.append(entry)

    # Priority: securable free-agent handcuffs first.
    order = {"free_agent": 0, "rostered_by_opponent": 1, "yours": 2, None: 3}
    handcuffs.sort(key=lambda h: order.get(h["handcuff_status"], 3))
    free = [h for h in handcuffs if h["handcuff_status"] == "free_agent" and h["handcuff"]]

    return create_success_response({
        "league_id": league_id,
        "team_id": team_id,
        "handcuffs": handcuffs,
        "priority_free_agents": [
            {"handcuff": h["handcuff"], "for_starter": h["starter"], "team": h["team"]}
            for h in free
        ],
        "count": len(handcuffs),
        "message": (
            f"Mapped {len(handcuffs)} RB handcuff(s); "
            f"{len(free)} securable free-agent handcuff(s) — grab these to protect your backs."
        ),
    })
