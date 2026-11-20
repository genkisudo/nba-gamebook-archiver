"""Plain records shared across the package."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Game:
    game_id: str
    date_et: str
    away: str
    home: str
    slug: str
    book_url: str
    tip_utc: datetime
    game_status: int | None = None
    game_status_text: str = ""
    period: int | None = None
    state: str = "SCHEDULED"
    final_seen_utc: datetime | None = None
    final_end_utc: datetime | None = None
    next_box_poll_utc: datetime | None = None
    next_book_poll_utc: datetime | None = None

    @property
    def matchup(self) -> str:
        return f"{self.away}@{self.home}"


@dataclass
class Version:
    id: int
    game_id: str
    url: str
    fetched_utc: str
    last_modified: str | None
    etag: str | None
    size: int | None
    sha256: str
    pdf_created: str | None
    pdf_modified: str | None
    pages: int | None
    kind: str
    path: str
    wayback_url: str | None
    source: str
    note: str | None = None
    late_capture: int = 0
    wayback_mismatch_url: str | None = None


@dataclass
class ParsedBook:
    kind: str
    players: dict[str, dict] = field(default_factory=dict)
    teams: dict[str, dict] = field(default_factory=dict)
    score: dict[str, int] = field(default_factory=dict)
    attendance: str | None = None

    def stats(self) -> dict[str, dict]:
        """Player lines plus team totals, the map diffs compare."""
        combined = dict(self.players)
        for name, row in self.teams.items():
            combined[f"{name} total"] = row
        return combined


@dataclass
class WaybackJob:
    id: int
    version_id: int
    target_url: str
    status: str
    job_id: str | None
    attempts: int
    next_try_utc: str | None
    wayback_url: str | None
    last_error: str | None
    created_utc: str | None = None
