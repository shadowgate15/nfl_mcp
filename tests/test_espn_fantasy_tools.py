"""
Tests for espn_fantasy_tools.py: the @handle_espn_auth_errors decorator
foundation, and the tools built on it (get_espn_league, get_espn_players,
get_espn_free_agents, get_espn_rosters, get_espn_standings,
get_espn_scoreboard, get_espn_matchups, get_espn_draft,
get_espn_transactions, get_espn_player_news).

Covers @handle_espn_auth_errors against a trivial wrapped function:
- credentials unset short-circuits before the wrapped function is called
- a mocked 401 response delegates to classify_espn_auth_error

Covers get_espn_league:
- success path on the 2018+ object-wrapped envelope
- success path on the pre-2018 leagueHistory array-wrapped envelope,
  normalizing to the same output shape
- a non-401/403 HTTP failure (500) falls through to @handle_http_errors
- registration in tool_registry.get_all_tools()

Covers get_espn_players (no cookies, `/players` endpoint, catalog §7b):
- a bare-array raw response and a wrapped {"players": [...]} raw response
  normalize to the same output shape
- no ESPN_S2/ESPN_SWID needed
- HTTP error path
- limit/offset pagination: default-limit slicing, explicit limit/offset,
  has_more boundary correctness, and the 150 KB hard size cap (ADR 0006)

Covers get_espn_free_agents (cookie-gated, `kona_player_info` view, catalog
§7a):
- success path, including the x-fantasy-filter header and scoringPeriodId
- missing-credentials short-circuit
- HTTP error path
- limit/offset pagination: default-limit slicing, explicit limit/offset,
  has_more boundary correctness, and the 150 KB hard size cap (ADR 0006)

Covers get_espn_rosters:
- success path on the 2018+ object-wrapped envelope, with a week passed
  through as scoringPeriodId
- success path on the pre-2018 leagueHistory array-wrapped envelope
- missing-credentials short-circuit
- registration in tool_registry.get_all_tools()
- the league response's top-level members[] is surfaced undiscarded,
  defaulting to [] when absent (issue #44)

Covers _extract_player, the shared roster-entry player-extraction helper
(issue #44): both of ESPN's player-nesting shapes
(playerPoolEntry.player vs. a bare player), and a missing player
returning {} rather than raising.

Covers resolve_team_owner_names, the shared owners-id-to-members-id join
(issue #44, ADR 0005/0007): resolution via primaryOwner, a co-manager
team resolving to just the primary owner's name (not a list), falling
back to the first owners[] entry when primaryOwner is absent, falling
back to firstName/lastName when displayName is absent, omitting teams
whose owner id has no matching member or that have no owners at all,
and resolving multiple teams in rosters[] independently.

Covers enrich_roster_entries, the shared roster-entry-to-enriched-player
helper (issue #45, ADR 0007): both of ESPN's player-nesting shapes via
_extract_player, joining a cached player id against player_cache for
name/team/position, falling back to the raw ESPN player's own fullName
and defaultPositionId (via POSITION_ID_MAP) for a player id missing from
the cache, and preserving lineupSlotId/entries order across multiple
entries.

Covers get_espn_standings:
- sort/tiebreak derivation from teams[] (rankFinal, then
  rankCalculatedFinal, then playoffSeed)
- missing-credentials short-circuit
- registration in tool_registry.get_all_tools()

Covers get_espn_scoreboard and get_espn_matchups:
- success path with no `week` filter (full season's schedule)
- success path with a `week` filter (returned schedule filtered
  client-side to that matchup period); get_espn_matchups additionally
  sends the x-fantasy-filter header (mirrors box_scores()), while
  get_espn_scoreboard does not (mirrors scoreboard() — the catalog never
  confirms that header scopes the lighter mMatchupScore view)
- the two tools request different `view=` params (mMatchupScore vs.
  mMatchup+mScoreboard) but share the same _fetch_espn_league_view helper
- a non-401/403 HTTP failure (500) falls through to @handle_http_errors

Covers get_espn_draft (same shape as get_espn_league, both built on
_fetch_espn_league_view):
- success path on the 2018+ object-wrapped envelope
- success path on the pre-2018 leagueHistory array-wrapped envelope,
  normalizing to the same output shape
- a non-401/403 HTTP failure (500) falls through to @handle_http_errors
- registration in tool_registry.get_all_tools()

Covers get_espn_transactions:
- success path, unfiltered
- success path, filtered by `types`
- `week` forwarded as `scoringPeriodId` only when given
- a missing `transactions` key is treated as an empty list, not an error
- error path (401)
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
    POSITION_ID_MAP,
    _extract_player,
    enrich_roster_entries,
    get_espn_draft,
    get_espn_free_agents,
    get_espn_league,
    get_espn_matchups,
    get_espn_player_news,
    get_espn_players,
    get_espn_rosters,
    get_espn_scoreboard,
    get_espn_standings,
    get_espn_transactions,
    handle_espn_auth_errors,
    resolve_team_owner_names,
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


class TestGetEspnPlayers:
    """Test get_espn_players, the cookie-free full pro-player pool tool."""

    @pytest.mark.asyncio
    async def test_bare_array_envelope_normalizes(self, monkeypatch):
        """A bare top-level JSON array (the raw /players shape) normalizes to {"players": [...]}."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        raw_players = [
            {"id": 1, "fullName": "Israel Abanikanda"},
            {"id": 2, "fullName": "Some Player"},
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = raw_players

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_players(year=2024)

        assert result["success"] is True
        assert result["players"] == raw_players

        call_args = mock_client.get.call_args
        assert call_args.args[0] == (
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2024/players"
        )
        assert call_args.kwargs["params"] == [("view", "players_wl")]
        assert "cookies" not in call_args.kwargs

    @pytest.mark.asyncio
    async def test_wrapped_envelope_normalizes_to_same_shape(self, monkeypatch):
        """A {"players": [...]} raw response normalizes to the identical output shape as the bare-array case."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        raw_players = [{"id": 1, "fullName": "Israel Abanikanda"}]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"players": raw_players}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_players(year=2024)

        assert result["success"] is True
        assert result["players"] == raw_players

    @pytest.mark.asyncio
    async def test_no_credentials_needed(self, monkeypatch):
        """get_espn_players succeeds with no ESPN_S2/ESPN_SWID configured at all."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = []

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_players()

        assert result["success"] is True
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_http_error_path(self, monkeypatch):
        """A non-2xx response is surfaced as a generic HTTP error via @handle_http_errors."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        response = httpx.Response(500, request=httpx.Request("GET", "https://example.com"))
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=response.request, response=response
        )

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_players()

        assert result["success"] is False
        assert result["error_type"] == ErrorType.HTTP
        assert result["players"] == []

    def test_registered_in_tool_registry(self):
        """get_espn_players is present in tool_registry.get_all_tools()."""
        from nfl_mcp.tool_registry import get_all_tools

        tool_names = [t.__name__ for t in get_all_tools()]
        assert "get_espn_players" in tool_names

    @pytest.mark.asyncio
    async def test_default_limit_pages_the_pool(self, monkeypatch):
        """With no limit/offset given, only the first 25 players are returned."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        raw_players = [{"id": i, "fullName": f"Player {i}"} for i in range(40)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = raw_players

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_players()

        assert result["success"] is True
        assert len(result["players"]) == 25
        assert result["players"] == raw_players[:25]
        assert result["total_players"] == 40
        assert result["has_more"] is True

    @pytest.mark.asyncio
    async def test_explicit_limit_and_offset(self, monkeypatch):
        """An explicit limit/offset selects the corresponding slice."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        raw_players = [{"id": i, "fullName": f"Player {i}"} for i in range(40)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = raw_players

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_players(limit=10, offset=30)

        assert result["success"] is True
        assert result["players"] == raw_players[30:40]
        assert result["total_players"] == 40
        assert result["has_more"] is False

    @pytest.mark.asyncio
    async def test_has_more_false_at_exact_boundary(self, monkeypatch):
        """offset+limit landing exactly on the total leaves has_more False."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        raw_players = [{"id": i} for i in range(10)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = raw_players

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_players(limit=5, offset=5)

        assert result["players"] == raw_players[5:10]
        assert result["has_more"] is False

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_players(limit=5, offset=4)

        assert result["players"] == raw_players[4:9]
        assert result["has_more"] is True

    @pytest.mark.asyncio
    async def test_response_stays_within_hard_size_cap(self, monkeypatch):
        """Even a large max-limit page is trimmed to stay under the 150 KB hard cap."""
        import json

        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        # Each entry is padded to be much heavier than a realistic bio-only
        # /players entry, to stress the size-based trimming safety net
        # independent of any particular byte-per-player estimate.
        raw_players = [
            {"id": i, "fullName": f"Player {i}", "blob": "x" * 5000}
            for i in range(200)
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = raw_players

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_players(limit=100, offset=0)

        assert result["success"] is True
        assert len(json.dumps(result["players"])) <= 150 * 1024
        assert result["total_players"] == 200
        assert result["has_more"] is True
        assert len(result["players"]) < 100


class TestGetEspnFreeAgents:
    """Test get_espn_free_agents, the cookie-gated league-scoped free-agent tool."""

    @pytest.mark.asyncio
    async def test_success_with_week(self, monkeypatch):
        """A week scopes the request via scoringPeriodId, and the filter header restricts to FREEAGENT/WAIVERS."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        raw_players = [{"id": 1, "player": {"fullName": "Free Agent Guy"}, "onTeamId": 0}]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"players": raw_players}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_free_agents("1234", week=3, year=2023)

        assert result["success"] is True
        assert result["players"] == raw_players

        call_args = mock_client.get.call_args
        assert call_args.args[0] == (
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/"
            "ffl/seasons/2023/segments/0/leagues/1234"
        )
        assert call_args.kwargs["params"] == [
            ("view", "kona_player_info"),
            ("scoringPeriodId", 3),
        ]
        assert call_args.kwargs["cookies"] == {"espn_s2": "some-cookie", "SWID": "some-swid"}
        assert "x-fantasy-filter" in call_args.kwargs["headers"]

    @pytest.mark.asyncio
    async def test_success_without_week_omits_scoring_period(self, monkeypatch):
        """With week=None, scoringPeriodId is left out of the request entirely."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"players": []}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_free_agents("1234", year=2023)

        assert result["success"] is True

        call_args = mock_client.get.call_args
        assert call_args.kwargs["params"] == [("view", "kona_player_info")]

    @pytest.mark.asyncio
    async def test_missing_credentials_short_circuits(self, monkeypatch):
        """Missing ESPN credentials short-circuit before any HTTP call is made."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client") as mock_create_client:
            result = await get_espn_free_agents("1234", week=3, year=2023)

        mock_create_client.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_CREDENTIALS_NOT_CONFIGURED

    @pytest.mark.asyncio
    async def test_http_error_path(self, monkeypatch):
        """A non-401/403 HTTP failure (500) falls through to @handle_http_errors."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        response = httpx.Response(500, request=httpx.Request("GET", "https://example.com"))
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=response.request, response=response
        )

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_free_agents("1234", year=2023)

        assert result["success"] is False
        assert result["error_type"] == ErrorType.HTTP
        assert result["players"] == []

    @pytest.mark.asyncio
    async def test_401_delegates_to_classifier(self, monkeypatch):
        """A 401 is classified via classify_espn_auth_error, not raised."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        response = httpx.Response(401, request=httpx.Request("GET", "https://example.com"))
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=response.request, response=response
        )

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_free_agents("1234", year=2023)

        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_EXPIRED_COOKIES

    def test_registered_in_tool_registry(self):
        """get_espn_free_agents is present in tool_registry.get_all_tools()."""
        from nfl_mcp.tool_registry import get_all_tools

        tool_names = [t.__name__ for t in get_all_tools()]
        assert "get_espn_free_agents" in tool_names

    @pytest.mark.asyncio
    async def test_default_limit_pages_the_free_agent_list(self, monkeypatch):
        """With no limit/offset given, only the first 25 free agents are returned."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        raw_players = [{"id": i, "player": {"fullName": f"FA {i}"}} for i in range(40)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"players": raw_players}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_free_agents("1234", year=2023)

        assert result["success"] is True
        assert result["players"] == raw_players[:25]
        assert result["total_free_agents"] == 40
        assert result["has_more"] is True

    @pytest.mark.asyncio
    async def test_explicit_limit_and_offset(self, monkeypatch):
        """An explicit limit/offset selects the corresponding slice."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        raw_players = [{"id": i, "player": {"fullName": f"FA {i}"}} for i in range(40)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"players": raw_players}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_free_agents("1234", year=2023, limit=10, offset=30)

        assert result["success"] is True
        assert result["players"] == raw_players[30:40]
        assert result["total_free_agents"] == 40
        assert result["has_more"] is False

    @pytest.mark.asyncio
    async def test_has_more_false_at_exact_boundary(self, monkeypatch):
        """offset+limit landing exactly on the total leaves has_more False."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        raw_players = [{"id": i} for i in range(10)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"players": raw_players}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_free_agents("1234", year=2023, limit=5, offset=5)

        assert result["players"] == raw_players[5:10]
        assert result["has_more"] is False

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_free_agents("1234", year=2023, limit=5, offset=4)

        assert result["players"] == raw_players[4:9]
        assert result["has_more"] is True

    @pytest.mark.asyncio
    async def test_response_stays_within_hard_size_cap(self, monkeypatch):
        """Even a large max-limit page is trimmed to stay under the 150 KB hard cap."""
        import json

        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        # Free-agent entries carry a full nested Player (incl. stats[]),
        # heavier than a bio-only /players entry (ADR 0006) - padded here to
        # stress the size-based trimming safety net directly.
        raw_players = [
            {"id": i, "player": {"fullName": f"FA {i}"}, "blob": "x" * 5000}
            for i in range(200)
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"players": raw_players}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_free_agents("1234", year=2023, limit=100, offset=0)

        assert result["success"] is True
        assert len(json.dumps(result["players"])) <= 150 * 1024
        assert result["total_free_agents"] == 200
        assert result["has_more"] is True
        assert len(result["players"]) < 100


