"""
Playoff odds via Monte-Carlo simulation of the rest of the season.

Turns qualitative standings into real probabilities: simulate every remaining
regular-season matchup thousands of times (each team scores ~ Normal(its
points-per-game, sd)), rank by record then points, and count how often each team
lands in a playoff seed.

Team strength defaults to season points-per-game (from ESPN's
`team.record.overall.pointsFor`), which is a simple, robust estimate; when the
season hasn't produced enough games it falls back to the league average.
"""

from __future__ import annotations

import logging
import random

from .errors import ErrorType, create_error_response, create_success_response, handle_http_errors
from .espn_fantasy_tools import (
    get_espn_league,
    get_espn_matchups,
    get_espn_rosters,
    resolve_team_owner_names,
)
from .nfl_enrichment import get_current_nfl_week

logger = logging.getLogger(__name__)

DEFAULT_PLAYOFF_TEAMS = 6
DEFAULT_PLAYOFF_WEEK_START = 15  # regular season = weeks 1..14
DEFAULT_SCORE_SD = 25.0


def _rank_key(w: float, p: float):
    return (w, p)


def _simulate(
    teams: list[dict], schedule: list[tuple[int, int]], playoff_teams: int,
    num_sims: int, score_sd: float, rng: random.Random,
) -> dict[int, dict[str, float]]:
    """Monte-Carlo the remaining schedule. teams: [{team_id, wins, points, mean}]."""
    made = {t["team_id"]: 0 for t in teams}
    seed_sum = {t["team_id"]: 0 for t in teams}
    ids = [t["team_id"] for t in teams]
    base_w = {t["team_id"]: t["wins"] for t in teams}
    base_p = {t["team_id"]: t["points"] for t in teams}
    mean = {t["team_id"]: t["mean"] for t in teams}

    for _ in range(num_sims):
        w = dict(base_w)
        p = dict(base_p)
        for a, b in schedule:
            sa = rng.gauss(mean[a], score_sd)
            sb = rng.gauss(mean[b], score_sd)
            p[a] += sa
            p[b] += sb
            if sa >= sb:
                w[a] += 1
            else:
                w[b] += 1
        order = sorted(ids, key=lambda tid: _rank_key(w[tid], p[tid]), reverse=True)
        for seed, tid in enumerate(order[:playoff_teams], 1):
            made[tid] += 1
            seed_sum[tid] += seed

    out = {}
    for tid in ids:
        m = made[tid]
        out[tid] = {
            "playoff_pct": round(m / num_sims * 100, 1),
            "avg_seed": round(seed_sum[tid] / m, 2) if m else None,
        }
    return out


async def _build_remaining_schedule(league_id: str, weeks: list[int]) -> list[tuple[int, int]]:
    """Reconstruct team-vs-team pairings for the given weeks from ESPN matchups.

    ESPN's `schedule[]` entries are already home/away-paired (unlike Sleeper's
    matchup_id-grouped rows), so each entry maps directly to a `(home teamId,
    away teamId)` tuple. A missing `home` or `away` key means a bye week
    (docs/ESPN_FANTASY_ENDPOINT_CATALOG.md §3) and is skipped.
    """
    schedule: list[tuple[int, int]] = []
    for wk in weeks:
        res = await get_espn_matchups(league_id, week=wk)
        if not res.get("success"):
            continue
        for m in res.get("matchups", []):
            home = m.get("home")
            away = m.get("away")
            if not home or not away:
                continue
            home_id = home.get("teamId")
            away_id = away.get("teamId")
            if home_id is None or away_id is None:
                continue
            schedule.append((home_id, away_id))
    return schedule


