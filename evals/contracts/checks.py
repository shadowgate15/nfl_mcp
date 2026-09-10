"""
Live data-source contract checks (Eval Layer B).

Each check hits a real upstream source (or drives our own code that does) and
asserts the fields we depend on still exist. A check "fails" by raising; the
runner turns that into a red result. CRITICAL failures make the process exit
non-zero so the scheduled workflow goes red and notifies.

Design notes
- Field-level checks hit the raw API so a failure pinpoints an *upstream* change.
- A couple of checks drive our own code paths (FantasyCalc via the value service,
  nflverse via the defense analyzer) so they also catch *our* parsing breaking.
- Everything is best-effort and isolated: one source being down doesn't abort the
  others.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

# (name, critical, fn)
CHECKS: list[tuple[str, bool, Callable[[], str]]] = []


def check(name: str, critical: bool = True):
    def deco(fn: Callable[[], str]):
        CHECKS.append((name, critical, fn))
        return fn
    return deco


def _get(url: str, **kw) -> httpx.Response:
    r = httpx.get(url, follow_redirects=True, timeout=45, **kw)
    r.raise_for_status()
    return r


def _completed_seasons() -> list[int]:
    """Recent seasons that should have full published data (newest first)."""
    now = datetime.now(UTC)
    base = now.year - (1 if now.month >= 3 else 2)
    return [base, base - 1]


# ---------------------------------------------------------------------------
# FantasyCalc — powers player values, trades, draft board, projections base
# ---------------------------------------------------------------------------
@check("fantasycalc.values", critical=True)
def _fantasycalc() -> str:
    data = _get(
        "https://api.fantasycalc.com/values/current",
        params={"isDynasty": "false", "numQbs": 1, "numTeams": 12, "ppr": 1},
    ).json()
    assert isinstance(data, list) and len(data) > 100, f"expected a big list, got {type(data)} len={len(data) if hasattr(data,'__len__') else '?'}"
    first = data[0]
    player = first.get("player") or {}
    assert player.get("espnId"), "player.espnId missing (breaks ESPN mapping)"
    assert player.get("position"), "player.position missing"
    assert first.get("value") is not None, "value missing"
    assert first.get("positionRank") is not None, "positionRank missing"
    # end-to-end through our service (catches our parsing too)
    from nfl_mcp.player_values import PlayerValuesService
    svc = PlayerValuesService(db=None)
    res = asyncio.run(svc.get_values(1.0, 1, 12, False))
    assert res.get("source") == "fantasycalc" and res.get("count", 0) > 100, \
        f"value service returned source={res.get('source')} count={res.get('count')}"
    return f"{len(data)} values; top={player.get('name')} (espnId ok)"


# ---------------------------------------------------------------------------
# nflverse — powers defense-vs-position rankings + backtest ground truth
# ---------------------------------------------------------------------------
@check("nflverse.defense_rankings", critical=True)
def _nflverse_defense() -> str:
    from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer
    an = DefenseRankingsAnalyzer(db=None)
    for season in _completed_seasons():
        rankings = asyncio.run(an.fetch_defense_rankings(season))
        wr = rankings.get("WR", [])
        if wr and wr[0].get("source") == "nflverse":
            assert len(wr) == 32, f"expected 32 teams, got {len(wr)}"
            for pos in ("QB", "RB", "TE"):
                assert len(rankings.get(pos, [])) == 32, f"{pos} not 32 teams"
            return f"{season}: 32 teams × 4 positions from nflverse"
    raise AssertionError(f"no nflverse defense data for seasons {_completed_seasons()}")


@check("nflverse.usage_columns", critical=True)
def _nflverse_columns() -> str:
    from evals.backtest.data import load_season
    for season in _completed_seasons():
        try:
            recs = load_season(season)
        except Exception:
            continue  # that season's CSV not published yet -> try older
        if recs:
            assert any(r["ppr"] for r in recs), "no fantasy_points_ppr values"
            assert any(r["touches"] > 0 for r in recs), "no targets/carries (touches)"
            return f"{season}: {len(recs)} REG records with ppr + touches"
    raise AssertionError("nflverse player_stats unavailable for recent seasons")


# ---------------------------------------------------------------------------
# ESPN — powers news/teams/injuries/standings/schedules/coaching (warn-level,
# except espn.state below: the hard cutover off Sleeper (ADR 0008) leaves no
# fallback state source, so that one check is critical)
# ---------------------------------------------------------------------------
@check("espn.state", critical=True)
def _espn_state() -> str:
    """ESPN-core's parameterless scoreboard endpoint: no league/auth needed."""
    d = _get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard").json()
    week = d.get("week") or {}
    season = d.get("season") or {}
    assert week.get("number") is not None, "week.number missing"
    assert season.get("type") is not None, "season.type missing"
    assert season.get("year") is not None, "season.year missing"
    return f"week={week.get('number')} season={season.get('year')} (type={season.get('type')})"


