"""Tests for the FAAB bid recommender (offline, mocked ESPN + values)."""

from unittest.mock import patch

from nfl_mcp import faab_tools as ft


class FakeService:
    def __init__(self, by_id):
        self._by_id = {str(k): v for k, v in by_id.items()}

    async def get_values(self, *a, **k):
        return {"source": "fantasycalc", "list": list(self._by_id.values())}

    def lookup(self, idx, player_id=None, name=None, position=None):
        return self._by_id.get(str(player_id)) or next(
            (v for v in self._by_id.values() if v.get("name") == name), None)


def _league(faab=True, budget=100):
    return {"success": True, "league": {
        "settings": {
            "scoringSettings": {"scoringItems": [{"statId": 53, "points": 1.0}]},
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "20": 6}},
            "size": 12,
            "draftSettings": {"keeperCount": 0},
            "acquisitionSettings": {
                "isUsingAcquisitionBudget": faab,
                "acquisitionBudget": budget if faab else 0,
            },
        },
    }}


def _entry(player_id, position_id=2, lineup_slot_id=20):
    return {
        "lineupSlotId": lineup_slot_id,
        "playerPoolEntry": {"player": {
            "id": player_id, "fullName": f"Player {player_id}", "defaultPositionId": position_id,
        }},
    }


# Target: elite RB (value 10000, RB#1). Some other RBs for "redundant" case.
VALUES = {
    "9509": {"player_id": "9509", "name": "Bijan Robinson", "position": "RB", "value": 10000, "position_rank": 1},
    "elite1": {"player_id": "elite1", "name": "Elite RB A", "position": "RB", "value": 9500, "position_rank": 2},
    "elite2": {"player_id": "elite2", "name": "Elite RB B", "position": "RB", "value": 9300, "position_rank": 3},
    "weak": {"player_id": "weak", "name": "Weak RB", "position": "RB", "value": 800, "position_rank": 60},
}


def _patches(league, rosters, week=10):
    async def L(league_id): return league
    async def R(league_id, detail="summary"): return rosters
    async def W(): return week
    return [
        patch.object(ft, "get_espn_league", L),
        patch.object(ft, "get_espn_rosters", R),
        patch.object(ft, "get_current_nfl_week", W),
        patch.object(ft, "get_values_service", lambda db=None: FakeService(VALUES)),
    ]


async def _run(**kwargs):
    league = kwargs.pop("league", _league())
    rosters = kwargs.pop("rosters", {"success": True, "rosters": []})
    import contextlib
    with contextlib.ExitStack() as stack:
        for p in _patches(league, rosters):
            stack.enter_context(p)
        return await ft.recommend_faab_bid(**kwargs)


class TestFaab:
    async def test_elite_add_thin_roster_is_must_add(self):
        rosters = {"success": True, "rosters": [
            {"id": 1, "roster": {"entries": [_entry("weak")]},
             "transactionCounter": {"acquisitionBudgetSpent": 20}}]}
        res = await _run(league_id="1", player_id="9509", team_id=1, rosters=rosters)
        r = res["recommendation"]
        assert res["is_faab_league"] is True
        assert r["tier"] == "must_add"
        assert r["bid_pct"] >= 30
        assert r["bid_absolute"] is not None
        assert res["remaining_budget"] == 80

    async def test_redundant_add_is_cheaper_and_warns(self):
        # I roster two RBs better than the target -> it's depth, not an upgrade.
        rosters = {"success": True, "rosters": [
            {"id": 1, "roster": {"entries": [_entry("9509"), _entry("elite1")]},
             "transactionCounter": {"acquisitionBudgetSpent": 0}}]}
        # Target the weaker RB (value 9300, below my last starter 9500).
        res = await _run(league_id="1", player_id="elite2", team_id=1, rosters=rosters)
        r = res["recommendation"]
        assert r["breakdown"]["upgrade_score"] == 0.0   # no upgrade
        assert any("strong at RB" in w for w in r["warnings"])

    async def test_non_faab_league(self):
        res = await _run(league_id="1", player_id="9509", league=_league(faab=False))
        assert res["is_faab_league"] is False
        assert any("Not a FAAB" in w for w in res["recommendation"]["warnings"])

    async def test_player_not_in_values(self):
        res = await _run(league_id="1", player_id="unknown_id")
        assert res["recommendation"] is None
        assert "consensus value list" in res["message"]

    async def test_requires_player(self):
        res = await ft.recommend_faab_bid(league_id="1", db=None)
        assert res["success"] is False

    async def test_demand_is_frozen_unavailable(self):
        """No ESPN equivalent to Sleeper's trending-adds signal -- demand is
        never fabricated, just reported as unavailable (issue #50)."""
        res = await _run(league_id="1", player_id="9509")
        r = res["recommendation"]
        assert r["breakdown"]["demand_mult"] == 1.0
        assert r["breakdown"]["demand_label"] == "unavailable"
        assert any("unavailable" in reason for reason in r["reasoning"])

    async def test_team_not_found_warns_and_falls_back_to_absolute_value(self):
        res = await _run(league_id="1", player_id="9509", team_id=999,
                          rosters={"success": True, "rosters": []})
        r = res["recommendation"]
        assert any("Team 999 not found" in w for w in r["warnings"])
