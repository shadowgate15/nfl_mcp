"""Tests for opponent_analysis_tools module."""
from unittest.mock import patch

import pytest

from nfl_mcp.opponent_analysis_tools import OpponentAnalyzer, analyze_opponent


class TestOpponentAnalyzer:
    """Test OpponentAnalyzer class."""

    @pytest.fixture
    def analyzer(self):
        return OpponentAnalyzer()

    def test_position_strength_empty_roster(self, analyzer):
        """Test position assessment with empty roster."""
        result = analyzer._assess_position_strength([], "RB")

        assert result["strength_score"] == 0
        assert result["depth_count"] == 0
        assert result["weakness_level"] == "critical"
        assert "No players at position" in result["concerns"][0]

    def test_position_strength_strong_roster(self, analyzer):
        """Test position assessment with strong roster."""
        players = [
            {"snap_pct": 85, "practice_status": "E", "usage_trend_overall": "stable"},
            {"snap_pct": 75, "practice_status": "E", "usage_trend_overall": "stable"},
            {"snap_pct": 60, "practice_status": "Q", "usage_trend_overall": "up"},
            {"snap_pct": 40, "practice_status": "E", "usage_trend_overall": "stable"},
        ]

        result = analyzer._assess_position_strength(players, "RB")

        assert result["strength_score"] >= 50
        assert result["depth_count"] == 4
        assert result["weakness_level"] == "strong"

    def test_position_strength_weak_roster(self, analyzer):
        """Test position assessment with weak roster."""
        players = [
            {"snap_pct": 30, "practice_status": "DNP", "usage_trend_overall": "down"}
        ]

        result = analyzer._assess_position_strength(players, "RB")

        assert result["weakness_level"] in ["weak", "critical"]
        assert result["injury_concerns"] == 1

    def test_position_strength_injury_concerns(self, analyzer):
        """Test injury concern detection."""
        players = [
            {"snap_pct": 70, "practice_status": "DNP", "usage_trend_overall": "stable"},
            {"snap_pct": 60, "practice_status": "LP", "usage_trend_overall": "stable"},
        ]

        result = analyzer._assess_position_strength(players, "WR")

        assert result["injury_concerns"] == 2
        assert any("injury" in c.lower() for c in result["concerns"])

    def test_identify_starter_weaknesses(self, analyzer):
        """Test starter weakness identification."""
        starters = [
            {
                "player_id": "1",
                "full_name": "John Smith",
                "position": "RB",
                "practice_status": "DNP",
                "usage_trend_overall": "down",
                "snap_pct": 45.0
            }
        ]

        weaknesses = analyzer._identify_starter_weaknesses(starters)

        assert len(weaknesses) == 1
        assert weaknesses[0]["player_name"] == "John Smith"
        assert any("DNP" in w for w in weaknesses[0]["weaknesses"])
        assert weaknesses[0]["severity"] == "high"

    def test_identify_starter_weaknesses_clean(self, analyzer):
        """Test starter weakness identification with no issues."""
        starters = [
            {
                "player_id": "1",
                "full_name": "Healthy Player",
                "position": "RB",
                "practice_status": "E",
                "usage_trend_overall": "stable",
                "snap_pct": 90.0
            }
        ]

        weaknesses = analyzer._identify_starter_weaknesses(starters)

        assert len(weaknesses) == 0

    def test_generate_exploitation_strategies(self, analyzer):
        """Test strategy generation."""
        position_assessments = {
            "RB": {
                "strength_score": 20,
                "weakness_level": "weak",
                "concerns": ["Shallow depth"]
            }
        }
        starter_weaknesses = []

        strategies = analyzer._generate_exploitation_strategies(position_assessments, starter_weaknesses)

        assert len(strategies) > 0
        assert strategies[0]["category"] == "position_weakness"
        assert strategies[0]["priority"] == "critical"

    def test_analyze_opponent_roster(self, analyzer):
        """Test comprehensive roster analysis."""
        roster = {
            "team_id": 123,
            "players_enriched": [
                {"player_id": "1", "full_name": "P1", "position": "RB", "snap_pct": 80, "practice_status": "E", "usage_trend_overall": "stable"},
                {"player_id": "2", "full_name": "P2", "position": "QB", "snap_pct": 30, "practice_status": "DNP", "usage_trend_overall": "down"},
            ],
            "starters_enriched": [
                {"player_id": "1", "full_name": "P1", "position": "RB"},
                {"player_id": "2", "full_name": "P2", "position": "QB"},
            ]
        }

        result = analyzer.analyze_opponent_roster(roster)

        assert "vulnerability_score" in result
        assert "vulnerability_level" in result
        assert "position_assessments" in result
        assert "exploitation_strategies" in result
        assert result["team_id"] == 123

    def test_analyze_opponent_roster_empty_is_no_data(self, analyzer):
        """An empty (undrafted) roster is flagged unknown, not maximally vulnerable."""
        result = analyzer.analyze_opponent_roster({"team_id": 5, "players_enriched": [], "starters_enriched": []})

        assert result["vulnerability_score"] is None
        assert result["vulnerability_level"] == "unknown"
        assert result["no_data"] is True
        assert result["team_id"] == 5