class TestGetEspnRosters:
    """Test get_espn_rosters, including the year/leagueHistory boundary helper."""

    @pytest.mark.asyncio
    async def test_2018_plus_object_envelope_with_week(self, monkeypatch):
        """A 2018+ season with a week hits the direct-season URL, requests
        mRoster+mTeam, and forwards week as scoringPeriodId."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        league_object = {
            "id": 1234,
            "seasonId": 2018,
            "teams": [{"id": 1, "roster": {"entries": []}}],
        }
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = league_object

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_rosters("1234", week=5, year=2018)

        assert result["success"] is True
        assert result["rosters"] == league_object["teams"]

        call_args = mock_client.get.call_args
        assert call_args.args[0] == (
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/"
            "ffl/seasons/2018/segments/0/leagues/1234"
        )
        assert call_args.kwargs["params"] == [
            ("view", "mRoster"),
            ("view", "mTeam"),
            ("scoringPeriodId", 5),
        ]

    @pytest.mark.asyncio
    async def test_pre_2018_array_envelope_normalizes_to_same_shape(self, monkeypatch):
        """A pre-2018 season hits the leagueHistory URL and unwraps the array envelope."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        league_object = {
            "id": 368876,
            "seasonId": 2015,
            "teams": [{"id": 2, "roster": {"entries": []}}],
        }
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [league_object]

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_rosters("368876", year=2015)

        assert result["success"] is True
        assert result["rosters"] == league_object["teams"]

        call_args = mock_client.get.call_args
        assert call_args.args[0] == (
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/leagueHistory/368876"
        )
        assert call_args.kwargs["params"] == [
            ("view", "mRoster"),
            ("view", "mTeam"),
            ("seasonId", "2015"),
        ]

    @pytest.mark.asyncio
    async def test_no_week_omits_scoring_period_param(self, monkeypatch):
        """Omitting week sends no scoringPeriodId param at all."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"teams": []}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            await get_espn_rosters("1234", year=2018)

        call_args = mock_client.get.call_args
        assert call_args.kwargs["params"] == [("view", "mRoster"), ("view", "mTeam")]

    @pytest.mark.asyncio
    async def test_missing_credentials_short_circuits(self, monkeypatch):
        """Missing ESPN credentials short-circuit before any HTTP call is made."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client") as mock_create_client:
            result = await get_espn_rosters("1234", year=2018)

        mock_create_client.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_CREDENTIALS_NOT_CONFIGURED

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
            result = await get_espn_rosters("1234", year=2018)

        assert result["success"] is False
        assert result["error_type"] == ErrorType.HTTP
        assert result["rosters"] == []
        assert result["members"] == []

    def test_registered_in_tool_registry(self):
        """get_espn_rosters is present in tool_registry.get_all_tools()."""
        from nfl_mcp.tool_registry import get_all_tools

        tool_names = [t.__name__ for t in get_all_tools()]
        assert "get_espn_rosters" in tool_names

    def _roster_entry(self, player_id, applied_total=12.5):
        return {
            "lineupSlotId": 2,
            "playerPoolEntry": {
                "player": {
                    "id": player_id,
                    "fullName": f"Player {player_id}",
                    "defaultPositionId": 3,
                    "proTeamId": 7,
                    "injuryStatus": "ACTIVE",
                    "ownership": {"percentOwned": 55.5},
                    "stats": [
                        {"statSourceId": 1, "appliedTotal": 20.0},
                        {"statSourceId": 0, "appliedTotal": applied_total},
                    ],
                }
            },
        }

    @pytest.mark.asyncio
    async def test_default_detail_is_summary(self, monkeypatch):
        """With no `detail` given, roster entries are trimmed to the summary allowlist."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        league_object = {
            "teams": [{"id": 1, "roster": {"entries": [self._roster_entry(101)]}}],
        }
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = league_object

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_rosters("1234", year=2018)

        assert result["success"] is True
        entries = result["rosters"][0]["roster"]["entries"]
        assert entries == [{
            "playerId": 101,
            "fullName": "Player 101",
            "defaultPositionId": 3,
            "proTeamId": 7,
            "lineupSlotId": 2,
            "injuryStatus": "ACTIVE",
            "appliedTotal": 12.5,
        }]

    @pytest.mark.asyncio
    async def test_detail_full_returns_entries_unfiltered(self, monkeypatch):
        """With detail="full", roster entries pass through unmodified."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        raw_entry = self._roster_entry(101)
        league_object = {"teams": [{"id": 1, "roster": {"entries": [raw_entry]}}]}
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = league_object

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_rosters("1234", year=2018, detail="full")

        assert result["success"] is True
        assert result["rosters"] == league_object["teams"]

    @pytest.mark.asyncio
    async def test_registry_rejects_invalid_detail(self):
        """tool_registry.get_espn_rosters rejects an unrecognized `detail` value."""
        from nfl_mcp.tool_registry import get_espn_rosters as registry_get_espn_rosters

        result = await registry_get_espn_rosters("1234", detail="verbose")

        assert result["success"] is False
        assert result["rosters"] == []

    @pytest.mark.asyncio
    async def test_total_teams_and_has_more_under_cap(self, monkeypatch):
        """A response well under the hard cap reports total_teams and has_more=False."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        league_object = {
            "teams": [
                {"id": 1, "roster": {"entries": [self._roster_entry(101)]}},
                {"id": 2, "roster": {"entries": [self._roster_entry(102)]}},
            ],
        }
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = league_object

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_rosters("1234", year=2018)

        assert result["success"] is True
        assert result["total_teams"] == 2
        assert result["has_more"] is False

    @pytest.mark.asyncio
    async def test_response_stays_within_hard_size_cap(self, monkeypatch):
        """A large `detail="full"` roster response is trimmed to stay under the
        150 KB hard cap (ADR 0006), with has_more surfacing the truncation."""
        import json

        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        def heavy_team(team_id):
            entries = [
                {
                    "lineupSlotId": 2,
                    "playerPoolEntry": {
                        "player": {
                            "id": team_id * 100 + i,
                            "fullName": f"Player {team_id}-{i}",
                            "blob": "x" * 2000,
                        }
                    },
                }
                for i in range(25)
            ]
            return {"id": team_id, "roster": {"entries": entries}}

        league_object = {"teams": [heavy_team(t) for t in range(16)]}
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = league_object

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_rosters("1234", year=2018, detail="full")

        assert result["success"] is True
        assert len(json.dumps(result["rosters"])) <= 150 * 1024
        assert result["total_teams"] == 16
        assert result["has_more"] is True
        assert len(result["rosters"]) < 16

    @pytest.mark.asyncio
    async def test_members_surfaced_alongside_rosters(self, monkeypatch):
        """The league response's top-level `members[]` is returned undiscarded."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        members = [{"id": "{abc}", "displayName": "someuser", "firstName": "Some", "lastName": "User"}]
        league_object = {"teams": [{"id": 1, "roster": {"entries": []}}], "members": members}
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = league_object

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_rosters("1234", year=2018)

        assert result["success"] is True
        assert result["members"] == members

    @pytest.mark.asyncio
    async def test_members_defaults_to_empty_list_when_absent(self, monkeypatch):
        """A league response with no `members` key surfaces `members: []`, not a KeyError."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"teams": []}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_rosters("1234", year=2018)

        assert result["success"] is True
        assert result["members"] == []


class TestExtractPlayer:
    """Test _extract_player, the shared roster-entry player extraction helper."""

    def test_nested_under_player_pool_entry(self):
        """The `playerPoolEntry.player` nesting (e.g. free-agent/matchup box-score entries)."""
        entry = {"lineupSlotId": 2, "playerPoolEntry": {"player": {"id": 101, "fullName": "Nested Guy"}}}

        assert _extract_player(entry) == {"id": 101, "fullName": "Nested Guy"}

    def test_bare_player(self):
        """The bare `player` nesting (e.g. some roster views)."""
        entry = {"lineupSlotId": 2, "player": {"id": 202, "fullName": "Bare Guy"}}

        assert _extract_player(entry) == {"id": 202, "fullName": "Bare Guy"}

    def test_missing_player_returns_empty_dict(self):
        """An entry with neither nesting returns {} rather than raising."""
        assert _extract_player({"lineupSlotId": 2}) == {}


class TestResolveTeamOwnerNames:
    """Test resolve_team_owner_names, the shared owners-id-to-members-id join (ADR 0005/0007)."""

    def test_resolves_via_primary_owner(self):
        """A team's primaryOwner id resolves to that member's displayName."""
        rosters = [{"id": 1, "owners": ["{owner-1}"], "primaryOwner": "{owner-1}"}]
        members = [{"id": "{owner-1}", "displayName": "someuser", "firstName": "Some", "lastName": "User"}]

        assert resolve_team_owner_names(rosters, members) == {1: "someuser"}

    def test_co_managers_resolve_to_primary_owner_only(self):
        """A team with multiple owners (co-managers) resolves to just the primaryOwner's name,
        not a list — no current caller consumes a co-manager list (issue #44)."""
        rosters = [{
            "id": 1,
            "owners": ["{owner-1}", "{owner-2}"],
            "primaryOwner": "{owner-2}",
        }]
        members = [
            {"id": "{owner-1}", "displayName": "first-owner"},
            {"id": "{owner-2}", "displayName": "second-owner"},
        ]

        assert resolve_team_owner_names(rosters, members) == {1: "second-owner"}

    def test_falls_back_to_first_owner_when_primary_owner_missing(self):
        """With no primaryOwner, the first entry in owners[] is used instead."""
        rosters = [{"id": 1, "owners": ["{owner-1}"]}]
        members = [{"id": "{owner-1}", "displayName": "someuser"}]

        assert resolve_team_owner_names(rosters, members) == {1: "someuser"}

    def test_falls_back_to_first_and_last_name_when_display_name_missing(self):
        """A member with no displayName resolves to "firstName lastName" instead."""
        rosters = [{"id": 1, "owners": ["{owner-1}"], "primaryOwner": "{owner-1}"}]
        members = [{"id": "{owner-1}", "firstName": "Some", "lastName": "User"}]

        assert resolve_team_owner_names(rosters, members) == {1: "Some User"}

    def test_unresolvable_owner_is_omitted(self):
        """A team whose owner id has no matching member is left out of the mapping."""
        rosters = [{"id": 1, "owners": ["{unknown}"], "primaryOwner": "{unknown}"}]

        assert resolve_team_owner_names(rosters, members=[]) == {}

    def test_team_with_no_owners_is_omitted(self):
        """A team with neither owners nor primaryOwner is left out of the mapping."""
        rosters = [{"id": 1}]
        members = [{"id": "{owner-1}", "displayName": "someuser"}]

        assert resolve_team_owner_names(rosters, members) == {}

    def test_multiple_teams(self):
        """Each team in rosters[] resolves independently."""
        rosters = [
            {"id": 1, "owners": ["{owner-1}"], "primaryOwner": "{owner-1}"},
            {"id": 2, "owners": ["{owner-2}"], "primaryOwner": "{owner-2}"},
        ]
        members = [
            {"id": "{owner-1}", "displayName": "first-team-owner"},
            {"id": "{owner-2}", "displayName": "second-team-owner"},
        ]

        assert resolve_team_owner_names(rosters, members) == {
            1: "first-team-owner",
            2: "second-team-owner",
        }


class TestEnrichRosterEntries:
    """Test enrich_roster_entries, the shared roster-entry-to-enriched-player join (ADR 0007)."""

    def test_joins_bare_player_against_cache(self):
        """A bare `entry["player"]` nesting joins against player_cache by id."""
        entries = [{"player": {"id": 4429795, "fullName": "Raw Name", "defaultPositionId": 2}, "lineupSlotId": 2}]
        player_cache = {"4429795": {"full_name": "Cached Name", "team": "SF", "position": "RB", "status": "Active"}}

        assert enrich_roster_entries(entries, player_cache) == [{
            "player_id": 4429795,
            "full_name": "Cached Name",
            "position": "RB",
            "team": "SF",
            "lineup_slot_id": 2,
        }]

    def test_joins_playerpoolentry_nested_player_against_cache(self):
        """A `playerPoolEntry.player` nesting also joins against player_cache by id."""
        entries = [{
            "playerPoolEntry": {"player": {"id": 101, "fullName": "Raw Name", "defaultPositionId": 6}},
            "lineupSlotId": 6,
        }]
        player_cache = {"101": {"full_name": "Cached TE", "team": "KC", "position": "TE", "status": "Active"}}

        assert enrich_roster_entries(entries, player_cache) == [{
            "player_id": 101,
            "full_name": "Cached TE",
            "position": "TE",
            "team": "KC",
            "lineup_slot_id": 6,
        }]

    def test_player_missing_from_cache_falls_back_to_raw_espn_fields(self):
        """A player id absent from player_cache still produces an entry, using the raw
        ESPN player's own fullName/defaultPositionId (via POSITION_ID_MAP) instead of
        being dropped."""
        entries = [{"player": {"id": 999, "fullName": "Uncached Guy", "defaultPositionId": 16}, "lineupSlotId": 20}]

        result = enrich_roster_entries(entries, player_cache={})

        assert result == [{
            "player_id": 999,
            "full_name": "Uncached Guy",
            "position": "DST",
            "team": "",
            "lineup_slot_id": 20,
        }]
        assert POSITION_ID_MAP[16] == "DST"

    def test_multiple_entries_preserve_order(self):
        """Enriched dicts come back in the same order as the input entries."""
        entries = [
            {"player": {"id": 1, "fullName": "First"}, "lineupSlotId": 0},
            {"player": {"id": 2, "fullName": "Second"}, "lineupSlotId": 1},
        ]
        player_cache = {
            "1": {"full_name": "First Cached", "team": "SF", "position": "QB", "status": "Active"},
            "2": {"full_name": "Second Cached", "team": "KC", "position": "WR", "status": "Active"},
        }

        result = enrich_roster_entries(entries, player_cache)

        assert [p["player_id"] for p in result] == [1, 2]
        assert [p["full_name"] for p in result] == ["First Cached", "Second Cached"]

    def test_empty_entries_returns_empty_list(self):
        assert enrich_roster_entries([], player_cache={}) == []

    def test_cached_entry_with_blank_position_falls_back_to_raw_espn_field(self):
        """A cached entry present but missing `position` still falls back to
        POSITION_ID_MAP, per field, rather than surfacing a blank value."""
        entries = [{"player": {"id": 5, "fullName": "Raw Name", "defaultPositionId": 17}, "lineupSlotId": 17}]
        player_cache = {"5": {"full_name": "", "team": "KC", "position": "", "status": "Active"}}

        result = enrich_roster_entries(entries, player_cache)

        assert result == [{
            "player_id": 5,
            "full_name": "Raw Name",
            "position": "K",
            "team": "KC",
            "lineup_slot_id": 17,
        }]


class TestGetEspnStandings:
    """Test get_espn_standings, including the sort/tiebreak derivation."""

    @pytest.mark.asyncio
    async def test_sorts_by_rank_final_then_rank_calculated_final_then_playoff_seed(
        self, monkeypatch
    ):
        """Teams sort by rankFinal when present, else rankCalculatedFinal, else playoffSeed."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        teams = [
            {"id": 1, "rankFinal": 3, "rankCalculatedFinal": None, "playoffSeed": 2},
            {"id": 2, "rankFinal": None, "rankCalculatedFinal": 1, "playoffSeed": 4},
            {"id": 3, "rankFinal": None, "rankCalculatedFinal": None, "playoffSeed": 2},
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"teams": teams}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_standings("1234", year=2023)

        assert result["success"] is True
        assert [team["id"] for team in result["standings"]] == [2, 3, 1]

        call_args = mock_client.get.call_args
        assert call_args.kwargs["params"] == [("view", "mStandings"), ("view", "mTeam")]

    @pytest.mark.asyncio
    async def test_pre_2018_array_envelope_normalizes_to_same_shape(self, monkeypatch):
        """A pre-2018 season hits the leagueHistory URL and unwraps the array envelope."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        league_object = {
            "id": 368876,
            "seasonId": 2015,
            "teams": [{"id": 1, "rankFinal": 1, "rankCalculatedFinal": None, "playoffSeed": 1}],
        }
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [league_object]

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_standings("368876", year=2015)

        assert result["success"] is True
        assert result["standings"] == league_object["teams"]

        call_args = mock_client.get.call_args
        assert call_args.args[0] == (
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/leagueHistory/368876"
        )
        assert call_args.kwargs["params"] == [
            ("view", "mStandings"),
            ("view", "mTeam"),
            ("seasonId", "2015"),
        ]

    @pytest.mark.asyncio
    async def test_missing_credentials_short_circuits(self, monkeypatch):
        """Missing ESPN credentials short-circuit before any HTTP call is made."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client") as mock_create_client:
            result = await get_espn_standings("1234", year=2023)

        mock_create_client.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_CREDENTIALS_NOT_CONFIGURED

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
            result = await get_espn_standings("1234", year=2023)

        assert result["success"] is False
        assert result["error_type"] == ErrorType.HTTP
        assert result["standings"] == []

    def test_registered_in_tool_registry(self):
        """get_espn_standings is present in tool_registry.get_all_tools()."""
        from nfl_mcp.tool_registry import get_all_tools

        tool_names = [t.__name__ for t in get_all_tools()]
        assert "get_espn_standings" in tool_names


class TestGetEspnScoreboard:
    """Test get_espn_scoreboard: final scores only (mMatchupScore)."""

    @pytest.mark.asyncio
    async def test_success_no_week_filter(self, monkeypatch):
        """With no week given, the full season's schedule is returned and no
        x-fantasy-filter header is sent."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        schedule = [
            {"id": 1, "matchupPeriodId": 1, "home": {"teamId": 1, "totalPoints": 100.0},
             "away": {"teamId": 2, "totalPoints": 90.0}},
            {"id": 2, "matchupPeriodId": 2, "home": {"teamId": 1, "totalPoints": 88.0},
             "away": {"teamId": 3, "totalPoints": 95.0}},
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"schedule": schedule}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_scoreboard("1234", year=2018)

        assert result["success"] is True
        assert result["scoreboard"] == schedule

        call_args = mock_client.get.call_args
        assert call_args.kwargs["params"] == [("view", "mMatchupScore")]
        assert "x-fantasy-filter" not in call_args.kwargs["headers"]

    @pytest.mark.asyncio
    async def test_success_with_week_filter(self, monkeypatch):
        """With week given, the returned schedule is filtered client-side to
        that matchup period (no x-fantasy-filter header — the catalog only
        confirms that header scopes box_scores()'s mMatchup+mScoreboard
        view, not this tool's lighter mMatchupScore view)."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        schedule = [
            {"id": 1, "matchupPeriodId": 1, "home": {"teamId": 1}, "away": {"teamId": 2}},
            {"id": 2, "matchupPeriodId": 3, "home": {"teamId": 1}, "away": {"teamId": 3}},
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"schedule": schedule}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_scoreboard("1234", week=3, year=2018)

        assert result["success"] is True
        assert result["scoreboard"] == [schedule[1]]

        call_args = mock_client.get.call_args
        assert "x-fantasy-filter" not in call_args.kwargs["headers"]

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
            result = await get_espn_scoreboard("1234", year=2018)

        assert result["success"] is False
        assert result["error_type"] == ErrorType.HTTP
        assert result["scoreboard"] == []

    @pytest.mark.asyncio
    async def test_missing_credentials_short_circuits(self, monkeypatch):
        """Missing ESPN credentials short-circuit before any HTTP call is made."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client") as mock_create_client:
            result = await get_espn_scoreboard("1234", year=2018)

        mock_create_client.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_CREDENTIALS_NOT_CONFIGURED

    def test_registered_in_tool_registry(self):
        """get_espn_scoreboard is present in tool_registry.get_all_tools()."""
        from nfl_mcp.tool_registry import get_all_tools

        tool_names = [t.__name__ for t in get_all_tools()]
        assert "get_espn_scoreboard" in tool_names

    @pytest.mark.asyncio
    async def test_week_omitted_caps_to_most_recent_weeks(self, monkeypatch):
        """With no `week`, a season longer than the cap is trimmed to the most
        recent weeks up to the league's current scoring period, with
        total_weeks/has_more surfacing the truncation (ADR 0006)."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        schedule = [
            {"id": w, "matchupPeriodId": w, "home": {"teamId": 1}, "away": {"teamId": 2}}
            for w in range(1, 8)
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"schedule": schedule, "scoringPeriodId": 6}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_scoreboard("1234", year=2018)

        assert result["success"] is True
        assert result["total_weeks"] == 7
        assert result["has_more"] is True
        returned_weeks = sorted(m["matchupPeriodId"] for m in result["scoreboard"])
        assert returned_weeks == [4, 5, 6]

    @pytest.mark.asyncio
    async def test_week_omitted_under_cap_returns_everything(self, monkeypatch):
        """A season with fewer weeks than the cap is returned in full, has_more False."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        schedule = [
            {"id": w, "matchupPeriodId": w, "home": {"teamId": 1}, "away": {"teamId": 2}}
            for w in range(1, 3)
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"schedule": schedule, "scoringPeriodId": 2}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_scoreboard("1234", year=2018)

        assert result["scoreboard"] == schedule
        assert result["total_weeks"] == 2
        assert result["has_more"] is False

    @pytest.mark.asyncio
    async def test_week_capping_boundary_exact_cap_returns_everything(self, monkeypatch):
        """A season with exactly `_MAX_WEEKS_UNSCOPED` weeks is returned in
        full — the boundary between "under cap" and "over cap"."""
        from nfl_mcp.espn_fantasy_tools import _MAX_WEEKS_UNSCOPED

        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        schedule = [
            {"id": w, "matchupPeriodId": w, "home": {"teamId": 1}, "away": {"teamId": 2}}
            for w in range(1, _MAX_WEEKS_UNSCOPED + 1)
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"schedule": schedule, "scoringPeriodId": _MAX_WEEKS_UNSCOPED}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_scoreboard("1234", year=2018)

        assert result["scoreboard"] == schedule
        assert result["total_weeks"] == _MAX_WEEKS_UNSCOPED
        assert result["has_more"] is False


