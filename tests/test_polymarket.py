import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from gbwatch.models import Game
from gbwatch.polymarket import (
    event_has_player_prop,
    fetch_events,
    is_player_prop,
    select_games,
)
from gbwatch.schedule import book_url

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _game(away, home, game_id="0022600258", day="2026-11-18"):
    tip = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
    return Game(
        game_id=game_id,
        date_et=day,
        away=away,
        home=home,
        slug=f"{away.lower()}-vs-{home.lower()}-{game_id}",
        book_url=book_url(datetime.fromisoformat(day).date(), away, home),
        tip_utc=tip,
    )


def test_prop_question_shapes():
    assert is_player_prop("Cedric Coward: Points O/U 16.5")
    assert is_player_prop("Cam Spencer: 3-Pointers Made O/U 0.5")
    assert is_player_prop("Ja'Kobe Walter: 3PM O/U 2.5")
    assert is_player_prop("Giannis Antetokounmpo: Rebounds O/U 11.5")
    assert not is_player_prop("Grizzlies vs. Cavaliers: O/U 236.5")
    assert not is_player_prop("Will Atlanta Hawks win the 2027 NBA Finals?")
    assert not is_player_prop("Spread: Cavaliers (-4.5)")


def test_select_games_from_cached_gamma_fixture():
    events = json.loads((FIXTURES / "gamma_nba_events.json").read_text())
    assert event_has_player_prop(events[0])
    assert not event_has_player_prop(events[1])

    mem = _game("MEM", "CLE", "0022600258")
    was = _game("WAS", "TOR", "0022600263")
    chosen = select_games([mem, was], "polymarket", events)
    assert [game.game_id for game in chosen] == ["0022600258"]

    assert [game.game_id for game in select_games([mem, was], "all", events)] == [
        "0022600258",
        "0022600263",
    ]
    assert [game.game_id for game in select_games([mem, was], "0022600263", events)] == [
        "0022600263",
    ]


def test_fetch_events_uses_series_then_slug():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), dict(request.url.params)))
        params = request.url.params
        if params.get("series_slug") == "nba-2026":
            assert params.get("tag_slug") == "nba"
            assert params.get("closed") == "false"
            assert params.get("limit") == "500"
            return httpx.Response(200, json=[{"slug": "nba-mem-cle-2026-11-18", "title": "x", "markets": []}])
        if params.get("slug") == "nba-was-tor-2026-11-18":
            return httpx.Response(
                200,
                json=[{"slug": "nba-was-tor-2026-11-18", "title": "Wizards vs. Raptors", "markets": []}],
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    games = [_game("MEM", "CLE"), _game("WAS", "TOR", "0022600263")]
    events = fetch_events(client, games, sleeper=lambda _s: None)
    slugs = {event["slug"] for event in events}
    assert slugs == {"nba-mem-cle-2026-11-18", "nba-was-tor-2026-11-18"}
    assert any("series_slug=nba-2026" in url for url, _params in calls)


def test_title_match_when_slug_differs():
    events = [
        {
            "slug": "some-other-slug",
            "title": "Grizzlies vs. Cavaliers",
            "eventDate": "2026-11-18",
            "markets": [{"question": "Cedric Coward: Assists O/U 4.5"}],
        }
    ]
    chosen = select_games([_game("MEM", "CLE")], "polymarket", events)
    assert len(chosen) == 1
    # A futures title that names only one club is not a game.
    futures = [
        {
            "slug": "nba-2027-champion",
            "title": "NBA: 2027 Champion",
            "markets": [{"question": "Will Atlanta Hawks win the 2027 NBA Finals?"}],
        }
    ]
    assert select_games([_game("ATL", "BOS", "1")], "polymarket", futures) == []


def test_event_cache_ttl(monkeypatch):
    from gbwatch.polymarket import EventCache

    now = datetime(2026, 11, 18, 18, tzinfo=timezone.utc)
    game = _game("MEM", "CLE")
    game.tip_utc = now + timedelta(hours=2)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json=[{"slug": "nba-mem-cle-2026-11-18", "title": "Grizzlies vs. Cavaliers", "markets": []}],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cache = EventCache()
    cache.get(now, client, [game], sleeper=lambda _s: None)
    cache.get(now + timedelta(minutes=10), client, [game], sleeper=lambda _s: None)
    assert calls["n"] == 1
    cache.get(now + timedelta(minutes=31), client, [game], sleeper=lambda _s: None)
    assert calls["n"] == 2
