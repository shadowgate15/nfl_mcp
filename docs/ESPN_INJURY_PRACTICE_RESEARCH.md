# ESPN Injury/Practice-Status Research: Findings and Recommendation

**Date:** September 9, 2026
**Ticket:** #34 ("Decide the module home (or supersession) for `_fetch_injuries`/`_fetch_practice_reports`"), part of the Sleeper-to-ESPN-Fantasy cutover map (#21)

---

## Question

`nfl_mcp/sleeper_enrichment.py` holds two more leaf helpers not covered by any
existing map decision: `_fetch_injuries()` and `_fetch_practice_reports(season,
week)`. Both are used only by `server.py`'s prefetch loop. Like
`_fetch_week_schedule` before them (issue #26/#33), this could be a pure
relocation question — but unlike the schedule helpers, `_fetch_injuries`
already imports `InjuryAggregator` from `nfl_mcp/injury_service.py`, raising
the possibility that `injury_service.py` already supersedes part of this
rather than it being a clean move into the new `nfl_enrichment.py` module.

All findings below were verified directly against this repo's source code
(the primary source for a codebase-organization question) on 2026-09-09.

---

## Findings

### 1. Both functions are 100% ESPN-core — zero Sleeper dependency

`nfl_mcp/sleeper_enrichment.py::_fetch_injuries()` (defined at line 285) calls
only:

```
GET https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/{team}/injuries?limit=50&page={page}
```

(`nfl_mcp/sleeper_enrichment.py:330`), paginating over all 32 NFL teams, then
following each returned `$ref` to fetch individual injury detail objects
(lines 354–394) and normalizing status via `InjuryAggregator.normalize_status`
/`get_severity` (imported at `nfl_mcp/sleeper_enrichment.py:402`). No call to
`api.sleeper.app` anywhere in the function.

`_fetch_practice_reports(season, week)` (defined at line 447) makes **no
network call of its own** — it calls `_fetch_injuries()` internally (line
465) and then derives a DNP/LP/FP practice-status label from each injury's
`injury_status` string via pure keyword matching (lines 476–487, e.g. `'OUT'
in status → 'DNP'`, `'QUESTIONABLE' in status → 'LP'`). This mirrors the
ticket's expectation: like `_enrich_usage_and_opponent` (issue #32/#33
finding), there is zero Sleeper dependency anywhere in this call chain.

**Conclusion:** both functions would be safe to migrate at the network-call
level exactly as `_fetch_week_schedule` was — but see Finding 2, which changes
the recommendation for `_fetch_injuries` specifically.

### 2. `injury_service.py` already re-implements (and improves on) `_fetch_injuries`'s exact fetch logic — this is a real supersession, not a naming issue

`nfl_mcp/injury_service.py`'s `InjuryAggregator.fetch_espn_injuries()` /
`_fetch_team_espn_injuries()` (lines 243–381) hit the **identical** ESPN
endpoint and walk the **identical** shape:

```
GET https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/{team}/injuries?limit=50&page={page}
```

(`nfl_mcp/injury_service.py:311`), paginate the same `pageCount`/`items[].$ref`
list, and follow each `$ref` to an injury-detail object (`_fetch_espn_injury_detail`,
lines 383–487) to extract `athlete`, `status`, `type`, `shortComment`/
`longComment`, and `date` — the same fields `_fetch_injuries` extracts by hand
(`nfl_mcp/sleeper_enrichment.py:396–418`).

This is not a coincidental similarity; it is the same ESPN endpoint being
fetched and parsed by two independent, hand-written implementations in this
codebase, with `injury_service.py`'s version strictly more capable:

- **Concurrency:** `injury_service.py` fetches all teams in parallel under a
  semaphore (`MAX_CONCURRENT_TEAMS = 6`, `nfl_mcp/injury_service.py:10, 261–287`)
  and fetches injury details per team in parallel too
  (`MAX_CONCURRENT_INJURIES = 15`, lines 359–372). `_fetch_injuries` loops over
  all 32 teams and all injuries sequentially (`nfl_mcp/sleeper_enrichment.py:317,
  354`).
