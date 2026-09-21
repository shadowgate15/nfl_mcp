# NFL MCP Server - AI/LLM Integration Guide

## Overview

The NFL MCP (Model Context Protocol) Server is a specialized FastMCP server designed to provide AI/LLM systems with comprehensive access to NFL and fantasy football data. This guide explains the MCP functionality, tool organization, and best practices for AI/LLM integration.

## What is MCP?

The Model Context Protocol (MCP) is a standardized protocol that allows AI/LLM systems to interact with external tools and data sources in a structured, type-safe manner. This server implements MCP to expose NFL data through a consistent interface that AI systems can understand and utilize.

## Architecture

### Server Design

The NFL MCP Server follows a simplified, maintainable architecture:

- **Single Tool Registry**: All tools defined in one centralized location (`tool_registry.py`)
- **FastMCP Framework**: Built on FastMCP 3.0+ for robust MCP protocol support
- **HTTP Transport**: Runs on HTTP transport (default port 9000)
- **Database Caching**: SQLite-based persistence for optimal performance
- **Async/Await**: Fully asynchronous for efficient concurrent operations

### Core Components

1. **Tool Registry** (`tool_registry.py`): Central registration of all MCP tools
2. **NFL Tools** (`nfl_tools.py`): ESPN API integration for NFL data
3. **ESPN Fantasy Tools** (`espn_fantasy_tools.py`): League/roster/draft/transactions via ESPN Fantasy (needs `ESPN_S2`/`ESPN_SWID` session cookies)
4. **Athlete Tools** (`athlete_tools.py`): Player data management and caching
5. **Web Tools** (`web_tools.py`): URL crawling and content extraction
6. **Waiver Tools** (`waiver_tools.py`): Advanced waiver wire analysis
7. **Trade Analyzer** (`trade_analyzer_tools.py`): Trade evaluation and analysis

## Tool Categories

The server provides **60+ MCP tools** organized into logical categories:

### 1. NFL Information Tools (9 tools)

Core NFL data access for teams, news, standings, and schedules:

- **`get_nfl_news`**: Latest NFL news from ESPN API
  - Parameters: `limit` (optional, default 50, max 50)
  - Use case: Get current NFL headlines and stories
  - Returns: Articles with headlines, descriptions, published dates

- **`get_teams`**: All NFL team information
  - Parameters: None
  - Use case: Retrieve complete list of NFL teams
  - Returns: Team data including names, abbreviations, IDs

- **`fetch_teams`**: Cache teams in local database
  - Parameters: None
  - Use case: Initialize or refresh team data cache
  - Returns: Count of teams processed and timestamp

- **`get_depth_chart`**: Team roster/depth chart
  - Parameters: `team_id` (required, e.g., "KC", "NE")
  - Use case: Analyze team composition and player depth
  - Returns: Positions with players in depth order

- **`get_team_injuries`**: Injury reports by team
  - Parameters: `team_id` (required), `limit` (optional, default 50)
  - Use case: Start/sit decisions based on injury status
  - Returns: Players with injury status and fantasy severity

- **`get_team_player_stats`**: Team player statistics
  - Parameters: `team_id` (required), `season`, `season_type`, `limit`
  - Use case: Evaluate player performance and fantasy relevance
  - Returns: Player stats with fantasy relevance indicators

- **`get_nfl_standings`**: Current NFL standings
  - Parameters: `season`, `season_type`, `group` (optional)
  - Use case: Understand playoff implications and team motivation
  - Returns: Standings with fantasy context and motivation levels

- **`get_team_schedule`**: Team schedules with fantasy context
  - Parameters: `team_id` (required), `season` (optional)
  - Use case: Strength of schedule and matchup analysis
  - Returns: Games with fantasy implications and matchup details

- **`get_league_leaders`**: NFL statistical leaders by category
  - Parameters: Category-specific parameters
  - Use case: Identify top performers across the league
  - Returns: Leader boards for various statistical categories

### 2. Coaching Intelligence Tools (4 tools)

Coaching staff information, coaching trees, and scheme analysis:

