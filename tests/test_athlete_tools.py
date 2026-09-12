"""Tests for athlete_tools module."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nfl_mcp.athlete_tools import (
    POSITION_ID_MAP,
    fetch_athletes,
    get_athletes_by_team,
    lookup_athlete,
    search_athletes,
)

_TEAMS = [
    {"id": "25", "abbreviation": "SF"},
    {"id": "12", "abbreviation": "KC"},
]


class TestFetchAthletes:
    """Test fetch_athletes function."""

    @pytest.mark.asyncio
    async def test_fetch_athletes_success(self):
        """Sources the full pool from ESPN and re-keys it on ESPN player ids."""
        mock_db = MagicMock()
        mock_db.get_all_teams.return_value = _TEAMS
        mock_db.upsert_athletes.return_value = 2
        mock_db.get_last_updated.return_value = "2026-01-01T00:00:00"

        espn_players = [
            {
                "id": 4429795, "fullName": "Test Player", "firstName": "Test",
                "lastName": "Player", "proTeamId": 25, "defaultPositionId": 2,
                "active": True,
            },
            {
                "id": 16, "fullName": "San Francisco 49ers D/ST", "firstName": "",
                "lastName": "", "proTeamId": 25, "defaultPositionId": 16,
                "active": True,
            },
        ]

        with patch(
            'nfl_mcp.athlete_tools.espn_fantasy_tools.fetch_all_espn_players',
            AsyncMock(return_value=espn_players),
        ):
            result = await fetch_athletes(mock_db)

        assert result["success"] is True
        assert result["athletes_count"] == 2
        assert result["last_updated"] == "2026-01-01T00:00:00"

        athletes_data = mock_db.upsert_athletes.call_args[0][0]
        assert set(athletes_data.keys()) == {"4429795", "16"}
        assert athletes_data["4429795"]["team"] == "SF"
        assert athletes_data["4429795"]["position"] == "RB"
        assert athletes_data["4429795"]["status"] == "Active"
        # defaultPositionId 16 -> "DST", required by the streaming/handcuff
        # DST lookup this cache feeds (ADR 0007).
        assert athletes_data["16"]["position"] == "DST"
        assert POSITION_ID_MAP[16] == "DST"

    @pytest.mark.asyncio
    async def test_fetch_athletes_unknown_team_and_position(self):
        """A proTeamId/defaultPositionId with no match resolves to empty string, not an error."""
        mock_db = MagicMock()
        mock_db.get_all_teams.return_value = _TEAMS
        mock_db.upsert_athletes.return_value = 1
        mock_db.get_last_updated.return_value = "2026-01-01T00:00:00"

        espn_players = [
            {
                "id": 99, "fullName": "Free Agent", "firstName": "Free",
                "lastName": "Agent", "proTeamId": 0, "defaultPositionId": 999,
                "active": False,
            },
        ]

        with patch(
            'nfl_mcp.athlete_tools.espn_fantasy_tools.fetch_all_espn_players',
            AsyncMock(return_value=espn_players),
        ):
            result = await fetch_athletes(mock_db)

        assert result["success"] is True
        athletes_data = mock_db.upsert_athletes.call_args[0][0]
        assert athletes_data["99"]["team"] == ""
        assert athletes_data["99"]["position"] == ""
        assert athletes_data["99"]["status"] == "Inactive"

    @pytest.mark.asyncio
    async def test_fetch_athletes_http_error(self):
        """Test fetch_athletes with HTTP error."""
        mock_db = MagicMock()
        mock_db.get_all_teams.return_value = _TEAMS

        with patch(
            'nfl_mcp.athlete_tools.espn_fantasy_tools.fetch_all_espn_players',
            AsyncMock(side_effect=httpx.HTTPStatusError(
                "500", request=MagicMock(), response=MagicMock(status_code=500, reason_phrase="Server Error")
            )),
        ):
            result = await fetch_athletes(mock_db)

        assert result["success"] is False


class TestLookupAthlete:
    """Test lookup_athlete function."""

    def test_lookup_athlete_found(self):
        """Test looking up existing athlete."""
        mock_db = MagicMock()
        mock_athlete = {"player_id": "1", "full_name": "Test Player"}
        mock_db.get_athlete_by_id.return_value = mock_athlete

        result = lookup_athlete(mock_db, "1")

        assert result["success"] is True
        assert result["found"] is True
        assert result["athlete"] == mock_athlete

    def test_lookup_athlete_not_found(self):
        """Test looking up non-existing athlete."""
        mock_db = MagicMock()
        mock_db.get_athlete_by_id.return_value = None

        result = lookup_athlete(mock_db, "999")

        assert result["success"] is True
        assert result["found"] is False
        assert result["athlete"] is None

    def test_lookup_athlete_calls_db(self):
        """Test that lookup calls database with correct ID."""
        mock_db = MagicMock()
        mock_athlete = {"player_id": "1"}
        mock_db.get_athlete_by_id.return_value = mock_athlete

        lookup_athlete(mock_db, "1")

        mock_db.get_athlete_by_id.assert_called_once_with("1")


class TestSearchAthletes:
    """Test search_athletes function."""

    def test_search_athletes_success(self):
        """Test successful athlete search."""
        mock_db = MagicMock()
        mock_athletes = [
            {"player_id": "1", "full_name": "John Smith"},
            {"player_id": "2", "full_name": "Jane Smith"}
        ]
        mock_db.search_athletes_by_name.return_value = mock_athletes

        result = search_athletes(mock_db, "Smith", limit=10)

        assert result["success"] is True
        assert result["count"] == 2
        assert result["search_term"] == "Smith"
        assert len(result["athletes"]) == 2

    def test_search_athletes_limit_validation(self):
        """Test that limit is validated."""
        mock_db = MagicMock()
        mock_athletes = [{"player_id": "1"}]
        mock_db.search_athletes_by_name.return_value = mock_athletes

        # Test with high limit - should be capped
        result = search_athletes(mock_db, "Smith", limit=1000)

        assert result["success"] is True


class TestGetAthletesByTeam:
    """Test get_athletes_by_team function."""

    def test_get_athletes_by_team_success(self):
        """Test getting athletes by team."""
        mock_db = MagicMock()
        mock_athletes = [
            {"player_id": "1", "full_name": "Player 1"},
            {"player_id": "2", "full_name": "Player 2"}
        ]
        mock_db.get_athletes_by_team.return_value = mock_athletes

        result = get_athletes_by_team(mock_db, "SF")

        assert result["success"] is True
        assert result["count"] == 2
        assert result["team_id"] == "SF"
        assert len(result["athletes"]) == 2

    def test_get_athletes_by_team_empty(self):
        """Test getting athletes from team with no players."""
        mock_db = MagicMock()
        mock_db.get_athletes_by_team.return_value = []

        result = get_athletes_by_team(mock_db, "EMPTY")

        assert result["success"] is True
        assert result["count"] == 0
        assert result["athletes"] == []
