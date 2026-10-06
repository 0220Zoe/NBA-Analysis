import unittest

from src.cleaning import (
    CleaningError,
    normalize_name,
    normalize_season,
    parse_date,
    parse_game_datetime,
    parse_height_inches,
    parse_minutes,
    parse_percentage,
    roster_name_to_full_name,
)


class CleaningTests(unittest.TestCase):
    def test_observed_date_formats_normalize(self):
        expected = "2025-01-15"
        for value in (
            "2025-01-15", "01/15/2025", "Jan 15, 2025", "January 15, 2025"
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_date(value), expected)

    def test_timezone_datetime_preserves_local_date_and_utc(self):
        local_date, utc_timestamp = parse_game_datetime("2024-10-22T19:30:00-04:00")
        self.assertEqual(local_date, "2024-10-22")
        self.assertEqual(utc_timestamp, "2024-10-22T23:30:00+00:00")

    def test_name_normalization_handles_punctuation(self):
        self.assertEqual(
            normalize_name("Shai Gilgeous-Alexander"),
            normalize_name("Shai Gilgeous Alexander"),
        )
        self.assertEqual(roster_name_to_full_name("Towns, Karl-Anthony"),
                         "Karl-Anthony Towns")

    def test_boxscore_values_normalize(self):
        self.assertEqual(parse_percentage(".333"), 0.333)
        self.assertEqual(parse_percentage(0.5), 0.5)
        self.assertEqual(parse_minutes("34:12"), 2052)
        self.assertEqual(parse_height_inches("6-8"), 80)

    def test_invalid_values_are_not_silently_coerced(self):
        with self.assertRaises(CleaningError):
            parse_percentage("56%")
        with self.assertRaises(CleaningError):
            parse_minutes("12:99")

    def test_season_formats_normalize(self):
        self.assertEqual(normalize_season("2024-25"), "2024-25")
        self.assertEqual(normalize_season("2024-2025"), "2024-25")


if __name__ == "__main__":
    unittest.main()

