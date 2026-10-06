# NBA Data Engineering Interview Prep

## 45-minute technical interview guide

### Project summary

This project ingests NBA data from several inconsistent API resources, cleans and reconciles the records, loads them into a relational SQLite database, and exposes analytical queries for coaches, scouts, executives, and performance staff.

The core design principle is: preserve trustworthy data and make uncertainty visible. The pipeline uses stable internal keys, source-to-canonical identifier mappings, idempotent upserts, data-quality issue records, and run/fetch audit logs.

### One-minute project pitch

> I built an idempotent NBA analytics ingestion pipeline that integrates teams, players, games, rosters, box scores, injuries, standings, and legacy statistics from inconsistent API endpoints. The most difficult part was entity resolution: source IDs differ across endpoints and some resources use names or slugs instead of canonical IDs. I resolve using the strongest available evidence, avoid fuzzy matching, retain unresolved records as flagged placeholders or audit issues, and expose player-game and team-game facts for downstream analysis. The design is intentionally honest about partial box scores and imprecise injury timestamps.

## Architecture

```text
Vendor API
   |
   v
NBAApiClient: authentication, timeout, retry, error detection
   |
   v
Fetch audit log + raw quality issues
   |
   v
Cleaning: names, dates, seasons, percentages, minutes, heights
   |
   v
Identity reconciliation: canonical IDs, source IDs, exact normalized names
   |
   v
SQLite relational model
   |
   +--> dimensions: teams, players, source ID mappings
   +--> history: roster, standings, injuries
   +--> facts: games, team-game stats, player-game stats
   +--> audit: pipeline runs, API fetches, data quality issues
   |
   v
Analytical SQL for coaching and front-office use cases
```

### Current load order

1. Teams
2. Games
3. Canonical players
4. Current roster snapshot
5. Per-game box scores
6. Injury reports
7. Standings snapshots
8. Legacy player-season statistics

The order is important because games establish mappings between short source team IDs and canonical teams before player and box-score records are loaded.

## Data model: know the grain

| Table | Grain | Purpose |
|---|---|---|
| `teams` | One row per canonical team | Team dimension |
| `players` | One row per internal player entity | Player dimension, including placeholders |
| `team_source_ids` | One source ID-to-team mapping | Cross-endpoint identity reconciliation |
| `games` | One row per game | Schedule and authoritative score |
| `team_game_stats` | One row per team per game | Simplifies team-level analysis |
| `player_game_stats` | One row per player per game | Player box-score fact |
| `roster_memberships` | One player-team membership per snapshot | Roster history |
| `injury_reports` | One source injury report event | Injury history and timing |
| `standings_snapshots` | One team per snapshot date | Historical standings |
| `pipeline_runs` | One pipeline execution | Run status and observability |
| `api_fetch_log` | One API request attempt/result | Endpoint auditability |
| `data_quality_issues` | One detected anomaly | Rejected/recoverable issue tracking |

Important keys:

- `player_game_stats(game_id, player_id)` prevents duplicate player-game facts.
- `team_game_stats(game_id, team_id)` creates one team perspective per game.
- `roster_memberships(team_id, player_id, as_of_date)` preserves roster history.
- Injury uniqueness uses source player, report date, injury type, and status.

## Strong design decisions

### 1. Conservative identity resolution

The source has incompatible team and player identifiers. The pipeline uses:

1. Canonical ID when available.
2. Explicit source-ID mapping.
3. Exact normalized name matching.
4. Birth year as an additional roster disambiguator.
5. Placeholder or unresolved audit record if the match is unsafe.

It deliberately does not use fuzzy matching. A false match can attach a stat line or injury to the wrong athlete, which is worse than an explicit missing link.

### 2. Idempotent reruns

The pipeline uses natural keys and `ON CONFLICT DO UPDATE`. Running the same snapshot twice does not duplicate games, player-game facts, roster rows, or injuries. This is verified by integration tests.

### 3. Auditability

The run ledger distinguishes `completed`, `completed_with_warnings`, and `failed`. Fetch logs record endpoint status and record counts. Quality issues preserve issue type, severity, source key, details, and sometimes raw JSON.

### 4. Honest analytical caveats

Box scores may be partial, so player averages are not necessarily full-season results. Injury reports contain dates but not report timestamps, so same-day games are excluded from before/after comparisons. The result is association, not causal impact.

## Downstream queries to explain

The repository includes five target analyses in `queries/validation.sql`.

### Player games with 30 or more points

Join `player_game_stats` to `players`, `games`, and `teams`, filter by player and points, and order by game date.

### Last 10 appearances

The query first filters to the player, orders by game date, limits to 10 rows, and only then calculates averages. This means “last 10 appearances,” not the team’s last 10 games. Always return `games_in_sample` so consumers know whether fewer than 10 rows were available.

### Current roster

Filter `roster_memberships.is_current = 1`, join to players and teams, and expose `is_placeholder` so a user knows whether the player was absent from the canonical player feed.

### Highest team points per game

`team_game_stats` avoids repeated home/away `CASE` logic. Filter final games by season, group by team, and calculate `AVG(points)`.

### Points before and after injury

Join injury reports to player-game facts and games. Exclude games on the report date because the source has no report time. Report the number of games in each group and avoid describing the difference as causal.

## What is missing relative to a production job

### Medallion architecture

The current implementation is direct API-to-curated SQLite. A production version should use:

- **Bronze:** immutable raw API payloads, fetch metadata, run ID, and source timestamps.
- **Silver:** normalized and reconciled teams, players, games, injuries, and roster records.
- **Gold:** player-game, team-game, injury-impact, roster, and executive-facing marts.

