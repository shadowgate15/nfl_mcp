# Docs, CI-contract, and test-suite cutover for the Sleeper removal

ADRs 0001-0004 stay untouched as historical record rather than being amended or superseded
outright: their technical content (CI secret storage and rotation, the cookie-pull script,
response-contract conventions, the tool inventory) remains correct after Sleeper's removal — only
their framing of ESPN Fantasy as an additive second source goes stale, and retconning that framing
isn't worth the churn. This ADR set (0005-0008) is what supersedes that framing going forward, the
same pattern issue #23 already used when it produced ADR 0005 directly.

README.md, `docs/TECHNICAL.md`, and `docs/DRAFT_DAY.md` all market the product around Sleeper-
specific language in multiple places — a "🏈 Your Sleeper league" section header and its
Sleeper-only tool list, a "your real Sleeper draft" war-room bullet, a credibility bullet naming
Sleeper as the live-league data source, a testimonial quote, and username/mock-draft-URL onboarding
steps in `DRAFT_DAY.md`/`TECHNICAL.md`. The onboarding rewrite is scoped around ESPN Fantasy's
actual auth flow — the cookie-pull script from ADR 0002 plus a `league_id` — rather than a literal
Sleeper-to-ESPN find-and-replace, since there is no ESPN equivalent to "tell us your Sleeper
username." The literal copy is deferred to the build phase; this decision fixes which sections
need rewriting and around what flow, not the final wording. `CONTEXT.md` and
`docs/agents/issue-tracker.md` were both audited for stale Sleeper mentions and needed no changes:
`CONTEXT.md`'s existing "ESPN Fantasy" glossary entry never actually invoked "second source"
framing in the first place, and the agent-facing docs had no Sleeper mentions at all.

`evals/contracts/checks.py` gets a concrete per-check disposition. `sleeper.state` (critical) is
deleted, replaced by the ESPN-core scoreboard check the schedule/state decision already specifies
(week, season type, season year, keyless). `sleeper.week_stats` (critical) is deleted with no
replacement decided at the time of this disposition — it backed the snap%/usage-stats enrichment
feature, which had no ESPN research anywhere on this map at that point, and was spun into its own
investigation rather than blocking this cutover on an open question. `sleeper.players`
(non-critical in its Sleeper form) is deleted and replaced by a new **critical** check against
`get_espn_players`, verifying the exact shape the player-identity cache ADR depends on (`id`,
`fullName`, `defaultPositionId`, `proTeamId`, `injured`/`injuryStatus`) — a deliberate escalation
from its predecessor's non-critical severity, since under a hard cutover there is no fallback
identity source left if this silently breaks. `fantasycalc.values` (critical) is edited in place
rather than replaced, swapping its assertion from `player.get("sleeperId")` to
`player.get("espnId")` to match the player-identity ADR's confirmation that FantasyCalc's payload
carries both fields. `espn_fantasy.league` (critical, pre-existing) needs no change, since its
auth coverage was already adequate.

The three `test_sleeper_*.py` files — `test_sleeper_tools.py`, `test_sleeper_field_mappings.py`,
`test_sleeper_new_endpoints.py` — are deleted outright alongside `sleeper_tools.py`/
`sleeper_strategy.py`, the modules they exclusively test. Every assertion in these files mocks
Sleeper's exact wire shapes (patched HTTP clients, raw Sleeper JSON keys like `user_id`/
`league_id`/`draft_id` asserted directly) against code that no longer exists after the cutover;
none of it is ESPN-shape-agnostic, so there is nothing to port. No dedicated test-rewrite ticket is
spun off for this — writing ESPN-targeting replacement tests is left to the build phase, consistent
with how the module-rewiring tickets already left test-writing for their own rewired modules
(`draft_tools`, `waiver_tools`, `handcuff_tools`, `streaming_tools`, `opponent_analysis_tools`,
`playoff_tools`, `sos_tools`, `weather_tools`) unticketed; deferring these three files' tests the
same way is not a unique gap, just the same "tests come later" handoff the whole cutover already
accepted. Separately and out of this decision's scope, `test_practice_status.py`,
`test_practice_status_integration.py`, `test_usage_integration.py`, `test_opponent_enrichment.py`,
and `test_usage_trend.py` don't match the `test_sleeper_*.py` glob but import
`_enrich_usage_and_opponent`/`_calculate_usage_trend` via `sleeper_tools.py`'s re-export — these
need only a mechanical import-path fixup once that logic's relocation into `nfl_enrichment.py`
happens, not a design decision of their own.

## Considered Options

- **Amend or supersede ADRs 0001-0004 outright** to remove their "second source" framing.
  Rejected: their technical content stays accurate after the cutover, and retconning historical
  decisions to match a framing that was true when written adds churn without adding information —
  a new ADR set superseding the framing is clearer than editing history.
- **Keep `sleeper.players`' replacement check at non-critical severity**, matching its Sleeper-era
  predecessor. Rejected: under the hard cutover there is no remaining fallback player-identity
  source, so a silent break here has no safety net the way it did when Sleeper was still available
  as a backup.
- **Rewrite `test_sleeper_*.py` to target the ESPN-equivalent modules** instead of deleting them.
  Rejected: none of their assertions are ESPN-shape-agnostic (Sleeper's keyless auth, endpoints,
  and JSON shapes bear no resemblance to ESPN's cookie-gated ones), so there was nothing salvageable
  to port rather than write fresh.
- **Spin off a dedicated test-rewrite ticket** for the deleted Sleeper test files. Rejected for
  consistency: every module-rewiring ticket in this cutover already left its own replacement tests
  unticketed for the build phase, and treating these three files differently would be an
  inconsistent carve-out rather than a substantively different situation.
