"""SQLite state and the on-disk PDF tree."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from gbwatch.models import Game, Version, WaybackJob
from gbwatch.timeutil import format_utc, parse_utc, stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    date_et TEXT NOT NULL,
    away TEXT NOT NULL,
    home TEXT NOT NULL,
    slug TEXT,
    book_url TEXT NOT NULL,
    tip_utc TEXT,
    game_status INTEGER,
    game_status_text TEXT,
    state TEXT NOT NULL,
    final_seen_utc TEXT,
    final_end_utc TEXT,
    next_box_poll_utc TEXT,
    next_book_poll_utc TEXT,
    updated_utc TEXT
);

CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY,
    game_id TEXT NOT NULL,
    url TEXT NOT NULL,
    fetched_utc TEXT NOT NULL,
    last_modified TEXT,
    etag TEXT,
    size INTEGER,
    sha256 TEXT NOT NULL,
    pdf_created TEXT,
    pdf_modified TEXT,
    pages INTEGER,
    kind TEXT,
    path TEXT,
    wayback_url TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    note TEXT,
    late_capture INTEGER NOT NULL DEFAULT 0,
    UNIQUE(game_id, sha256)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    created_utc TEXT NOT NULL,
    type TEXT NOT NULL,
    game_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    acked_utc TEXT
);

CREATE TABLE IF NOT EXISTS wayback_jobs (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL,
    target_url TEXT NOT NULL,
    status TEXT NOT NULL,
    job_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try_utc TEXT,
    wayback_url TEXT,
    last_error TEXT,
    created_utc TEXT
);
"""


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.data_dir / "gbwatch.sqlite")
        self.conn.row_factory = sqlite3.Row
        # The alerts CLI can hold the database; don't crash the daemon on that.
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(SCHEMA)
        self._add_column("versions", "note", "TEXT")
        self._add_column("versions", "late_capture", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("games", "final_end_utc", "TEXT")
        self._add_column("wayback_jobs", "created_utc", "TEXT")
        self.conn.commit()

    def _add_column(self, table: str, name: str, ddl: str) -> None:
        columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        self.conn.close()

    # --- games -----------------------------------------------------------

    def get_game(self, game_id: str) -> Game | None:
        row = self.conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
        return _game(row) if row else None

    def find_matchup(self, away: str, home: str, date_et: str) -> Game | None:
        row = self.conn.execute(
            "SELECT * FROM games WHERE away = ? AND home = ? AND date_et = ?",
            (away.upper(), home.upper(), date_et),
        ).fetchone()
        return _game(row) if row else None

    def games_on(self, date_et: str) -> list[Game]:
        rows = self.conn.execute(
            "SELECT * FROM games WHERE date_et = ? ORDER BY tip_utc, game_id",
            (date_et,),
        ).fetchall()
        return [_game(row) for row in rows]

    def all_games(self) -> list[Game]:
        rows = self.conn.execute("SELECT * FROM games ORDER BY tip_utc, game_id").fetchall()
        return [_game(row) for row in rows]

    def active_games(self) -> list[Game]:
        rows = self.conn.execute(
            "SELECT * FROM games WHERE state != 'DONE' ORDER BY tip_utc, game_id"
        ).fetchall()
        return [_game(row) for row in rows]

    def upsert_game(self, game: Game) -> Game:
        """Insert a new game, or refresh slate fields without touching watcher state."""
        existing = self.get_game(game.game_id)
        if existing is None:
            self.save_game(game)
            return game
        existing.date_et = game.date_et
        existing.away = game.away
        existing.home = game.home
        existing.slug = game.slug
        existing.book_url = game.book_url
        existing.tip_utc = game.tip_utc
        existing.game_status = game.game_status
        existing.game_status_text = game.game_status_text
        if game.final_end_utc is not None:
            existing.final_end_utc = game.final_end_utc
        if existing.final_seen_utc is None and game.final_seen_utc is not None:
            existing.final_seen_utc = game.final_seen_utc
        self.save_game(existing)
        return existing

    def save_game(self, game: Game) -> None:
        self.conn.execute(
            """
            INSERT INTO games (
                game_id, date_et, away, home, slug, book_url, tip_utc,
                game_status, game_status_text, state, final_seen_utc, final_end_utc,
                next_box_poll_utc, next_book_poll_utc, updated_utc
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(game_id) DO UPDATE SET
                date_et=excluded.date_et,
                away=excluded.away,
                home=excluded.home,
                slug=excluded.slug,
                book_url=excluded.book_url,
                tip_utc=excluded.tip_utc,
                game_status=excluded.game_status,
                game_status_text=excluded.game_status_text,
                state=excluded.state,
                final_seen_utc=excluded.final_seen_utc,
                final_end_utc=excluded.final_end_utc,
                next_box_poll_utc=excluded.next_box_poll_utc,
                next_book_poll_utc=excluded.next_book_poll_utc,
                updated_utc=excluded.updated_utc
            """,
            (
                game.game_id,
                game.date_et,
                game.away,
                game.home,
                game.slug,
                game.book_url,
                _fmt(game.tip_utc),
                game.game_status,
                game.game_status_text,
                game.state,
                _fmt(game.final_seen_utc),
                _fmt(game.final_end_utc),
                _fmt(game.next_box_poll_utc),
                _fmt(game.next_book_poll_utc),
                format_utc(datetime.now().astimezone()),
            ),
        )
        self.conn.commit()

    # --- versions --------------------------------------------------------

    def version_path(self, game: Game, fetched_at: datetime, sha256: str, suffix: str) -> Path:
        ymd = game.date_et.replace("-", "")
        folder = self.data_dir / ymd / f"{game.away}{game.home}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{stamp(fetched_at)}_{sha256[:8]}{suffix}"

    def pdf_path(self, game: Game, fetched_at: datetime, sha256: str) -> Path:
        return self.version_path(game, fetched_at, sha256, ".pdf")

    def version_by_hash(self, game_id: str, sha256: str) -> Version | None:
        row = self.conn.execute(
            "SELECT * FROM versions WHERE game_id = ? AND sha256 = ?",
            (game_id, sha256),
        ).fetchone()
        return _version(row) if row else None

    def latest_version(self, game_id: str) -> Version | None:
        row = self.conn.execute(
            "SELECT * FROM versions WHERE game_id = ? ORDER BY fetched_utc DESC, id DESC LIMIT 1",
            (game_id,),
        ).fetchone()
        return _version(row) if row else None

    def versions(self, game_id: str) -> list[Version]:
        rows = self.conn.execute(
            "SELECT * FROM versions WHERE game_id = ? ORDER BY fetched_utc, id",
            (game_id,),
        ).fetchall()
        return [_version(row) for row in rows]

    def insert_version(self, **fields) -> Version:
        cur = self.conn.execute(
            """
            INSERT INTO versions (
                game_id, url, fetched_utc, last_modified, etag, size, sha256,
                pdf_created, pdf_modified, pages, kind, path, wayback_url, source, note,
                late_capture
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                fields["game_id"],
                fields["url"],
                fields["fetched_utc"],
                fields.get("last_modified"),
                fields.get("etag"),
                fields.get("size"),
                fields["sha256"],
                fields.get("pdf_created"),
                fields.get("pdf_modified"),
                fields.get("pages"),
                fields.get("kind"),
                fields.get("path"),
                fields.get("wayback_url"),
                fields.get("source") or "live",
                fields.get("note"),
                1 if fields.get("late_capture") else 0,
            ),
        )
        self.conn.commit()
        return self._version_by_id(cur.lastrowid)

    def touch_validators(self, version_id: int, last_modified, etag, size) -> None:
        self.conn.execute(
            "UPDATE versions SET last_modified = ?, etag = ?, size = ? WHERE id = ?",
            (last_modified, etag, size, version_id),
        )
        self.conn.commit()

    def update_version(self, version_id: int, **fields) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key} = ?" for key in fields)
        self.conn.execute(
            f"UPDATE versions SET {columns} WHERE id = ?",
            (*fields.values(), version_id),
        )
        self.conn.commit()

    def set_late_capture(self, version_id: int) -> None:
        self.conn.execute("UPDATE versions SET late_capture = 1 WHERE id = ?", (version_id,))
        self.conn.commit()

    def set_wayback_url(self, version_id: int, wayback_url: str) -> None:
        self.conn.execute(
            "UPDATE versions SET wayback_url = ? WHERE id = ?",
            (wayback_url, version_id),
        )
        self.conn.commit()

    def get_version(self, version_id: int) -> Version:
        row = self.conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            raise KeyError(version_id)
        return _version(row)

    def _version_by_id(self, version_id: int) -> Version:
        row = self.conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
        return _version(row)

    # --- alerts ----------------------------------------------------------

    def add_alert(self, payload: dict) -> dict:
        cur = self.conn.execute(
            "INSERT INTO alerts (created_utc, type, game_id, payload, acked_utc) VALUES (?,?,?,?,NULL)",
            (payload["created_utc"], payload["type"], payload["game_id"], "{}"),
        )
        payload = dict(payload)
        payload["id"] = cur.lastrowid
        encoded = json.dumps(payload, separators=(",", ":"), default=_json_default)
        self.conn.execute("UPDATE alerts SET payload = ? WHERE id = ?", (encoded, payload["id"]))
        self.conn.commit()
        with (self.data_dir / "alerts.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
        return payload

    def unread_alerts(self, since: int = 0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, payload, acked_utc FROM alerts WHERE id > ? ORDER BY id",
            (since,),
        ).fetchall()
        found = []
        for row in rows:
            if row["acked_utc"]:
                continue
            found.append(json.loads(row["payload"]))
        return found

    def ack(self, alert_id: int, when: datetime) -> bool:
        row = self.conn.execute("SELECT id FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if row is None:
            return False
        self.conn.execute(
            "UPDATE alerts SET acked_utc = ? WHERE id = ?",
            (format_utc(when), alert_id),
        )
        self.conn.commit()
        return True

    def alert_count(self, game_id: str, alert_type: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM alerts WHERE game_id = ? AND type = ?",
            (game_id, alert_type),
        ).fetchone()
        return int(row["n"])

    # --- wayback queue ---------------------------------------------------

    def enqueue_wayback(self, version_id: int, target_url: str, created_at: datetime | None = None) -> None:
        existing = self.conn.execute(
            "SELECT id FROM wayback_jobs WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        if existing:
            return
        self.conn.execute(
            """
            INSERT INTO wayback_jobs (version_id, target_url, status, attempts, created_utc)
            VALUES (?, ?, 'pending', 0, ?)
            """,
            (version_id, target_url, format_utc(created_at or datetime.now(timezone.utc))),
        )
        self.conn.commit()

    def wayback_by_status(self, status: str) -> list[WaybackJob]:
        rows = self.conn.execute(
            "SELECT * FROM wayback_jobs WHERE status = ? ORDER BY id",
            (status,),
        ).fetchall()
        return [_job(row) for row in rows]

    def wayback_ready(self, now: datetime, limit: int) -> list[WaybackJob]:
        rows = self.conn.execute(
            """
            SELECT * FROM wayback_jobs
            WHERE status = 'pending' AND (next_try_utc IS NULL OR next_try_utc <= ?)
            ORDER BY id LIMIT ?
            """,
            (format_utc(now), limit),
        ).fetchall()
        return [_job(row) for row in rows]

    def update_wayback_job(self, row_id: int, **fields) -> None:
        columns = ", ".join(f"{key} = ?" for key in fields)
        self.conn.execute(
            f"UPDATE wayback_jobs SET {columns} WHERE id = ?",
            (*fields.values(), row_id),
        )
        self.conn.commit()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fmt(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return format_utc(moment)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return parse_utc(value)


def _game(row: sqlite3.Row) -> Game:
    return Game(
        game_id=row["game_id"],
        date_et=row["date_et"],
        away=row["away"],
        home=row["home"],
        slug=row["slug"] or "",
        book_url=row["book_url"],
        tip_utc=_parse(row["tip_utc"]) or parse_utc("1970-01-01T00:00:00Z"),
        game_status=row["game_status"],
        game_status_text=row["game_status_text"] or "",
        state=row["state"],
        final_seen_utc=_parse(row["final_seen_utc"]),
        final_end_utc=_parse(row["final_end_utc"]),
        next_box_poll_utc=_parse(row["next_box_poll_utc"]),
        next_book_poll_utc=_parse(row["next_book_poll_utc"]),
    )


def _version(row: sqlite3.Row) -> Version:
    return Version(
        id=row["id"],
        game_id=row["game_id"],
        url=row["url"],
        fetched_utc=row["fetched_utc"],
        last_modified=row["last_modified"],
        etag=row["etag"],
        size=row["size"],
        sha256=row["sha256"],
        pdf_created=row["pdf_created"],
        pdf_modified=row["pdf_modified"],
        pages=row["pages"],
        kind=row["kind"] or "",
        path=row["path"] or "",
        wayback_url=row["wayback_url"],
        source=row["source"],
        note=row["note"],
        late_capture=int(row["late_capture"] or 0),
    )


def _job(row: sqlite3.Row) -> WaybackJob:
    return WaybackJob(
        id=row["id"],
        version_id=row["version_id"],
        target_url=row["target_url"],
        status=row["status"],
        job_id=row["job_id"],
        attempts=row["attempts"],
        next_try_utc=row["next_try_utc"],
        wayback_url=row["wayback_url"],
        last_error=row["last_error"],
        created_utc=row["created_utc"],
    )


def _json_default(value):
    return str(value)
