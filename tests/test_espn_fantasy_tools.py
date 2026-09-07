"""
Tests for the ESPN Fantasy auth-error decorator foundation and tools.

Covers @handle_espn_auth_errors against a trivial wrapped function:
- credentials unset short-circuits before the wrapped function is called
- a mocked 401 response delegates to classify_espn_auth_error

Also covers get_espn_player_news, the one tool in this module that
deliberately omits @handle_espn_auth_errors (ADR 0004).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nfl_mcp.errors import ErrorType
from nfl_mcp.espn_errors import classify_espn_auth_error
from nfl_mcp.espn_fantasy_tools import get_espn_player_news, handle_espn_auth_errors


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


def _mock_http_client(response: MagicMock) -> AsyncMock:
    client = AsyncMock()
    client.get.return_value = response
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


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
