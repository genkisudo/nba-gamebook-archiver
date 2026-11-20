"""A book downloaded long after the final is not the first post-game version."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from gbwatch.cli import _refresh_schedule, main
from gbwatch.config import Config
from gbwatch.ingest import ingest_bytes
from gbwatch.models import Game
from gbwatch.notify import render_show
from gbwatch.schedule import book_url
from gbwatch.store import Store
from gbwatch.watcher import Watcher

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF = (FIXTURES / "MEMCLE_final.pdf").read_bytes()


def _game(**overrides) -> Game:
    tip = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
    game = Game(
        game_id="0022600258",
        date_et="2026-11-18",
        away="MEM",
        home="CLE",
        slug="mem-vs-cle-0022600258",
        book_url=book_url(tip.date(), "MEM", "CLE"),
        tip_utc=tip,
        game_status=3,
        game_status_text="Final",
        state="WATCH_BOOK",
    )
    for key, value in overrides.items():
        setattr(game, key, value)
    return game


def _box(tip: str, end: str | None) -> str:
    game = {
        "gameId": "0022600258",
        "gameStatus": 3,
        "gameStatusText": "Final",
        "gameTimeUTC": tip,
        "gameEt": "2026-11-18T19:00:00Z",
        "awayTeam": {"teamTricode": "MEM"},
        "homeTeam": {"teamTricode": "CLE"},
    }
    if end is not None:
        game["actualEndTimeUTC"] = end
    payload = {"props": {"pageProps": {"game": game}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'


def test_on_time_final_stays_first_final(tmp_path, monkeypatch):
    monkeypatch.delenv("LATE_CAPTURE_MINUTES", raising=False)
    store = Store(tmp_path)
    fetched = datetime(2026, 11, 19, 2, 40, tzinfo=timezone.utc)
    game = _game(final_seen_utc=fetched - timedelta(minutes=10))
    store.save_game(game)
    assert ingest_bytes(
        store, game, PDF, url=game.book_url, fetched_at=fetched, source="live",
        last_modified=None, etag=None, size=len(PDF), now=fetched,
    ) == "stored"
    version = store.versions(game.game_id)[0]
    assert version.kind == "final"
    assert version.late_capture == 0
    assert store.unread_alerts(0)[0]["type"] == "first_final"


def test_late_first_capture_when_book_arrives_after_the_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LATE_CAPTURE_MINUTES", raising=False)
    store = Store(tmp_path)
    fetched = datetime(2026, 11, 19, 3, 2, tzinfo=timezone.utc)
    game = _game(final_end_utc=fetched - timedelta(minutes=30), final_seen_utc=None)
    store.save_game(game)
    ingest_bytes(
        store, game, PDF, url=game.book_url, fetched_at=fetched, source="live",
        last_modified=None, etag=None, size=len(PDF), now=fetched,
    )
    version = store.versions(game.game_id)[0]
    assert version.kind == "final"
    assert version.late_capture == 1
    alert = store.unread_alerts(0)[0]
    assert alert["type"] == "late_first_capture"
    assert alert["gap_minutes"] == 30
    store.close()
    assert main(["show", "MEMCLE", "2026-11-18"]) == 0
    assert "LATE: not proof of first book" in capsys.readouterr().out


def test_lateness_uses_the_earlier_of_the_live_observation_and_the_end(tmp_path, monkeypatch):
    """The live observation does not win when the box score ended earlier."""
    monkeypatch.delenv("LATE_CAPTURE_MINUTES", raising=False)
    store = Store(tmp_path)
    fetched = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)
    game = _game(
        final_seen_utc=fetched - timedelta(minutes=10),
        final_end_utc=fetched - timedelta(minutes=40),
    )
    store.save_game(game)
    ingest_bytes(
        store, game, PDF, url=game.book_url, fetched_at=fetched, source="live",
        last_modified=None, etag=None, size=len(PDF), now=fetched,
    )
    assert store.versions(game.game_id)[0].late_capture == 1
    assert store.unread_alerts(0)[0]["gap_minutes"] == 40


def test_late_window_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("LATE_CAPTURE_MINUTES", "60")
    store = Store(tmp_path)
    fetched = datetime(2026, 11, 19, 3, 2, tzinfo=timezone.utc)
    game = _game(final_seen_utc=fetched - timedelta(minutes=30))
    store.save_game(game)
    ingest_bytes(
        store, game, PDF, url=game.book_url, fetched_at=fetched, source="live",
        last_modified=None, etag=None, size=len(PDF), now=fetched,
    )
    assert store.versions(game.game_id)[0].late_capture == 0
    assert store.unread_alerts(0)[0]["type"] == "first_final"


def _refresh(store, end: str, tip: str, started: datetime, books: list):
    games = '<a href="/game/mem-vs-cle-0022600258">MEM</a>'
    box = _box(tip, end)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "statsdmz.nba.com" in url:
            books.append(url)
            if request.method == "HEAD":
                return httpx.Response(
                    200,
                    headers={"ETag": "v1", "Last-Modified": "t", "Content-Length": str(len(PDF))},
                )
            return httpx.Response(200, content=PDF)
        if "games?date=2026-11-18" in url:
            return httpx.Response(200, text=games)
        if "games?date=" in url:
            return httpx.Response(200, text="<html></html>")
        if "box-score" in url:
            return httpx.Response(200, text=box)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    _refresh_schedule(
        store,
        client,
        Config(data_dir=store.data_dir, game_filter="all"),
        _Cache(),
        started,
        lambda _s: None,
        started,
    )
    return client


class _Cache:
    def get(self, *args, **kwargs):
        return []


def test_startup_skips_finals_more_than_six_hours_old(tmp_path):
    store = Store(tmp_path)
    started = datetime(2026, 11, 19, 16, 19, tzinfo=timezone.utc)
    books: list[str] = []
    client = _refresh(store, "2026-11-19T02:32:00Z", "2026-11-19T00:00:00Z", started, books)
    game = store.get_game("0022600258")
    assert game is not None
    assert game.state == "DONE"
    assert game.final_seen_utc is None
    assert game.final_end_utc == datetime(2026, 11, 19, 2, 32, tzinfo=timezone.utc)

    class Clock:
        def now(self):
            return started

    Watcher(store, client, Clock(), sleeper=lambda _s: None).tick(started)
    assert books == []
    assert store.unread_alerts(0) == []


def test_startup_still_watches_a_final_inside_six_hours_and_flags_it(tmp_path):
    store = Store(tmp_path)
    started = datetime(2026, 11, 19, 16, 19, tzinfo=timezone.utc)
    books: list[str] = []
    client = _refresh(store, "2026-11-19T10:30:00Z", "2026-11-19T08:00:00Z", started, books)
    game = store.get_game("0022600258")
    # The end was hours ago, so this is the watch-book window, not a live final.
    assert game.state == "WATCH_BOOK"
    assert game.final_seen_utc is None
    assert game.final_discovered_utc == started

    class Clock:
        def now(self):
            return started

    Watcher(store, client, Clock(), sleeper=lambda _s: None).tick(started)
    assert books
    version = store.versions("0022600258")[0]
    assert version.kind == "final"
    assert version.late_capture == 1
    alert = store.unread_alerts(0)[0]
    assert alert["type"] == "late_first_capture"
    assert alert["gap_minutes"] == 349
    assert store.get_game("0022600258").state == "DONE"
    assert "LATE: not proof of first book" in render_show(store, store.get_game("0022600258"))


def test_startup_flags_late_when_the_box_has_no_end_time(tmp_path):
    """Already final at discovery, and the box score has no actualEndTimeUTC."""
    store = Store(tmp_path)
    started = datetime(2026, 11, 19, 16, 19, tzinfo=timezone.utc)
    books: list[str] = []
    client = _refresh(store, None, "2026-11-19T08:00:00Z", started, books)
    game = store.get_game("0022600258")
    assert game is not None
    assert game.final_seen_utc is None
    assert game.final_end_utc is None

    class Clock:
        def now(self):
            return started

    Watcher(store, client, Clock(), sleeper=lambda _s: None).tick(started)
    assert books
    version = store.versions("0022600258")[0]
    assert version.kind == "final"
    assert version.late_capture == 1
    alert = store.unread_alerts(0)[0]
    assert alert["type"] == "late_first_capture"