class TestAnalyzeOpponent:
    """Test analyze_opponent async function."""

    @pytest.mark.asyncio
    async def test_analyze_opponent_missing_league_id(self):
        """Test with missing league_id."""
        result = await analyze_opponent("", 1)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_analyze_opponent_missing_team_id(self):
        """Test with missing opponent_team_id."""
        result = await analyze_opponent("league1", None)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_analyze_opponent_team_not_found(self):
        """Test with non-existent team."""
        mock_result = {"success": True, "rosters": [{"id": 2}], "members": []}

        async def mock_get_espn_rosters(league_id):
            return mock_result

        with patch('nfl_mcp.opponent_analysis_tools.get_espn_rosters', side_effect=mock_get_espn_rosters):
            result = await analyze_opponent("league1", 999)
            assert result["success"] is False

    @pytest.mark.asyncio
    async def test_analyze_opponent_success(self):
        """Test successful analysis, including owner-name resolution."""
        mock_roster = {
            "id": 1,
            "owners": ["{owner-1}"],
            "primaryOwner": "{owner-1}",
            "roster": {"entries": []},
        }
        mock_members = [{"id": "{owner-1}", "displayName": "Test User"}]

        async def mock_get_espn_rosters(league_id):
            return {"success": True, "rosters": [mock_roster], "members": mock_members}

        with patch('nfl_mcp.opponent_analysis_tools.get_espn_rosters', side_effect=mock_get_espn_rosters):
            result = await analyze_opponent("league1", 1)

            assert result["success"] is True
            assert result["opponent_name"] == "Test User"
            assert "vulnerability_score" in result

    @pytest.mark.asyncio
    async def test_analyze_opponent_co_managed_team_resolves_primary_owner(self):
        """A team with multiple owners (co-managers) resolves to the primary owner's name."""
        mock_roster = {
            "id": 1,
            "owners": ["{owner-1}", "{owner-2}"],
            "primaryOwner": "{owner-2}",
            "roster": {"entries": []},
        }
        mock_members = [
            {"id": "{owner-1}", "displayName": "First Owner"},
            {"id": "{owner-2}", "displayName": "Second Owner"},
        ]

        async def mock_get_espn_rosters(league_id):
            return {"success": True, "rosters": [mock_roster], "members": mock_members}

        with patch('nfl_mcp.opponent_analysis_tools.get_espn_rosters', side_effect=mock_get_espn_rosters):
            result = await analyze_opponent("league1", 1)

            assert result["success"] is True
            assert result["opponent_name"] == "Second Owner"

    @pytest.mark.asyncio
    async def test_analyze_opponent_builds_enriched_players_from_roster_entries(self):
        """Roster entries (ESPN summary shape) become players_enriched/starters_enriched,
        splitting starters from bench (slot 20) and IR (slot 21)."""
        mock_roster = {
            "id": 1,
            "owners": [],
            "roster": {"entries": [
                {"playerId": 100, "fullName": "Starter Guy", "defaultPositionId": 2, "lineupSlotId": 2},
                {"playerId": 200, "fullName": "Bench Guy", "defaultPositionId": 2, "lineupSlotId": 20},
                {"playerId": 300, "fullName": "IR Guy", "defaultPositionId": 2, "lineupSlotId": 21},
            ]},
        }

        async def mock_get_espn_rosters(league_id):
            return {"success": True, "rosters": [mock_roster], "members": []}

        with patch('nfl_mcp.opponent_analysis_tools.get_espn_rosters', side_effect=mock_get_espn_rosters):
            result = await analyze_opponent("league1", 1)

        assert result["success"] is True
        # analyze_opponent_roster only surfaces starter_weaknesses/position_assessments,
        # not the raw player lists, so verify indirectly via position depth.
        assert result["position_assessments"]["RB"]["depth_count"] == 3

    @pytest.mark.asyncio
    async def test_analyze_opponent_batches_player_cache_lookups(self):
        """Player-identity lookups go through one batched db.get_athletes_by_ids
        call, not one db.get_athlete_by_id call per roster entry."""
        class _FakeDB:
            def __init__(self):
                self.batch_calls: list[list[str]] = []

            def get_athletes_by_ids(self, athlete_ids):
                self.batch_calls.append(list(athlete_ids))
                return {"100": {"full_name": "Cached Name", "position": "RB", "team_id": "KC"}}

        mock_roster = {
            "id": 1,
            "owners": [],
            "roster": {"entries": [
                {"playerId": 100, "fullName": "Raw Name", "defaultPositionId": 2, "lineupSlotId": 2},
                {"playerId": 200, "fullName": "Other Guy", "defaultPositionId": 3, "lineupSlotId": 3},
            ]},
        }

        async def mock_get_espn_rosters(league_id):
            return {"success": True, "rosters": [mock_roster], "members": []}

        fake_db = _FakeDB()
        with patch('nfl_mcp.opponent_analysis_tools.get_espn_rosters', side_effect=mock_get_espn_rosters):
            result = await analyze_opponent("league1", 1, db=fake_db)

        assert result["success"] is True
        # one batched call covering both entries, not two individual lookups
        assert fake_db.batch_calls == [["100", "200"]]

    @pytest.mark.asyncio
    async def test_analyze_opponent_with_matchup_context_projected_none_when_not_live(self):
        """projected_points degrades to None when totalProjectedPointsLive is absent
        (i.e. outside an in-progress week), rather than a fabricated value."""
        mock_roster = {"id": 2, "owners": [], "roster": {"entries": []}}

        async def mock_get_espn_rosters(league_id):
            return {"success": True, "rosters": [mock_roster], "members": []}

        async def mock_get_espn_matchups(league_id, week=None):
            return {"success": True, "matchups": [
                {"id": 1, "home": {"teamId": 2, "totalPoints": 105.5}, "away": {"teamId": 3, "totalPoints": 99.0}}
            ]}

        with patch('nfl_mcp.opponent_analysis_tools.get_espn_rosters', side_effect=mock_get_espn_rosters), \
             patch('nfl_mcp.opponent_analysis_tools.get_espn_matchups', side_effect=mock_get_espn_matchups):
            result = await analyze_opponent("league1", 2, current_week=10)

        assert result["success"] is True
        assert result["matchup_context"]["week"] == 10
        assert result["matchup_context"]["points"] == 105.5
        assert result["matchup_context"]["projected_points"] is None

    @pytest.mark.asyncio
    async def test_analyze_opponent_matchup_context_projected_points_when_live(self):
        """projected_points surfaces totalProjectedPointsLive when the week is in progress."""
        mock_roster = {"id": 2, "owners": [], "roster": {"entries": []}}

        async def mock_get_espn_rosters(league_id):
            return {"success": True, "rosters": [mock_roster], "members": []}

        async def mock_get_espn_matchups(league_id, week=None):
            return {"success": True, "matchups": [
                {"id": 1, "home": {"teamId": 2, "totalPoints": 50.0, "totalPointsLive": 50.0,
                                    "totalProjectedPointsLive": 110.2},
                 "away": {"teamId": 3, "totalPoints": 40.0}}
            ]}

        with patch('nfl_mcp.opponent_analysis_tools.get_espn_rosters', side_effect=mock_get_espn_rosters), \
             patch('nfl_mcp.opponent_analysis_tools.get_espn_matchups', side_effect=mock_get_espn_matchups):
            result = await analyze_opponent("league1", 2, current_week=10)

        assert result["matchup_context"]["projected_points"] == 110.2

    @pytest.mark.asyncio
    async def test_analyze_opponent_matchup_bye_week_leaves_context_none(self):
        """A bye-week matchup entry (missing home or away) doesn't match and is skipped."""
        mock_roster = {"id": 2, "owners": [], "roster": {"entries": []}}

        async def mock_get_espn_rosters(league_id):
            return {"success": True, "rosters": [mock_roster], "members": []}

        async def mock_get_espn_matchups(league_id, week=None):
            return {"success": True, "matchups": [{"id": 1, "home": {"teamId": 5}}]}

        with patch('nfl_mcp.opponent_analysis_tools.get_espn_rosters', side_effect=mock_get_espn_rosters), \
             patch('nfl_mcp.opponent_analysis_tools.get_espn_matchups', side_effect=mock_get_espn_matchups):
            result = await analyze_opponent("league1", 2, current_week=10)

        assert result["matchup_context"] is None
