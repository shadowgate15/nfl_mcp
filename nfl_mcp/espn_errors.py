"""
ESPN Fantasy auth-error classification.

Tells "cookies not configured," "cookies expired," and "not an auth problem
at all" apart from an ESPN HTTP response, in exactly one place. Deliberately
independent of errors.py's response-envelope convention (create_error_response
etc.) so it can be called directly by both the tool-level decorator and the
CI contracts check (see ADR 0001, ADR 0003).
"""

from dataclasses import dataclass

import httpx

# ESPN's 403 is not a reliable auth signal: the endpoint-catalog research
# observed it on both a public league with zero cookies and a private league
# with confirmed-correct credentials, so its message hedges rather than
# asserting expired cookies outright.
_EXPIRED_COOKIES_MESSAGE = (
    "Your ESPN cookies expired — rerun the cookie-pull script to refresh "
    "ESPN_S2/ESPN_SWID."
)
_POSSIBLE_AUTH_ISSUE_MESSAGE = (
    "ESPN returned 403, which isn't a reliable auth signal — this can mean "
    "expired ESPN cookies, a wrong league ID, or a transient ESPN block."
)


@dataclass
class EspnAuthErrorClassification:
    """Classification of an ESPN response as an auth error (or not)."""

    category: str
    message: str


def classify_espn_auth_error(response: httpx.Response) -> EspnAuthErrorClassification:
    """
    Classify an ESPN HTTP response as an auth error, or not.

    Args:
        response: The httpx.Response received from ESPN.

    Returns:
        EspnAuthErrorClassification with a category and human-readable message.
    """
    if response.status_code == 401:
        return EspnAuthErrorClassification(
            category="expired_cookies",
            message=_EXPIRED_COOKIES_MESSAGE,
        )

    if response.status_code == 403:
        return EspnAuthErrorClassification(
            category="possible_auth_issue",
            message=_POSSIBLE_AUTH_ISSUE_MESSAGE,
        )

    return EspnAuthErrorClassification(
        category="not_an_auth_error",
        message=f"HTTP {response.status_code} is not an ESPN auth error.",
    )
