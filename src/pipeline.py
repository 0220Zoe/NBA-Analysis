"""End-to-end extraction, cleaning, reconciliation, and loading.

The vendor exposes incompatible identifiers across API versions. This pipeline
uses internal SQLite keys and resolves each record with the strongest available
evidence: canonical ID first, then an exact normalized label/name. It never uses
fuzzy matching because silently assigning a fact to the wrong player is worse
than retaining an explicit unmatched issue.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from .api_client import ApiError, NBAApiClient
from .cleaning import (
    CleaningError,
    birth_year_from_roster_pid,
    normalize_name,
    normalize_season,
    normalize_text,
    parse_date,
    parse_game_datetime,
    parse_height_inches,
    parse_minutes,
    parse_percentage,
    roster_name_to_full_name,
    season_for_date,
)
from .database import infer_record_count, log_fetch, log_quality_issue


class Pipeline:
    def __init__(
        self,
        client: NBAApiClient,
        connection: sqlite3.Connection,
        run_id: int,
        *,
        roster_as_of: Optional[str] = None,
    ) -> None:
        self.client = client
        self.db = connection
        self.run_id = run_id
        self.roster_as_of = roster_as_of or date.today().isoformat()
        self.warning_count = 0

    def issue(
        self,
        endpoint: str,
        entity_type: str,
        issue_code: str,
        details: str,
        *,
        source_key: Optional[str] = None,
        severity: str = "warning",
        raw_record: Optional[Any] = None,
    ) -> None:
        self.warning_count += 1
        log_quality_issue(
            self.db,
            self.run_id,
            endpoint,
            entity_type,
            issue_code,
            details,
            source_key=source_key,
            severity=severity,
            raw_record=raw_record,
        )

    def fetch(
        self,
        path: str,
        *,
        params: Optional[Dict[str, str]] = None,
        required: bool = True,
    ) -> Optional[Any]:
        endpoint = path
        if params:
            # Used for human-readable audit rows only; the client handles encoding.
            endpoint += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        try:
            response = self.client.get(path, params)
            log_fetch(
                self.db,
                self.run_id,
                response.endpoint,
                status=response.status,
                success=True,
                record_count=infer_record_count(response.payload),
            )
            return response.payload
        except ApiError as exc:
            log_fetch(
                self.db,
                self.run_id,
                endpoint,
                status=exc.status,
                success=False,
                error_message=str(exc),
            )
            self.issue(
                endpoint,
                "api_request",
                "api_fetch_failed",
                str(exc),
                severity="error" if required else "warning",
            )
            self.db.commit()
            if required:
                raise
            return None

    def run(self) -> Dict[str, int]:
        """Execute dependency-ordered loads and return final table counts."""
        teams = self.fetch("/v2/teams")
        games = self.fetch("/v1/games")
        players = self.fetch("/v1/players")
        assert teams is not None and games is not None and players is not None

        self.load_teams(teams.get("data", []))
        # Games establish mappings between the short team IDs used by v1 and the
        # canonical team IDs/names returned by v2.
        self.load_games(games.get("games", []))
        self.load_players(players.get("players", []))

        rosters = self.fetch("/rosters/current", required=False)
        if rosters:
            self.load_rosters(rosters.get("rosters", []))

        for game in games.get("games", []):
            game_id = normalize_text(game.get("game_id"))
            if not game_id:
                continue
            boxscore = self.fetch(f"/v1/games/{game_id}/boxscore", required=False)
            if boxscore:
                self.load_boxscore(boxscore)

        for status in ("out", "day-to-day", "questionable"):
            payload = self.fetch(
                "/v1/injuries", params={"status": status}, required=False
            )
            if payload:
                self.load_injuries(payload.get("injuries", []), requested_status=status)

        standings = self.fetch("/v2/standings", required=False)
        if standings:
            self.load_standings(standings.get("data", []))

        legacy = self.fetch("/legacy/player-stats", required=False)
        if legacy:
            self.load_legacy_stats(legacy)

        self.db.commit()
        return self.table_counts()

    def load_teams(self, records: Sequence[Dict[str, Any]]) -> None:
        endpoint = "/v2/teams"
        seen_ids: set[str] = set()
        for record in records:
            source_id = normalize_text(record.get("teamId"))
            abbreviation = normalize_text(record.get("abbreviation")).upper()
            if not source_id or not abbreviation:
                self.issue(
                    endpoint, "team", "missing_natural_key",
                    "Team requires teamId and abbreviation; record skipped.",
                    raw_record=record, severity="error",
                )
                continue
            if source_id in seen_ids:
                self.issue(
                    endpoint, "team", "duplicate_source_record",
                    "Duplicate teamId encountered; last record wins via upsert.",
                    source_key=source_id, raw_record=record,
                )
            seen_ids.add(source_id)
            try:
                self.db.execute(
                    """
                    INSERT INTO teams(
                        nba_team_id, name, city, abbreviation, conference, division
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(nba_team_id) DO UPDATE SET
                        name=excluded.name, city=excluded.city,
                        abbreviation=excluded.abbreviation,
                        conference=excluded.conference, division=excluded.division
                    """,
                    (
                        source_id,
                        normalize_text(record.get("teamName")),
                        normalize_text(record.get("city")),
                        abbreviation,
                        normalize_text(record.get("conference")) or None,
                        normalize_text(record.get("division")) or None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                self.issue(
                    endpoint, "team", "conflicting_team_natural_key",
                    f"Team conflicts with an existing canonical ID or abbreviation: {exc}",
                    source_key=source_id, raw_record=record, severity="error",
                )
        self.db.commit()

    def _team_lookup(self) -> Dict[str, int]:
        lookup: Dict[str, int] = {}
        for row in self.db.execute("SELECT * FROM teams"):
            labels = {
                row["nba_team_id"], row["abbreviation"], row["name"], row["city"],
                f"{row['city']} {row['name']}",
            }
            for label in labels:
                if label:
                    lookup[normalize_name(label)] = int(row["team_id"])

        # These are explicit source aliases observed in the legacy feed. Keeping
        # them here is auditable and safer than broad fuzzy matching.
        aliases = {
            "ny knicks": "NYK",
            "la lakers": "LAL",
        }
        for alias, abbreviation in aliases.items():
            target = self.db.execute(
                "SELECT team_id FROM teams WHERE abbreviation = ?", (abbreviation,)
            ).fetchone()
            if target:
                lookup[normalize_name(alias)] = int(target[0])
        return lookup

    def resolve_team(
        self,
        *,
        label: Any = None,
        canonical_id: Any = None,
        source_name: Optional[str] = None,
        source_id: Any = None,
    ) -> Optional[int]:
        if canonical_id is not None:
            row = self.db.execute(
                "SELECT team_id FROM teams WHERE nba_team_id = ?",
                (normalize_text(canonical_id),),
            ).fetchone()
            if row:
                return int(row[0])
        if source_name and source_id is not None:
            row = self.db.execute(
                "SELECT team_id FROM team_source_ids "
                "WHERE source_name = ? AND source_team_id = ?",
                (source_name, normalize_text(source_id)),
            ).fetchone()
            if row:
                return int(row[0])
        return self._team_lookup().get(normalize_name(label))

    def _register_team_source_id(
        self, source_name: str, source_id: Any, team_id: int, endpoint: str
    ) -> None:
        external_id = normalize_text(source_id)
        existing = self.db.execute(
            "SELECT team_id FROM team_source_ids "
            "WHERE source_name = ? AND source_team_id = ?",
            (source_name, external_id),
        ).fetchone()
        if existing and int(existing[0]) != team_id:
            self.issue(
                endpoint, "team", "conflicting_team_id_mapping",
                f"{source_name} ID {external_id} maps to multiple canonical teams; "
                "existing mapping retained.",
                source_key=external_id, severity="error",
            )
            return
        self.db.execute(
            "INSERT OR IGNORE INTO team_source_ids(source_name, source_team_id, team_id) "
            "VALUES (?, ?, ?)",
            (source_name, external_id, team_id),
        )

    def load_games(self, records: Sequence[Dict[str, Any]]) -> None:
        endpoint = "/v1/games"
        seen_game_ids: set[str] = set()
        for record in records:
            game_id = normalize_text(record.get("game_id"))
            if game_id in seen_game_ids:
                self.issue(
                    endpoint, "game", "duplicate_source_record",
                    "Duplicate game_id encountered; last valid record wins via upsert.",
                    source_key=game_id, raw_record=record,
                )
            seen_game_ids.add(game_id)
            try:
                game_date, game_datetime_utc = parse_game_datetime(record.get("game_date"))
                home_id = self.resolve_team(label=record.get("home"))
                away_id = self.resolve_team(label=record.get("away"))
                if not game_id or home_id is None or away_id is None:
                    raise CleaningError("game ID or team mapping is missing")
                self._register_team_source_id(
                    "v1_short", record.get("home_team_id"), home_id, endpoint
                )
                self._register_team_source_id(
                    "v1_short", record.get("away_team_id"), away_id, endpoint
                )
                home_score = int(record["home_score"])
                away_score = int(record["away_score"])
                self.db.execute(
                    """
                    INSERT INTO games(
                        game_id, season, game_date, game_datetime_utc,
                        home_team_id, away_team_id, home_score, away_score, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(game_id) DO UPDATE SET
                        season=excluded.season, game_date=excluded.game_date,
                        game_datetime_utc=excluded.game_datetime_utc,
                        home_team_id=excluded.home_team_id,
                        away_team_id=excluded.away_team_id,
                        home_score=excluded.home_score, away_score=excluded.away_score,
                        status=excluded.status
                    """,
                    (
                        game_id, season_for_date(game_date), game_date,
                        game_datetime_utc, home_id, away_id, home_score, away_score,
                        normalize_text(record.get("status")),
                    ),
                )
                for team_id, opponent_id, is_home, points, opponent_points in (
                    (home_id, away_id, 1, home_score, away_score),
                    (away_id, home_id, 0, away_score, home_score),
                ):
                    self.db.execute(
                        """
                        INSERT INTO team_game_stats(
                            game_id, team_id, opponent_team_id, is_home,
                            points, opponent_points
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(game_id, team_id) DO UPDATE SET
                            opponent_team_id=excluded.opponent_team_id,
                            is_home=excluded.is_home, points=excluded.points,
                            opponent_points=excluded.opponent_points
                        """,
                        (game_id, team_id, opponent_id, is_home, points, opponent_points),
                    )
            except (CleaningError, KeyError, TypeError, ValueError) as exc:
                self.issue(
                    endpoint, "game", "invalid_game_record", str(exc),
                    source_key=game_id or None, raw_record=record, severity="error",
                )
        self.db.commit()

    def load_players(self, records: Sequence[Dict[str, Any]]) -> None:
        endpoint = "/v1/players"
        for record in records:
            source_id = normalize_text(record.get("player_id"))
            try:
                if not source_id:
                    raise CleaningError("player_id is missing")
                first = normalize_text(record.get("first_name"))
                last = normalize_text(record.get("last_name"))
                full = normalize_text(f"{first} {last}")
                birth_date = parse_date(record.get("birthdate"), field_name="birthdate")
                height_text = normalize_text(record.get("height")) or None
                height_inches = parse_height_inches(height_text)
                team_id = self.resolve_team(
                    source_name="v1_short", source_id=record.get("team_id")
                )
                if team_id is None:
                    raise CleaningError(f"unknown short team ID {record.get('team_id')!r}")
                self.db.execute(
                    """
                    INSERT INTO players(
                        nba_player_id, first_name, last_name, full_name,
                        normalized_name, birth_date, birth_year, height_text,
                        height_inches, weight_lbs, primary_position, is_placeholder
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(nba_player_id) DO UPDATE SET
                        first_name=excluded.first_name, last_name=excluded.last_name,
                        full_name=excluded.full_name,
                        normalized_name=excluded.normalized_name,
                        birth_date=excluded.birth_date, birth_year=excluded.birth_year,
                        height_text=excluded.height_text,
                        height_inches=excluded.height_inches,
                        weight_lbs=excluded.weight_lbs,
                        primary_position=excluded.primary_position,
                        is_placeholder=0
                    """,
                    (
                        source_id, first, last, full, normalize_name(full), birth_date,
                        int(birth_date[:4]), height_text, height_inches, record.get("weight"),
                        normalize_text(record.get("position")) or None,
                    ),
                )
            except (CleaningError, TypeError, ValueError) as exc:
                self.issue(
                    endpoint, "player", "invalid_player_record", str(exc),
                    source_key=source_id or None, raw_record=record, severity="error",
                )
        self.db.commit()

    def _players_named(self, full_name: str) -> List[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM players WHERE normalized_name = ?",
                (normalize_name(full_name),),
            )
        )

    def load_rosters(self, records: Sequence[Dict[str, Any]]) -> None:
        endpoint = "/rosters/current"
        # A fresh successful roster response supersedes prior snapshots for the
        # definition of "current", while history remains queryable.
        self.db.execute("UPDATE roster_memberships SET is_current = 0")
        for team_record in records:
            team_id = self.resolve_team(label=team_record.get("team"))
            if team_id is None:
                self.issue(
                    endpoint, "roster", "unknown_team",
                    f"Could not resolve team {team_record.get('team')!r}.",
                    raw_record=team_record, severity="error",
                )
                continue
            for record in team_record.get("players", []):
                source_pid = normalize_text(record.get("pid"))
                full_name = roster_name_to_full_name(record.get("name"))
                candidates = self._players_named(full_name)
                birth_year = birth_year_from_roster_pid(source_pid)
                if birth_year is not None:
                    year_matches = [
                        row for row in candidates
                        if row["birth_year"] and int(row["birth_year"]) == birth_year
                    ]
                    candidates = year_matches

                if len(candidates) == 1:
                    player_id = int(candidates[0]["player_id"])
                    self.db.execute(
                        "UPDATE players SET roster_pid = COALESCE(roster_pid, ?) "
                        "WHERE player_id = ?",
                        (source_pid or None, player_id),
                    )
                elif len(candidates) == 0:
                    # Retain roster-only players as clearly marked placeholders;
                    # this is essential for a complete current-roster query.
                    row = self.db.execute(
                        "SELECT player_id FROM players WHERE roster_pid = ?",
                        (source_pid,),
                    ).fetchone()
                    if row:
                        player_id = int(row[0])
                    else:
                        name_parts = normalize_text(record.get("name")).split(",", 1)
                        last = normalize_text(name_parts[0]) if name_parts else ""
                        first = normalize_text(name_parts[1]) if len(name_parts) == 2 else ""
                        cursor = self.db.execute(
                            """
                            INSERT INTO players(
                                roster_pid, first_name, last_name, full_name,
                                normalized_name, birth_year, primary_position,
                                is_placeholder
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                            """,
                            (
                                source_pid or None, first or None, last or None,
                                full_name, normalize_name(full_name), birth_year,
                                normalize_text(record.get("pos")) or None,
                            ),
                        )
                        player_id = int(cursor.lastrowid)
                    self.issue(
                        endpoint, "player", "roster_only_player",
                        "No canonical player record matched; a flagged placeholder "
                        "was created so the current roster remains complete.",
                        source_key=source_pid or None, raw_record=record,
                    )
                else:
                    self.issue(
                        endpoint, "player", "ambiguous_roster_player",
                        f"{len(candidates)} exact-name candidates; roster row skipped.",
                        source_key=source_pid or None, raw_record=record, severity="error",
                    )
                    continue

                self.db.execute(
                    """
                    INSERT INTO roster_memberships(
                        team_id, player_id, as_of_date, jersey_number,
                        position, roster_status, is_current
                    ) VALUES (?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(team_id, player_id, as_of_date) DO UPDATE SET
                        jersey_number=excluded.jersey_number,
                        position=excluded.position,
                        roster_status=excluded.roster_status,
                        is_current=1
                    """,
                    (
                        team_id, player_id, self.roster_as_of, record.get("number"),
                        normalize_text(record.get("pos")) or None,
                        normalize_text(record.get("status")) or None,
                    ),
                )
        self.db.commit()

    def _resolve_or_create_boxscore_player(
        self, record: Dict[str, Any], endpoint: str
    ) -> int:
        source_id = normalize_text(record.get("personId"))
        row = self.db.execute(
            "SELECT player_id FROM players WHERE nba_player_id = ?", (source_id,)
        ).fetchone()
        if row:
            return int(row[0])

        abbreviated_name = normalize_text(record.get("name")) or f"Unknown {source_id}"
        self.db.execute(
            """
            INSERT INTO players(
                nba_player_id, full_name, normalized_name, is_placeholder
            ) VALUES (?, ?, ?, 1)
            ON CONFLICT(nba_player_id) DO UPDATE SET
                full_name=excluded.full_name,
                normalized_name=excluded.normalized_name
            """,
            (source_id, abbreviated_name, normalize_name(abbreviated_name)),
        )
        row = self.db.execute(
            "SELECT player_id FROM players WHERE nba_player_id = ?", (source_id,)
        ).fetchone()
        assert row is not None
        player_id = int(row[0])
        self.issue(
            endpoint, "player", "boxscore_only_player",
            "Box-score personId was absent from /v1/players; a flagged placeholder "
            "was created so the stat line is not discarded.",
            source_key=source_id, raw_record=record,
        )
        return player_id

    def load_boxscore(self, record: Dict[str, Any]) -> None:
        game_id = normalize_text(record.get("gameId"))
        endpoint = f"/v1/games/{game_id}/boxscore"
        game = self.db.execute(
            "SELECT * FROM games WHERE game_id = ?", (game_id,)
        ).fetchone()
        if not game:
            self.issue(
                endpoint, "boxscore", "unknown_game",
                "Box score does not match a loaded game; payload skipped.",
                source_key=game_id, raw_record=record, severity="error",
            )
            return
        try:
            box_date = parse_date(record.get("gameDate"), field_name="boxscore gameDate")
            if box_date != game["game_date"]:
                self.issue(
                    endpoint, "boxscore", "game_date_mismatch",
                    f"Box score date {box_date} differs from game date {game['game_date']}; "
                    "the authoritative game date was retained.",
                    source_key=game_id,
                )
        except CleaningError as exc:
            self.issue(
                endpoint, "boxscore", "invalid_boxscore_date", str(exc),
                source_key=game_id, raw_record=record,
            )

        seen_player_ids: set[str] = set()
        for side_key in ("homeTeam", "awayTeam"):
            team_record = record.get(side_key, {})
            team_id = self.resolve_team(
                label=team_record.get("abbreviation"),
                source_name="v1_short",
                source_id=team_record.get("teamId"),
            )
            if team_id is None:
                self.issue(
                    endpoint, "team", "unknown_boxscore_team",
                    f"Could not resolve {side_key}; player rows skipped.",
                    source_key=normalize_text(team_record.get("teamId")),
                    raw_record=team_record, severity="error",
                )
                continue
            for player in team_record.get("players", []):
                source_player_id = normalize_text(player.get("personId"))
                if source_player_id in seen_player_ids:
                    self.issue(
                        endpoint, "player_game_stat", "duplicate_stat_line",
                        "Duplicate personId in one box score; last record wins via upsert.",
                        source_key=f"{game_id}:{source_player_id}", raw_record=player,
                    )
                seen_player_ids.add(source_player_id)
                try:
                    player_id = self._resolve_or_create_boxscore_player(
                        player, endpoint
                    )
                    values = (
                        game_id, player_id, team_id,
                        parse_minutes(player.get("minutes")),
                        int(player["points"]), int(player["rebounds"]),
                        int(player["assists"]), int(player["steals"]),
                        int(player["blocks"]), int(player["turnovers"]),
                        int(player["fgm"]), int(player["fga"]),
                        parse_percentage(player.get("fg_pct")),
                        int(player["fg3m"]), int(player["fg3a"]),
                        parse_percentage(player.get("fg3_pct")),
                        int(player["ftm"]), int(player["fta"]),
                        parse_percentage(player.get("ft_pct")),
                    )
                    self.db.execute(
                        """
                        INSERT INTO player_game_stats VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        ON CONFLICT(game_id, player_id) DO UPDATE SET
                            team_id=excluded.team_id,
                            minutes_seconds=excluded.minutes_seconds,
                            points=excluded.points, rebounds=excluded.rebounds,
                            assists=excluded.assists, steals=excluded.steals,
                            blocks=excluded.blocks, turnovers=excluded.turnovers,
                            field_goals_made=excluded.field_goals_made,
                            field_goals_attempted=excluded.field_goals_attempted,
                            field_goal_pct=excluded.field_goal_pct,
                            three_pointers_made=excluded.three_pointers_made,
                            three_pointers_attempted=excluded.three_pointers_attempted,
                            three_point_pct=excluded.three_point_pct,
                            free_throws_made=excluded.free_throws_made,
                            free_throws_attempted=excluded.free_throws_attempted,
                            free_throw_pct=excluded.free_throw_pct
                        """,
                        values,
                    )
                except (CleaningError, KeyError, TypeError, ValueError) as exc:
                    self.issue(
                        endpoint, "player_game_stat", "invalid_stat_line", str(exc),
                        source_key=f"{game_id}:{source_player_id}",
                        raw_record=player, severity="error",
                    )
        self.db.commit()

    def _resolve_injury_player(
        self, source_id: str, full_name: str, endpoint: str, record: Dict[str, Any]
    ) -> Optional[int]:
        by_id = self.db.execute(
            "SELECT player_id FROM players WHERE nba_player_id = ? AND is_placeholder = 0",
            (source_id,),
        ).fetchone()
        if by_id:
            return int(by_id[0])
        candidates = [row for row in self._players_named(full_name) if not row["is_placeholder"]]
        if len(candidates) == 1:
            self.issue(
                endpoint, "injury", "injury_id_mismatch",
                "player_id did not match, but one exact normalized canonical name did; "
                "the name match was used and the source ID was retained.",
                source_key=source_id, raw_record=record,
            )
            return int(candidates[0]["player_id"])
        self.issue(
            endpoint, "injury", "unmatched_injury_player",
            "No unique canonical player matched; injury retained with NULL player_id.",
            source_key=source_id, raw_record=record,
        )
        return None

    def load_injuries(
        self, records: Sequence[Dict[str, Any]], *, requested_status: str
    ) -> None:
        endpoint = f"/v1/injuries?status={requested_status}"
        for record in records:
            source_id = normalize_text(record.get("player_id"))
            try:
                reported = parse_date(record.get("date_reported"), field_name="date_reported")
                expected_text = normalize_text(record.get("expected_return")) or None
                expected = None
                if expected_text:
                    try:
                        expected = parse_date(
                            expected_text, field_name="expected_return"
                        )
                    except CleaningError:
                        self.issue(
                            endpoint, "injury", "non_date_expected_return",
                            "expected_return contains descriptive text; it was preserved "
                            "while expected_return_date remains NULL.",
                            source_key=source_id, raw_record=record,
                        )
                team_id = self.resolve_team(label=record.get("team"))
                player_id = self._resolve_injury_player(
                    source_id, normalize_text(record.get("player_name")), endpoint, record
                )
                actual_status = normalize_text(record.get("status")).lower()
                if actual_status != requested_status:
                    self.issue(
                        endpoint, "injury", "status_filter_mismatch",
                        f"Requested {requested_status!r}, received {actual_status!r}.",
                        source_key=source_id, raw_record=record,
                    )
                self.db.execute(
                    """
                    INSERT INTO injury_reports(
                        source_player_id, player_id, team_id, injury_type,
                        status, date_reported, expected_return_date,
                        expected_return_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        source_player_id, date_reported, injury_type, status
                    ) DO UPDATE SET
                        player_id=excluded.player_id,
                        team_id=excluded.team_id,
                        expected_return_date=excluded.expected_return_date,
                        expected_return_text=excluded.expected_return_text
                    """,
                    (
                        source_id, player_id, team_id,
                        normalize_text(record.get("injury")), actual_status,
                        reported, expected, expected_text,
                    ),
                )
            except (CleaningError, TypeError, ValueError) as exc:
                self.issue(
                    endpoint, "injury", "invalid_injury_record", str(exc),
                    source_key=source_id or None, raw_record=record, severity="error",
                )
        self.db.commit()

    def load_standings(self, records: Sequence[Dict[str, Any]]) -> None:
        endpoint = "/v2/standings"
        for record in records:
            source_id = normalize_text(record.get("teamId"))
            try:
                team_id = self.resolve_team(
                    canonical_id=source_id, label=record.get("teamName")
                )
                if team_id is None:
                    raise CleaningError(f"unknown team {source_id!r}")
                snapshot_date = parse_date(record.get("date"), field_name="standings date")
                self.db.execute(
                    """
                    INSERT INTO standings_snapshots(
                        team_id, snapshot_date, wins, losses, win_pct,
                        games_back, rank
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(team_id, snapshot_date) DO UPDATE SET
                        wins=excluded.wins, losses=excluded.losses,
                        win_pct=excluded.win_pct, games_back=excluded.games_back,
                        rank=excluded.rank
                    """,
                    (
                        team_id, snapshot_date, int(record["wins"]),
                        int(record["losses"]), float(record["pct"]),
                        float(record["games_back"]), int(record["rank"]),
                    ),
                )
            except (CleaningError, KeyError, TypeError, ValueError) as exc:
                self.issue(
                    endpoint, "standing", "invalid_standing_record", str(exc),
                    source_key=source_id or None, raw_record=record, severity="error",
                )
        self.db.commit()

    def load_legacy_stats(self, records: Sequence[Dict[str, Any]]) -> None:
        endpoint = "/legacy/player-stats"
        for record in records:
            full_name = normalize_text(record.get("PLAYER_NAME"))
            candidates = [row for row in self._players_named(full_name) if not row["is_placeholder"]]
            if len(candidates) != 1:
                self.issue(
                    endpoint, "legacy_player_stat", "unmatched_legacy_player",
                    f"Expected one canonical name match, found {len(candidates)}; row skipped.",
                    source_key=full_name or None, raw_record=record,
                )
                continue
            try:
                team_id = self.resolve_team(label=record.get("TEAM"))
                season = normalize_season(record.get("SEASON"))
                self.db.execute(
                    """
                    INSERT INTO legacy_player_season_stats(
                        player_id, team_id, season, games_played,
                        points_per_game, rebounds_per_game, assists_per_game
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(player_id, season) DO UPDATE SET
                        team_id=excluded.team_id,
                        games_played=excluded.games_played,
                        points_per_game=excluded.points_per_game,
                        rebounds_per_game=excluded.rebounds_per_game,
                        assists_per_game=excluded.assists_per_game
                    """,
                    (
                        int(candidates[0]["player_id"]), team_id, season,
                        int(record["GP"]), float(record["PPG"]),
                        float(record["RPG"]), float(record["APG"]),
                    ),
                )
                if team_id is None:
                    self.issue(
                        endpoint, "legacy_player_stat", "unknown_legacy_team",
                        "Player was retained but the inconsistent team label did not resolve.",
                        source_key=full_name, raw_record=record,
                    )
            except (CleaningError, KeyError, TypeError, ValueError) as exc:
                self.issue(
                    endpoint, "legacy_player_stat", "invalid_legacy_stat", str(exc),
                    source_key=full_name or None, raw_record=record, severity="error",
                )
        self.db.commit()

    def table_counts(self) -> Dict[str, int]:
        tables = (
            "teams", "players", "games", "team_game_stats",
            "player_game_stats", "roster_memberships", "injury_reports",
            "standings_snapshots", "legacy_player_season_stats",
            "data_quality_issues",
        )
        return {
            table: int(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
