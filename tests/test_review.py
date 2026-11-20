"""Review fixes. Synthetic book text is labeled where it is not a real PDF extract."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from gbwatch.classify import classify_kind, parse_text
from gbwatch.cli import _refresh_schedule, serve
from gbwatch.config import Config
from gbwatch.models import Game
from gbwatch.schedule import book_url
from gbwatch.store import Store
from gbwatch.watcher import Watcher

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FINAL_PDF = (FIXTURES / "MEMCLE_final.pdf").read_bytes()
PARTIAL_PDF = (FIXTURES / "MEMCLE_1Q_partial.pdf").read_bytes()


def test_q4_running_book_is_not_final_without_final_box():
    # SYNTHETIC. Counted attendance and columns 1-4 used to look like a final.
    text = (
        "4th QUARTER (12:00) - 4th QUARTER (02:11)\n"
        "Attendance: 19432\n"
        "SCORE BY PERIOD              1        2       3        4       FINAL\n"
        "Grizzlies                   30       41      29       10        110\n"
    )
    assert classify_kind(text) == "in_game"
    assert "FINAL BOX" not in text


def test_final_box_wins_over_uncounted_attendance_and_is_noted(tmp_path):
    # SYNTHETIC page-1 header. A real first final can still say attendance is open.
    text = "FINAL BOX\nAttendance: Not Yet Counted\nVISITOR: Memphis Grizzlies (11-4)\n"
    assert classify_kind(text) == "final"
    assert parse_text(text).attendance == "Not Yet Counted"

    from gbwatch.ingest import ingest_text

    store = Store(tmp_path)
    tip = datetime(2026, 11, 19, tzinfo=timezone.utc)
    game = Game(
        game_id="0022600258",
        date_et="2026-11-18",
        away="MEM",
        home="CLE",
        slug="mem-vs-cle-0022600258",
        book_url=book_url(tip.date(), "MEM", "CLE"),
        tip_utc=tip,
        state="FINAL_SEEN",
        final_seen_utc=tip,
    )
    store.save_game(game)
    ingest_text(
        store, game, text, url=game.book_url, fetched_at=tip, source="manual", now=tip,
    )
    version = store.versions(game.game_id)[0]
    assert version.kind == "final"
    assert version.note == "attendance: Not Yet Counted"


class Clock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now += timedelta(seconds=seconds)


def _live_game(status=2, text="Q1 5:00"):
    tip = datetime(2026, 11, 19, tzinfo=timezone.utc)
    return Game(
        game_id="0022600258",
        date_et="2026-11-18",
        away="MEM",
        home="CLE",
        slug="mem-vs-cle-0022600258",
        book_url=book_url(tip.date(), "MEM", "CLE"),
        tip_utc=tip,
        state="LIVE",
        game_status=status,
        game_status_text=text,
    )


def test_unknown_parse_does_not_lock_headers_and_retries(tmp_path):
    calls = {"parse": 0}

    def parse(path):
        calls["parse"] += 1
        if calls["parse"] == 1:
            raise RuntimeError("parser failed once")
        from gbwatch.classify import parse_pdf

        return parse_pdf(path)

    gets = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(
                200,
                headers={"ETag": "final", "Last-Modified": "t", "Content-Length": str(len(FINAL_PDF))},
            )
        gets["n"] += 1
        return httpx.Response(
            200,
            headers={"ETag": "final", "Last-Modified": "t", "Content-Length": str(len(FINAL_PDF))},
            content=FINAL_PDF,
        )

    store = Store(tmp_path)
    game = _live_game(status=3, text="Final")
    game.final_seen_utc = datetime(2026, 11, 19, 2, 40, tzinfo=timezone.utc)
    game.state = "FINAL_SEEN"
    store.save_game(game)
    clock = Clock(datetime(2026, 11, 19, 2, 45, tzinfo=timezone.utc))
    watcher = Watcher(store, httpx.Client(transport=httpx.MockTransport(handler)), clock, sleeper=lambda _s: None, parse_pdf=parse)
    watcher.tick(clock.now())
    first = store.versions(game.game_id)[0]
    assert first.kind == "unknown"
    assert first.etag is None
    assert first.last_modified is None
    assert Path(first.path).is_file()
    assert store.get_game(game.game_id).state != "DONE"

    clock.advance(30)
    watcher.tick(clock.now())
    assert gets["n"] == 2
    saved = store.versions(game.game_id)[0]
    assert saved.kind == "final"
    assert saved.etag == "final"
    assert store.get_game(game.game_id).state == "DONE"
    assert store.unread_alerts(0)[0]["type"] == "first_final"


def test_early_partial_is_not_saved(tmp_path):
    box = """
    <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"game":{
      "gameId":"0022600258","gameStatus":2,"gameStatusText":"Q1 5:00",
      "gameTimeUTC":"2026-11-19T00:00:00Z","gameEt":"2026-11-18T19:00:00Z",
      "awayTeam":{"teamTricode":"MEM"},"homeTeam":{"teamTricode":"CLE"}
    }}}}</script>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "box-score" in str(request.url):
            return httpx.Response(200, text=box)
        headers = {"ETag": "p", "Last-Modified": "t1", "Content-Length": str(len(PARTIAL_PDF))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, content=PARTIAL_PDF)

    store = Store(tmp_path)
    store.save_game(_live_game())
    clock = Clock(datetime(2026, 11, 19, 1, 45, tzinfo=timezone.utc))
    Watcher(store, httpx.Client(transport=httpx.MockTransport(handler)), clock, sleeper=lambda _s: None).tick(clock.now())
    row = store.versions("0022600258")[0]
    assert row.kind == "in_game"
    assert row.path == ""
    assert row.etag == "p"
    assert list(tmp_path.rglob("*.pdf")) == []


