"""
Pre-draft flight check — validate the whole ESPN draft flow against a REAL
league, before draft day.

Drives the actual code paths (espn_fantasy_tools.get_espn_draft /
get_espn_league and draft_tools.recommend_draft_pick) so any mismatch between
our parsing and the live ESPN API surfaces now, not while you're on the clock.

Usage:
    python -m evals.live.validate_draft --league-id 1234
    python -m evals.live.validate_draft --league-id 1234 --year 2025 --my-slot 3
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile

from nfl_mcp import draft_tools as dt
from nfl_mcp import espn_fantasy_tools as eft
from nfl_mcp.database import NFLDatabase

_results: list[dict] = []


def _ok(name: str, detail: str):
    _results.append({"name": name, "ok": True, "detail": detail})
    print(f"  ✅ {name:<26} {detail}")


def _fail(name: str, detail: str):
    _results.append({"name": name, "ok": False, "detail": detail})
    print(f"  ❌ {name:<26} {detail}")


async def run(args) -> int:
    print("=" * 78)
    print("PRE-DRAFT FLIGHT CHECK")
    print("=" * 78)

    db = NFLDatabase(tempfile.mktemp(suffix=".db"))

    # 1) get_espn_league — settings & format parsing
    lg = await eft.get_espn_league(args.league_id, args.year)
    if not (lg.get("success") and lg.get("league")):
        _fail("get_espn_league", f"{lg.get('error')}")
        return 1
    espn_settings = lg["league"].get("settings", {}) or {}
    settings = dt._espn_settings_to_sleeper_shape(espn_settings)
    reqs = dt._starter_requirements(settings)
    ppr = dt._ppr_from_espn_settings(espn_settings)
    scoring = dt._scoring_label_from_ppr(ppr)
    _ok("get_espn_league", f"teams={settings.get('teams')} scoring={scoring} starters={reqs}")

    # 2) get_espn_draft — pick shape & player ids
    d = await eft.get_espn_draft(args.league_id, args.year)
    if not (d.get("success") and d.get("draft")):
        _fail("get_espn_draft", f"{d.get('error')}")
        return 1
    draft_detail = (d["draft"] or {}).get("draftDetail") or {}
    picks = draft_detail.get("picks") or []
    if picks:
        pk = picks[0]
        if pk.get("playerId") is not None and pk.get("teamId") is not None:
            _ok("get_espn_draft", f"{len(picks)} picks; sample playerId={pk.get('playerId')} teamId={pk.get('teamId')}")
        else:
            _fail("get_espn_draft", f"pick shape unexpected: keys={sorted(pk.keys())}")
    else:
        _ok("get_espn_draft", "0 picks (draft not started yet — fine pre-draft)")

    # 3) recommend_draft_pick — end-to-end (values + roster need)
    r = await dt.recommend_draft_pick(
        args.league_id, year=args.year, my_slot=args.my_slot, num_suggestions=3, db=db
    )
    if not r.get("success"):
        _fail("recommend_draft_pick", f"{r.get('error')}")
        return 1
    sugg = r.get("suggestions", [])
    if sugg:
        top = ", ".join(f"{x['name']}({x['position']})" for x in sugg)
        _ok("recommend_draft_pick", f"picks_made={r.get('picks_made')} format={r.get('format', {}).get('scoring')} "
                                    f"source={r.get('source')} | top: {top}")
    else:
        _ok("recommend_draft_pick", f"picks_made={r.get('picks_made')} (no players left / pool exhausted)")

    print("-" * 78)
    ok = sum(1 for x in _results if x["ok"])
    crit_fail = sum(1 for x in _results if not x["ok"])
    print(f"  {ok}/{len(_results)} checks passed, {crit_fail} failure(s)")
    print("=" * 78)
    return 1 if crit_fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the ESPN draft flow against a real league")
    ap.add_argument("--league-id", required=True)
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--my-slot", type=int, default=1)
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    import sys
    sys.exit(main())
