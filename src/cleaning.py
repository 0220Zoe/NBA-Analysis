"""Pure cleaning helpers used by the ingestion pipeline.

These functions intentionally do not know about SQLite or HTTP. Keeping vendor
format normalization isolated makes the high-risk rules easy to unit test.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Any, Optional, Tuple


DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%b %d, %Y",
    "%B %d, %Y",
)


class CleaningError(ValueError):
    """Raised when a value cannot be normalized without inventing information."""


def normalize_text(value: Any) -> str:
    """Trim and collapse whitespace; return an empty string for None."""
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def normalize_name(value: Any) -> str:
    """Create a conservative comparison key for person names.

    Hyphens and punctuation are removed so values such as "Karl-Anthony Towns"
    and "Karl Anthony Towns" match, while word order remains significant.
    """
    text = unicodedata.normalize("NFKD", normalize_text(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def roster_name_to_full_name(value: Any) -> str:
    """Convert the roster's documented 'Last, First' convention to display form."""
    text = normalize_text(value)
    if "," not in text:
        return text
    last, first = (normalize_text(part) for part in text.split(",", 1))
    return normalize_text(f"{first} {last}")


def parse_date(value: Any, *, field_name: str = "date") -> str:
    """Parse observed vendor date formats into an ISO calendar date."""
    text = normalize_text(value)
    if not text:
        raise CleaningError(f"{field_name} is empty")

    # datetime.fromisoformat handles both dates and timezone-bearing timestamps.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise CleaningError(f"unrecognized {field_name} format: {text!r}")


def parse_game_datetime(value: Any) -> Tuple[str, Optional[str]]:
    """Return (local calendar date, UTC timestamp when source includes a time)."""
    text = normalize_text(value)
    if "T" not in text:
        return parse_date(text, field_name="game_date"), None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CleaningError(f"unrecognized game datetime: {text!r}") from exc
    utc_value = None
    if parsed.tzinfo is not None:
        utc_value = parsed.astimezone(timezone.utc).isoformat()
    return parsed.date().isoformat(), utc_value


def season_for_date(iso_date: str) -> str:
    """Map an NBA game date to a season label such as 2024-25."""
    parsed = date.fromisoformat(iso_date)
    start_year = parsed.year if parsed.month >= 7 else parsed.year - 1
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def normalize_season(value: Any) -> str:
    """Normalize both observed '2024-25' and '2024-2025' forms."""
    text = normalize_text(value)
    match = re.fullmatch(r"(\d{4})-(\d{2}|\d{4})", text)
    if not match:
        raise CleaningError(f"unrecognized season: {text!r}")
    start = int(match.group(1))
    end = int(match.group(2))
    if end >= 100:
        end %= 100
    if end != (start + 1) % 100:
        raise CleaningError(f"non-consecutive season: {text!r}")
    return f"{start}-{end:02d}"


def parse_percentage(value: Any) -> Optional[float]:
    """Normalize mixed string/number percentages to a 0..1 float."""
    if value is None or normalize_text(value) == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CleaningError(f"invalid percentage: {value!r}") from exc
    if not 0.0 <= parsed <= 1.0:
        raise CleaningError(f"percentage outside 0..1: {value!r}")
    return parsed


def parse_minutes(value: Any) -> Optional[int]:
    """Convert MM:SS to seconds; empty minutes remain NULL."""
    text = normalize_text(value)
    if not text:
        return None
    match = re.fullmatch(r"(\d+):(\d{2})", text)
    if not match or int(match.group(2)) >= 60:
        raise CleaningError(f"invalid minutes value: {text!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


def parse_height_inches(value: Any) -> Optional[int]:
    """Convert an observed feet-inches string (for example 6-8) to inches."""
    text = normalize_text(value)
    if not text:
        return None
    match = re.fullmatch(r"(\d+)[-'](\d{1,2})\"?", text)
    if not match or int(match.group(2)) >= 12:
        raise CleaningError(f"invalid height: {text!r}")
    return int(match.group(1)) * 12 + int(match.group(2))


def birth_year_from_roster_pid(value: Any) -> Optional[int]:
    """Extract the terminal birth year used in roster slug IDs."""
    match = re.search(r"-(\d{4})$", normalize_text(value))
    return int(match.group(1)) if match else None
