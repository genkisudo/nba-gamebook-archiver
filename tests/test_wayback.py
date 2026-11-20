import os
from datetime import datetime, timezone

import httpx

from gbwatch.models import Game
from gbwatch.notify import render_show
from gbwatch.schedule import book_url
from gbwatch.store import Store, sha256_bytes
from gbwatch.wayback import Wayback

ARCHIVED = b"%PDF-1.4 first final bytes\n"
SNAPSHOT = (
    "https://web.archive.org/web/20261119031141/"
    "https://statsdmz.nba.com/pdfs/20261118/20261118_MEMCLE_book.pdf"
)

SECRET = "SUPERSECRETVALUE"
ACCESS = "ACCESSKEYVALUE"


class Clock:
    def __init__(self):
        self.now_at = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)
        self.slept = []

    def now(self):
        return self.now_at


def _game():
    return Game(
        game_id="0022600258",
        date_et="2026-11-18",
        away="MEM",
        home="CLE",
        slug="mem-vs-cle-0022600258",
        book_url=book_url(__import__("datetime").date(2026, 11, 18), "MEM", "CLE"),
        tip_utc=datetime(2026, 11, 19, tzinfo=timezone.utc),
    )


def test_save_stores_snapshot_and_does_not_log_secret(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("IA_ACCESS", ACCESS)
    monkeypatch.setenv("IA_SECRET", SECRET)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["auth"] = request.headers.get("authorization")
            seen["accept"] = request.headers.get("accept")
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"url": "https://statsdmz.nba.com/x", "job_id": "spn-1"})
        if "id_/" in str(request.url):
            return httpx.Response(200, content=ARCHIVED)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "job_id": "spn-1",
                "timestamp": "20261119031141",
                "original_url": "https://statsdmz.nba.com/pdfs/20261118/20261118_MEMCLE_book.pdf",
            },
        )

    store = Store(tmp_path)
    game = _game()
    store.save_game(game)
    version = store.insert_version(
        game_id=game.game_id,
        url=game.book_url,
        fetched_utc="2026-11-19T03:11:41Z",
        sha256=sha256_bytes(ARCHIVED),
        kind="final",
        path="/tmp/x.pdf",
        source="live",
    )
    clock = Clock()
    wayback = Wayback(store, httpx.Client(transport=httpx.MockTransport(handler)), clock, sleeper=lambda s: clock.slept.append(s))
    wayback.enqueue(version.id, game.book_url)
    wayback.drain()
    stored = store.get_version(version.id)
    assert stored.wayback_url == (
        "https://web.archive.org/web/20261119031141/"
        "https://statsdmz.nba.com/pdfs/20261118/20261118_MEMCLE_book.pdf"
    )
    assert seen["auth"] == f"LOW {ACCESS}:{SECRET}"
    assert seen["accept"] == "application/json"
    assert "capture_all=1" in seen["body"]
    assert SECRET not in caplog.text
    assert ACCESS not in caplog.text
    assert SECRET.encode() not in (tmp_path / "gbwatch.sqlite").read_bytes()


def test_wayback_failure_does_not_raise_and_429_backs_off(tmp_path, monkeypatch):
    monkeypatch.setenv("IA_ACCESS", ACCESS)
    monkeypatch.setenv("IA_SECRET", SECRET)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(500, text="nope")

    store = Store(tmp_path)
    game = _game()
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
    clock = Clock()
    wayback = Wayback(
        store,
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock,
        sleeper=lambda seconds: clock.slept.append(seconds),
    )
    wayback.enqueue(version.id, game.book_url)
    wayback.drain()
    assert clock.slept == []  # 429 schedules next_try; it does not sleep the daemon
    pending = store.wayback_by_status("pending")
    assert pending and pending[0].next_try_utc
    assert store.get_version(version.id).wayback_url is None
    assert store.wayback_by_status("failed") == []
    # Still pending an hour later. The job is abandoned 24h after it was queued.
    clock.now_at = datetime(2026, 11, 20, 3, 0, 1, tzinfo=timezone.utc)
    wayback.drain()
    jobs = store.wayback_by_status("failed")
    assert len(jobs) == 1
    assert SECRET not in (jobs[0].last_error or "")


