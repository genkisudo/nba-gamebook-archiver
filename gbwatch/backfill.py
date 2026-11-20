"""Pull historical book captures from the Wayback Machine.

CDX is often offline. A failure falls back to archive.org/wayback/available
for each game on that ET date, and a later run can try CDX again.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

import httpx

from gbwatch.config import DEFAULT_PREFIXES
from gbwatch.http import request
from gbwatch.ingest import ingest_bytes
from gbwatch.models import Game
from gbwatch.schedule import book_url, get_games
from gbwatch.store import Store
from gbwatch.timeutil import ET

log = logging.getLogger("gbwatch.backfill")

CDX_URL = (
    "https://web.archive.org/cdx/search/cdx"
    "?url=statsdmz.nba.com/pdfs/{ymd}/*&output=json&fl=timestamp,original,digest,length"
)
AVAILABLE_URL = "https://archive.org/wayback/available"


def backfill_date(
    day: date,
    store: Store,
    client: httpx.Client,
    *,
    prefixes: tuple[str, ...] = DEFAULT_PREFIXES,
    sleeper=None,
    clock=None,
) -> int:
    ymd = day.strftime("%Y%m%d")
    kwargs = {"sleeper": sleeper} if sleeper else {}
    rows = _cdx_rows(client, ymd, kwargs)
    games: list[Game] = []
    # An empty CDX body is not proof that nothing was archived: the index is
    # often offline or returns no rows while wayback/available still has a capture.
    if not rows:
        if rows is None:
            log.warning("CDX unavailable for %s; falling back to wayback available", ymd)
        else:
            log.warning("CDX returned no captures for %s; trying wayback available", ymd)
        games = _load_games(day, store, client, prefixes, kwargs)
        rows = []
        for game in games:
            snap = _available(client, game.book_url, kwargs)
            if snap:
                rows.append(snap)
    else:
        games = _load_games(day, store, client, prefixes, kwargs)
    rows = sorted(rows, key=lambda row: row["timestamp"])
    stored = 0
    now = clock.now() if clock else datetime.now(timezone.utc)
    for row in rows:
        original = _normalize(row["original"])
        game = _game_for(original, games, store, day)
        download = f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"
        try:
            response = request(client, "GET", download, **kwargs)
        except httpx.HTTPError:
            log.warning("wayback download failed ts=%s", row["timestamp"])
            continue
        if response.status_code != 200:
            log.warning("wayback download HTTP %s ts=%s", response.status_code, row["timestamp"])
            continue
        result = ingest_bytes(
            store,
            game,
            response.content,
            url=original,
            fetched_at=_cdx_time(row["timestamp"]),
            source="wayback",
            last_modified=None,
            etag=row.get("digest"),
            size=len(response.content),
            wayback=None,
            wayback_url=f"https://web.archive.org/web/{row['timestamp']}/{row['original']}",
            now=now,
        )
        if result == "stored":
            stored += 1
            log.info("backfill stored %s %s %s", game.matchup, row["timestamp"], result)
    return stored


def _load_games(day, store, client, prefixes, kwargs) -> list[Game]:
    try:
        games = get_games(day, client, prefixes, **kwargs)
    except Exception:
        log.warning("schedule unavailable during backfill %s", day.isoformat())
        return []
    for game in games:
        store.upsert_game(game)
    return games


def _cdx_rows(client, ymd: str, kwargs) -> list[dict] | None:
    try:
        response = request(client, "GET", CDX_URL.format(ymd=ymd), retries=1, **kwargs)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, list):
        return None
    rows = []
    for item in payload:
        if not isinstance(item, list) or len(item) < 2:
            continue
        if item[0] == "timestamp":
            continue
        rows.append(
            {
                "timestamp": str(item[0]),
                "original": str(item[1]),
                "digest": str(item[2]) if len(item) > 2 else "",
            }
        )
    return rows


def _available(client, book: str, kwargs) -> dict | None:
    try:
        response = request(client, "GET", AVAILABLE_URL, params={"url": book}, **kwargs)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except Exception:
        return None
    closest = ((body.get("archived_snapshots") or {}).get("closest")) or {}
    if not closest.get("available") or not closest.get("timestamp"):
        return None
    return {"timestamp": str(closest["timestamp"]), "original": book, "digest": closest.get("digest") or ""}


def _normalize(original: str) -> str:
    text = original.strip()
    if not text.startswith("http"):
        text = "https://" + text
    return text.split("?")[0]


def _cdx_time(timestamp: str) -> datetime:
    return datetime.strptime(timestamp[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def _game_for(original: str, games: list[Game], store: Store, day: date) -> Game:
    path = original.split("?")[0]
    for game in games:
        if game.book_url.split("?")[0] == path:
            found = store.get_game(game.game_id)
            return found or game
    match = re.search(r"/(\d{8})_([A-Z]{3})([A-Z]{3})_book\.pdf", path, re.I)
    if match:
        ymd, away, home = match.group(1), match.group(2).upper(), match.group(3).upper()
        game_day = datetime.strptime(ymd, "%Y%m%d").date()
    else:
        away, home, game_day = "UNK", "UNK", day
    game_id = f"book-{game_day.strftime('%Y%m%d')}-{away}{home}"
    existing = store.get_game(game_id)
    if existing:
        return existing
    tip = datetime(game_day.year, game_day.month, game_day.day, tzinfo=ET)
    game = Game(
        game_id=game_id,
        date_et=game_day.isoformat(),
        away=away,
        home=home,
        slug="",
        book_url=book_url(game_day, away, home),
        tip_utc=tip,
        state="DONE",
    )
    store.save_game(game)
    return game