- **HTTP-level caching:** `injury_service.py` sends `If-None-Match`/
  `If-Modified-Since` conditional-request headers and honors ESPN's `304 Not
  Modified` (lines 314–335). `_fetch_injuries` has no conditional-request
  logic at all.
- **DB-level delta caching:** `InjuryAggregator.fetch_all_injuries()`
  (lines 510–575) only re-fetches teams whose cache is stale, combining fresh
  cached rows with newly fetched ones. `_fetch_injuries` always re-fetches all
  32 teams from scratch every prefetch cycle.
- **Confidence scoring:** `injury_service.py` grades confidence by status
  certainty — 90 for Out/IR/Doubtful, 65 for Questionable, 55 for
  Probable/Active/Limited, 50 otherwise (lines 450–463, with an inline comment
  explaining a flat 60 made `get_high_confidence_injuries(min_confidence=70)`
  always return empty). `_fetch_injuries` hard-codes `'confidence': 60` for
  every single row (`nfl_mcp/sleeper_enrichment.py:415`) — the exact bug
  `injury_service.py`'s comment says it fixed.

Both implementations also write to, and are normalized against, the **same**
database surface: `_fetch_injuries`'s output is fed by `server.py`'s prefetch
loop into `nfl_db.upsert_injuries()` (`nfl_mcp/server.py:267`), and
`injury_service.py`'s `_cache_injuries()` (lines 615–628) calls the identical
`self._db.upsert_injuries(injury_dicts)`. The two dict shapes are
field-for-field compatible with `NFLDatabase.upsert_injuries()`'s documented
schema (`nfl_mcp/database.py:1207–1224`: `player_id`, `player_name`, `team_id`,
`position`, `injury_status`, `injury_type`, `injury_description`,
`game_status`, `severity`, `confidence`, `sources`, `date_reported`) —
`_fetch_injuries`'s dict just omits `game_status` (always absent/`None`).

`injury_service.py` is not a dormant module either: it is the live
implementation behind three user-facing MCP tools in `tool_registry.py` —
`get_injury_report` (`nfl_mcp/tool_registry.py:2354`, via
`InjuryAggregator`/`get_injury_reports`), `get_high_confidence_injuries`
(line 2420), and `get_gameday_inactives` (line 2483) — all three calling
`injury_service.get_injury_reports(db=get_db(), use_cache=...)`.

**Conclusion:** `injury_service.py` genuinely supersedes `_fetch_injuries()`.
Relocating `_fetch_injuries()` as-is into the new `nfl_enrichment.py` module
(the pattern issue #33 used for `_fetch_week_schedule`) would be the wrong
call — it would permanently enshrine a second, inferior, duplicate
implementation of the same ESPN endpoint fetch in the codebase, rather than
consolidating on the one (`injury_service.py`) that three real tools already
depend on and that already fixed a documented bug (the flat-60-confidence
issue) `_fetch_injuries` still has today.

### 3. `_fetch_practice_reports` is a genuinely distinct, non-superseded concern — but its derivation logic is already duplicated a second time elsewhere in this same file

`injury_service.py` has no concept of practice status (DNP/LP/FP) anywhere —
`InjuryReport` (lines 26–56) has no such field, and no function in the module
derives one. So `_fetch_practice_reports`'s ~15-line keyword-derivation logic
(`nfl_mcp/sleeper_enrichment.py:476–487`) is not superseded by anything in
`injury_service.py`.

However, that exact derivation logic already has a **second, near-verbatim
copy** inside this same file: `_enrich_usage_and_opponent()`'s
"no cached practice status" fallback branch
(`nfl_mcp/sleeper_enrichment.py:882–899`) re-implements the identical
`'OUT'/'RESERVE'/'PUP' → DNP`, `'DOUBTFUL'/'LIMITED' → LP`,
`'QUESTIONABLE' → LP`, `'PROBABLE'/'FULL' → FP` mapping, keyword-for-keyword.
`_enrich_usage_and_opponent` was already decided (issue #32/#33) to relocate
to `nfl_enrichment.py`. Once both functions live in the same module, this is
an easy, low-risk simplification (extract one shared
`_derive_practice_status_from_injury(status: str) -> str | None` helper) —
worth flagging for whoever does the build, but not a blocker for this
ticket's module-home decision.

### 4. `server.py`'s prefetch loop imports both functions via `sleeper_tools`, not directly from `sleeper_enrichment`

The ticket's framing describes the current import as
`from .sleeper_enrichment import _fetch_injuries, _fetch_practice_reports, ...`.
That's not quite what's on disk: `server.py`'s prefetch loop actually imports
from `sleeper_tools` (`nfl_mcp/server.py:113–121`):

```python
from .sleeper_tools import (
    ADVANCED_ENRICH_ENABLED,
    _fetch_injuries,
    _fetch_practice_reports,
    _fetch_week_player_snaps,
    _fetch_week_schedule,
    _fetch_weekly_usage_stats,
    get_nfl_state,
)
```

`sleeper_tools.py` itself just re-exports these names from
`sleeper_enrichment.py` for backward compatibility
(`nfl_mcp/sleeper_tools.py:36–47`, `# noqa: F401`). So today there are two
hops (`sleeper_tools` → `sleeper_enrichment`), not one. This matters for the
exact import-path update (Finding 5) once `sleeper_tools.py` is deleted per
#21's hard cutover.

### 5. Usage is prefetch-only; call sites and cadence

Both functions have exactly one real-world caller each, both inside
`server.py::_prefetch_loop`:

- `_fetch_injuries()` — called once per prefetch cycle regardless of weekday
  (`nfl_mcp/server.py:265`), result passed straight to
  `nfl_db.upsert_injuries(injuries)` (line 267).
- `_fetch_practice_reports(season, week)` — called only on Thursday/Friday/
  Saturday (`weekday in [3, 4, 5]`, `nfl_mcp/server.py:288–294`, matching the
  real-world NFL practice-report reporting cadence), result passed to
  `nfl_db.upsert_practice_status(practice_reports)` (line 296).

`grep` across `nfl_mcp/` confirms no other module calls either function.

---

## Recommendation

**Split treatment — do not relocate `_fetch_injuries` and `_fetch_practice_reports` together into `nfl_enrichment.py` as a pair.**

- **`_fetch_injuries()`: retire, don't relocate.** It is superseded by
  `injury_service.py` (Finding 2) — a strictly more capable implementation of
  the same ESPN fetch that three live MCP tools already depend on. Delete
  `_fetch_injuries()` from `sleeper_enrichment.py` and repoint its only caller
  (`server.py`'s prefetch loop) directly at
  `injury_service.get_injury_reports(db=nfl_db, use_cache=...)`. This is a
  net deletion of ~160 duplicate lines, and gets the prefetch loop
  `injury_service.py`'s superior concurrency, ETag delta-caching, and
  corrected confidence scoring for free. Since `get_injury_reports()` already
  calls `nfl_db.upsert_injuries()` internally on any cache miss
  (`nfl_mcp/injury_service.py:568–570`), the prefetch loop's own explicit
  `nfl_db.upsert_injuries(injuries)` call (`nfl_mcp/server.py:267`) becomes
  redundant and can be dropped too — left as a build-time detail, along with
  the `use_cache`/`force_refresh` choice (today's `_fetch_injuries` always
  fetches live every cycle; `use_cache=True` would additionally pick up
  `injury_service.py`'s delta-fetch behavior, which is a reasonable free
  upgrade but a behavior change worth calling out explicitly to the builder).

- **`_fetch_practice_reports(season, week)`: relocate to `nfl_enrichment.py`.**
  Unlike `_fetch_injuries`, this is not superseded by anything — it is a small
  (zero-network-call, pure-derivation) piece of genuinely distinct logic that
  belongs alongside the other ESPN-core-only leaf helpers issue #33 already
  moved there (`_fetch_week_schedule`, `_fetch_all_team_schedules`,
  `_enrich_usage_and_opponent`). It should be rewired to source its injury
  data from `injury_service.get_injury_reports()` instead of the
  now-deleted `_fetch_injuries()`. Flag (but don't block on) the Finding-3
  duplication with `_enrich_usage_and_opponent`'s inline derivation fallback,
  now that both would live in the same module — a good follow-on cleanup, not
  a prerequisite.

### Exact import-path update for `server.py`'s prefetch loop

Today (two hops, per Finding 4):

```python
from .sleeper_tools import (
    ADVANCED_ENRICH_ENABLED,
    _fetch_injuries,
    _fetch_practice_reports,
    _fetch_week_player_snaps,
    _fetch_week_schedule,
    _fetch_weekly_usage_stats,
    get_nfl_state,
)
```

After this ticket's recommendation (plus issue #33's already-decided move of
`_fetch_week_schedule`; the two Sleeper-sourced snap/usage fetchers are out of
scope for both tickets and unaffected here):

```python
from .injury_service import get_injury_reports
from .nfl_enrichment import _fetch_practice_reports, _fetch_week_schedule
```

with the `_fetch_injuries()` call site (`nfl_mcp/server.py:265`) replaced by
`injuries = await get_injury_reports(db=nfl_db, use_cache=...)`, and
`_fetch_practice_reports`'s internal `_fetch_injuries()` call
(`nfl_mcp/sleeper_enrichment.py:465`, moving to `nfl_enrichment.py`) updated to
call `get_injury_reports(db=nfl_db, use_cache=...)` the same way.

**Net effect:** this resolves the module-home question for both functions
named in #21's "Decisions so far" gap — one is deleted in favor of the
already-superior `injury_service.py`, the other relocates into
`nfl_enrichment.py` alongside its issue #33 neighbors, and both keep zero
Sleeper dependency end to end.

---

## Accepted Follow-on (not a blocker)

The DNP/LP/FP keyword-derivation logic (Finding 3) exists in two copies today
(`_fetch_practice_reports` and `_enrich_usage_and_opponent`'s fallback
branch). Both are relocating to `nfl_enrichment.py` under this ticket and
issue #33 respectively, which puts them side by side for the first time.
Extracting a shared `_derive_practice_status_from_injury()` helper at that
point is a natural, low-risk simplification — recorded here for the builder,
not resolved by this ticket.