class TestGetEspnMatchups:
    """Test get_espn_matchups: full box-score/lineup detail (mMatchup+mScoreboard)."""

    @pytest.mark.asyncio
    async def test_success_no_week_filter(self, monkeypatch):
        """With no week given, the full season's schedule is returned, requesting
        both mMatchup and mScoreboard views, and no x-fantasy-filter header is sent."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        schedule = [
            {"id": 1, "matchupPeriodId": 1,
             "home": {"teamId": 1, "totalPoints": 100.0,
                       "rosterForCurrentScoringPeriod": {"entries": []}},
             "away": {"teamId": 2, "totalPoints": 90.0,
                       "rosterForCurrentScoringPeriod": {"entries": []}}},
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"schedule": schedule}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_matchups("1234", year=2018)

        assert result["success"] is True
        assert result["matchups"] == schedule

        call_args = mock_client.get.call_args
        assert call_args.kwargs["params"] == [("view", "mMatchup"), ("view", "mScoreboard")]
        assert "x-fantasy-filter" not in call_args.kwargs["headers"]

    @pytest.mark.asyncio
    async def test_success_with_week_filter(self, monkeypatch):
        """With week given, x-fantasy-filter scopes the request and the
        returned schedule is filtered client-side to that matchup period."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        schedule = [
            {"id": 1, "matchupPeriodId": 1, "home": {"teamId": 1}, "away": {"teamId": 2}},
            {"id": 2, "matchupPeriodId": 5, "home": {"teamId": 1}, "away": {"teamId": 3}},
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"schedule": schedule}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_matchups("1234", week=5, year=2018)

        assert result["success"] is True
        assert result["matchups"] == [schedule[1]]

        call_args = mock_client.get.call_args
        import json as _json
        assert _json.loads(call_args.kwargs["headers"]["x-fantasy-filter"]) == {
            "schedule": {"filterMatchupPeriodIds": {"value": [5]}}
        }

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
            result = await get_espn_matchups("1234", year=2018)

        assert result["success"] is False
        assert result["error_type"] == ErrorType.HTTP
        assert result["matchups"] == []

    @pytest.mark.asyncio
    async def test_missing_credentials_short_circuits(self, monkeypatch):
        """Missing ESPN credentials short-circuit before any HTTP call is made."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client") as mock_create_client:
            result = await get_espn_matchups("1234", year=2018)

        mock_create_client.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_CREDENTIALS_NOT_CONFIGURED

    def test_registered_in_tool_registry(self):
        """get_espn_matchups is present in tool_registry.get_all_tools()."""
        from nfl_mcp.tool_registry import get_all_tools

        tool_names = [t.__name__ for t in get_all_tools()]
        assert "get_espn_matchups" in tool_names

    def _box_score_entry(self, player_id):
        return {
            "lineupSlotId": 4,
            "playerPoolEntry": {
                "player": {
                    "id": player_id,
                    "fullName": f"Player {player_id}",
                    "defaultPositionId": 2,
                    "proTeamId": 9,
                    "injuryStatus": "ACTIVE",
                    "ownership": {"percentOwned": 80.0},
                    "stats": [{"statSourceId": 0, "appliedTotal": 15.5}],
                }
            },
        }

    def _matchup(self, matchup_period_id, roster_size=2):
        entries = [self._box_score_entry(pid) for pid in range(roster_size)]
        return {
            "id": matchup_period_id,
            "matchupPeriodId": matchup_period_id,
            "home": {"teamId": 1, "totalPoints": 100.0,
                      "rosterForCurrentScoringPeriod": {"entries": entries}},
            "away": {"teamId": 2, "totalPoints": 90.0,
                      "rosterForCurrentScoringPeriod": {"entries": entries}},
        }

    @pytest.mark.asyncio
    async def test_week_omitted_caps_to_most_recent_weeks(self, monkeypatch):
        """With no `week`, a season longer than the cap is trimmed to the most
        recent weeks up to the league's current scoring period, with
        total_weeks/has_more surfacing the truncation (ADR 0006)."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        schedule = [self._matchup(w) for w in range(1, 8)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"schedule": schedule, "scoringPeriodId": 6}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_matchups("1234", year=2018)

        assert result["success"] is True
        assert result["total_weeks"] == 7
        assert result["has_more"] is True
        returned_weeks = sorted(m["matchupPeriodId"] for m in result["matchups"])
        assert returned_weeks == [4, 5, 6]

    @pytest.mark.asyncio
    async def test_week_capping_boundary_exact_cap_returns_everything(self, monkeypatch):
        """A season with exactly `_MAX_WEEKS_UNSCOPED` weeks is returned in
        full — the boundary between "under cap" and "over cap"."""
        from nfl_mcp.espn_fantasy_tools import _MAX_WEEKS_UNSCOPED

        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        schedule = [self._matchup(w) for w in range(1, _MAX_WEEKS_UNSCOPED + 1)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"schedule": schedule, "scoringPeriodId": _MAX_WEEKS_UNSCOPED}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_matchups("1234", year=2018, detail="full")

        assert result["matchups"] == schedule
        assert result["total_weeks"] == _MAX_WEEKS_UNSCOPED
        assert result["has_more"] is False

    @pytest.mark.asyncio
    async def test_week_given_total_weeks_reflects_server_scoped_fetch(self, monkeypatch):
        """When `week` is given, ESPN's x-fantasy-filter already narrows the
        fetch server-side, so total_weeks reflects just the requested period
        (1), not a season-wide count — unlike get_espn_scoreboard, which
        never sends that header."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        schedule = [self._matchup(5, roster_size=1)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"schedule": schedule, "scoringPeriodId": 5}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_matchups("1234", week=5, year=2018)

        assert result["total_weeks"] == 1
        assert result["has_more"] is False

    @pytest.mark.asyncio
    async def test_default_detail_trims_box_score_entries(self, monkeypatch):
        """With no `detail` given, each side's box-score roster entries are
        trimmed to the summary allowlist."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        schedule = [self._matchup(1, roster_size=1)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"schedule": schedule, "scoringPeriodId": 1}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_matchups("1234", week=1, year=2018)

        home_entries = result["matchups"][0]["home"]["rosterForCurrentScoringPeriod"]["entries"]
        assert home_entries == [{
            "playerId": 0,
            "fullName": "Player 0",
            "defaultPositionId": 2,
            "proTeamId": 9,
            "lineupSlotId": 4,
            "injuryStatus": "ACTIVE",
            "appliedTotal": 15.5,
        }]
        # Untouched fields survive summary trimming.
        assert result["matchups"][0]["home"]["totalPoints"] == 100.0

    @pytest.mark.asyncio
    async def test_detail_full_returns_box_score_entries_unfiltered(self, monkeypatch):
        """With detail="full", box-score roster entries pass through unmodified."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        schedule = [self._matchup(1, roster_size=1)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"schedule": schedule, "scoringPeriodId": 1}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_matchups("1234", week=1, year=2018, detail="full")

        assert result["matchups"] == schedule

    @pytest.mark.asyncio
    async def test_registry_rejects_invalid_detail(self):
        """tool_registry.get_espn_matchups rejects an unrecognized `detail` value."""
        from nfl_mcp.tool_registry import get_espn_matchups as registry_get_espn_matchups

        result = await registry_get_espn_matchups("1234", detail="verbose")

        assert result["success"] is False
        assert result["matchups"] == []

    @pytest.mark.asyncio
    async def test_full_season_box_score_worst_case_stays_within_hard_cap(self, monkeypatch):
        """A full-season, box-score-mode, `detail="full"` worst case — many weeks,
        many players, heavy per-player payloads — still stays within the 150 KB
        hard cap (ADR 0006), via the week cap plus the size-shrink safety net."""
        import json

        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        def heavy_entry(player_id):
            return {
                "lineupSlotId": 4,
                "playerPoolEntry": {
                    "player": {
                        "id": player_id,
                        "fullName": f"Player {player_id}",
                        "defaultPositionId": 2,
                        "proTeamId": 9,
                        "injuryStatus": "ACTIVE",
                        "ownership": {"percentOwned": 80.0, "percentStarted": 70.0},
                        "stats": [
                            {"statSourceId": 0, "appliedTotal": 15.5,
                             "stats": {str(i): float(i) for i in range(30)}}
                        ],
                    }
                },
            }

        def heavy_matchup(matchup_period_id, matchup_id):
            entries = [heavy_entry(pid) for pid in range(20)]
            return {
                "id": matchup_id,
                "matchupPeriodId": matchup_period_id,
                "home": {"teamId": matchup_id, "totalPoints": 100.0,
                          "rosterForCurrentScoringPeriod": {"entries": entries}},
                "away": {"teamId": matchup_id + 100, "totalPoints": 90.0,
                          "rosterForCurrentScoringPeriod": {"entries": entries}},
            }

        # 17 weeks x 8 matchups/week (16 teams) x 20-player rosters/side - the
        # full-season/box-score worst case the response-size research flagged
        # as the single biggest offender.
        schedule = [
            heavy_matchup(week, week * 100 + i)
            for week in range(1, 18)
            for i in range(8)
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"schedule": schedule, "scoringPeriodId": 10}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_matchups("1234", year=2018, detail="full")

        assert result["success"] is True
        assert len(json.dumps(result["matchups"])) <= 150 * 1024
        assert result["total_weeks"] == 17
        assert result["has_more"] is True


class TestGetEspnDraft:
    """Test get_espn_draft, including the year/leagueHistory boundary helper."""

    @pytest.mark.asyncio
    async def test_2018_plus_object_envelope(self, monkeypatch):
        """A 2018+ season hits the direct-season URL and passes the object envelope through."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        draft_object = {
            "id": 1234,
            "seasonId": 2018,
            "draftDetail": {
                "completeDate": 1535000000000,
                "drafted": True,
                "inProgress": False,
                "picks": [
                    {
                        "autoDraftTypeId": 0,
                        "bidAmount": 0,
                        "id": 1,
                        "keeper": False,
                        "lineupSlotId": 0,
                        "memberId": "{ABC}",
                        "nominatingTeamId": 1,
                        "overallPickNumber": 1,
                        "owningTeamIds": [1],
                        "playerId": 3139477,
                        "reservedForKeeper": False,
                        "roundId": 1,
                        "roundPickNumber": 1,
                        "teamId": 1,
                        "tradeLocked": False,
                    }
                ],
            },
        }
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = draft_object

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_draft("1234", year=2018)

        assert result["success"] is True
        assert result["draft"] == draft_object

        call_args = mock_client.get.call_args
        assert call_args.args[0] == (
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/"
            "ffl/seasons/2018/segments/0/leagues/1234"
        )
        assert call_args.kwargs["params"] == [("view", "mDraftDetail")]
        assert call_args.kwargs["cookies"] == {"espn_s2": "some-cookie", "SWID": "some-swid"}

    @pytest.mark.asyncio
    async def test_pre_2018_array_envelope_normalizes_to_same_shape(self, monkeypatch):
        """A pre-2018 season hits the leagueHistory URL and unwraps the array envelope."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        draft_object = {
            "id": 368876,
            "seasonId": 2015,
            "draftDetail": {
                "completeDate": 1440000000000,
                "drafted": True,
                "inProgress": False,
                "picks": [
                    {
                        "autoDraftTypeId": 0,
                        "bidAmount": 0,
                        "id": 1,
                        "keeper": False,
                        "lineupSlotId": 0,
                        "memberId": "{XYZ}",
                        "nominatingTeamId": 1,
                        "overallPickNumber": 1,
                        "owningTeamIds": [1],
                        "playerId": 12345,
                        "reservedForKeeper": False,
                        "roundId": 1,
                        "roundPickNumber": 1,
                        "teamId": 1,
                        "tradeLocked": False,
                    }
                ],
            },
        }
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [draft_object]

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_draft("368876", year=2015)

        assert result["success"] is True
        # Same shape as the 2018+ object-envelope case: a bare draft dict,
        # not the array ESPN actually sent.
        assert result["draft"] == draft_object

        call_args = mock_client.get.call_args
        assert call_args.args[0] == (
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/leagueHistory/368876"
        )
        assert call_args.kwargs["params"] == [("view", "mDraftDetail"), ("seasonId", "2015")]

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
            result = await get_espn_draft("1234", year=2018)

        assert result["success"] is False
        assert result["error_type"] == ErrorType.HTTP
        assert result["draft"] is None

    @pytest.mark.asyncio
    async def test_missing_credentials_short_circuits(self, monkeypatch):
        """Missing ESPN credentials short-circuit before any HTTP call is made."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client") as mock_create_client:
            result = await get_espn_draft("1234", year=2018)

        mock_create_client.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_CREDENTIALS_NOT_CONFIGURED

    def test_registered_in_tool_registry(self):
        """get_espn_draft is present in tool_registry.get_all_tools()."""
        from nfl_mcp.tool_registry import get_all_tools

        tool_names = [t.__name__ for t in get_all_tools()]
        assert "get_espn_draft" in tool_names


