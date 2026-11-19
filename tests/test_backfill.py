from datetime import date
from pathlib import Path

import httpx

from gbwatch.backfill import backfill_date
from gbwatch.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BOOK = "https://statsdmz.nba.com/pdfs/20261118/20261118_MEMCLE_book.pdf"


def test_cdx_orders_partial_then_final_and_alerts_once(tmp_path):
    partial = (FIXTURES / "MEMCLE_1Q_partial.pdf").read_bytes()
    final = (FIXTURES / "MEMCLE_final.pdf").read_bytes()
    cdx = [
        ["timestamp", "original", "digest", "length"],
        ["20261119031141", BOOK, "finaldigest", "2"],
        ["20261119002036", BOOK, "partialdigest", "1"],
    ]
    bodies = {
        "20261119002036": partial,
        "20261119031141": final,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "cdx/search" in url:
            assert "20261118" in url
            assert "fl=timestamp,original,digest,length" in url
            return httpx.Response(200, json=cdx)
        if "/web/" in url and "id_/" in url:
            for stamp, body in bodies.items():
                if f"/web/{stamp}id_/" in url:
                    return httpx.Response(200, content=body)
        # Schedule lookup is best-effort; a failure must not drop CDX rows.
        return httpx.Response(503)

    store = Store(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    stored = backfill_date(date(2026, 11, 18), store, client, sleeper=lambda _s: None)
    assert stored == 2
    versions = store.versions("book-20261118-MEMCLE")
    assert [version.kind for version in versions] == ["in_game", "final"]
    assert [version.source for version in versions] == ["wayback", "wayback"]
    assert versions[0].fetched_utc == "2026-11-19T00:20:36Z"
    assert versions[1].wayback_url.startswith("https://web.archive.org/web/20261119031141/")
    alerts = store.unread_alerts(0)
    assert [alert["type"] for alert in alerts] == ["first_final"]

    # Same captures again are hash-deduped.
    again = backfill_date(date(2026, 11, 18), store, client, sleeper=lambda _s: None)
    assert again == 0
    assert len(store.versions("book-20261118-MEMCLE")) == 2


def test_empty_cdx_falls_back_to_available(tmp_path):
    _assert_available_fallback(tmp_path, httpx.Response(200, json=[]))


def test_cdx_outage_falls_back_to_available(tmp_path):
    _assert_available_fallback(tmp_path, httpx.Response(503, text="offline"))


def _assert_available_fallback(tmp_path, cdx_response):
    final = (FIXTURES / "MEMCLE_final.pdf").read_bytes()
    games = '<a href="/game/mem-vs-cle-0022600258">MEM</a>'
    box = (FIXTURES / "box_mem_cle.html").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "cdx/search" in url:
            return cdx_response
        if "nba.com/games" in url:
            return httpx.Response(200, text=games)
        if "box-score" in url:
            return httpx.Response(200, text=box)
        if "wayback/available" in url:
            assert request.url.params["url"] == BOOK
            return httpx.Response(
                200,
                json={
                    "archived_snapshots": {
                        "closest": {
                            "available": True,
                            "timestamp": "20261119031141",
                            "status": "200",
                        }
                    }
                },
            )
        if "/web/20261119031141id_/" in url:
            return httpx.Response(200, content=final)
        return httpx.Response(404)

    store = Store(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    stored = backfill_date(date(2026, 11, 18), store, client, sleeper=lambda _s: None)
    assert stored == 1
    version = store.versions("0022600258")[0]
    assert version.kind == "final"
    assert version.source == "wayback"
    assert store.unread_alerts(0)[0]["type"] == "first_final"