- **`get_coaching_staff`**: Get coaching staff for a specific NFL team
  - Parameters: `team_id` (required, e.g., "KC", "NE")
  - Use case: Identify head coach, coordinators, and position coaches
  - Returns: Head coach, offensive/defensive coordinators, all coaches with roles

- **`get_all_coaching_staffs`**: Get coaching staff summary for all 32 NFL teams
  - Parameters: None
  - Use case: Quick overview of all head coaches across the league
  - Returns: List of teams with head coach names and coach counts

- **`get_coaching_tree`**: Get coaching tree information for a coach
  - Parameters: `coach_name` (required, e.g., "Andy Reid", "Bill Belichick")
  - Use case: Understand coaching lineage, mentors, and proteges
  - Returns: Mentors, proteges, scheme family, what coach is known for
  - Available coaches: Andy Reid, Bill Belichick, Kyle Shanahan, Sean McVay, Mike Tomlin, Sean Payton

- **`get_scheme_classification`**: Get offensive/defensive scheme for a team
  - Parameters: `team_id` (required, e.g., "SF", "KC")
  - Use case: Analyze scheme fit for players, understand play-calling tendencies
  - Returns: Offensive scheme (West Coast, Shanahan, McVay, etc.), defensive base, scheme notes

### 3. Player/Athlete Tools (4 tools)

Player data management, sourced from ESPN's pro-player pool:

- **`fetch_athletes`**: Import all NFL players (expensive operation)
  - Parameters: None
  - Use case: Initialize comprehensive player database
  - Warning: Large data operation, use sparingly

- **`lookup_athlete`**: Find player by ID
  - Parameters: `athlete_id` (required)
  - Use case: Get specific player information
  - Returns: Detailed athlete data

- **`search_athletes`**: Search players by name
  - Parameters: `name` (required), `limit` (optional, default 10, max 100)
  - Use case: Find players with partial name matching
  - Returns: Matching athletes with positions and teams

- **`get_athletes_by_team`**: Get team roster
  - Parameters: `team_id` (required, e.g., "KC")
  - Use case: Analyze team composition
  - Returns: All athletes on specified team

### 4. Web Scraping Tools (1 tool)

Generic URL content extraction:

- **`crawl_url`**: Extract text from any webpage
  - Parameters: `url` (required), `max_length` (optional, default 10000)
  - Use case: Scrape NFL-related content from any website
  - Returns: Cleaned text content optimized for LLM consumption
  - Security: Validates URLs, removes scripts, sanitizes content

### 5. ESPN Fantasy League Tools (10 tools)

Your live ESPN Fantasy league — settings, rosters, draft, transactions. Every tool
here needs `ESPN_S2`/`ESPN_SWID` session cookies (see `docs/TECHNICAL.md`); there is
no username-based lookup like Sleeper's — ESPN requires a real login even for public
leagues.

- **`get_espn_league`**: League settings and metadata
  - Parameters: `league_id` (required), `year` (optional, defaults to current year)
  - Returns: `{league, success, error?, error_type?}`

- **`get_espn_players`**: A page of the ESPN pro-player pool for a season, unscoped to any league
  - Parameters: `year` (optional), `limit` (optional, default 25, range 1-100), `offset` (optional, default 0)
  - Returns: `{players: [...], total_players, has_more, success, error?, error_type?}`

- **`get_espn_free_agents`**: A page of free-agent/waiver-available players for a league
  - Parameters: `league_id` (required), `week` (optional), `year` (optional), `limit` (optional, default 25, range 1-100), `offset` (optional, default 0)
  - Returns: `{players: [...], total_free_agents, has_more, success, error?, error_type?}`

- **`get_espn_rosters`**: Every team's roster
  - Parameters: `league_id` (required), `week` (optional, defaults to current roster), `year` (optional), `detail` (optional, `"summary"` default or `"full"` — `"summary"` trims each roster entry to a field allowlist)
  - Returns: `{rosters: [...], success, error?, error_type?}`

- **`get_espn_standings`**: League standings
  - Parameters: `league_id` (required), `year` (optional)
  - Returns: `{standings: [...], success, error?, error_type?}`

