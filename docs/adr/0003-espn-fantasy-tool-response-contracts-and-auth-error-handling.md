# ESPN fantasy tool response contracts and auth-error handling

`espn_fantasy_tools.py` returns plain dicts via `create_success_response`/`create_error_response`, matching every existing tool (`sleeper_tools.py`, `cbs_fantasy_tools.py`) rather than introducing `pydantic.BaseModel` response models, even though the originating ticket's own framing proposed Pydantic. `pydantic.BaseModel` appears nowhere in this repo's tool layer today (only in `config_manager.py`, for unrelated config parsing), and `response_validation.py` — despite its name suggesting a Pydantic-adjacent convention — is a hand-rolled `ValidationResult` shape-checker used only by `sleeper_enrichment.py`. Matching the repo's actual, repeated convention outweighs the ticket's original (mistaken) premise. Tools that want ingest-time shape checking follow `sleeper_enrichment.py`'s pattern of `ValidationResult`-style checks, not a new validation mechanism.

Two raw-response inconsistencies surfaced by the endpoint-catalog research (`docs/ESPN_FANTASY_ENDPOINT_CATALOG.md`) are normalized away at the tool layer rather than passed through: `/players` returns a bare array while every league-scoped players view wraps in `{"players": [...]}`; pre-2018 `leagueHistory` responses are array-wrapped (`[{...}]`) vs 2018+ object-wrapped (`{...}`), same inner schema. Every ESPN fantasy tool presents one consistent envelope shape regardless of which raw form ESPN sent — leaking that inconsistency to callers would be surprising and contrary to every other tool's behavior.

This ADR specifies the pattern plus representative worked examples (a normally-wrapped endpoint, the bare-array/wrapped case, the pre/post-2018 case), not the full tool inventory — the map defers exact tool naming/count to a later step, and this ADR hands off the build rather than doing it.

## Auth-error handling

A new `nfl_mcp/espn_errors.py` module holds `classify_espn_auth_error(response: httpx.Response) -> EspnAuthErrorClassification` (a plain dataclass — `category` + `message`, no dependency on `errors.py`'s response-envelope convention) so it can be called directly by both the tool decorator and `evals/contracts/checks.py`, per ADR 0001's requirement that the CI check reuse the tool-level detector rather than run a second one.

A single decorator, `@handle_espn_auth_errors`, does both auth-failure jobs for a tool: before calling the wrapped function, it checks `ESPN_S2`/`ESPN_SWID` are set and short-circuits with a distinct "credentials not configured" error if not (ESPN's 401 body is byte-identical whether zero cookies or wrong cookies were sent, so only the client can tell these apart); after the call, it catches `httpx.HTTPStatusError` and classifies 401/403 via `classify_espn_auth_error`, re-raising anything else. It stacks inside `@handle_http_errors` (closer to the function) so the outer decorator's generic handling still covers every other failure mode:

```python
@handle_http_errors(...)
@handle_espn_auth_errors
async def get_espn_league(...): ...
```

**Refines ADR 0001's message wording** (not its severity, secret storage, or rotation decisions): the endpoint-catalog research found 403 is not a reliable expired-cookie signal — observed on both a public league with zero cookies and a private league with confirmed-correct credentials, and no true 403 body was ever captured live. `classify_espn_auth_error` gives 401 the confident "your ESPN cookies expired, rerun the cookie-pull script" message (justified — the `AUTH_LEAGUE_NOT_VISIBLE` body is live-verified), but hedges 403's wording to also name a wrong league ID or a transient ESPN block as possible causes. ADR 0001's literal message text is superseded by this hedged version; its critical-severity and single-shared-detector decisions stand unchanged.

## Considered Options

- **Pydantic `BaseModel` response models**, as originally proposed. Rejected: no other tool in the repo uses Pydantic for responses; introducing it here would be a one-off convention future tools would have to either follow inconsistently or ignore.
- **Two separate mechanisms** (an inline credential pre-flight helper, called like `param_validator.py`'s `validate_params`, plus a separate post-response classification decorator). Rejected: one decorator per ESPN tool is simpler to apply consistently than remembering to wire up two.
- **Identical 401/403 message text**, exactly as ADR 0001 states. Rejected: a maintainer staring at a red CI build deserves the accurate caveat that 403 is ambiguous; ADR 0001's actual commitment was critical severity and a single shared detector, not the literal string.
