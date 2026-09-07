"""
Tests for espn_fantasy_tools.py: the @handle_espn_auth_errors decorator
foundation, and get_espn_league / get_espn_player_news, the tools built on it.

Covers @handle_espn_auth_errors against a trivial wrapped function:
- credentials unset short-circuits before the wrapped function is called
- a mocked 401 response delegates to classify_espn_auth_error

Covers get_espn_league:
- success path on the 2018+ object-wrapped envelope
- success path on the pre-2018 leagueHistory array-wrapped envelope,
  normalizing to the same output shape
- a non-401/403 HTTP failure (500) falls through to @handle_http_errors
- registration in tool_registry.get_all_tools()

Also covers get_espn_player_news, the one tool in this module that
deliberately omits @handle_espn_auth_errors (ADR 0004).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nfl_mcp.errors import ErrorType
from nfl_mcp.espn_errors import classify_espn_auth_error
from nfl_mcp.espn_fantasy_tools import (
    get_espn_league,
    get_espn_player_news,
    handle_espn_auth_errors,
)


class TestHandleEspnAuthErrors:
    """Test the @handle_espn_auth_errors decorator."""

    @pytest.mark.asyncio
    async def test_missing_credentials_short_circuits(self, monkeypatch):
        """Unset ESPN_S2/ESPN_SWID short-circuits before the wrapped call, with a distinct message."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        wrapped = AsyncMock()

        @handle_espn_auth_errors
        async def get_league():
            return await wrapped()

        result = await get_league()

        wrapped.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_CREDENTIALS_NOT_CONFIGURED
        assert "not configured" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_partial_credentials_short_circuits(self, monkeypatch):
        """Only one of ESPN_S2/ESPN_SWID set is still treated as not configured."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.delenv("ESPN_SWID", raising=False)

        wrapped = AsyncMock()

        @handle_espn_auth_errors
        async def get_league():
            return await wrapped()

        result = await get_league()

        wrapped.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_CREDENTIALS_NOT_CONFIGURED

    @pytest.mark.asyncio
    async def test_401_delegates_to_classifier(self, monkeypatch):
        """A 401 HTTPStatusError is caught and classified via classify_espn_auth_error."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        response = httpx.Response(401, request=httpx.Request("GET", "https://example.com"))
        expected = classify_espn_auth_error(response)

        @handle_espn_auth_errors
        async def get_league():
            raise httpx.HTTPStatusError("401", request=response.request, response=response)

        result = await get_league()

        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_EXPIRED_COOKIES
        assert result["error"] == expected.message

    @pytest.mark.asyncio
    async def test_403_delegates_to_classifier(self, monkeypatch):
        """A 403 HTTPStatusError is caught and classified via classify_espn_auth_error."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        response = httpx.Response(403, request=httpx.Request("GET", "https://example.com"))
        expected = classify_espn_auth_error(response)

        @handle_espn_auth_errors
        async def get_league():
            raise httpx.HTTPStatusError("403", request=response.request, response=response)

        result = await get_league()

        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_POSSIBLE_AUTH_ISSUE
        assert result["error"] == expected.message

    @pytest.mark.asyncio
    async def test_non_auth_http_status_error_is_reraised(self, monkeypatch):
        """A non-auth HTTPStatusError (e.g. 500) is re-raised, not swallowed."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        response = httpx.Response(500, request=httpx.Request("GET", "https://example.com"))

        @handle_espn_auth_errors
        async def get_league():
            raise httpx.HTTPStatusError("500", request=response.request, response=response)

        with pytest.raises(httpx.HTTPStatusError):
            await get_league()

    @pytest.mark.asyncio
    async def test_successful_call_passes_through(self, monkeypatch):
        """With credentials configured and no error, the wrapped result passes through untouched."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        @handle_espn_auth_errors
        async def get_league():
            return {"success": True, "error": None, "error_type": None, "league": "data"}

        result = await get_league()

        assert result == {"success": True, "error": None, "error_type": None, "league": "data"}


def _mock_http_client(mock_response):
    """An async-context-manager mock standing in for create_http_client()'s client."""
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    return mock_client


