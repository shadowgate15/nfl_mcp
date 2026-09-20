"""Tests for the draft assistant (draft_tools.py).

Values and ESPN draft calls are patched so tests are deterministic/offline.
"""

import tempfile
from unittest.mock import patch

from nfl_mcp import draft_tools as dt
from nfl_mcp import player_values as pv
from nfl_mcp.database import NFLDatabase

# A small but position-diverse value pool.
POOL = [
    {"player_id": "1", "name": "RB One", "position": "RB", "team": "ATL", "value": 10000, "overall_rank": 1, "position_rank": 1, "tier": 1, "trend_30day": 5},
    {"player_id": "2", "name": "RB Two", "position": "RB", "team": "DAL", "value": 8000, "overall_rank": 4, "position_rank": 2, "tier": 1, "trend_30day": 0},
    {"player_id": "3", "name": "WR One", "position": "WR", "team": "CIN", "value": 9500, "overall_rank": 2, "position_rank": 1, "tier": 1, "trend_30day": 2},
    {"player_id": "4", "name": "WR Two", "position": "WR", "team": "LAR", "value": 9000, "overall_rank": 3, "position_rank": 2, "tier": 2, "trend_30day": 1},
    {"player_id": "5", "name": "QB One", "position": "QB", "team": "BUF", "value": 5000, "overall_rank": 10, "position_rank": 1, "tier": 3, "trend_30day": 0},
    {"player_id": "6", "name": "TE One", "position": "TE", "team": "KC", "value": 4000, "overall_rank": 12, "position_rank": 1, "tier": 3, "trend_30day": 0},
]


def _temp_db():
    return NFLDatabase(tempfile.mktemp(suffix=".db"))


def _service_with_pool(db):
    pv._service = None
    svc = pv.get_values_service(db)
    return svc


class TestPureHelpers:
    def test_replacement_baselines_superflex(self):
        base = dt.replacement_baselines(12, superflex=False)
        base_sf = dt.replacement_baselines(12, superflex=True)
        assert base["QB"] == 12
        assert base_sf["QB"] == 24  # superflex doubles QB baseline
        assert base["WR"] > base["QB"]

    def test_compute_vbd_orders_by_value_over_replacement(self):
        out = dt.compute_vbd(POOL, num_teams=12, superflex=False)
        # every ranked player gets a vbd
        assert all(p["vbd"] is not None for p in out["players"])
        # replacement value recorded per position
        assert set(out["replacement"].keys()) == {"RB", "WR", "QB", "TE"}
        # highest vbd first
        vbds = [p["vbd"] for p in out["players"]]
        assert vbds == sorted(vbds, reverse=True)

    def test_starter_requirements(self):
        reqs = dt._starter_requirements({"slots_qb": 1, "slots_rb": 2, "slots_wr": 3, "slots_te": 1, "slots_flex": 1})
        assert reqs == {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1}

    def test_starter_requirements_counts_all_flex_variants(self):
        # Real Sleeper league: flex + wrrb_flex + rec_flex = 3 flex slots; super_flex -> QB.
        reqs = dt._starter_requirements({
            "slots_qb": 1, "slots_rb": 2, "slots_wr": 2, "slots_te": 1,
            "slots_flex": 1, "slots_wrrb_flex": 1, "slots_rec_flex": 1,
            "slots_super_flex": 1,
        })
        assert reqs["FLEX"] == 3
        assert reqs["QB"] == 2  # base QB + superflex

    def test_need_multiplier(self):
        reqs = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1}
        # need a starter -> boosted
        mult, label = dt._need_multiplier("RB", {"RB": 0}, reqs, flex_filled=0)
        assert mult > 1 and label == "need_starter"
        # overfilled -> discounted
        mult, label = dt._need_multiplier("RB", {"RB": 4}, reqs, flex_filled=1)
        assert mult < 1 and label == "overfilled"


class TestDraftBoard:
    async def test_get_draft_board(self):
        db = _temp_db()
        svc = _service_with_pool(db)
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(POOL)):
            res = await dt.get_draft_board(scoring="ppr", num_teams=12, limit=10, db=db)
        assert res["success"] is True
        assert res["total"] == len(POOL)
        # board sorted by vbd desc
        vbds = [p["vbd"] for p in res["board"]]
        assert vbds == sorted(vbds, reverse=True)
        assert "RB" in res["tiers_by_position"]
        pv._service = None

    async def test_get_draft_board_position_filter(self):
        db = _temp_db()
        svc = _service_with_pool(db)
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(POOL)):
            res = await dt.get_draft_board(scoring="ppr", position="WR", db=db)
        assert all(p["position"] == "WR" for p in res["board"])
        pv._service = None


