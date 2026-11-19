from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from gbwatch.models import Game
from gbwatch.schedule import book_url
from gbwatch.store import Store
from gbwatch.watcher import Watcher, book_interval, state_for

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TIP = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
BOX = """
<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"game":{
  "gameId":"0022600258","gameStatus":3,"gameStatusText":"Final",
  "gameTimeUTC":"2026-11-19T00:00:00Z","gameEt":"2026-11-18T19:00:00Z",
  "awayTeam":{"teamTricode":"MEM"},"homeTeam":{"teamTricode":"CLE"}
}}}}</script>
"""


class Clock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now += timedelta(seconds=seconds)


def _game():
    return Game(
        game_id="0022600258",
        date_et="2026-11-18",
        away="MEM",
        home="CLE",
        slug="mem-vs-cle-0022600258",
        book_url=book_url(datetime(2026, 11, 18).date(), "MEM", "CLE"),
        tip_utc=TIP,
        state="LIVE",
        game_status=2,
        game_status_text="Q3 5:00",
    )


def test_intervals_and_postponed_done():
    final = TIP + timedelta(hours=2)
    assert state_for(TIP - timedelta(minutes=11), TIP, None) == "SCHEDULED"
    assert state_for(TIP - timedelta(minutes=10), TIP, None) == "LIVE"
    assert state_for(final, TIP, final) == "FINAL_SEEN"
    assert book_interval(final, TIP, final) == timedelta(seconds=30)
    assert state_for(final + timedelta(minutes=30), TIP, final) == "WATCH_BOOK"
    assert book_interval(final + timedelta(minutes=30), TIP, final) == timedelta(seconds=30)
    assert book_interval(final + timedelta(hours=3), TIP, final) == timedelta(seconds=30)
    assert state_for(final + timedelta(hours=12), TIP, final) == "DONE"
    assert book_interval(final + timedelta(hours=12), TIP, final) is None
    assert state_for(TIP + timedelta(hours=6), TIP, None) == "DONE"
    assert book_interval(TIP - timedelta(seconds=1), TIP, None) is None
    assert book_interval(TIP, TIP, None) == timedelta(seconds=60)


def test_sequence_saves_first_final_then_stops(tmp_path, monkeypatch):
    monkeypatch.delenv("IA_ACCESS", raising=False)
    monkeypatch.delenv("IA_SECRET", raising=False)
    partial = (FIXTURES / "MEMCLE_1Q_partial.pdf").read_bytes()
    final = (FIXTURES / "MEMCLE_final.pdf").read_bytes()
    script = [
        {"status": 404},
        {"status": 200, "etag": "partial", "lm": "t1", "body": partial},
        {"status": 200, "etag": "final", "lm": "t2", "body": final},
        {"status": 200, "etag": "corrected", "lm": "t3", "body": final + b"\n%later\n"},
    ]
    calls = {"head": 0, "get": 0, "urls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "box-score" in url:
            assert request.headers["user-agent"].startswith("Mozilla/5.0")
            return httpx.Response(200, text=BOX)
        if "statsdmz.nba.com" in url:
            assert "t=" in url
            calls["urls"].append(request.method)
            if request.method == "HEAD":
                spec = script[min(calls["head"], len(script) - 1)]
                calls["head"] += 1
                if spec["status"] == 404:
                    return httpx.Response(404)
                return httpx.Response(
                    200,
                    headers={
                        "ETag": spec["etag"],
                        "Last-Modified": spec["lm"],
                        "Content-Length": str(len(spec["body"])),
                    },
                )
            spec = script[calls["head"] - 1]
            calls["get"] += 1
            return httpx.Response(200, content=spec["body"])
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = Store(tmp_path)
    store.save_game(_game())
    clock = Clock(datetime(2026, 11, 19, 1, 0, tzinfo=timezone.utc))
    watcher = Watcher(store, client, clock, sleeper=lambda _s: None)

    watcher.tick(clock.now())
    assert store.versions("0022600258") == []

    clock.advance(30)
    watcher.tick(clock.now())
    partial_rows = store.versions("0022600258")
    assert [row.kind for row in partial_rows] == ["in_game"]
    # The box score already said Final, so this partial is kept as evidence.
    assert partial_rows[0].path.endswith(".pdf")
    assert Path(partial_rows[0].path).is_file()

    clock.advance(30)
    watcher.tick(clock.now())
    versions = store.versions("0022600258")
    assert [row.kind for row in versions] == ["in_game", "final"]
    assert versions[1].path.endswith(".pdf")
    assert Path(versions[1].path).is_file()
    alerts = store.unread_alerts(0)
    assert [alert["type"] for alert in alerts] == ["first_final"]
    assert alerts[0]["matchup"] == "MEM@CLE"
    assert alerts[0]["sha256"] == versions[1].sha256
    assert store.get_game("0022600258").state == "DONE"
    gets_after_final = calls["get"]

    # A later book must not be fetched or alerted. Restart stays stopped.
    clock.advance(30)
    watcher.tick(clock.now())
    store.close()
    store2 = Store(tmp_path)
    watcher2 = Watcher(store2, client, clock, sleeper=lambda _s: None)
    clock.advance(30)
    watcher2.tick(clock.now())
    assert calls["get"] == gets_after_final
    assert [row.kind for row in store2.versions("0022600258")] == ["in_game", "final"]
    assert [alert["type"] for alert in store2.unread_alerts(0)] == ["first_final"]
    assert store2.get_game("0022600258").state == "DONE"
    assert (tmp_path / "alerts.jsonl").read_text().count("\n") == 1


def test_postponed_game_stops_without_requests(tmp_path):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP for a game already past tip+6h")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = Store(tmp_path)
    game = _game()
    store.save_game(game)
    clock = Clock(TIP + timedelta(hours=6, seconds=1))
    Watcher(store, client, clock, sleeper=lambda _s: None).tick(clock.now())
    assert store.get_game(game.game_id).state == "DONE"