class TestGetEspnLeague:
    """Test get_espn_league, including the year/leagueHistory boundary helper."""

    @pytest.mark.asyncio
    async def test_2018_plus_object_envelope(self, monkeypatch):
        """A 2018+ season hits the direct-season URL and passes the object envelope through."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        league_object = {
            "id": 1234,
            "seasonId": 2018,
            "settings": {"name": "Test League", "size": 12},
        }
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = league_object

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_league("1234", year=2018)

        assert result["success"] is True
        assert result["league"] == league_object

        call_args = mock_client.get.call_args
        assert call_args.args[0] == (
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/"
            "ffl/seasons/2018/segments/0/leagues/1234"
        )
        assert call_args.kwargs["params"] == [("view", "mSettings")]
        assert call_args.kwargs["cookies"] == {"espn_s2": "some-cookie", "SWID": "some-swid"}

    @pytest.mark.asyncio
    async def test_pre_2018_array_envelope_normalizes_to_same_shape(self, monkeypatch):
        """A pre-2018 season hits the leagueHistory URL and unwraps the array envelope."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        league_object = {
            "id": 368876,
            "seasonId": 2015,
            "settings": {"name": "Old League", "size": 10},
        }
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [league_object]

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_league("368876", year=2015)

        assert result["success"] is True
        # Same shape as the 2018+ object-envelope case: a bare league dict,
        # not the array ESPN actually sent.
        assert result["league"] == league_object

        call_args = mock_client.get.call_args
        assert call_args.args[0] == (
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/leagueHistory/368876"
        )
        assert call_args.kwargs["params"] == [("view", "mSettings"), ("seasonId", "2015")]

    @pytest.mark.asyncio
    async def test_non_auth_http_error_falls_through_to_handle_http_errors(self, monkeypatch):
        """A non-401/403 HTTP failure (500) is not swallowed by the auth decorator."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        response = httpx.Response(500, request=httpx.Request("GET", "https://example.com"))
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=response.request, response=response
        )

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_league("1234", year=2018)

        assert result["success"] is False
        assert result["error_type"] == ErrorType.HTTP
        assert result["league"] is None

    @pytest.mark.asyncio
    async def test_missing_credentials_short_circuits(self, monkeypatch):
        """Missing ESPN credentials short-circuit before any HTTP call is made."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client") as mock_create_client:
            result = await get_espn_league("1234", year=2018)

        mock_create_client.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_CREDENTIALS_NOT_CONFIGURED

    def test_registered_in_tool_registry(self):
        """get_espn_league is present in tool_registry.get_all_tools()."""
        from nfl_mcp.tool_registry import get_all_tools

        tool_names = [t.__name__ for t in get_all_tools()]
        assert "get_espn_league" in tool_names