class TestGetEspnTransactions:
    """Test get_espn_transactions."""

    @pytest.mark.asyncio
    async def test_success_unfiltered(self, monkeypatch):
        """All transactions ESPN returns come back untouched when `types` is omitted."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        league_data = {
            "id": 1234,
            "transactions": [
                {"id": "txn-1", "type": "WAIVER", "teamId": 1},
                {"id": "txn-2", "type": "TRADE", "teamId": 2},
            ],
        }
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = league_data

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_transactions("1234", week=3, year=2023)

        assert result["success"] is True
        assert result["total_transactions"] == 2
        assert result["transactions"] == league_data["transactions"]

        call_args = mock_client.get.call_args
        assert call_args.args[0] == (
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/"
            "ffl/seasons/2023/segments/0/leagues/1234"
        )
        assert call_args.kwargs["params"] == [
            ("view", "mTransactions2"),
            ("scoringPeriodId", "3"),
        ]
        assert call_args.kwargs["cookies"] == {"espn_s2": "some-cookie", "SWID": "some-swid"}

    @pytest.mark.asyncio
    async def test_success_filtered_by_types(self, monkeypatch):
        """`types` filters the returned list to matching transaction types only."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        league_data = {
            "transactions": [
                {"id": "txn-1", "type": "WAIVER"},
                {"id": "txn-2", "type": "TRADE"},
                {"id": "txn-3", "type": "WAIVER"},
                {"id": "txn-4", "type": "FREEAGENT"},
            ],
        }
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = league_data

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_transactions("1234", types=["WAIVER"])

        assert result["success"] is True
        assert result["total_transactions"] == 2
        assert all(t["type"] == "WAIVER" for t in result["transactions"])

    @pytest.mark.asyncio
    async def test_week_omitted_resolves_current_scoring_period(self, monkeypatch):
        """With no `week`, the league's current `scoringPeriodId` is resolved via
        one extra lightweight request first, then forwarded to the mTransactions2
        request — mirroring espn_api's own two-step resolution, since the catalog
        documents scoringPeriodId as a required param for this view."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        settings_response = MagicMock()
        settings_response.raise_for_status.return_value = None
        settings_response.json.return_value = {"scoringPeriodId": 7, "settings": {}}

        transactions_response = MagicMock()
        transactions_response.raise_for_status.return_value = None
        transactions_response.json.return_value = {"transactions": []}

        mock_client = AsyncMock()
        mock_client.get.side_effect = [settings_response, transactions_response]
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_transactions("1234", year=2023)

        assert result["success"] is True
        assert mock_client.get.call_count == 2

        first_call, second_call = mock_client.get.call_args_list
        assert first_call.kwargs["params"] == [("view", "mSettings")]
        assert second_call.kwargs["params"] == [
            ("view", "mTransactions2"),
            ("scoringPeriodId", "7"),
        ]

    @pytest.mark.asyncio
    async def test_missing_transactions_key_is_empty_not_error(self, monkeypatch):
        """A response with no `transactions` key means no matches, not an error
        (catalog: indistinguishable from a real empty-result 200 at the wire level)."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"id": 1234}

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_transactions("1234", year=2023)

        assert result["success"] is True
        assert result["transactions"] == []
        assert result["total_transactions"] == 0

    @pytest.mark.asyncio
    async def test_401_error_path(self, monkeypatch):
        """A 401 response is classified as expired ESPN cookies, not a generic HTTP error."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        response = httpx.Response(401, request=httpx.Request("GET", "https://example.com"))
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=response.request, response=response
        )

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_transactions("1234", year=2023)

        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_EXPIRED_COOKIES

    @pytest.mark.asyncio
    async def test_non_auth_http_error_falls_through_to_handle_http_errors(self, monkeypatch):
        """A non-401/403 HTTP failure (500) is not swallowed by the auth decorator
        and comes back with the outer decorator's default_data merged in."""
        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        response = httpx.Response(500, request=httpx.Request("GET", "https://example.com"))
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=response.request, response=response
        )

        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await get_espn_transactions("1234", year=2023)

        assert result["success"] is False
        assert result["error_type"] == ErrorType.HTTP
        assert result["transactions"] == []
        assert result["total_transactions"] == 0

    @pytest.mark.asyncio
    async def test_missing_credentials_short_circuits(self, monkeypatch):
        """Missing ESPN credentials short-circuit before any HTTP call is made."""
        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client") as mock_create_client:
            result = await get_espn_transactions("1234", year=2023)

        mock_create_client.assert_not_called()
        assert result["success"] is False
        assert result["error_type"] == ErrorType.ESPN_CREDENTIALS_NOT_CONFIGURED

    def test_registered_in_tool_registry(self):
        """get_espn_transactions is present in tool_registry.get_all_tools()."""
        from nfl_mcp.tool_registry import get_all_tools

        tool_names = [t.__name__ for t in get_all_tools()]
        assert "get_espn_transactions" in tool_names


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


