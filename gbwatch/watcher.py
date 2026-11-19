"""Per-game poll loop. State lives in SQLite, so a restart continues the same watch."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from gbwatch import classify
from gbwatch.http import request
from gbwatch.ingest import ingest_bytes
from gbwatch.models import Game, Version
from gbwatch.schedule import BOX_URL, is_final_status, parse_box_score
from gbwatch.store import Store
from gbwatch.wayback import Wayback

log = logging.getLogger("gbwatch.watcher")

# A game already final this long before process start is not worth a live watch.
STARTUP_FINAL_CUTOFF = timedelta(hours=6)


def discovered_too_late(game: Game, started_at: datetime) -> bool:
    """True when the box score says the game ended more than 6h before daemon start."""
    if not is_final_status(game.game_status, game.game_status_text):
        return False
    if game.final_end_utc is None:
        return False
    return started_at - game.final_end_utc > STARTUP_FINAL_CUTOFF


def _final_anchor(final_seen: datetime | None, final_end: datetime | None) -> datetime | None:
    """Live observation wins. Otherwise the box score's actual end time."""
    return final_seen if final_seen is not None else final_end


def state_for(
    now: datetime,
    tip: datetime,
    final_seen: datetime | None,
    final_end: datetime | None = None,
) -> str:
    anchor = _final_anchor(final_seen, final_end)
    if anchor is not None:
        if now >= anchor + timedelta(hours=12):
            return "DONE"
        if now >= anchor + timedelta(minutes=30):
            return "WATCH_BOOK"
        return "FINAL_SEEN"
    if now >= tip + timedelta(hours=6):
        return "DONE"
    if now >= tip - timedelta(minutes=10):
        return "LIVE"
    return "SCHEDULED"


def book_interval(
    now: datetime,
    tip: datetime,
    final_seen: datetime | None,
    final_end: datetime | None = None,
) -> timedelta | None:
    """How long to wait after a book poll, or None when the book is not polled."""
    anchor = _final_anchor(final_seen, final_end)
    if anchor is None:
        if now < tip or now >= tip + timedelta(hours=6):
            return None
        return timedelta(seconds=60)
    elapsed = now - anchor
    if elapsed >= timedelta(hours=12):
        return None
    if elapsed < timedelta(minutes=30):
        return timedelta(seconds=30)
    if elapsed < timedelta(hours=3):
        return timedelta(seconds=60)
    return timedelta(minutes=15)


def box_interval(
    now: datetime,
    tip: datetime,
    final_seen: datetime | None,
    final_end: datetime | None = None,
) -> timedelta | None:
    if _final_anchor(final_seen, final_end) is not None:
        return None
    if now < tip - timedelta(minutes=10) or now >= tip + timedelta(hours=6):
        return None
    return timedelta(seconds=120)


def headers_changed(latest: Version | None, headers) -> bool:
    """True when Last-Modified, ETag, or Content-Length differs from the last copy."""
    if latest is None:
        return True
    saw = False
    last_modified = headers.get("last-modified")
    etag = headers.get("etag")
    length = headers.get("content-length")
    if last_modified is not None:
        saw = True
        if (latest.last_modified or "") != last_modified:
            return True
    if etag is not None:
        saw = True
        if (latest.etag or "") != etag:
            return True
    if length is not None:
        saw = True
        try:
            if int(length) != int(latest.size if latest.size is not None else -1):
                return True
        except ValueError:
            return True
    return not saw


def cache_bust(url: str, now: datetime) -> str:
    ms = int(now.timestamp() * 1000)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={ms}"