class TestEspnSettingsAdapters:
    def test_espn_settings_to_sleeper_shape(self):
        espn_settings = {
            "size": 10,
            "rosterSettings": {"lineupSlotCounts": {
                "0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "7": 0,
            }},
        }
        shape = dt._espn_settings_to_sleeper_shape(espn_settings)
        assert shape == {
            "teams": 10, "slots_qb": 1, "slots_rb": 2, "slots_wr": 2, "slots_te": 1,
            "slots_flex": 1, "slots_rb_wr": 0, "slots_wr_te": 0, "slots_super_flex": 0,
        }

    def test_espn_settings_to_sleeper_shape_superflex(self):
        espn_settings = {"size": 12, "rosterSettings": {"lineupSlotCounts": {"0": 1, "7": 1}}}
        shape = dt._espn_settings_to_sleeper_shape(espn_settings)
        assert shape["slots_super_flex"] == 1

    def test_espn_settings_to_sleeper_shape_handles_missing_rosterSettings(self):
        assert dt._espn_settings_to_sleeper_shape({})["teams"] == 12

    def test_ppr_from_espn_settings_reads_statid_53(self):
        assert dt._ppr_from_espn_settings(
            {"scoringSettings": {"scoringItems": [{"statId": 53, "points": 1.0}]}}
        ) == 1.0
        assert dt._ppr_from_espn_settings(
            {"scoringSettings": {"scoringItems": [{"statId": 53, "points": 0.5}]}}
        ) == 0.5
        assert dt._ppr_from_espn_settings(
            {"scoringSettings": {"scoringItems": [{"statId": 53, "points": 0}]}}
        ) == 0.0

    def test_ppr_from_espn_settings_falls_back_to_full_ppr(self):
        # statId 53 absent entirely -> full PPR, per ADR 0007's convention.
        assert dt._ppr_from_espn_settings({"scoringSettings": {"scoringItems": []}}) == 1.0
        assert dt._ppr_from_espn_settings({}) == 1.0

    def test_scoring_label_from_ppr(self):
        assert dt._scoring_label_from_ppr(1.0) == "ppr"
        assert dt._scoring_label_from_ppr(0.5) == "half-ppr"
        assert dt._scoring_label_from_ppr(0.0) == "standard"


class TestEspnPickAdapter:
    """The ESPN draftDetail.picks[] -> Sleeper-shaped pick dict adapter."""

    def test_adapts_using_player_cache(self):
        pick = {"playerId": 100, "roundId": 2, "teamId": 5, "overallPickNumber": 17}
        cache = {"100": {"full_name": "Cache Player", "position": "wr"}}
        out = dt._espn_pick_to_sleeper_pick(pick, cache, {})
        assert out == {
            "player_id": "100",
            "round": 2,
            "draft_slot": 5,
            "metadata": {"first_name": "Cache", "last_name": "Player", "position": "WR"},
        }

    def test_falls_back_to_fantasycalc_values_when_uncached(self):
        pick = {"playerId": 200, "roundId": 1, "teamId": 3}
        values_by_id = {"200": {"name": "Value Player", "position": "RB"}}
        out = dt._espn_pick_to_sleeper_pick(pick, {}, values_by_id)
        assert out["player_id"] == "200"
        assert out["metadata"] == {"first_name": "Value", "last_name": "Player", "position": "RB"}

    def test_player_cache_takes_priority_over_values(self):
        pick = {"playerId": 300, "roundId": 1, "teamId": 1}
        cache = {"300": {"full_name": "Cached Name", "position": "TE"}}
        values_by_id = {"300": {"name": "Stale Value Name", "position": "WR"}}
        out = dt._espn_pick_to_sleeper_pick(pick, cache, values_by_id)
        assert out["metadata"]["first_name"] == "Cached"
        assert out["metadata"]["position"] == "TE"

    def test_missing_player_id_yields_no_id_and_blank_metadata(self):
        out = dt._espn_pick_to_sleeper_pick({"roundId": 1, "teamId": 1}, {}, {})
        assert out["player_id"] is None
        assert out["metadata"] == {"first_name": "", "last_name": "", "position": ""}