Benefits include replayability, lineage, backfills, debugging, and separation between source preservation and business logic.

### Prefect orchestration

The current code has a Python pipeline runner but no Prefect flow. A production design could use tasks such as:

- `fetch_teams`
- `fetch_games`
- `fetch_players`
- `fetch_boxscore(game_id)`
- `fetch_injuries(status)`
- `normalize_entities`
- `load_dimensions`
- `load_facts`
- `run_quality_checks`
- `publish_gold_models`

Box-score tasks are independent and can be mapped with controlled concurrency and API rate limits.

### Incremental processing

The current pipeline largely reloads full endpoints. Production improvements:

- Store per-source watermarks.
- Use API update timestamps when available.
- Reprocess a configurable lookback window for late-arriving data.
- Track failed box scores separately for targeted retries.
- Support date, season, and game-ID backfills.

### Atomic publication

The current pipeline commits at multiple stages, then marks the run status. A stronger warehouse design loads staging tables, runs validation, and publishes a run version only after required checks pass.

### Other production concerns

- Retry `Retry-After` and use exponential backoff with jitter.
- Add pagination and rate limiting.
- Batch inserts with `executemany`.
- Avoid repeated commits in large loops.
- Add schema migrations rather than relying only on `schema.sql`.
- Add freshness, completeness, null-rate, and reconciliation metrics.
- Protect the current roster snapshot from being cleared if a new response is incomplete.
- Add structured logs, alerting, lineage, and ownership documentation.

## Data-quality checks to propose

### Referential integrity

- Facts must resolve to valid games, players, and teams.
- Unresolved injuries should be counted and surfaced.

### Duplicate detection

- No duplicate player-game facts.
- No duplicate team-game facts.
- No duplicate injury events under the declared business key.

### Domain checks

- Made shots cannot exceed attempts.
- Scores and minutes cannot be negative.
- A game cannot have the same home and away team.
- Wins and losses cannot be negative.
- Dates must parse and seasons must be consecutive.

### Completeness checks

- Expected team count.
- Expected games per date or season.
- Two team rows per completed game.
- Box-score coverage by game.
- Roster count and current-snapshot freshness.

### Reconciliation checks

- Team score versus summed player points when the box score is complete.
- API record counts versus loaded record counts.
- Current standings versus expected source snapshot.

## SQL topics to practice

Be ready to discuss:

- Fact-table grain.
- Inner join versus left join.
- `WHERE` versus `HAVING`.
- Window functions and rolling averages.
- Deduplication with `ROW_NUMBER()`.
- Null behavior in averages.
- Fanout joins.
- Composite index order.
- Partitioning versus indexing.
- `EXPLAIN QUERY PLAN` and `EXPLAIN ANALYZE`.
- Cursor pagination versus offset pagination.

Example rolling average:

```sql
SELECT
    player_id,
    game_date,
    points,
    AVG(points) OVER (
        PARTITION BY player_id
        ORDER BY game_date
        ROWS BETWEEN 9 PRECEDING AND CURRENT ROW
    ) AS rolling_10_game_points
FROM player_game_stats
JOIN games USING (game_id);
```

## Python and reliability questions

Prepare concise answers for:

- How would you parallelize box-score calls safely?
- How would you handle API rate limits?
- How would you retry a single failed game?
- How would you avoid duplicate data after a retry?
- What happens if the job dies halfway through?
- How would you test malformed API records?
- How would you avoid loading a huge response into memory?
- What would you profile first?

The main performance bottleneck here is I/O, not CPU: sequential HTTP calls, repeated database commits, and one SQL operation per record. The likely improvements are controlled concurrency, caching lookups, batching writes, and stage-level transactions.

## Stakeholder questions

The technical design should serve different consumers:

- **Coaches:** recent form, workload, availability, opponent context.
- **Scouts:** player comparisons, role, position, consistency, sample size.
- **Executives:** team performance, roster changes, data freshness, confidence.
- **Salary-cap strategists:** historical roster membership and player-team timelines.
- **Medical/performance staff:** injury timing, workload, return-to-play context.

Every metric should expose freshness, sample size, data coverage, and limitations.

## 45-minute presentation plan

| Time | Topic |
|---:|---|
| 2 min | Problem, users, and one-minute project pitch |
| 5 min | End-to-end architecture and load order |
| 8 min | Schema and table grain |
| 8 min | Identifier reconciliation and data quality |
| 7 min | Idempotency, retries, and failure handling |
| 5 min | SQL queries and indexing |
| 5 min | Scaling, Prefect, and medallion production design |
| 5 min | Limitations, tradeoffs, and next steps |

## Final answer to “What would you build next?”

> I would preserve immutable raw responses in object storage, introduce explicit Bronze/Silver/Gold layers, orchestrate the flow with Prefect, add incremental watermarks and targeted backfills, parallelize box-score extraction with rate limiting, add stronger completeness and reconciliation checks, and publish only validated run versions to downstream consumers. SQLite is appropriate for this small submission, but I would move the serving layer to a managed warehouse or PostgreSQL based on concurrency, volume, and latency requirements.

## Files to reference during the interview

- `README.md`: design overview and known limitations.
- `DATA_QUALITY.md`: observed source problems and vendor recommendations.
- `schema.sql`: constraints, table grain, and indexes.
- `src/api_client.py`: retry and application-error handling.
- `src/cleaning.py`: pure normalization functions and unit-testable rules.
- `src/pipeline.py`: dependency order, entity resolution, and upserts.
- `queries/validation.sql`: downstream analytical examples.
- `tests/test_pipeline.py`: idempotency and unresolved-entity behavior.