- **`get_espn_scoreboard`**: Final scores for the league's matchups
  - Parameters: `league_id` (required), `week` (optional; omit for a capped window of recent weeks — see `total_weeks`/`has_more`), `year` (optional)
  - Returns: `{scoreboard: [...], total_weeks, has_more, success, error?, error_type?}`

- **`get_espn_matchups`**: Full box-score/lineup detail for the league's matchups
  - Parameters: `league_id` (required), `week` (optional; same capping as `get_espn_scoreboard`), `year` (optional), `detail` (optional, `"summary"` default or `"full"`)
  - Returns: `{matchups: [...], total_weeks, has_more, success, error?, error_type?}`

- **`get_espn_draft`**: Draft results
  - Parameters: `league_id` (required), `year` (optional)
  - Returns: `{draft, success, error?, error_type?}`

- **`get_espn_transactions`**: Transaction/waiver activity log
  - Parameters: `league_id` (required), `week` (optional), `types` (optional, e.g. `["WAIVER", "TRADE"]`), `year` (optional)
  - Returns: `{transactions: [...], total_transactions, success, error?, error_type?}`

- **`get_espn_player_news`**: Latest ESPN fantasy player news, optionally filtered to one player
  - Parameters: `player_id` (optional), `limit` (optional)
  - Returns: `{news: [...], total_news, success, error?}`

### 6. Draft Assistant Tools (3 tools)

VBD-tiered draft board plus a best-effort live pick recommender for a real ESPN draft.
There is no standalone Sleeper-style `draft_id` — a draft is addressed by `league_id` + `year`.

- **`get_draft_board`**: VBD-ranked draft board (offline, not league-scoped)
- **`recommend_draft_pick`**: Best pick(s) right now in a live ESPN draft
  - Parameters: `league_id` (required), `year` (optional), `my_slot` (optional — your ESPN team id, `mTeam.id`), `num_suggestions` (optional, default 5)
  - Returns: `{suggestions, top_pick, best_available_by_position, value_cliffs, positional_run, my_roster, format, source, stale, success}`
- **`simulate_draft`**: Offline mock draft from a given slot

### 7. Waiver Wire & FAAB Tools (5 tools)

Waiver intelligence and handcuff mapping, all sourced from `get_espn_transactions`/`get_espn_rosters`.

- **`get_waiver_log`**: Waiver transactions with de-duplication
  - Parameters: `league_id` (required), `week` (optional), `year` (optional), `dedupe` (optional, default true)

- **`check_re_entry_status`**: Players dropped then re-added
  - Parameters: `league_id` (required), `week` (optional), `year` (optional)

- **`get_waiver_wire_dashboard`**: Combined waiver log + re-entry analysis
  - Parameters: `league_id` (required), `week` (optional), `year` (optional)

- **`recommend_faab_bid`**: Recommend a FAAB bid (% of budget + absolute) for a player
  - Parameters: `league_id` (required), `player_id` (optional, preferred), `player_name` (optional fallback), `team_id` (optional, your ESPN team id for roster-need weighting)
  - Returns: `{recommendation: {bid_pct, bid_absolute, tier, range, reasoning, breakdown}, success}`
  - Note: ESPN has no trending-adds signal (unlike Sleeper), so league demand is frozen at neutral (`demand_label: "unavailable"`) rather than fabricated.

- **`get_handcuff_map`**: Map each RB starter to its handcuff + the handcuff's availability
  - Parameters: `league_id` (required), `team_id` (required, your ESPN team id)
  - Returns: `{handcuffs: [{starter, team, handcuff, handcuff_status, handcuff_player_id, match}], priority_free_agents, count, success, error?}`

### 8. Trade & Opponent Analysis Tools (2 tools)

- **`analyze_trade`**: Evaluate a proposed trade for fairness and fit
  - Parameters: `league_id` (required), `team1_id` (required, ESPN team id), `team2_id` (required), `team1_gives` (required, list of ESPN player IDs), `team2_gives` (required, list of ESPN player IDs)
  - Returns: `{recommendation, fairness_score, team1_analysis, team2_analysis, trade_details, warnings, success, error?}`
  - Example: `analyze_trade(league_id="12345", team1_id=1, team2_id=2, team1_gives=["4034", "4035"], team2_gives=["4036"])`

