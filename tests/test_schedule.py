from datetime import date
from pathlib import Path

import httpx

from gbwatch.http import NBA_UA
from gbwatch.schedule import book_url, find_slugs, get_games, parse_box_score, teams_from_slug

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_book_url_mem_cle():
    assert teams_from_slug("mem-vs-cle-0022600258") == ("MEM", "CLE")
    assert book_url(date(2026, 11, 18), "MEM", "CLE") == (
        "https://statsdmz.nba.com/pdfs/20261118/20261118_MEMCLE_book.pdf"
    )


def test_find_slugs_uses_configurable_prefixes():
    html = (FIXTURES / "games_2026-11-18.html").read_text()
    slugs = find_slugs(html)
    assert "mem-vs-cle-0022600258" in slugs
    assert "was-vs-tor-0022600263" in slugs
    assert "mia-vs-mil-0042600007" in slugs
    assert "sac-vs-phx-0052600012" in slugs
    assert all("00126" not in slug and "00326" not in slug for slug in slugs)
    assert len(slugs) == 14
    only_regular = find_slugs(html, prefixes=("00226",))
    assert "mia-vs-mil-0042600007" not in only_regular
    assert len(only_regular) == 12


def test_get_games_reads_next_data_and_sends_nba_user_agent():
    games_html = """
    <a href="/game/mem-vs-cle-0022600258">game</a>
    <a href="/game/bos-vs-lal-0012600999">old</a>
    """
    box_html = (FIXTURES / "box_mem_cle.html").read_text()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.headers.get("user-agent")))
        if "games?" in str(request.url):
            return httpx.Response(200, text=games_html)
        if "mem-vs-cle" in str(request.url):
            return httpx.Response(200, text=box_html)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    games = get_games(date(2026, 11, 18), client)
    assert len(games) == 1
    game = games[0]
    assert game.game_id == "0022600258"
    assert game.away == "MEM" and game.home == "CLE"
    assert game.date_et == "2026-11-18"
    assert game.tip_utc.isoformat().startswith("2026-11-19T00:00:00")
    assert game.game_status == 3
    assert game.game_status_text == "Final"
    assert game.book_url.endswith("/20261118/20261118_MEMCLE_book.pdf")
    assert all(ua == NBA_UA for _method, url, ua in seen if "nba.com" in url)

    parsed = parse_box_score(box_html, "mem-vs-cle-0022600258", date(2026, 11, 18))
    assert parsed.game_id == game.game_id
