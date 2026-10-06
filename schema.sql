PRAGMA foreign_keys = ON;

-- A run ledger makes partial loads observable instead of silently presenting them
-- as complete. A run can finish as "completed_with_warnings" when optional API
-- resources (for example, a single box score) remain unavailable after retries.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'running', 'completed', 'completed_with_warnings', 'failed'
    )),
    base_url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_fetch_log (
    fetch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES pipeline_runs(run_id),
    endpoint TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    http_status INTEGER,
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    record_count INTEGER,
    error_message TEXT
);

-- team_id is an internal surrogate key. nba_team_id is the canonical identifier
-- from /v2/teams; short IDs used by v1 resources live in team_source_ids.
CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY AUTOINCREMENT,
    nba_team_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    abbreviation TEXT NOT NULL UNIQUE,
    conference TEXT,
    division TEXT
);

CREATE TABLE IF NOT EXISTS team_source_ids (
    source_name TEXT NOT NULL,
    source_team_id TEXT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES teams(team_id),
    PRIMARY KEY (source_name, source_team_id)
);

-- Players also use an internal key because roster-only and boxscore-only people
-- do not always have a canonical /v1/players identifier.
CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY AUTOINCREMENT,
    nba_player_id TEXT UNIQUE,
    roster_pid TEXT UNIQUE,
    first_name TEXT,
    last_name TEXT,
    full_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    birth_date TEXT,
    birth_year INTEGER CHECK (birth_year BETWEEN 1900 AND 2100),
    height_text TEXT,
    height_inches INTEGER,
    weight_lbs INTEGER,
    primary_position TEXT,
    is_placeholder INTEGER NOT NULL DEFAULT 0 CHECK (is_placeholder IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_players_normalized_name
    ON players(normalized_name);

CREATE TABLE IF NOT EXISTS roster_memberships (
    team_id INTEGER NOT NULL REFERENCES teams(team_id),
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    as_of_date TEXT NOT NULL,
    jersey_number INTEGER,
    position TEXT,
    roster_status TEXT,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    PRIMARY KEY (team_id, player_id, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_roster_current_team
    ON roster_memberships(team_id, is_current);

CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    season TEXT NOT NULL,
    game_date TEXT NOT NULL,
    game_datetime_utc TEXT,
    home_team_id INTEGER NOT NULL REFERENCES teams(team_id),
    away_team_id INTEGER NOT NULL REFERENCES teams(team_id),
    home_score INTEGER,
    away_score INTEGER,
    status TEXT NOT NULL,
    CHECK (home_team_id <> away_team_id)
);

CREATE INDEX IF NOT EXISTS idx_games_date ON games(game_date DESC);
CREATE INDEX IF NOT EXISTS idx_games_season ON games(season);

-- This fact table is derived from games. It removes home/away CASE expressions
-- from team analytics and makes season PPG a simple AVG(points) GROUP BY query.
CREATE TABLE IF NOT EXISTS team_game_stats (
    game_id TEXT NOT NULL REFERENCES games(game_id),
    team_id INTEGER NOT NULL REFERENCES teams(team_id),
    opponent_team_id INTEGER NOT NULL REFERENCES teams(team_id),
    is_home INTEGER NOT NULL CHECK (is_home IN (0, 1)),
    points INTEGER,
    opponent_points INTEGER,
    PRIMARY KEY (game_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_team_game_stats_team
    ON team_game_stats(team_id, game_id);

CREATE TABLE IF NOT EXISTS player_game_stats (
    game_id TEXT NOT NULL REFERENCES games(game_id),
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    team_id INTEGER NOT NULL REFERENCES teams(team_id),
    minutes_seconds INTEGER,
    points INTEGER NOT NULL,
    rebounds INTEGER NOT NULL,
    assists INTEGER NOT NULL,
    steals INTEGER NOT NULL,
    blocks INTEGER NOT NULL,
    turnovers INTEGER NOT NULL,
    field_goals_made INTEGER NOT NULL,
    field_goals_attempted INTEGER NOT NULL,
    field_goal_pct REAL,
    three_pointers_made INTEGER NOT NULL,
    three_pointers_attempted INTEGER NOT NULL,
    three_point_pct REAL,
    free_throws_made INTEGER NOT NULL,
    free_throws_attempted INTEGER NOT NULL,
    free_throw_pct REAL,
    PRIMARY KEY (game_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_player_game_stats_player_game
    ON player_game_stats(player_id, game_id);
CREATE INDEX IF NOT EXISTS idx_player_game_stats_points
    ON player_game_stats(player_id, points);

CREATE TABLE IF NOT EXISTS injury_reports (
    injury_report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_player_id TEXT NOT NULL,
    player_id INTEGER REFERENCES players(player_id),
    team_id INTEGER REFERENCES teams(team_id),
    injury_type TEXT NOT NULL,
    status TEXT NOT NULL,
    date_reported TEXT NOT NULL,
    expected_return_date TEXT,
    expected_return_text TEXT,
    UNIQUE (source_player_id, date_reported, injury_type, status)
);

CREATE INDEX IF NOT EXISTS idx_injury_player_date
    ON injury_reports(player_id, date_reported);

CREATE TABLE IF NOT EXISTS standings_snapshots (
    team_id INTEGER NOT NULL REFERENCES teams(team_id),
    snapshot_date TEXT NOT NULL,
    wins INTEGER NOT NULL,
    losses INTEGER NOT NULL,
    win_pct REAL NOT NULL,
    games_back REAL NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY (team_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS legacy_player_season_stats (
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    team_id INTEGER REFERENCES teams(team_id),
    season TEXT NOT NULL,
    games_played INTEGER NOT NULL,
    points_per_game REAL NOT NULL,
    rebounds_per_game REAL NOT NULL,
    assists_per_game REAL NOT NULL,
    PRIMARY KEY (player_id, season)
);

-- Both rejected records and recoverable anomalies are retained here. raw_json is
-- deliberately local to SQLite; logs avoid printing potentially large payloads.
CREATE TABLE IF NOT EXISTS data_quality_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES pipeline_runs(run_id),
    endpoint TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    source_key TEXT,
    issue_code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('warning', 'error')),
    details TEXT NOT NULL,
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_quality_run_severity
    ON data_quality_issues(run_id, severity);