@check("espn.teams", critical=False)
def _espn_teams() -> str:
    d = _get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams").json()
    teams = d.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    assert len(teams) >= 32, f"expected ≥32 teams, got {len(teams)}"
    t0 = teams[0].get("team", {})
    assert t0.get("abbreviation"), "team.abbreviation missing"
    return f"{len(teams)} teams; sample={t0.get('abbreviation')}"


@check("espn.news", critical=False)
def _espn_news() -> str:
    d = _get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/news").json()
    arts = d.get("articles", [])
    assert arts and arts[0].get("headline"), "news articles/headline missing"
    return f"{len(arts)} articles"


# ---------------------------------------------------------------------------
# ESPN Fantasy — the maintainer's own private league; critical, since a red
# build here means ESPN's shape broke or the cookies expired (ADR 0001/0003)
# ---------------------------------------------------------------------------
# Not a secret, matching every check above. Maintainer: replace with your league ID.
_ESPN_FANTASY_LEAGUE_ID = "1804399283"


@check("espn_fantasy.league", critical=True)
def _espn_fantasy_league() -> str:
    from nfl_mcp.errors import ErrorType
    from nfl_mcp.espn_fantasy_tools import get_espn_league

    # get_espn_league stacks @handle_espn_auth_errors under @handle_http_errors,
    # so an ESPN httpx.HTTPStatusError never reaches this check as an exception
    # — it's already been run through espn_errors.classify_espn_auth_error and
    # turned into this dict's error/error_type (ADR 0003's "same detector,
    # not a second one"). Surface that classified message as-is rather than
    # re-deriving it.
    result = asyncio.run(get_espn_league(_ESPN_FANTASY_LEAGUE_ID))

    if not result.get("success"):
        error_type = result.get("error_type")
        if error_type in (ErrorType.ESPN_EXPIRED_COOKIES, ErrorType.ESPN_POSSIBLE_AUTH_ISSUE):
            raise AssertionError(result.get("error"))
        raise AssertionError(f"get_espn_league failed ({error_type}): {result.get('error')}")

    league = result.get("league") or {}
    settings = league.get("settings") or {}
    assert league.get("id") is not None, "league.id missing"
    assert settings.get("name"), "league.settings.name missing"
    assert settings.get("size"), "league.settings.size missing"
    assert settings.get("rosterSettings", {}).get("lineupSlotCounts"), \
        "league.settings.rosterSettings.lineupSlotCounts missing"
    assert settings.get("scoringSettings", {}).get("scoringItems"), \
        "league.settings.scoringSettings.scoringItems missing"
    return f"league id={league.get('id')} name={settings.get('name')!r} size={settings.get('size')}"


@check("espn_fantasy.players", critical=True)
def _espn_fantasy_players() -> str:
    """
    Replaces sleeper.players (ADR 0008): under the hard cutover there's no
    fallback identity source left if this silently breaks, so this is
    critical where its Sleeper predecessor was not.

    Only asserts the identity fields this catalog endpoint (`/players?view=
    players_wl`, no auth) actually carries live: id, fullName,
    defaultPositionId, proTeamId. injured/injuryStatus — present on
    roster-scoped views (kona_player_info/mRoster, see
    _summarize_roster_entry) — were live-probed absent from this endpoint's
    2,627-player pool (0 of 2,627 carried either key), so they're not
    asserted here.
    """
    from nfl_mcp.espn_fantasy_tools import get_espn_players

    result = asyncio.run(get_espn_players(limit=50))
    assert result.get("success"), f"get_espn_players failed: {result.get('error')}"
    players = result.get("players") or []
    assert players, "players list empty"
    sample = players[0]
    for field in ("id", "fullName", "defaultPositionId", "proTeamId"):
        assert field in sample, f"player.{field} missing"
    return f"{result.get('total_players')} players; sample={sample.get('fullName')!r}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_all() -> list[dict]:
    results = []
    for name, critical, fn in CHECKS:
        try:
            detail = fn()
            results.append({"name": name, "critical": critical, "ok": True, "detail": detail})
        except Exception as e:
            results.append({"name": name, "critical": critical, "ok": False, "detail": f"{type(e).__name__}: {e}"})
    return results


def exit_code(results: list[dict]) -> int:
    """Non-zero iff a CRITICAL check failed (warnings don't fail the job)."""
    return 1 if any((not r["ok"] and r["critical"]) for r in results) else 0


def main() -> int:
    print("=" * 78)
    print(f"DATA-SOURCE CONTRACT CHECKS — {datetime.now(UTC).isoformat(timespec='seconds')}")
    print("=" * 78)
    results = run_all()
    crit_fail = 0
    warn_fail = 0
    for r in results:
        icon = "✅" if r["ok"] else ("❌" if r["critical"] else "⚠️ ")
        tag = "CRIT" if r["critical"] else "warn"
        print(f"  {icon} [{tag}] {r['name']:<28} {r['detail']}")
        if not r["ok"]:
            if r["critical"]:
                crit_fail += 1
            else:
                warn_fail += 1
    print("-" * 78)
    ok = sum(1 for r in results if r["ok"])
    print(f"  {ok}/{len(results)} passed | {crit_fail} critical failure(s), {warn_fail} warning(s)")
    print("=" * 78)
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