def test_wayback_429_is_scheduled_not_slept_and_retried_by_serve(tmp_path, monkeypatch):
    monkeypatch.setenv("IA_ACCESS", "ACCESSKEYVALUE")
    monkeypatch.setenv("IA_SECRET", "SUPERSECRETVALUE")
    monkeypatch.setenv("GAME_FILTER", "all")
    posts = {"n": 0}
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "games?date=" in url:
            return httpx.Response(200, text="<html></html>")
        if request.method == "POST":
            posts["n"] += 1
            if posts["n"] == 1:
                return httpx.Response(429)
            return httpx.Response(200, json={"job_id": "job-1"})
        if "/save/status/" in url:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "timestamp": "20261119031141",
                    "original_url": "https://statsdmz.nba.com/x.pdf",
                },
            )
        return httpx.Response(404)

    store = Store(tmp_path)
    tip = datetime(2026, 11, 19, 3, tzinfo=timezone.utc)
    game = _live_game(status=3, text="Final")
    game.tip_utc = tip
    game.state = "DONE"
    store.save_game(game)
    version = store.insert_version(
        game_id=game.game_id,
        url=game.book_url,
        fetched_utc="2026-11-19T03:11:41Z",
        sha256="abc",
        kind="final",
        path="/tmp/x.pdf",
        source="live",
    )
    store.enqueue_wayback(version.id, game.book_url, created_at=tip)
    store.close()

    clock = Clock(tip)
    serve(
        Config(data_dir=tmp_path, game_filter="all"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock,
        sleep=lambda seconds: slept.append(seconds),
        max_ticks=1,
    )
    pending = Store(tmp_path)
    job = pending.wayback_by_status("pending")[0]
    assert job.next_try_utc
    assert slept == []
    pending.close()

    clock.advance(30)
    serve(
        Config(data_dir=tmp_path, game_filter="all"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock,
        sleep=lambda seconds: slept.append(seconds),
        max_ticks=1,
    )
    done = Store(tmp_path)
    assert done.get_version(version.id).wayback_url
    assert done.wayback_by_status("pending") == []
    done.close()


def test_gamma_failure_watches_all_games(tmp_path, monkeypatch):
    monkeypatch.delenv("GAME_FILTER", raising=False)
    box = """
    <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"game":{
      "gameId":"0022600258","gameStatus":2,"gameStatusText":"Q1 5:00",
      "gameTimeUTC":"2026-11-19T00:00:00Z","gameEt":"2026-11-18T19:00:00Z",
      "awayTeam":{"teamTricode":"MEM"},"homeTeam":{"teamTricode":"CLE"}
    }}}}</script>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "gamma-api" in url:
            return httpx.Response(500, text="down")
        if "games?date=2026-11-18" in url:
            return httpx.Response(200, text='<a href="/game/mem-vs-cle-0022600258">g</a>')
        if "games?date=" in url:
            return httpx.Response(200, text="<html></html>")
        if "box-score" in url:
            return httpx.Response(200, text=box)
        return httpx.Response(404)

    store = Store(tmp_path)
    started = datetime(2026, 11, 18, 23, tzinfo=timezone.utc)
    _refresh_schedule(
        store,
        httpx.Client(transport=httpx.MockTransport(handler)),
        Config(data_dir=tmp_path, game_filter="polymarket"),
        __import__("gbwatch.polymarket", fromlist=["EventCache"]).EventCache(),
        started,
        lambda _s: None,
        started,
    )
    game = store.get_game("0022600258")
    assert game is not None
    assert game.state != "DONE"


def test_serve_loop_survives_a_locked_database(tmp_path, monkeypatch):
    calls = {"n": 0}

    def tick(self, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("gbwatch.cli.Watcher.tick", tick)

    def handler(request: httpx.Request) -> httpx.Response:
        if "games?date=" in str(request.url):
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(404)

    serve(
        Config(data_dir=tmp_path, game_filter="all"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=Clock(datetime(2026, 11, 19, 12, tzinfo=timezone.utc)),
        sleep=lambda _s: None,
        max_ticks=2,
    )
    assert calls["n"] == 2
    store = Store(tmp_path)
    assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    store.close()
