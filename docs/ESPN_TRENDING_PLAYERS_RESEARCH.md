# ESPN Trending-Players Substitute Research

## Overview

This document investigates GitHub issue #25 (part of the wayfinder map, issue #21, for a hard
cutover from Sleeper to ESPN Fantasy as this repo's sole fantasy data source): `faab_tools.py` and
`trade_analyzer_tools.py` both call `sleeper_tools.get_trending_players`, a cross-league
adds/drops signal computed server-side by Sleeper. This is a **pure investigation** — it gathers
facts and scopes effort. It does not decide whether to accept the gap, drop the feature, or build
a substitute; that decision is deferred to a follow-on ticket (see [Handoff](#handoff)).

**Date:** September 8, 2026

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [What Sleeper's Trending Endpoint Provides](#what-sleepers-trending-endpoint-provides)
3. [How the Two Consumers Use It](#how-the-two-consumers-use-it)
4. [Does ESPN Expose an Equivalent?](#does-espn-expose-an-equivalent)
5. [What a Substitute Would Require](#what-a-substitute-would-require)
6. [Effort Estimate](#effort-estimate)
7. [Handoff](#handoff)

---

## Executive Summary

**Key findings:**

- Sleeper's `GET /v1/players/nfl/trending/{trend_type}` endpoint is a **cross-league, velocity**
  signal: each returned player carries a server-computed `count` field (platform-wide adds/drops
  in the lookback window). ESPN has no equivalent — nothing in its documented surface aggregates
  activity across leagues at all.
- ESPN's closest adjacent data is the `ownership: {percentOwned, percentStarted, ...}` field on
  its `Player` object (`docs/ESPN_FANTASY_ENDPOINT_CATALOG.md:127`), returned by both the unscoped
  pro-player-pool endpoint (§7b, implemented as `get_espn_players`) and the league-scoped
  `kona_player_info` free-agent view (§7a, implemented as `get_espn_free_agents`). These are
  **point-in-time ownership snapshots**, not velocity/change-over-time metrics — there is no
  documented `percentOwnedChange`, `trend`, or delta field anywhere in the catalog.
- ESPN's transaction data (`get_espn_transactions`, `mTransactions2` view, catalog §6) is strictly
  **per-league** — it takes a single `league_id` and returns that league's own ADD/DROP/TRADE/
  WAIVER events, with no cross-league aggregation.
- **Verdict: no direct ESPN equivalent exists.** A substitute could only be approximated by
  polling `percentOwned` over time and diffing snapshots — this is genuinely new infrastructure
  (poller, storage, diff/rank logic), not a field mapping.
- The two consumers would **not** degrade equally: `faab_tools.py`'s demand multiplier is a real
  signal loss, while `trade_analyzer_tools.py`'s `is_trending` flag is explicitly informational
  and would degrade gracefully.

---

## What Sleeper's Trending Endpoint Provides

`nfl_mcp/sleeper_tools.py:685-861` implements `get_trending_players(nfl_db, trend_type,
lookback_hours, limit)`. It calls Sleeper's own trending endpoint directly:

```python
url = f"https://api.sleeper.app/v1/players/nfl/trending/{trend_type}?lookback_hours={lookback_hours}&limit={limit}"
```
(`nfl_mcp/sleeper_tools.py:759`)

- `trend_type` is constrained to `"add"` or `"drop"` (`nfl_mcp/sleeper_tools.py:712`).
- `lookback_hours` and `limit` are validated against `LIMITS["trending_lookback_min"/"_max"]` and
  `LIMITS["trending_limit_min"/"_max"]` (`nfl_mcp/sleeper_tools.py:713-714`).
- Each raw item returned by Sleeper carries a `count` field, extracted and passed straight
  through: `count = item.get("count")  # Sleeper trending provides count`
  (`nfl_mcp/sleeper_tools.py:803`). This `count` is **not computed locally** — Sleeper's server
  returns it directly. The docstring's own field description confirms this is the load-bearing
  value (`nfl_mcp/sleeper_tools.py:699-706`).
- The tool then enriches each trending player with local athlete data (name, position, team,
  injury/practice status, usage) from `nfl_db`, but the trending signal itself — which players are
  even in the list, and their order — comes entirely from Sleeper's platform-wide velocity
  computation, not from anything this repo computes.

This is a genuinely cross-league, aggregate signal: Sleeper computes it once, across its entire
platform (all leagues, all users), and every caller gets the same list for a given
`trend_type`/`lookback_hours`. It is not scoped to any single league or roster.

---

## How the Two Consumers Use It

### `faab_tools.py` — real signal loss

`nfl_mcp/faab_tools.py:136` calls `get_trending_players(db, "add", 48, 100)` inside the FAAB bid
recommendation logic. The surrounding code (`nfl_mcp/faab_tools.py:132-149`) uses the result to
compute a **demand multiplier**:

```python
# --- Demand (how contested is he) ---
demand_mult = 1.0
demand_label = "low"
try:
    trend = await get_trending_players(db, "add", 48, 100)
    if trend.get("success"):
        order = [str(tp.get("player_id")) for tp in trend.get("trending_players", [])]
        pid = str(target.get("player_id"))
        if pid in order:
            idx = order.index(pid)
            if idx < 10:
                demand_mult, demand_label = 1.30, "high"
            elif idx < 30:
                demand_mult, demand_label = 1.15, "moderate"
            else:
                demand_mult, demand_label = 1.05, "light"
except Exception as e:
    logger.debug(f"trending fetch failed: {e}")
```
(`nfl_mcp/faab_tools.py:132-149`)

The target player's **rank/index** in the trending-add list (top 10 / top 30 / in-list-at-all)
directly sets `demand_mult`, which is then multiplied into the final bid percentage:

```python
bid_pct = round(min(_MAX_BID_PCT, base_pct * demand_mult * timing_mult), 1)
```
(`nfl_mcp/faab_tools.py:167`)

Without this signal, `demand_mult` silently stays at its default `1.0` ("low" demand) for every
player — the `try/except` already swallows failures today (`nfl_mcp/faab_tools.py:148-149`), so
the code path degrades *mechanically* without crashing, but it loses real information: the FAAB
recommendation would no longer distinguish a widely-added waiver-wire riser from an obscure
same-value player. This is a genuine feature regression, not cosmetic.

### `trade_analyzer_tools.py` — cosmetic/informational only

`nfl_mcp/trade_analyzer_tools.py:346` calls `get_trending_players(nfl_db, "add", 24, 50)`. The
comment directly above the call states the intent explicitly:

```python
# Optional trending context (informational only; not used for value).
trending_ids = set()
if include_trending:
    try:
        trending_result = await get_trending_players(nfl_db, "add", 24, 50)
        if trending_result.get("success"):
            for tp in trending_result.get("trending_players", []):
                if tp.get("player_id"):
                    trending_ids.add(str(tp["player_id"]))
    except Exception as e:
        logger.warning(f"Could not fetch trending data: {e}")
```
(`nfl_mcp/trade_analyzer_tools.py:342-352`)

The resulting `trending_ids` set is used only to set a boolean flag on each traded player:

```python
player["is_trending"] = str(player_id) in trending_ids
```
(`nfl_mcp/trade_analyzer_tools.py:389`)

This flag is never read by `analyzer._evaluate_trade_fairness` (called immediately after, at
`nfl_mcp/trade_analyzer_tools.py:397-399`) or by any value/fairness calculation — the comment at
line 342 says so directly ("not used for value"), and the call site is also gated behind an
`include_trending` flag, meaning it is already optional today. Losing this data source degrades
the tool gracefully: trade fairness scoring is entirely unaffected, and the only visible change is
that `is_trending` would always be `False` (or the flag/field could simply be dropped).

---

## Does ESPN Expose an Equivalent?

**No direct equivalent exists.** Three ESPN surfaces were checked:

### 1. Ownership percentage fields (point-in-time, not velocity)

`docs/ESPN_FANTASY_ENDPOINT_CATALOG.md` was grepped for `ownership`, `percentOwned`,
`percentStarted`, `percentChange`, and `trend`. Two hits total, both in the same file:

- `docs/ESPN_FANTASY_ENDPOINT_CATALOG.md:127` — the ESPN `Player` object's documented shape
  includes `ownership: {percentOwned, percentStarted, ...}` as part of the roster-entry player
  payload (§2, roster/matchup views).
- `docs/ESPN_FANTASY_ENDPOINT_CATALOG.md:298` — an incidental mention that free-agent
  *ownership%* fields were historically unreliable pre-2019 (unrelated to a change/delta field;
  it's about data quality in old seasons, not a trending metric).

**No `percentOwnedChange`, delta, or trend field is documented anywhere in the catalog.**
Importantly, the `...` after `percentStarted` at line 127 means the catalog's live-probe research
did not fully enumerate every subfield of the `ownership` object — so the absence of a
change/delta field is **unconfirmed, not 100% certain**. It is undocumented, which is the
strongest claim this research can support without a fresh live probe against ESPN's endpoint.

This `ownership` field is returned by two already-implemented tools:

- `get_espn_players` (`nfl_mcp/espn_fantasy_tools.py:253-287`) — hits the unscoped `/players`
  pro-player-pool endpoint (catalog §7b), takes no `league_id`, needs no auth cookies. Returns the
  full ESPN pro-player pool for a season, each entry containing the `Player` object (and thus
  `ownership`) described in the catalog.
- `get_espn_free_agents` (`nfl_mcp/espn_fantasy_tools.py:302-342`) — hits the league-scoped
  `kona_player_info` view (catalog §7a), restricted server-side to `FREEAGENT`/`WAIVERS` status.
  Also returns `Player` objects with the same `ownership` shape, but scoped to one league's
  free-agent pool.

Even if `percentOwned` is a reasonably fresh number, both endpoints return **a snapshot at request
time**. Neither returns how that percentage moved over the last N hours — there is no built-in
"how fast is this rising" signal the way Sleeper's `count` field is.

### 2. Transactions (strictly per-league, no cross-league aggregation)

`get_espn_transactions` (`nfl_mcp/espn_fantasy_tools.py:595-652`) hits the `mTransactions2` view
(catalog §6, `docs/ESPN_FANTASY_ENDPOINT_CATALOG.md:237-239`). Its signature takes a single
`league_id: str` (`nfl_mcp/espn_fantasy_tools.py:596`) and returns ADD/DROP/TRADE/WAIVER events
scoped to that one league only — there is no parameter or documented ESPN endpoint that aggregates
transaction volume *across* leagues. Sleeper's trending endpoint is meaningful precisely because
it aggregates over Sleeper's entire platform; ESPN has no analog to "the whole platform" from a
single fantasy league's perspective — each league's transaction feed only reflects that league's
own roster moves.

### 3. No scheduling/polling infrastructure exists to compute a substitute

```
grep -rn -i "apscheduler|cron|scheduler|background_task|BackgroundTasks" nfl_mcp/*.py pyproject.toml
```

returned **zero results** — there is no recurring job runner, cron integration, or background task
framework anywhere in this codebase today. Any poll-and-diff approach to approximate a trend from
`percentOwned` snapshots would need this built from scratch, not just wired into something already
running.

### 4. A schema pattern exists but is unused

`nfl_mcp/database.py` defines two relevant snapshot tables:

- `roster_snapshots` (`nfl_mcp/database.py:323-340`, migration v3) — `league_id`, `payload_json`,
  `fetched_at`, indexed by `(league_id, fetched_at DESC)`.
- `transaction_snapshots` (`nfl_mcp/database.py:342-360`, migration v4) — `league_id`, `week`,
  `payload_json`, `fetched_at`, indexed by `(league_id, week, fetched_at DESC)`.

```
grep -rn "roster_snapshots|transaction_snapshots" nfl_mcp/*.py   # excluding database.py
```

returned **zero results.** Both tables are defined in the schema (as migrations) but nothing in
the codebase currently inserts into or queries them. This means the *storage shape* for
"append-only, timestamped JSON blobs keyed by league" has a working precedent to copy, but there
is no live poller, no populate path, and no read path to model a new ownership-snapshot table's
plumbing on — that part would be unbuilt from scratch, not adapted from something already running.

---

## What a Substitute Would Require

If a trending-players substitute were built anyway on top of ESPN's ownership data, it would need
all of the following net-new pieces:

1. **A new recurring poller.** Something needs to call `get_espn_players` (or
   `get_espn_free_agents` per league) on a schedule and persist `percentOwned`/`percentStarted`
   snapshots over time. No scheduler exists in this repo (see above) — this is new infrastructure,
   not a config change to something already running.
2. **A new or extended snapshot table.** The `roster_snapshots`/`transaction_snapshots` pattern
   (`league_id`/`payload_json`/`fetched_at`, `nfl_mcp/database.py:323-360`) is a reasonable shape
   to copy, but since `get_espn_players` is *unscoped* to any league, a trending-style feature
   would likely want a player-keyed (not league-keyed) snapshot table instead — a genuinely new
   schema, only structurally similar to the existing pattern.
3. **A diff-and-rank query/tool.** Even once snapshots exist, something must compute
   `percentOwned(t) - percentOwned(t - lookback)` per player and rank the result — logic that
   doesn't exist anywhere in this codebase today. Sleeper hands back a ready-made `count` for
   free, computed server-side; ESPN hands back nothing but the raw snapshot value, so *all* of the
   velocity computation would have to be built and maintained by this repo.

This is **not** a trivial field-remapping exercise (e.g., "swap `count` for `percentOwnedChange`")
— there is no such field to swap in. It is closer to standing up a small new subsystem: a poller +
a storage layer + an aggregation query, all currently absent.

---

## Effort Estimate

**Medium-to-high** — this would be net-new subsystem work (poller + storage + diff/rank logic),
not a trivial mapping onto an existing ESPN field. There is no existing scheduler to hook into, no
existing snapshot-consumption code to extend (the closest analogs, `roster_snapshots` and
`transaction_snapshots`, are themselves unused today), and no server-side "trending" computation
ESPN performs that this repo could simply forward — unlike Sleeper, where the entire feature is
one HTTP call. This is a qualitative bucket, not a time estimate; no day/hour figure here would be
well-grounded given how much of the design (snapshot cadence, storage retention, ranking window)
is still undecided.

---

## Handoff

This document is scoped as pure investigation only. It does not recommend accepting the gap,
dropping the trending-context feature, or committing to build the substitute described above. That
decision — accept / drop / build — is left to a follow-on ticket per issue #25 and the broader
Sleeper-to-ESPN wayfinder map (issue #21).