class TestEspnPlayersFreeAgentsRegistryValidation:
    """Test the tool_registry.py wrappers' limit/offset validation for both tools."""

    @pytest.mark.asyncio
    async def test_get_espn_players_out_of_range_limit_snaps_to_default(self, monkeypatch):
        """A limit outside 1-100 snaps to the default of 25 (validate_limit's semantics)."""
        from nfl_mcp.tool_registry import get_espn_players as registry_get_espn_players

        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        raw_players = [{"id": i} for i in range(40)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = raw_players
        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await registry_get_espn_players(limit=1000)

        assert result["success"] is True
        assert len(result["players"]) == 25

    @pytest.mark.asyncio
    async def test_get_espn_players_negative_offset_clamps_to_zero(self, monkeypatch):
        """A negative offset clamps to 0 rather than erroring."""
        from nfl_mcp.tool_registry import get_espn_players as registry_get_espn_players

        monkeypatch.delenv("ESPN_S2", raising=False)
        monkeypatch.delenv("ESPN_SWID", raising=False)

        raw_players = [{"id": i} for i in range(5)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = raw_players
        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await registry_get_espn_players(limit=5, offset=-10)

        assert result["success"] is True
        assert result["players"] == raw_players[:5]

    @pytest.mark.asyncio
    async def test_get_espn_free_agents_out_of_range_limit_snaps_to_default(self, monkeypatch):
        """A limit outside 1-100 snaps to the default of 25 (validate_limit's semantics)."""
        from nfl_mcp.tool_registry import get_espn_free_agents as registry_get_espn_free_agents

        monkeypatch.setenv("ESPN_S2", "some-cookie")
        monkeypatch.setenv("ESPN_SWID", "some-swid")

        raw_players = [{"id": i} for i in range(40)]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"players": raw_players}
        mock_client = _mock_http_client(mock_response)

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client", return_value=mock_client):
            result = await registry_get_espn_free_agents("1234", limit=0)

        assert result["success"] is True
        assert len(result["players"]) == 25

    @pytest.mark.asyncio
    async def test_get_espn_free_agents_invalid_league_id_returns_error(self):
        """An invalid league_id short-circuits with a validation error, no HTTP call made."""
        from nfl_mcp.tool_registry import get_espn_free_agents as registry_get_espn_free_agents

        with patch("nfl_mcp.espn_fantasy_tools.create_http_client") as mock_create_client:
            result = await registry_get_espn_free_agents("x" * 100)

        mock_create_client.assert_not_called()
        assert result["success"] is False


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