@handle_http_errors(default_data={"odds": []}, operation_name="computing playoff odds")
async def get_playoff_odds(
    league_id: str,
    current_week: int | None = None,
    num_sims: int = 10000,
    score_sd: float = DEFAULT_SCORE_SD,
    team_id: int | None = None,
    seed: int | None = None,
    db=None,
) -> dict:
    """Compute playoff probabilities by simulating the rest of the regular season.

    Args:
        league_id: ESPN league id.
        current_week: First not-yet-played week (defaults to
            nfl_enrichment.get_current_nfl_week() / inferred).
        num_sims: Monte-Carlo iterations (default 10000).
        score_sd: Weekly scoring standard deviation (default 25).
        team_id: If given, also returns your win-this-week vs lose-this-week swing.
        seed: RNG seed for reproducibility.

    Returns: {odds: [{team_id, name, record, mean_ppg, playoff_pct, avg_seed}], ...}
    """
    league_res = await get_espn_league(league_id)
    if not league_res.get("success") or not league_res.get("league"):
        return create_error_response(f"Could not load league: {league_res.get('error')}",
                                     ErrorType.HTTP, {"odds": []})
    league = league_res["league"]
    settings = league.get("settings", {}) or {}
    schedule_settings = settings.get("scheduleSettings", {}) or {}
    playoff_teams = int(schedule_settings.get("playoffTeamCount", DEFAULT_PLAYOFF_TEAMS) or DEFAULT_PLAYOFF_TEAMS)
    matchup_period_count = schedule_settings.get("matchupPeriodCount")
    playoff_week_start = int(matchup_period_count) + 1 if matchup_period_count else DEFAULT_PLAYOFF_WEEK_START
    regular_weeks = playoff_week_start - 1

    rosters_res = await get_espn_rosters(league_id)
    if not rosters_res.get("success"):
        return create_error_response(f"Could not load rosters: {rosters_res.get('error')}",
                                     ErrorType.HTTP, {"odds": []})
    rosters = rosters_res.get("rosters", [])
    members = rosters_res.get("members", [])

    names = resolve_team_owner_names(rosters, members)

    # Build teams with current record + season scoring
    teams = []
    total_ppg = 0.0
    counted = 0
    for r in rosters:
        record = (r.get("record") or {}).get("overall", {}) or {}
        wins = float(record.get("wins", 0) or 0)
        losses = float(record.get("losses", 0) or 0)
        ties = float(record.get("ties", 0) or 0)
        games = wins + losses + ties
        fpts = float(record.get("pointsFor", 0) or 0)
        mean = (fpts / games) if games > 0 else None
        if mean is not None:
            total_ppg += mean
            counted += 1
        teams.append({
            "team_id": r.get("id"),
            "wins": wins + 0.5 * ties,   # ties count as half a win for ranking
            "points": fpts,
            "games": games,
            "mean": mean,
            "record": f"{int(wins)}-{int(losses)}" + (f"-{int(ties)}" if ties else ""),
        })
    league_avg_ppg = (total_ppg / counted) if counted else 100.0
    for t in teams:
        if t["mean"] is None:
            t["mean"] = league_avg_ppg

    # current week
    if current_week is None:
        try:
            current_week = await get_current_nfl_week()
        except Exception:
            current_week = None
    if not current_week or current_week < 1:
        max_games = max((t["games"] for t in teams), default=0)
        current_week = int(max_games) + 1

    remaining_weeks = list(range(current_week, regular_weeks + 1))
    schedule = await _build_remaining_schedule(league_id, remaining_weeks) if remaining_weeks else []

    # No remaining games (e.g. preseason / schedule not published) -> the sim
    # would otherwise emit a deterministic 100/0 split by team id. Flag it.
    if not schedule:
        return create_success_response({
            "odds": [],
            "playoff_teams": playoff_teams,
            "regular_season_weeks": regular_weeks,
            "current_week": current_week,
            "games_remaining": 0,
            "computable": False,
            "message": ("Playoff odds can't be computed yet — no remaining scheduled "
                        "games (preseason or schedule not available)."),
        })

    rng = random.Random(seed)
    sim = _simulate(teams, schedule, playoff_teams, max(100, min(int(num_sims), 50000)), score_sd, rng)

    odds = []
    for t in teams:
        tid = t["team_id"]
        odds.append({
            "team_id": tid,
            "name": names.get(tid, f"Team {tid}"),
            "record": t["record"],
            "mean_ppg": round(t["mean"], 1),
            "playoff_pct": sim[tid]["playoff_pct"],
            "avg_seed": sim[tid]["avg_seed"],
        })
    odds.sort(key=lambda x: x["playoff_pct"], reverse=True)

    result = {
        "odds": odds,
        "playoff_teams": playoff_teams,
        "regular_season_weeks": regular_weeks,
        "current_week": current_week,
        "games_remaining": len(schedule),
        "num_sims": max(100, min(int(num_sims), 50000)),
        "message": (f"Playoff odds over {len(schedule)} remaining games "
                    f"({max(100, min(int(num_sims), 50000))} sims); top {playoff_teams} make it"),
    }

    # Optional: win-this-week vs lose-this-week swing for one team.
    if team_id is not None and schedule:
        my_game = next(((a, b) for (a, b) in schedule if team_id in (a, b)), None)
        if my_game:
            opp = my_game[1] if my_game[0] == team_id else my_game[0]
            rest = [g for g in schedule if g != my_game]

            def _clone(win_id):
                cloned = []
                for t in teams:
                    c = dict(t)
                    if c["team_id"] == win_id:
                        c["wins"] = c["wins"] + 1
                    cloned.append(c)
                return cloned

            win_sim = _simulate(_clone(team_id), rest, playoff_teams, 5000, score_sd, random.Random(seed))
            lose_sim = _simulate(_clone(opp), rest, playoff_teams, 5000, score_sd, random.Random(seed))
            result["this_week_swing"] = {
                "team_id": team_id,
                "opponent_team_id": opp,
                "if_win_pct": win_sim[team_id]["playoff_pct"],
                "if_lose_pct": lose_sim[team_id]["playoff_pct"],
            }

    return create_success_response(result)