- **`analyze_opponent`**: Identify an opponent's roster weaknesses and exploitation opportunities
  - Parameters: `league_id` (required), `opponent_team_id` (required, ESPN team id of the opponent), `current_week` (optional)
  - Returns: `{vulnerability_score, vulnerability_level, position_assessments, starter_weaknesses, exploitation_strategies, matchup_context?, opponent_name, success, error?}`

### 9. Season Strategy Tools (1 tool)

- **`get_playoff_odds`**: Monte-Carlo playoff probability simulation for the rest of the season
  - Parameters: `league_id` (required), `current_week` (optional, defaults to current NFL week), `num_sims` (optional, default 10000), `score_sd` (optional, default 25.0), `team_id` (optional, ESPN team id — also returns your win/lose-this-week swing), `seed` (optional, for reproducibility)
  - Returns: `{odds: [{team_id, name, record, mean_ppg, playoff_pct, avg_seed}], this_week_swing?, playoff_teams, current_week, success}`

## Advanced Features

### Data Enrichment

The server provides multiple levels of data enrichment to enhance fantasy decision-making:

#### Basic Enrichment (Always Available)
- Player name resolution from IDs
- Team assignment and abbreviations
- Position information
- Basic statistics

#### Advanced Enrichment (NFL_MCP_ADVANCED_ENRICH=1)
- **Snap Percentages**: Offensive snap % for current week
  - `snap_pct`: Actual or estimated snap percentage
  - `snap_pct_source`: "cached" or "estimated"
  
- **Opponent Information**: Next opponent for defense units
  - `opponent`: Team abbreviation
  - `opponent_source`: "cached" or "fetched"

- **Practice Status**: Latest injury practice designation
  - `practice_status`: DNP, LP, FP, Full
  - `practice_status_date`: Date of practice report
  - `practice_status_age_hours`: Age of report
  - `practice_status_stale`: Boolean if older than 72h

- **Usage Metrics** (WR/RB/TE only): 3-week rolling averages
  - `targets_avg`: Average targets per game
  - `routes_avg`: Average routes run per game
  - `rz_touches_avg`: Average red zone touches per game
  - `snap_share_avg`: Average snap share percentage
  - `weeks_sample`: Number of weeks in sample (1-3)

- **Usage Trends**: Directional trend analysis
  - `targets`: Trend for targets (up/down/flat)
  - `routes`: Trend for routes (up/down/flat)
  - `snap_share`: Trend for snap percentage (up/down/flat)
  - `usage_trend_overall`: Overall trend direction

### Prefetch & Caching

Background data prefetching for optimal performance:

- **Enable**: Set `NFL_MCP_PREFETCH=1`
- **Interval**: `NFL_MCP_PREFETCH_INTERVAL` (default: 900 seconds = 15 min)
- **Athletes refresh**: `NFL_MCP_PREFETCH_ATHLETES` (default: on) every `NFL_MCP_PREFETCH_ATHLETES_INTERVAL` (default: 86400 seconds = daily)

The prefetch system automatically:
1. Determines the current NFL week via ESPN-core's scoreboard
2. Fetches team schedules (caches opponent data)
3. Fetches injury reports and (Thu-Sat) practice-status reports
4. Refreshes the athletes cache (player names/teams/positions) at startup and daily
5. Refreshes on configured intervals