def test_network_retries_back_off_and_log_once(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("IA_ACCESS", ACCESS)
    monkeypatch.setenv("IA_SECRET", SECRET)
    caplog.set_level("WARNING")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tls handshake failed")

    store = Store(tmp_path)
    game = _game()
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
    clock = Clock()
    wayback = Wayback(store, httpx.Client(transport=httpx.MockTransport(handler)), clock, sleeper=lambda _s: None)
    wayback.enqueue(version.id, game.book_url)
    wayback.drain()
    first = store.wayback_by_status("pending")[0]
    assert first.attempts == 1
    assert first.next_try_utc == "2026-11-19T03:00:05Z"
    warnings = [rec for rec in caplog.records if rec.levelname == "WARNING"]
    assert len(warnings) == 1

    delay = 5
    for _ in range(12):
        clock.now_at = clock.now_at + __import__("datetime").timedelta(seconds=delay)
        wayback.drain()
        pending = store.wayback_by_status("pending")
        assert pending, "gave up before 24h"
        gap = (
            __import__("datetime").datetime.fromisoformat(pending[0].next_try_utc.replace("Z", "+00:00"))
            - clock.now_at
        ).total_seconds()
        delay = gap
        assert delay <= 30 * 60
    assert store.wayback_by_status("pending")[0].attempts > 1
    assert delay == 30 * 60
    assert len([rec for rec in caplog.records if rec.levelname == "WARNING"]) == 1

    clock.now_at = datetime(2026, 11, 20, 3, 0, 1, tzinfo=timezone.utc)
    wayback.drain()
    assert len(store.wayback_by_status("failed")) == 1
    assert store.wayback_by_status("pending") == []


def test_concurrent_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("IA_ACCESS", ACCESS)
    monkeypatch.setenv("IA_SECRET", SECRET)
    posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts["n"] += 1
            return httpx.Response(200, json={"job_id": f"job-{posts['n']}"})
        return httpx.Response(200, json={"status": "pending", "job_id": "job"})

    store = Store(tmp_path)
    game = _game()
    store.save_game(game)
    for index in range(12):
        version = store.insert_version(
            game_id=game.game_id,
            url=game.book_url,
            fetched_utc=f"2026-11-19T03:11:{index:02d}Z",
            sha256=f"{index:064x}",
            kind="in_game",
            path=f"/tmp/{index}.pdf",
            source="live",
        )
        store.enqueue_wayback(version.id, game.book_url)
    wayback = Wayback(
        store,
        httpx.Client(transport=httpx.MockTransport(handler)),
        Clock(),
        sleeper=lambda _s: None,
        max_concurrent=10,
    )
    wayback.drain()
    assert posts["n"] == 10
    assert len(store.wayback_by_status("submitted")) == 10
    assert len(store.wayback_by_status("pending")) == 2


def test_pending_status_expires_after_a_day_and_frees_the_slot(tmp_path, monkeypatch, caplog):
    """A Save Page Now job that stays pending must not hold a slot forever."""
    monkeypatch.setenv("IA_ACCESS", ACCESS)
    monkeypatch.setenv("IA_SECRET", SECRET)
    caplog.set_level("WARNING")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": "stuck-1"})
        return httpx.Response(200, json={"status": "pending", "job_id": "stuck-1"})

    store = Store(tmp_path)
    game = _game()
    store.save_game(game)
    clock = Clock()
    wayback = Wayback(
        store,
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock,
        sleeper=lambda _s: None,
        max_concurrent=10,
    )
    first = store.insert_version(
        game_id=game.game_id,
        url=game.book_url,
        fetched_utc="2026-11-19T03:11:41Z",
        sha256="a" * 64,
        kind="final",
        path="/tmp/a.pdf",
        source="live",
    )
    wayback.enqueue(first.id, game.book_url)
    wayback.drain()
    assert len(store.wayback_by_status("submitted")) == 1

    clock.now_at = clock.now_at + __import__("datetime").timedelta(hours=25)
    wayback.drain()
    failed = store.wayback_by_status("failed")
    assert len(failed) == 1
    assert failed[0].last_error == "status pending >24h"
    assert store.wayback_by_status("submitted") == []
    assert len([rec for rec in caplog.records if rec.levelname == "WARNING"]) == 1
    assert SECRET not in caplog.text

    second = store.insert_version(
        game_id=game.game_id,
        url=game.book_url,
        fetched_utc="2026-11-20T04:11:41Z",
        sha256="b" * 64,
        kind="final",
        path="/tmp/b.pdf",
        source="live",
    )
    wayback.enqueue(second.id, game.book_url)
    wayback.drain()
    submitted = store.wayback_by_status("submitted")
    assert len(submitted) == 1
    assert submitted[0].version_id == second.id


def _success_handler(archived: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": "spn-verify"})
        if "id_/" in url:
            return httpx.Response(200, content=archived)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "job_id": "spn-verify",
                "timestamp": "20261119031141",
                "original_url": "https://statsdmz.nba.com/pdfs/20261118/20261118_MEMCLE_book.pdf",
            },
        )

    return handler


