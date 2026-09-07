"""
Tests for ESPN Fantasy auth-error classification.

Covers classify_espn_auth_error against constructed httpx.Response-shaped
inputs for 401, 403, and a non-auth status code.
"""

import httpx

from nfl_mcp.espn_errors import EspnAuthErrorClassification, classify_espn_auth_error


class TestClassifyEspnAuthError:
    """Test classify_espn_auth_error status-code classification."""

    def test_401_is_confident_expired_cookies(self):
        """401 gets the confident expired-cookies message."""
        response = httpx.Response(401, request=httpx.Request("GET", "https://example.com"))

        result = classify_espn_auth_error(response)

        assert isinstance(result, EspnAuthErrorClassification)
        assert result.category == "expired_cookies"
        assert "expired" in result.message.lower()
        assert "rerun" in result.message.lower()

    def test_403_is_hedged(self):
        """403 hedges: expired cookies is only one of several named causes."""
        response = httpx.Response(403, request=httpx.Request("GET", "https://example.com"))

        result = classify_espn_auth_error(response)

        assert isinstance(result, EspnAuthErrorClassification)
        assert result.category == "possible_auth_issue"
        message_lower = result.message.lower()
        assert "expired" in message_lower
        assert "league id" in message_lower or "league" in message_lower
        assert "block" in message_lower or "transient" in message_lower

    def test_non_auth_status_is_not_classified_as_auth_error(self):
        """A non-auth status code (e.g. 500) should not be treated as an auth error."""
        response = httpx.Response(500, request=httpx.Request("GET", "https://example.com"))

        result = classify_espn_auth_error(response)

        assert isinstance(result, EspnAuthErrorClassification)
        assert result.category == "not_an_auth_error"

    def test_404_is_not_an_auth_error(self):
        """404 is also not an auth-related status."""
        response = httpx.Response(404, request=httpx.Request("GET", "https://example.com"))

        result = classify_espn_auth_error(response)

        assert result.category == "not_an_auth_error"