Note: player-snap and usage-stat prefetching (Sleeper-sourced) were dropped in the
Sleeper-to-ESPN cutover (issue #52) with no ESPN replacement decided yet (ADR 0008);
`snap_pct`/`usage_last_3_weeks` enrichment still works from cached/estimated data.

### Robustness & Resilience

Many endpoints implement retry logic with snapshot fallback:

- **Retry Strategy**: Multiple attempts with exponential backoff
- **Snapshot Fallback**: Returns stale but usable data on failure
- **Metadata Fields**:
  - `retries_used`: Number of retry attempts
  - `stale`: Boolean indicating stale data
  - `failure_reason`: Last error code/category
  - `snapshot_fetched_at`: Timestamp of cached data
  - `snapshot_age_seconds`: Age of snapshot

Example response with snapshot:
```json
{
  "success": false,
  "stale": true,
  "retries_used": 3,
  "failure_reason": "timeout",
  "snapshot_fetched_at": "2025-09-13T11:22:33Z",
  "snapshot_age_seconds": 642,
  "data": { ... }
}
```

## Configuration

### Environment Variables

The server supports extensive configuration via environment variables:

#### Core Settings
- `NFL_MCP_TIMEOUT_TOTAL`: Total request timeout (default: 30.0 seconds)
- `NFL_MCP_TIMEOUT_CONNECT`: Connection timeout (default: 10.0 seconds)
- `NFL_MCP_NFL_NEWS_MAX`: Max news articles (default: 50)
- `NFL_MCP_SERVER_VERSION`: Server version string

#### Advanced Features
- `NFL_MCP_ADVANCED_ENRICH`: Enable advanced enrichment (0 or 1)
- `NFL_MCP_PREFETCH`: Enable background prefetch (0 or 1)
- `NFL_MCP_PREFETCH_INTERVAL`: Prefetch interval in seconds (default: 900)
- `NFL_MCP_PREFETCH_ATHLETES`: Refresh athletes cache during prefetch (0 or 1, default: 1)
- `NFL_MCP_PREFETCH_ATHLETES_INTERVAL`: Athletes refresh interval in seconds (default: 86400)

#### Logging
- `NFL_MCP_LOG_LEVEL`: Log verbosity (DEBUG, INFO, WARNING, ERROR, CRITICAL)

### Configuration Files

Alternative to environment variables, use YAML or JSON config files:

```yaml
timeout:
  total: 45.0
  connect: 15.0

limits:
  nfl_news_max: 75
  athletes_search_max: 150

rate_limits:
  default_requests_per_minute: 120

security:
  max_string_length: 2000
```

## Security Considerations

The server implements comprehensive security measures:

### Input Validation
- SQL injection pattern detection
- XSS injection pattern detection
- Command injection pattern detection
- Path traversal pattern detection
- Type checking and range validation

### Content Security
- HTML sanitization with script removal
- URL validation (HTTP/HTTPS only)
- Private network blocking
- Dangerous pattern detection

### Request Safety
- Configurable timeouts (30s total, 10s connect)
- Proper User-Agent headers
- No arbitrary code execution
- Parameterized SQL queries

### Rate Limiting
- Per-endpoint rate limiting
- Configurable limits and time windows
- In-memory storage (Redis recommended for production)

## Best Practices for AI/LLM Integration

### Tool Selection Guidelines

1. **No separate "current week" call needed**: league-scoped tools (`get_espn_scoreboard`, `get_espn_matchups`, `get_playoff_odds`, `recommend_draft_pick`, waiver tools, …) resolve the current NFL/scoring period internally when `week`/`year` is omitted — there's no standalone state tool to call first.
2. **Cache Awareness**: `fetch_teams` and `fetch_athletes` are expensive; call once
3. **Enrichment Trade-offs**: Advanced enrichment provides better insights but uses more resources
4. **Waiver Intelligence**: Combine `get_waiver_log` and `check_re_entry_status` for comprehensive analysis

### Performance Optimization

1. **Prefetch**: Enable prefetch for frequently accessed data
2. **Caching**: Leverage database caching for repeat queries
3. **Batch Operations**: Use aggregator tools to reduce API calls
4. **Limits**: Set appropriate limits to balance completeness and speed
5. **Timeouts**: Adjust timeouts based on network conditions

### Error Handling

1. **Check `success` field**: Always verify operation success
2. **Handle stale data**: Check `stale` flag for degraded responses
3. **Retry logic**: Server handles retries automatically
4. **Snapshot awareness**: Use snapshot data when fresh data unavailable
5. **Error messages**: Parse `error` field for actionable information

### Workflow Patterns

#### Start/Sit Decision Workflow
1. `get_team_injuries` → Check injury status
2. `get_espn_matchups` → See weekly box-score matchup (with enrichment)
3. `get_team_schedule` → Analyze opponent difficulty
4. Evaluate based on snap%, usage trends, practice status

#### Waiver Wire Research Workflow
1. `get_espn_free_agents` → See who's available in your league
2. `get_waiver_log` → Check league-specific activity
3. `check_re_entry_status` → Identify volatile players
4. `search_athletes` → Get detailed player info
5. `get_team_player_stats` → Verify fantasy relevance

#### Trade Evaluation Workflow
1. `get_espn_rosters` → Understand team compositions
2. `get_espn_matchups` → See current matchup context
3. `analyze_trade` → Evaluate trade proposal
4. `analyze_opponent` → Check the other side's weaknesses post-trade

#### Playoff Preparation Workflow
1. `get_playoff_odds` → Monte-Carlo playoff probabilities for the rest of the season
2. `get_playoff_sos` → Strength of schedule for weeks 15-17
3. `get_waiver_wire_dashboard` → Monitor waiver opportunities
4. `get_handcuff_map` → Secure handcuffs before the stretch run

## Response Format

All tools return consistent response structures:

### Successful Response
```json
{
  "success": true,
  "data": { ... },
  "count": 10,
  "timestamp": "2025-10-29T15:00:00Z"
}
```

### Error Response
```json
{
  "success": false,
  "error": "Descriptive error message",
  "error_type": "validation_error"
}
```

### Snapshot Response (Degraded)
```json
{
  "success": false,
  "stale": true,
  "retries_used": 3,
  "failure_reason": "timeout",
  "snapshot_fetched_at": "2025-10-29T14:45:00Z",
  "snapshot_age_seconds": 900,
  "data": { ... }
}
```

## API Endpoints

### MCP Endpoints
- **MCP Protocol**: `http://localhost:9000/mcp/`
- All tools accessible via MCP client

### REST Endpoints
- **Health Check**: `GET http://localhost:9000/health`
  - Returns: `{"status": "healthy", "service": "NFL MCP Server", "version": "0.5.8"}`

## Example Usage

### Python with FastMCP Client
```python
from fastmcp import Client

async with Client("http://localhost:9000/mcp/") as client:
    # Get league matchups with enrichment (week omitted -> current scoring period)
    matchups = await client.call_tool("get_espn_matchups", {
        "league_id": "123456789"
    })

    # Analyze waiver activity
    waiver_dashboard = await client.call_tool("get_waiver_wire_dashboard", {
        "league_id": "123456789"
    })

    # Monte-Carlo playoff odds for the rest of the season
    odds = await client.call_tool("get_playoff_odds", {
        "league_id": "123456789",
        "team_id": 4
    })
```

## Version Information

- **Server Version**: 0.5.16
- **FastMCP Version**: 3.0+
- **Python Version**: 3.9+
- **Protocol**: Model Context Protocol (MCP)

## Additional Resources

- **Repository**: https://github.com/gtonic/nfl_mcp
- **FastMCP Documentation**: https://github.com/jlowin/fastmcp
- **ESPN Fantasy**: Unofficial ESPN Fantasy API integration (session-cookie auth — see `docs/TECHNICAL.md`)

## Summary

The NFL MCP Server provides AI/LLM systems with comprehensive access to NFL and fantasy football data through a standardized MCP interface. With 60+ tools, advanced data enrichment, intelligent caching, and robust error handling, it enables sophisticated fantasy football analysis and decision-making workflows.

Key strengths:
- ✅ Comprehensive tool coverage (NFL data, ESPN fantasy leagues, player stats)
- ✅ Advanced enrichment (snap %, usage trends, practice status)
- ✅ Season strategy (Monte-Carlo playoff odds, strength of schedule, handcuff mapping)
- ✅ Robust design (retry logic, snapshot fallback, caching)
- ✅ Security-first (input validation, content sanitization, rate limiting)
- ✅ LLM-optimized (consistent responses, detailed metadata, error handling)

This server transforms raw NFL data into actionable fantasy football intelligence optimized for AI/LLM consumption.
