"""Tests for nfl_mcp/nfl_enrichment.py.

Covers the ESPN-core-only leaf helpers relocated here under ADR 0007
(_fetch_week_schedule, _fetch_all_team_schedules, _enrich_usage_and_opponent,
_fetch_practice_reports) plus the new get_current_nfl_week() coroutine.
"""
from unittest.mock import MagicMock, patch

import pytest


class MockResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


class MockClient:
    def __init__(self, response):
        self._response = response

    async def get(self, url, **kwargs):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestGetCurrentNflWeek:
    """Tests for get_current_nfl_week()."""

    @pytest.mark.asyncio
    async def test_returns_week_number_on_success(self, monkeypatch):
        from nfl_mcp import nfl_enrichment

        response = MockResponse(status_code=200, json_data={"week": {"number": 7}})
        monkeypatch.setattr(nfl_enrichment, "create_http_client", lambda: MockClient(response))

        result = await nfl_enrichment.get_current_nfl_week()

        assert result == 7

    @pytest.mark.asyncio
    async def test_returns_none_on_non_200(self, monkeypatch):
        from nfl_mcp import nfl_enrichment

        response = MockResponse(status_code=500, json_data={})
        monkeypatch.setattr(nfl_enrichment, "create_http_client", lambda: MockClient(response))

        result = await nfl_enrichment.get_current_nfl_week()

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_week_missing(self, monkeypatch):
        from nfl_mcp import nfl_enrichment

        response = MockResponse(status_code=200, json_data={"events": []})
        monkeypatch.setattr(nfl_enrichment, "create_http_client", lambda: MockClient(response))

        result = await nfl_enrichment.get_current_nfl_week()

        assert result is None

    @pytest.mark.asyncio
    async def test_not_gated_by_advanced_enrich_flag(self, monkeypatch):
        """get_current_nfl_week is a core primitive, unlike the opportunistic
        enrichment fetchers, so it must work with NFL_MCP_ADVANCED_ENRICH unset."""
        from nfl_mcp import nfl_enrichment

        monkeypatch.setattr(nfl_enrichment, "ADVANCED_ENRICH_ENABLED", False)
        response = MockResponse(status_code=200, json_data={"week": {"number": 3}})
        monkeypatch.setattr(nfl_enrichment, "create_http_client", lambda: MockClient(response))

        result = await nfl_enrichment.get_current_nfl_week()

        assert result == 3


class TestFetchWeekSchedule:
    """Tests for the relocated _fetch_week_schedule."""

    @pytest.mark.asyncio
    async def test_skipped_when_disabled_and_not_forced(self, monkeypatch):
        from nfl_mcp import nfl_enrichment

        monkeypatch.setattr(nfl_enrichment, "ADVANCED_ENRICH_ENABLED", False)

        result = await nfl_enrichment._fetch_week_schedule(2024, 10)

        assert result == []

    @pytest.mark.asyncio
    async def test_force_fetches_even_when_disabled(self, monkeypatch):
        from nfl_mcp import nfl_enrichment

        monkeypatch.setattr(nfl_enrichment, "ADVANCED_ENRICH_ENABLED", False)

        event = {
            "date": "2024-11-03T18:00Z",
            "competitions": [{
                "competitors": [
                    {"homeAway": "home", "team": {"abbreviation": "KC"}},
                    {"homeAway": "away", "team": {"abbreviation": "BUF"}},
                ]
            }]
        }
        response = MockResponse(status_code=200, json_data={"events": [event]})
        monkeypatch.setattr(nfl_enrichment, "create_http_client", lambda: MockClient(response))
        monkeypatch.setattr(
            "nfl_mcp.response_validation.validate_response_and_log",
            lambda data, validator, name, allow_partial=True: True,
        )

        result = await nfl_enrichment._fetch_week_schedule(2024, 10, force=True)

        assert len(result) == 2
        teams = {row["team"] for row in result}
        assert teams == {"KC", "BUF"}


