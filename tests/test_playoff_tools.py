"""Tests for the Monte-Carlo playoff odds tool (offline, mocked ESPN)."""

import contextlib
import random
from unittest.mock import patch

from nfl_mcp import playoff_tools as pt


class TestSimulate:
    def test_strong_beats_weak_and_deterministic(self):
        teams = [
            {"team_id": 1, "wins": 10, "points": 1440, "mean": 120},
            {"team_id": 2, "wins": 8, "points": 1344, "mean": 112},
            {"team_id": 3, "wins": 2, "points": 1080, "mean": 90},
            {"team_id": 4, "wins": 1, "points": 1020, "mean": 85},
        ]
        schedule = [(1, 3), (2, 4), (1, 2), (3, 4)]
        a = pt._simulate(teams, schedule, 2, 3000, 25.0, random.Random(1))
        b = pt._simulate(teams, schedule, 2, 3000, 25.0, random.Random(1))
        # deterministic with same seed
        assert a[1]["playoff_pct"] == b[1]["playoff_pct"]
        # strong team makes playoffs far more often than weak team
        assert a[1]["playoff_pct"] > a[4]["playoff_pct"]
        # top-2 league: total made ≈ 2 per sim -> pct sums near 200
        assert abs(sum(v["playoff_pct"] for v in a.values()) - 200.0) < 1.0


class TestBuildRemainingSchedule:
    async def test_skips_bye_week_entries(self):
        """An entry missing `home` or `away` (a bye week) is skipped, not crashed on."""
        async def M(league_id, week=None):
            return {"success": True, "matchups": [
                {"id": 1, "home": {"teamId": 1}, "away": {"teamId": 2}},
                {"id": 2, "home": {"teamId": 3}},  # bye week: no `away`
                {"id": 3, "away": {"teamId": 4}},  # bye week: no `home`
            ]}

        with patch.object(pt, "get_espn_matchups", M):
            schedule = await pt._build_remaining_schedule("123", [1])

        assert schedule == [(1, 2)]


def _mock_league(playoff_teams=4, matchup_period_count=14):
    return {"success": True, "league": {"settings": {"scheduleSettings": {
        "playoffTeamCount": playoff_teams, "matchupPeriodCount": matchup_period_count}}}}


def _mock_rosters():
    def team(tid, w, l, ppg):
        return {
            "id": tid,
            "owners": [f"{{owner-{tid}}}"],
            "primaryOwner": f"{{owner-{tid}}}",
            "record": {"overall": {"wins": w, "losses": l, "ties": 0, "pointsFor": ppg * (w + l)}},
        }
    return {
        "success": True,
        "rosters": [
            team(1, 10, 2, 120), team(2, 8, 4, 112), team(3, 7, 5, 108),
            team(4, 6, 6, 104), team(5, 4, 8, 98), team(6, 2, 10, 90),
        ],
        "members": [
            {"id": f"{{owner-{i}}}", "displayName": f"Team{i}"} for i in range(1, 7)
        ],
    }


def _mock_matchups(week):
    pairs = {13: [(1, 6), (2, 5), (3, 4)], 14: [(1, 2), (3, 6), (4, 5)]}.get(week, [])
    ms = [{"id": mid, "home": {"teamId": a}, "away": {"teamId": b}} for mid, (a, b) in enumerate(pairs, 1)]
    return {"success": True, "matchups": ms, "week": week}


@contextlib.contextmanager
def _patched():
    async def L(league_id): return _mock_league()
    async def R(league_id): return _mock_rosters()
    async def W(): return 13
    async def M(league_id, week=None): return _mock_matchups(week)
    with patch.object(pt, "get_espn_league", L), patch.object(pt, "get_espn_rosters", R), \
         patch.object(pt, "get_current_nfl_week", W), \
         patch.object(pt, "get_espn_matchups", M):
        yield


class TestGetPlayoffOdds:
    async def test_ordering_and_bounds(self):
        with _patched():
            res = await pt.get_playoff_odds("123", num_sims=5000, seed=42)
        assert res["success"] is True
        assert res["current_week"] == 13
        assert res["games_remaining"] == 6
        odds = res["odds"]
        by_id = {o["team_id"]: o for o in odds}
        assert by_id[1]["playoff_pct"] >= by_id[6]["playoff_pct"]
        assert by_id[1]["playoff_pct"] == 100.0    # 10-2 juggernaut always in
        assert by_id[6]["playoff_pct"] < 20.0       # 2-10 near-dead
        # sorted best-first
        assert odds == sorted(odds, key=lambda x: x["playoff_pct"], reverse=True)
        # owner names resolved via resolve_team_owner_names
        assert by_id[1]["name"] == "Team1"

    async def test_this_week_swing(self):
        with _patched():
            res = await pt.get_playoff_odds("123", num_sims=3000, seed=1, team_id=4)
        sw = res["this_week_swing"]
        assert sw["team_id"] == 4
        # winning never hurts your odds
        assert sw["if_win_pct"] >= sw["if_lose_pct"]

    async def test_league_load_failure(self):
        async def L(league_id): return {"success": False, "error": "nope"}
        with patch.object(pt, "get_espn_league", L):
            res = await pt.get_playoff_odds("123")
        assert res["success"] is False

    async def test_co_managed_team_resolves_to_primary_owner(self):
        """A team with multiple owners (co-managers) still resolves to one name."""
        async def L(league_id): return _mock_league()

        async def R(league_id):
            rosters = _mock_rosters()
            rosters["rosters"][0]["owners"] = ["{owner-1}", "{co-owner-1}"]
            rosters["rosters"][0]["primaryOwner"] = "{co-owner-1}"
            rosters["members"].append({"id": "{co-owner-1}", "displayName": "Co-Manager"})
            return rosters

        async def W(): return 13
        async def M(league_id, week=None): return _mock_matchups(week)

        with patch.object(pt, "get_espn_league", L), patch.object(pt, "get_espn_rosters", R), \
             patch.object(pt, "get_current_nfl_week", W), patch.object(pt, "get_espn_matchups", M):
            res = await pt.get_playoff_odds("123", num_sims=1000, seed=1)

        by_id = {o["team_id"]: o for o in res["odds"]}
        assert by_id[1]["name"] == "Co-Manager"