class Watcher:
    def __init__(self, store: Store, client: httpx.Client, clock, *, sleeper=None, parse_pdf=classify.parse_pdf):
        self.store = store
        self.client = client
        self.clock = clock
        self.sleeper = sleeper or (lambda _seconds: None)
        self.parse_pdf = parse_pdf
        self.wayback = Wayback(store, client, clock, sleeper=self.sleeper)
        self._fail: dict[tuple[str, str], int] = {}

    def tick(self, now: datetime | None = None) -> None:
        moment = now or self.clock.now()
        for game in self.store.active_games():
            try:
                self._tick_game(game, moment)
            except Exception:
                log.warning("tick failed game=%s", game.game_id)

    def seconds_until_due(self, now: datetime) -> float:
        soonest: float | None = None
        watching = False
        for game in self.store.active_games():
            if box_interval(now, game.tip_utc, game.final_seen_utc, game.final_end_utc) is not None:
                watching = True
                soonest = _sooner(soonest, game.next_box_poll_utc, now)
            if book_interval(now, game.tip_utc, game.final_seen_utc, game.final_end_utc) is not None:
                watching = True
                soonest = _sooner(soonest, game.next_book_poll_utc, now)
        if not watching or soonest is None:
            return 30.0
        return max(1.0, soonest)

    def _tick_game(self, game: Game, now: datetime) -> None:
        # final_seen_utc is only set when we watch the status flip to final.
        # Discovering a game that is already final must not pretend we saw it now.
        game.state = state_for(now, game.tip_utc, game.final_seen_utc, game.final_end_utc)
        if game.state == "DONE":
            self.store.save_game(game)
            return
        if _due(now, game.next_box_poll_utc) and box_interval(
            now, game.tip_utc, game.final_seen_utc, game.final_end_utc
        ):
            try:
                self._poll_box(game, now)
                self._fail.pop((game.game_id, "box"), None)
            except Exception:
                game.next_box_poll_utc = now + self._backoff(game.game_id, "box")
                log.warning("box poll failed game=%s", game.game_id)
            else:
                interval = box_interval(now, game.tip_utc, game.final_seen_utc, game.final_end_utc)
                if interval is not None:
                    game.next_box_poll_utc = now + interval
            game.state = state_for(now, game.tip_utc, game.final_seen_utc, game.final_end_utc)
            if game.state == "DONE":
                self.store.save_game(game)
                return
        if _due(now, game.next_book_poll_utc) and book_interval(
            now, game.tip_utc, game.final_seen_utc, game.final_end_utc
        ):
            try:
                self._poll_book(game, now)
                self._fail.pop((game.game_id, "book"), None)
            except Exception:
                game.next_book_poll_utc = now + self._backoff(game.game_id, "book")
                log.warning("book poll failed game=%s", game.game_id)
            else:
                interval = book_interval(now, game.tip_utc, game.final_seen_utc, game.final_end_utc)
                if interval is not None:
                    game.next_book_poll_utc = now + interval
        self.store.save_game(game)

    def _poll_box(self, game: Game, now: datetime) -> None:
        response = request(self.client, "GET", BOX_URL.format(slug=game.slug), sleeper=self.sleeper)
        if response.status_code != 200:
            raise RuntimeError(f"box HTTP {response.status_code}")
        previous_status = game.game_status
        previous_text = game.game_status_text
        parsed = parse_box_score(response.text, game.slug, _date(game.date_et))
        game.game_status = parsed.game_status
        game.game_status_text = parsed.game_status_text
        if parsed.final_end_utc is not None:
            game.final_end_utc = parsed.final_end_utc
        became_final = is_final_status(parsed.game_status, parsed.game_status_text)
        saw_it_live = previous_status is not None and not is_final_status(previous_status, previous_text)
        if game.final_seen_utc is None and became_final and saw_it_live:
            game.final_seen_utc = now
        elif game.final_seen_utc is None and not became_final:
            game.tip_utc = parsed.tip_utc

    def _poll_book(self, game: Game, now: datetime) -> None:
        url = cache_bust(game.book_url, now)
        latest = self.store.latest_version(game.game_id)
        head = request(self.client, "HEAD", url, sleeper=self.sleeper)
        if head.status_code == 404:
            return
        if head.status_code not in (200, 405, 501):
            raise RuntimeError(f"book HEAD HTTP {head.status_code}")
        probe = None if head.status_code in (405, 501) else head
        if probe is not None and not headers_changed(latest, probe.headers):
            return
        got = request(self.client, "GET", url, sleeper=self.sleeper)
        if got.status_code == 404:
            return
        if got.status_code != 200:
            raise RuntimeError(f"book GET HTTP {got.status_code}")
        headers = probe.headers if probe is not None else got.headers
        length = headers.get("content-length")
        try:
            size = int(length) if length is not None else len(got.content)
        except ValueError:
            size = len(got.content)
        ingest_bytes(
            self.store,
            game,
            got.content,
            url=game.book_url,
            fetched_at=now,
            source="live",
            last_modified=headers.get("last-modified"),
            etag=headers.get("etag"),
            size=size,
            wayback=self.wayback,
            parse_pdf=self.parse_pdf,
            now=now,
        )

    def _backoff(self, game_id: str, kind: str) -> timedelta:
        count = self._fail.get((game_id, kind), 0) + 1
        self._fail[(game_id, kind)] = count
        return timedelta(seconds=min(300, 5 * (2 ** (count - 1))))


def _due(now: datetime, next_at: datetime | None) -> bool:
    return next_at is None or now >= next_at


def _sooner(soonest: float | None, moment: datetime | None, now: datetime) -> float:
    delta = 0.0 if moment is None else (moment - now).total_seconds()
    if soonest is None:
        return delta
    return min(soonest, delta)


def _date(value: str):
    from datetime import date

    return date.fromisoformat(value)