class TestFetchAllTeamSchedules:
    """Tests for the relocated _fetch_all_team_schedules."""

    @pytest.mark.asyncio
    async def test_skipped_when_disabled(self, monkeypatch):
        from nfl_mcp import nfl_enrichment

        monkeypatch.setattr(nfl_enrichment, "ADVANCED_ENRICH_ENABLED", False)

        result = await nfl_enrichment._fetch_all_team_schedules(2024)

        assert result == []


class TestFetchPracticeReports:
    """Tests for the relocated _fetch_practice_reports, rewired onto injury_service."""

    @pytest.mark.asyncio
    async def test_sources_from_injury_service_not_deleted_fetch_injuries(self, monkeypatch):
        from nfl_mcp import nfl_enrichment

        monkeypatch.setattr(nfl_enrichment, "ADVANCED_ENRICH_ENABLED", True)

        injuries = [
            {"player_id": "1", "injury_status": "Out", "date_reported": "2024-11-01"},
            {"player_id": "2", "injury_status": "Questionable", "date_reported": "2024-11-01"},
            {"player_id": "3", "injury_status": "Probable", "date_reported": "2024-11-01"},
            {"player_id": "4", "injury_status": "Active", "date_reported": "2024-11-01"},
        ]

        async def fake_get_injury_reports(*args, **kwargs):
            return injuries

        with patch("nfl_mcp.injury_service.get_injury_reports", side_effect=fake_get_injury_reports) as mock_get:
            result = await nfl_enrichment._fetch_practice_reports(2024, 10)

        mock_get.assert_called_once()
        by_player = {row["player_id"]: row["status"] for row in result}
        assert by_player["1"] == "DNP"
        assert by_player["2"] == "LP"
        assert by_player["3"] == "FP"
        assert "4" not in by_player  # "Active" maps to no practice-status signal
        assert all(row["source"] == "espn_injuries" for row in result)

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_injuries(self, monkeypatch):
        from nfl_mcp import nfl_enrichment

        monkeypatch.setattr(nfl_enrichment, "ADVANCED_ENRICH_ENABLED", True)

        async def fake_get_injury_reports(*args, **kwargs):
            return []

        with patch("nfl_mcp.injury_service.get_injury_reports", side_effect=fake_get_injury_reports):
            result = await nfl_enrichment._fetch_practice_reports(2024, 10)

        assert result == []

    @pytest.mark.asyncio
    async def test_skipped_when_disabled(self, monkeypatch):
        from nfl_mcp import nfl_enrichment

        monkeypatch.setattr(nfl_enrichment, "ADVANCED_ENRICH_ENABLED", False)

        result = await nfl_enrichment._fetch_practice_reports(2024, 10)

        assert result == []


class TestEnrichUsageAndOpponentNewHome:
    """Confirms _enrich_usage_and_opponent works when imported from its new home."""

    def test_importable_and_functional_from_nfl_enrichment(self):
        from nfl_mcp.nfl_enrichment import _enrich_usage_and_opponent

        mock_db = MagicMock()
        mock_db.get_player_snap_pct.return_value = {"snap_pct": 80}
        mock_db.get_opponent.return_value = "KC"
        mock_db.get_player_injury_from_cache.return_value = None
        mock_db.get_latest_practice_status.return_value = None
        mock_db.get_usage_last_n_weeks.return_value = None

        athlete = {
            "id": "12345",
            "full_name": "Test Player",
            "position": "WR",
            "team_id": "DAL",
        }

        with patch("nfl_mcp.matchup_tools.get_defense_analyzer") as mock_analyzer:
            mock_instance = MagicMock()
            mock_instance.get_matchup_difficulty.return_value = {"is_fallback": True}
            mock_analyzer.return_value = mock_instance

            result = _enrich_usage_and_opponent(mock_db, athlete, 2024, 10)

        assert result.get("opponent") == "KC"
        assert result.get("snap_pct") == 80


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