class TestGetEspnPlayerNews:
    """Test get_espn_player_news, the one ESPN fantasy tool with no cookie auth."""

    @pytest.mark.asyncio
    async def test_success_with_player_id_filter(self, monkeypatch):
        """A player_id filter is forwarded as the playerId query param."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "news": {
                "feed": [{"id": 1, "headline": "Mahomes questionable for Sunday"}],
                "resultsCount": 1,
            }
        }
        client = _mock_http_client(response)

        with patch('nfl_mcp.espn_fantasy_tools.create_http_client', return_value=client):
            result = await get_espn_player_news(player_id=3139477, limit=10)

        assert result["success"] is True
        assert result["total_news"] == 1
        assert result["news"][0]["headline"] == "Mahomes questionable for Sunday"

        _, kwargs = client.get.call_args
        assert kwargs["params"] == {"playerId": 3139477, "limit": 10}

    @pytest.mark.asyncio
    async def test_limit_enforced_client_side(self, monkeypatch):
        """limit is enforced on the returned feed even if ESPN's server ignores
        the query param (the catalog only confirms `playerId`, not `limit`,
        as a real server-side param for this endpoint)."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "news": {"feed": [{"id": i} for i in range(5)]}
        }
        client = _mock_http_client(response)

        with patch('nfl_mcp.espn_fantasy_tools.create_http_client', return_value=client):
            result = await get_espn_player_news(limit=2)

        assert result["success"] is True
        assert result["total_news"] == 2
        assert len(result["news"]) == 2

    @pytest.mark.asyncio
    async def test_success_without_player_id_filter(self, monkeypatch):
        """With no filters, no playerId/limit params are sent at all."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "news": {
                "feed": [
                    {"id": 1, "headline": "Story one"},
                    {"id": 2, "headline": "Story two"},
                ],
                "resultsCount": 2,
            }
        }
        client = _mock_http_client(response)

        with patch('nfl_mcp.espn_fantasy_tools.create_http_client', return_value=client):
            result = await get_espn_player_news()

        assert result["success"] is True
        assert result["total_news"] == 2

        _, kwargs = client.get.call_args
        assert kwargs["params"] == {}

    @pytest.mark.asyncio
    async def test_no_credentials_needed(self, monkeypatch):
        """Proves the omitted @handle_espn_auth_errors preflight is correct, not
        accidentally still gated: the call succeeds with no ESPN_S2/ESPN_SWID
        set at all."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.json.return_value = {"news": {"feed": []}}
        client = _mock_http_client(response)

        with patch('nfl_mcp.espn_fantasy_tools.create_http_client', return_value=client):
            result = await get_espn_player_news()

        assert result["success"] is True
        assert result["error"] is None
        assert result["error_type"] is None

    @pytest.mark.asyncio
    async def test_http_error_path(self, monkeypatch):
        """A non-2xx response is surfaced as a generic HTTP error via @handle_http_errors."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        response = MagicMock()
        response.status_code = 500
        response.reason_phrase = "Internal Server Error"
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=response
        )
        client = _mock_http_client(response)

        with patch('nfl_mcp.espn_fantasy_tools.create_http_client', return_value=client):
            result = await get_espn_player_news(player_id=3139477)

        assert result["success"] is False
        assert result["error_type"] == ErrorType.HTTP
        assert result["news"] == []
        assert result["total_news"] == 0

    @pytest.mark.asyncio
    async def test_retries_on_403(self, monkeypatch):
        """Same site.api.espn.com WAF quirk as get_nfl_news: a 403 triggers one
        retry with the default User-Agent, which is accepted."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        forbidden = MagicMock()
        forbidden.status_code = 403

        ok = MagicMock()
        ok.status_code = 200
        ok.raise_for_status = MagicMock()
        ok.json.return_value = {"news": {"feed": [{"id": 1}]}}

        client = AsyncMock()
        client.get.side_effect = [forbidden, ok]
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch('nfl_mcp.espn_fantasy_tools.create_http_client', return_value=client):
            result = await get_espn_player_news()

        assert result["success"] is True
        assert result["total_news"] == 1
        assert client.get.call_count == 2


class TestEspnFantasyToolRegistryIntegration:
    """Test that get_espn_player_news is properly registered."""

    def test_espn_fantasy_tools_in_registry(self):
        """Test get_espn_player_news is registered with the MCP server."""
        from nfl_mcp.tool_registry import get_all_tools

        tool_names = [t.__name__ for t in get_all_tools()]

        assert "get_espn_player_news" in tool_names

    def test_espn_fantasy_tools_module_exports(self):
        """Test espn_fantasy_tools module exposes get_espn_player_news."""
        from nfl_mcp import espn_fantasy_tools

        assert hasattr(espn_fantasy_tools, 'get_espn_player_news')
