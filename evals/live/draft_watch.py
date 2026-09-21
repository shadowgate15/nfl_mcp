"""
Live draft "war room" — watch a real ESPN draft and get a recommendation each
time you're on the clock.

This is the reusable version of the ad-hoc loop we used to validate the draft
flow live. Point it at a live/mock ESPN league and your team id; it polls the
draft state, and when it's your turn it prints roster context + the top picks
(with value cliffs and positional runs). In the bench rounds it flips to a
depth overlay (RB/WR + handcuffs), because pure VBD goes blind on deep benches.

Usage:
    python -m evals.live.draft_watch --league-id 1234 --my-slot 4
    python -m evals.live.draft_watch --league-id 1234 --my-slot 4 --once   # single check
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from collections import Counter

from nfl_mcp import draft_tools as dt
from nfl_mcp import espn_fantasy_tools as eft
from nfl_mcp.database import NFLDatabase

VBD_POS = ("QB", "RB", "WR", "TE")


def _starters_full(counts: dict, reqs: dict) -> bool:
    base_full = all(counts.get(p, 0) >= reqs.get(p, 0) for p in ("QB", "RB", "WR", "TE"))
    flex_full = dt._flex_filled(counts, reqs) >= reqs.get("FLEX", 0)
    return base_full and flex_full


def _label(db, player_id) -> tuple[str, str]:
    """Best-effort (name, position) for a bare ESPN playerId via the athletes cache."""
    athlete = db.get_athlete_by_id(str(player_id)) or {}
    return athlete.get("full_name") or f"player {player_id}", athlete.get("position") or "?"


async def _show_turn(db, league_id: str, year: int | None, my_slot: int, teams: int, reqs: dict, picks, num: int):
    n = len(picks)
    mine = [pk for pk in picks if pk.get("teamId") == my_slot]
    my_positions = []
    for pk in mine:
        _, pos = _label(db, pk.get("playerId"))
        my_positions.append(pos)
    counts = Counter(my_positions)
    my_last = max((pk.get("overallPickNumber", 0) for pk in mine), default=0)
    since = [pk for pk in picks if pk.get("overallPickNumber", 0) > my_last]
    rnd = n // teams + 1

    print("\n" + "=" * 70)
    print(f">>> YOU'RE ON THE CLOCK — pick #{n + 1} (round {rnd}), team {my_slot}")
    print("=" * 70)
    team = []
    for pk in mine:
        name, pos = _label(db, pk.get("playerId"))
        team.append(f"{name}({pos})")
    print("Your roster:", team or "(empty)")
    if since:
        since_positions = [_label(db, pk.get("playerId"))[1] for pk in since]
        print(f"Gone since your last pick ({len(since)}): {dict(Counter(since_positions))}")

    r = await dt.recommend_draft_pick(league_id, year=year, my_slot=my_slot, num_suggestions=num, db=db)
    if not r.get("success"):
        print("recommend error:", r.get("error"))
        return

    bench_mode = _starters_full(counts, reqs)
    if bench_mode:
        bp = r.get("best_available_by_position") or {}
        print("\nBENCH MODE — starters filled. Prioritise RB/WR depth + handcuffs;")
        print("ignore backup QB / extra TE (pure VBD is blind on deep benches).")
        for pos in ("RB", "WR"):
            if pos in bp:
                print(f"  best {pos} available: {bp[pos]['name']} (vbd {bp[pos]['vbd']})")
    else:
        print("\nTop picks (by need-weighted value):")
        for s in r.get("suggestions", []):
            print(f"  {s['name']:<22} {s['position']:<3} vbd={s['vbd']:<7} :: {'; '.join(s['reasoning'])}")
        if r.get("value_cliffs"):
            print("  value cliffs:", r["value_cliffs"])
    top = r.get("top_pick")
    if top:
        print(f"\n=> Suggested: {top['name']} ({top['position']})"
              + (" — but in bench mode, take the RB/WR body above" if bench_mode else ""))


def _final(db, picks, my_slot):
    mine = sorted(
        (pk for pk in picks if pk.get("teamId") == my_slot),
        key=lambda x: x.get("overallPickNumber", 0),
    )
    print("\n" + "=" * 70)
    print("DRAFT COMPLETE — your roster:")
    for pk in mine:
        name, pos = _label(db, pk.get("playerId"))
        print(f"  R{pk.get('roundId')}  {name} ({pos})")
    print("=" * 70)


async def run(league_id: str, year: int | None, my_slot: int, interval: int, num: int, once: bool):
    db = NFLDatabase(tempfile.mktemp(suffix=".db"))
    lg = await eft.get_espn_league(league_id, year)
    if not (lg.get("success") and lg.get("league")):
        print("Could not load league settings:", lg.get("error"))
        return 1
    espn_settings = lg["league"].get("settings", {}) or {}
    settings = dt._espn_settings_to_sleeper_shape(espn_settings)
    teams = int(settings.get("teams", 12) or 12)
    reqs = dt._starter_requirements(settings)
    ppr = dt._ppr_from_espn_settings(espn_settings)
    print(f"Watching league {league_id}: {teams}-team {dt._scoring_label_from_ppr(ppr)}. "
          f"You are team {my_slot}. Starters: {reqs}")

    recommended_for = -1
    while True:
        d = await eft.get_espn_draft(league_id, year)
        if not (d.get("success") and d.get("draft")):
            print("Could not load draft:", d.get("error"))
            return 1
        draft_detail = (d["draft"] or {}).get("draftDetail") or {}
        picks = sorted(draft_detail.get("picks") or [], key=lambda p: p.get("overallPickNumber") or 0)
        n = len(picks)
        if draft_detail.get("drafted") and not draft_detail.get("inProgress") and n > 0:
            _final(db, picks, my_slot)
            return 0
        # Best-effort: assumes standard snake order (team ids picking 1..N,
        # reversing each round). ESPN's actual draft order per team id isn't
        # confirmed anywhere in the catalog, so this can misfire for a league
        # with a randomized/non-sequential team-id draft order.
        on_clock = dt._snake_slot(n, teams) if teams else None
        if on_clock == my_slot and n != recommended_for:
            await _show_turn(db, league_id, year, my_slot, teams, reqs, picks, num)
            recommended_for = n
            if once:
                return 0
            print("\n(make your pick in ESPN; watching for the next turn — Ctrl-C to stop)")
        await asyncio.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description="Live ESPN draft watcher")
    ap.add_argument("--league-id", required=True)
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--my-slot", type=int, required=True, help="your ESPN team id (mTeam.id)")
    ap.add_argument("--interval", type=int, default=8, help="poll seconds")
    ap.add_argument("--num", type=int, default=6, help="suggestions to show")
    ap.add_argument("--once", action="store_true", help="single check, don't loop")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args.league_id, args.year, args.my_slot, args.interval, args.num, args.once))
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