class TestRecommendPick:
    LEAGUE = {
        "success": True,
        "league": {
            "settings": {
                "size": 12,
                "rosterSettings": {"lineupSlotCounts": {
                    "0": 1, "2": 2, "4": 2, "6": 1, "23": 1,
                }},
                "scoringSettings": {"scoringItems": [{"statId": 53, "points": 1.0}]},
                "draftSettings": {"keeperCount": 0},
            },
        },
    }

    def _draft(self, picks):
        return {
            "success": True,
            "draft": {"draftDetail": {"drafted": True, "inProgress": False, "picks": picks}},
        }

    async def test_recommend_weights_by_roster_need(self):
        db = _temp_db()
        svc = _service_with_pool(db)
        # My team=3 already has 2 RBs -> RB starters filled, should prefer WR/QB/TE.
        picks = [
            {"playerId": 1, "roundId": 1, "teamId": 3, "overallPickNumber": 1},
            {"playerId": 2, "roundId": 2, "teamId": 3, "overallPickNumber": 2},
        ]
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(POOL)), \
             patch.object(dt, "get_espn_draft", return_value=self._draft(picks)), \
             patch.object(dt, "get_espn_league", return_value=self.LEAGUE):
            res = await dt.recommend_draft_pick("1234", my_slot=3, num_suggestions=3, db=db)
        assert res["success"] is True
        assert res["picks_made"] == 2
        # drafted RBs must not be suggested
        suggested_ids = {s["player_id"] for s in res["suggestions"]}
        assert "1" not in suggested_ids and "2" not in suggested_ids
        # top suggestion should NOT be an (overfilled) RB
        assert res["top_pick"]["position"] != "RB"
        assert res["my_roster"]["position_counts"]["RB"] == 2
        pv._service = None

    async def test_recommend_best_available_without_slot(self):
        db = _temp_db()
        svc = _service_with_pool(db)
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(POOL)), \
             patch.object(dt, "get_espn_draft", return_value=self._draft([])), \
             patch.object(dt, "get_espn_league", return_value=self.LEAGUE):
            res = await dt.recommend_draft_pick("1234", my_slot=None, num_suggestions=3, db=db)
        assert res["success"] is True
        assert res["my_roster"] is None
        # highest-VBD player leads
        assert res["top_pick"]["vbd"] == max(s["vbd"] for s in res["suggestions"])
        pv._service = None

    async def test_recommend_requires_league_id(self):
        res = await dt.recommend_draft_pick("", db=_temp_db())
        assert res["success"] is False

    async def test_recommend_surfaces_draft_load_error(self):
        db = _temp_db()
        svc = _service_with_pool(db)
        failed_draft = {"success": False, "draft": None, "error": "boom"}
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(POOL)), \
             patch.object(dt, "get_espn_draft", return_value=failed_draft), \
             patch.object(dt, "get_espn_league", return_value=self.LEAGUE):
            res = await dt.recommend_draft_pick("1234", db=db)
        assert res["success"] is False
        pv._service = None

    async def test_recommend_surfaces_league_settings_load_error(self):
        db = _temp_db()
        svc = _service_with_pool(db)
        failed_league = {"success": False, "league": None, "error": "boom"}
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=list(POOL)), \
             patch.object(dt, "get_espn_draft", return_value=self._draft([])), \
             patch.object(dt, "get_espn_league", return_value=failed_league):
            res = await dt.recommend_draft_pick("1234", db=db)
        assert res["success"] is False
        pv._service = None


# A larger pool so a full mock draft can fill starters + bench for all teams
# without the player pool starving under position caps.
def _big_pool(n_per_pos=60):
    pool = []
    rank = 1
    for pos, base in (("RB", 10000), ("WR", 9800), ("QB", 6000), ("TE", 5000)):
        for i in range(n_per_pos):
            pool.append({
                "player_id": f"{pos}{i}", "name": f"{pos} Player {i}", "position": pos,
                "team": "ATL", "value": base - i * 100, "overall_rank": rank,
                "position_rank": i + 1, "tier": (i // 6) + 1, "trend_30day": 0,
            })
            rank += 1
    return pool


class TestSimulateDraft:
    async def _run(self, db, **kw):
        svc = _service_with_pool(db)
        with patch.object(svc, "_fetch_from_fantasycalc", return_value=_big_pool()):
            return await dt.simulate_draft(db=db, **kw)

    async def test_single_sim_fills_starters(self):
        db = _temp_db()
        res = await self._run(db, my_slot=3, num_teams=12, rounds=15, seed=42)
        assert res["success"] is True
        sample = res["sample"]
        assert len(sample["my_team"]) == 15
        # roster must satisfy starter requirements (QB1/RB2/WR2/TE1)
        assert sample["starters_filled"] is True
        counts = sample["my_position_counts"]
        assert counts.get("QB", 0) >= 1 and counts.get("TE", 0) >= 1
        # no position wildly over-stacked (caps enforced)
        assert counts.get("WR", 0) <= 7 and counts.get("RB", 0) <= 7
        assert 1 <= sample["my_value_rank"] <= 12
        pv._service = None

    async def test_deterministic_with_seed(self):
        db = _temp_db()
        a = await self._run(db, my_slot=5, num_teams=10, seed=7)
        b = await self._run(db, my_slot=5, num_teams=10, seed=7)
        assert [r["player_id"] for r in a["sample"]["my_team"]] == \
               [r["player_id"] for r in b["sample"]["my_team"]]
        pv._service = None

    async def test_multi_sim_aggregate(self):
        db = _temp_db()
        res = await self._run(db, my_slot=1, num_teams=12, num_sims=10, seed=1)
        assert res["num_sims"] == 10
        agg = res["aggregate"]
        assert "avg_position_counts" in agg
        assert "avg_value_rank" in agg
        assert sum(agg["grade_distribution"].values()) == 10
        pv._service = None

    async def test_invalid_slot(self):
        db = _temp_db()
        res = await self._run(db, my_slot=20, num_teams=12)
        assert res["success"] is False
        pv._service = None