def _queued(tmp_path, handler, digest: str):
    store = Store(tmp_path)
    game = _game()
    store.save_game(game)
    version = store.insert_version(
        game_id=game.game_id,
        url=game.book_url,
        fetched_utc="2026-11-19T03:11:41Z",
        sha256=digest,
        kind="final",
        path="/tmp/verify.pdf",
        source="live",
    )
    clock = Clock()
    wayback = Wayback(
        store,
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock,
        sleeper=lambda _s: None,
    )
    wayback.enqueue(version.id, game.book_url)
    wayback.drain()
    return store, game, version, wayback


def test_wayback_url_is_set_only_when_archived_bytes_match(tmp_path, monkeypatch):
    monkeypatch.setenv("IA_ACCESS", ACCESS)
    monkeypatch.setenv("IA_SECRET", SECRET)
    store, game, version, _wayback = _queued(tmp_path, _success_handler(ARCHIVED), sha256_bytes(ARCHIVED))
    stored = store.get_version(version.id)
    assert stored.wayback_url == SNAPSHOT
    assert stored.wayback_mismatch_url is None
    shown = render_show(store, game)
    assert f"wayback: {SNAPSHOT} (verified)" in shown
    assert SECRET not in shown


def test_wayback_mismatch_is_recorded_and_not_attached(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("IA_ACCESS", ACCESS)
    monkeypatch.setenv("IA_SECRET", SECRET)
    caplog.set_level("WARNING")
    store, game, version, _wayback = _queued(
        tmp_path, _success_handler(b"%PDF-1.4 a later revision\n"), sha256_bytes(ARCHIVED)
    )
    stored = store.get_version(version.id)
    assert stored.wayback_url is None
    assert stored.wayback_mismatch_url == SNAPSHOT
    assert store.wayback_by_status("submitted") == []
    shown = render_show(store, game)
    assert f"wayback: MISMATCH {SNAPSHOT}" in shown
    assert "(verified)" not in shown
    assert SECRET not in caplog.text
    assert ACCESS not in caplog.text


def test_wayback_download_failure_retries_without_a_new_save(tmp_path, monkeypatch):
    monkeypatch.setenv("IA_ACCESS", ACCESS)
    monkeypatch.setenv("IA_SECRET", SECRET)
    posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts["n"] += 1
            return httpx.Response(200, json={"job_id": "spn-retry"})
        if "id_/" in str(request.url):
            return httpx.Response(503, text="unavailable")
        return httpx.Response(
            200,
            json={
                "status": "success",
                "timestamp": "20261119031141",
                "original_url": "https://statsdmz.nba.com/pdfs/20261118/20261118_MEMCLE_book.pdf",
            },
        )

    store, _game, version, wayback = _queued(tmp_path, handler, sha256_bytes(ARCHIVED))
    stored = store.get_version(version.id)
    assert stored.wayback_url is None
    assert stored.wayback_mismatch_url is None
    job = store.wayback_by_status("submitted")[0]
    assert job.version_id == version.id
    assert job.next_try_utc == "2026-11-19T03:00:05Z"
    assert posts["n"] == 1
    # Still inside the backoff, so drain must not start another Save Page Now.
    wayback.drain()
    assert posts["n"] == 1


def test_submitted_status_is_polled_once_per_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("IA_ACCESS", ACCESS)
    monkeypatch.setenv("IA_SECRET", SECRET)
    calls = {"status": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/save/status/" in str(request.url):
            calls["status"] += 1
            return httpx.Response(200, json={"status": "pending", "job_id": "job-1"})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    store = Store(tmp_path)
    game = _game()
    store.save_game(game)
    version = store.insert_version(
        game_id=game.game_id,
        url=game.book_url,
        fetched_utc="2026-11-19T03:11:41Z",
        sha256="c" * 64,
        kind="final",
        path="/tmp/c.pdf",
        source="live",
    )
    clock = Clock()
    store.enqueue_wayback(version.id, game.book_url, created_at=clock.now())
    job = store.wayback_by_status("pending")[0]
    store.update_wayback_job(job.id, status="submitted", job_id="job-1")
    wayback = Wayback(
        store,
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock,
        sleeper=lambda _s: None,
    )
    wayback.drain()
    wayback.drain()
    assert calls["status"] == 1
    assert store.wayback_by_status("submitted")[0].next_try_utc == "2026-11-19T03:00:15Z"


def test_missing_credentials_skip_the_request(tmp_path, monkeypatch):
    monkeypatch.delenv("IA_ACCESS", raising=False)
    monkeypatch.delenv("IA_SECRET", raising=False)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not call archive.org without keys")

    store = Store(tmp_path)
    wayback = Wayback(store, httpx.Client(transport=httpx.MockTransport(handler)), Clock())
    wayback.enqueue(1, "https://statsdmz.nba.com/pdfs/20261118/20261118_MEMCLE_book.pdf")
    wayback.drain()
    assert os.environ.get("IA_SECRET") is None
