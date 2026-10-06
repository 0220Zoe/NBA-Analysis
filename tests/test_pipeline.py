import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from src.api_client import ApiResponse
from src.database import connect_database, finish_run, start_run
from src.pipeline import Pipeline


class FakeClient:
    base_url = "https://example.invalid"

    def __init__(self, payloads):
        self.payloads = payloads

    def get(self, path, params=None):
        endpoint = path + (f"?{urlencode(params)}" if params else "")
        return ApiResponse(endpoint, 200, self.payloads[endpoint])


class PipelineIntegrationTests(unittest.TestCase):
    def test_end_to_end_load_is_queryable_and_idempotent(self):
        payloads = {
            "/v2/teams": {"data": [
                {"teamId": "100", "teamName": "Alphas", "city": "A City",
                 "abbreviation": "AAA", "conference": "East", "division": "One"},
                {"teamId": "200", "teamName": "Betas", "city": "B City",
                 "abbreviation": "BBB", "conference": "West", "division": "Two"},
            ]},
            "/v1/games": {"games": [
                {"game_id": "g1", "game_date": "01/15/2025", "home": "AAA",
                 "away": "B City Betas", "home_team_id": 1, "away_team_id": 2,
                 "home_score": 110, "away_score": 100, "status": "Final"}
            ]},
            "/v1/players": {"players": [
                {"player_id": 10, "first_name": "Test", "last_name": "Player",
                 "birthdate": "2000-01-02", "height": "6-8", "weight": 210,
                 "jersey": 1, "position": "F", "team_id": 1}
            ]},
            "/rosters/current": {"rosters": [
                {"team": "AAA", "players": [
                    {"pid": "test-player-2000", "name": "Player, Test",
                     "number": 1, "pos": "SF", "status": "Active"}
                ]}
            ]},
            "/v1/games/g1/boxscore": {
                "gameId": "g1", "gameDate": "2025-01-15",
                "homeTeam": {"teamId": 1, "abbreviation": "AAA", "players": [{
                    "personId": 10, "name": "T. Player", "minutes": "30:00",
                    "points": 35, "rebounds": 8, "assists": 5, "steals": 1,
                    "blocks": 0, "turnovers": 2, "fgm": 12, "fga": 20,
                    "fg_pct": ".600", "fg3m": 4, "fg3a": 8, "fg3_pct": 0.5,
                    "ftm": 7, "fta": 8, "ft_pct": ".875"
                }]},
                "awayTeam": {"teamId": 2, "abbreviation": "BBB", "players": []},
            },
            "/v1/injuries?status=out": {"injuries": []},
            "/v1/injuries?status=day-to-day": {"injuries": []},
            "/v1/injuries?status=questionable": {"injuries": []},
            "/v2/standings": {"data": []},
            "/legacy/player-stats": [],
        }

        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            db = connect_database(
                Path(temp_dir) / "test.db", project_root / "schema.sql"
            )
            for _ in range(2):
                run_id = start_run(db, FakeClient.base_url)
                pipeline = Pipeline(
                    FakeClient(payloads), db, run_id, roster_as_of="2025-01-20"
                )
                pipeline.run()
                finish_run(db, run_id, "completed")

            # Natural-key upserts make rerunning the exact snapshot safe.
            self.assertEqual(db.execute("SELECT COUNT(*) FROM games").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM player_game_stats").fetchone()[0], 1
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM roster_memberships").fetchone()[0], 1
            )
            result = db.execute(
                """
                SELECT p.full_name, pgs.points
                FROM player_game_stats pgs
                JOIN players p USING (player_id)
                WHERE pgs.points >= 30
                """
            ).fetchone()
            self.assertEqual(
                (result["full_name"], result["points"]), ("Test Player", 35)
            )
            db.close()

    def test_roster_birth_year_mismatch_creates_placeholder(self):
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            db = connect_database(
                Path(temp_dir) / "test.db", project_root / "schema.sql"
            )
            run_id = start_run(db, FakeClient.base_url)
            db.execute(
                "INSERT INTO teams(nba_team_id, name, city, abbreviation) "
                "VALUES ('100', 'Alphas', 'A City', 'AAA')"
            )
            db.execute(
                "INSERT INTO players(nba_player_id, full_name, normalized_name, "
                "birth_year) "
                "VALUES ('10', 'Alex Smith', 'alex smith', 2000)"
            )

            pipeline = Pipeline(
                FakeClient({}), db, run_id, roster_as_of="2025-01-20"
            )
            pipeline.load_rosters([{
                "team": "AAA",
                "players": [{"pid": "alex-smith-1999", "name": "Smith, Alex"}],
            }])

            linked = db.execute(
                """
                SELECT p.nba_player_id, p.birth_year, p.is_placeholder
                FROM roster_memberships AS rm
                JOIN players AS p USING (player_id)
                """
            ).fetchone()
            self.assertEqual(
                (
                    linked["nba_player_id"],
                    linked["birth_year"],
                    linked["is_placeholder"],
                ),
                (None, 1999, 1),
            )
            db.close()

    def test_unresolved_injury_team_remains_idempotent(self):
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            db = connect_database(
                Path(temp_dir) / "test.db", project_root / "schema.sql"
            )
            run_id = start_run(db, FakeClient.base_url)
            db.execute(
                "INSERT INTO players(nba_player_id, full_name, normalized_name) "
                "VALUES ('10', 'Test Player', 'test player')"
            )
            pipeline = Pipeline(FakeClient({}), db, run_id)
            injury = {
                "player_id": 10,
                "player_name": "Test Player",
                "team": "Unknown",
                "injury": "ankle",
                "status": "out",
                "date_reported": "2025-01-15",
                "expected_return": None,
            }

            pipeline.load_injuries([injury], requested_status="out")
            pipeline.load_injuries([injury], requested_status="out")

            count = db.execute("SELECT COUNT(*) FROM injury_reports").fetchone()[0]
            self.assertEqual(count, 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
