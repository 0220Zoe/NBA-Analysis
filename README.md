# NBA coaching analytics pipeline

This project pulls data from the provided NBA API, cleans it up, and loads it
into a SQLite database. I kept the pipeline fairly simple and used audit tables
for records that could not be matched safely instead of dropping them or
guessing.

## Design

- **Canonical entities:** `teams` and `players` use internal surrogate keys
  because vendor identifiers are incompatible or absent across resources.
  Canonical NBA IDs remain unique attributes.
- **Identifier reconciliation:** `team_source_ids` records short v1 IDs. Teams
  resolve from exact IDs or exact normalized aliases. Roster people resolve from
  normalized full name plus birth year. There is deliberately no fuzzy match.
- **Facts:** `player_game_stats` has one row per player-game.
  `team_game_stats` has one row per team-game and is derived from authoritative
  game scores, making team PPG direct and indexable.
- **History:** games, injury reports, standings snapshots, and roster snapshots
  retain dates. Prior roster snapshots remain but only the latest successful
  response has `is_current = 1`.
- **Partial temporal values:** roster slug birth years are stored separately from
  exact birth dates, and non-date injury return text is preserved separately from
  a parsed return date. The loader does not invent January 1 dates.
- **Auditability:** `pipeline_runs` distinguishes complete, partial, and failed
  loads. `api_fetch_log` records resource outcomes. `data_quality_issues` retains
  rejected and recoverable anomalies, including the source JSON.
- **Legacy data:** `/legacy/player-stats` is retained separately for comparison,
  not used as the source of per-game analytics because it is deprecated and has
  no stable player ID.

Foreign keys and check constraints protect referential/domain integrity. Indexes
cover player/date, team/date, season, current-roster, scoring, and injury access
patterns. DDL is available separately in `schema.sql`.

## Target queries

Runnable templates for all five requested analyses are in
`queries/validation.sql`:

1. Player games with at least 30 points
2. A player's averages over their last 10 appearances
3. A team's current roster and positions
4. Highest team points per game by season
5. Average player points before versus after an injury report

The injury comparison excludes games on the report date because the vendor gives
only a date—not a report time—and causal impact cannot be inferred from this
observational dataset. Results should be described as association, especially
with the assessment's small and partial box-score sample.

## Project layout

```text
.
├── README.md                  Design overview and project structure
├── DATA_QUALITY.md            Data issues and vendor recommendations
├── run_pipeline.py            Command-line entry point
├── schema.sql                 SQLite tables, constraints, and indexes
├── queries/
│   └── validation.sql         Example analytical queries
├── src/
│   ├── __init__.py
│   ├── api_client.py          API authentication, requests, and retries
│   ├── cleaning.py            Data normalization helpers
│   ├── database.py            Database setup and audit logging
│   └── pipeline.py            Data reconciliation and loading
└── tests/
    ├── test_cleaning.py
    └── test_pipeline.py
```

## Known limitations

The source dataset is intentionally small, box scores appear partial, and some
box-score endpoints fail. Therefore, "last 10" may contain fewer than 10 loaded
appearances, and injury before/after groups can be empty. The schema and queries
support complete future data without redesign. See `DATA_QUALITY.md` for details.
