"""Per-game poll loop. State lives in SQLite, so a restart continues the same watch."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from gbwatch import classify
from gbwatch.http import request
from gbwatch.ingest import ingest_bytes
from gbwatch.models import Game, Version
from gbwatch.schedule import BOX_URL, is_final_status, parse_box_score
from gbwatch.paths import resolve_stored_path
from gbwatch.store import Store, sha256_bytes
from gbwatch.timeutil import format_utc
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


def quiet_minutes() -> int:
    """No book polls until this many minutes after tip. Games run about 2h15m."""
    raw = os.environ.get("QUIET_MINUTES", "105").strip()
    try:
        value = int(raw)
    except ValueError:
        return 105
    return value if value >= 0 else 105


def _shown_period(period: int | None, status_text: str) -> int | None:
    if period is not None:
        return period
    match = re.search(r"\bQ\s*([1-9])\b", status_text or "", re.IGNORECASE)
    if match:
        return int(match.group(1))
    if re.search(r"\bOT\b|overtime", status_text or "", re.IGNORECASE):
        return 5
    return None


def _book_window_open(
    now: datetime,
    tip: datetime,
    *,
    status: int | None,
    status_text: str,
    period: int | None,
) -> bool:
    """True from tip+QUIET_MINUTES, or from Q4, whichever comes first.

    A listed tip that is still pre-game 30 minutes later is stale. Stay quiet
    until the game is actually on and one of those same triggers hits.
    """
    shown = _shown_period(period, status_text)
    if shown is not None and shown >= 4:
        return True
    if status == 1 and now >= tip + timedelta(minutes=30):
        return False
    return now >= tip + timedelta(minutes=quiet_minutes())


def book_interval(
    now: datetime,
    tip: datetime,
    final_seen: datetime | None,
    final_end: datetime | None = None,
    *,
    status: int | None = None,
    status_text: str = "",
    period: int | None = None,
) -> timedelta | None:
    """How long to wait after a book poll, or None when the book is not polled."""
    anchor = _final_anchor(final_seen, final_end)
    if anchor is not None or is_final_status(status, status_text):
        if anchor is not None and now >= anchor + timedelta(hours=12):
            return None
        return timedelta(seconds=30)
    if now >= tip + timedelta(hours=6):
        return None
    if not _book_window_open(now, tip, status=status, status_text=status_text, period=period):
        return None
    return timedelta(seconds=60)


def box_interval(
    now: datetime,
    tip: datetime,
    final_seen: datetime | None,
    final_end: datetime | None = None,
    *,
    status: int | None = None,
    status_text: str = "",
    period: int | None = None,
) -> timedelta | None:
    if _final_anchor(final_seen, final_end) is not None or is_final_status(status, status_text):
        return None
    if now >= tip + timedelta(hours=6):
        return None
    if _book_window_open(now, tip, status=status, status_text=status_text, period=period):
        return timedelta(seconds=60)
    return timedelta(minutes=10)


def headers_changed(latest: Version | None, headers) -> bool:
    """True when Last-Modified, ETag, or Content-Length differs from the last copy."""
    if latest is None or latest.kind == "unknown":
        return True
    if not (latest.last_modified or latest.etag or latest.size):
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
            if box_interval(now, game.tip_utc, game.final_seen_utc, game.final_end_utc, **_pace(game)) is not None:
                watching = True
                soonest = _sooner(soonest, game.next_box_poll_utc, now)
            if book_interval(now, game.tip_utc, game.final_seen_utc, game.final_end_utc, **_pace(game)) is not None:
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
            now, game.tip_utc, game.final_seen_utc, game.final_end_utc, **_pace(game)
        ):
            try:
                self._poll_box(game, now)
                self._fail.pop((game.game_id, "box"), None)
            except Exception:
                game.next_box_poll_utc = now + self._backoff(game, "box")
                log.warning("box poll failed game=%s", game.game_id)
            else:
                interval = box_interval(now, game.tip_utc, game.final_seen_utc, game.final_end_utc, **_pace(game))
                if interval is not None:
                    game.next_box_poll_utc = now + interval
            game.state = state_for(now, game.tip_utc, game.final_seen_utc, game.final_end_utc)
            if game.state == "DONE":
                self.store.save_game(game)
                return
        if _due(now, game.next_book_poll_utc) and book_interval(
            now, game.tip_utc, game.final_seen_utc, game.final_end_utc, **_pace(game)
        ):
            try:
                self._poll_book(game, now)
                self._fail.pop((game.game_id, "book"), None)
            except Exception:
                game.next_book_poll_utc = now + self._backoff(game, "book")
                log.warning("book poll failed game=%s", game.game_id)
            else:
                if game.state != "DONE":
                    interval = book_interval(
                        now, game.tip_utc, game.final_seen_utc, game.final_end_utc, **_pace(game)
                    )
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
        if parsed.period is not None:
            game.period = parsed.period
        became_final = is_final_status(parsed.game_status, parsed.game_status_text)
        saw_it_live = previous_status is not None and not is_final_status(previous_status, previous_text)
        if game.final_seen_utc is None and became_final and saw_it_live:
            game.final_seen_utc = now
        elif game.final_seen_utc is None and not became_final:
            game.tip_utc = parsed.tip_utc

    def _poll_book(self, game: Game, now: datetime) -> None:
        url = cache_bust(game.book_url, now)
        latest = self.store.latest_version(game.game_id)
        head = request(self.client, "HEAD", url, sleeper=self.sleeper, timeout=10.0, retries=1)
        if head.status_code == 404:
            return
        if head.status_code not in (200, 405, 501):
            raise RuntimeError(f"book HEAD HTTP {head.status_code}")
        probe = None if head.status_code in (405, 501) else head
        if probe is not None and not headers_changed(latest, probe.headers):
            return
        got = request(self.client, "GET", url, sleeper=self.sleeper, timeout=10.0, retries=1)
        fetched_at = self.clock.now()
        if got.status_code == 404:
            return
        if got.status_code != 200:
            raise RuntimeError(f"book GET HTTP {got.status_code}")
        # Store the GET headers. HEAD is only the change check and can disagree.
        headers = got.headers
        length = headers.get("content-length")
        try:
            size = int(length) if length is not None else len(got.content)
        except ValueError:
            size = len(got.content)
        if not got.content.startswith(b"%PDF"):
            log.warning("book response is not a pdf game=%s", game.game_id)
            return
        digest = sha256_bytes(got.content)
        kind = self._kind(got.content, game.game_id)
        post_final = game.final_seen_utc is not None or is_final_status(
            game.game_status, game.game_status_text
        )
        existing = self.store.version_by_hash(game.game_id, digest)
        if existing is not None:
            self._revisit(game, existing, kind, got.content, headers, size, fetched_at, digest)
            return
        if kind == "unknown":
            # No ETag / Last-Modified / size, so the next HEAD still misses and we retry.
            if post_final:
                self._save_pdf(game, got.content, digest, fetched_at, "unknown", None, None, None)
            return
        if kind != "final":
            if post_final:
                self._save_pdf(
                    game,
                    got.content,
                    digest,
                    fetched_at,
                    "in_game",
                    headers.get("last-modified"),
                    headers.get("etag"),
                    size,
                )
            else:
                self.store.insert_version(
                    game_id=game.game_id,
                    url=game.book_url,
                    fetched_utc=format_utc(fetched_at),
                    last_modified=headers.get("last-modified"),
                    etag=headers.get("etag"),
                    size=size,
                    sha256=digest,
                    kind="in_game",
                    path="",
                    source="live",
                )
            return
        ingest_bytes(
            self.store,
            game,
            got.content,
            url=game.book_url,
            fetched_at=fetched_at,
            source="live",
            last_modified=headers.get("last-modified"),
            etag=headers.get("etag"),
            size=size,
            wayback=self.wayback,
            parse_pdf=self.parse_pdf,
            now=fetched_at,
        )
        game.state = "DONE"

    def _revisit(self, game, existing, kind, body, headers, size, fetched_at, digest) -> None:
        if existing.kind == "unknown" and kind == "unknown":
            return
        if kind == "final" and existing.kind != "final":
            resolved = resolve_stored_path(self.store.data_dir, existing.path)
            if not existing.path or not resolved.is_file():
                path = str(self.store.pdf_path(game, fetched_at, digest))
                Path(path).write_bytes(body)
            else:
                path = str(resolved)
            self.store.update_version(
                existing.id,
                kind="final",
                path=path,
                last_modified=headers.get("last-modified"),
                etag=headers.get("etag"),
                size=size,
            )
            from gbwatch.ingest import record_final_alert

            record_final_alert(self.store, game, self.store.get_version(existing.id), fetched_at)
            if self.wayback.enabled():
                self.wayback.enqueue(existing.id, game.book_url)
            game.state = "DONE"
            return
        if existing.kind == "final":
            game.state = "DONE"
            return
        if existing.kind != "unknown":
            self.store.touch_validators(
                existing.id, headers.get("last-modified"), headers.get("etag"), size
            )

    def _save_pdf(self, game, body, digest, fetched_at, kind, last_modified, etag, size) -> None:
        path = self.store.pdf_path(game, fetched_at, digest)
        path.write_bytes(body)
        self.store.insert_version(
            game_id=game.game_id,
            url=game.book_url,
            fetched_utc=format_utc(fetched_at),
            last_modified=last_modified,
            etag=etag,
            size=size,
            sha256=digest,
            kind=kind,
            path=str(path),
            source="live",
        )

    def _kind(self, body: bytes, game_id: str) -> str:
        handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        path = Path(handle.name)
        try:
            handle.write(body)
            handle.close()
            return self.parse_pdf(path).kind
        except Exception:
            log.warning("book parse failed game=%s", game_id)
            return "unknown"
        finally:
            path.unlink(missing_ok=True)

    def _backoff(self, game: Game, kind: str) -> timedelta:
        count = self._fail.get((game.game_id, kind), 0) + 1
        self._fail[(game.game_id, kind)] = count
        cap = 60 if kind == "book" and (
            game.final_seen_utc is not None
            or is_final_status(game.game_status, game.game_status_text)
        ) else 300
        return timedelta(seconds=min(cap, 5 * (2 ** (count - 1))))


def _pace(game: Game) -> dict:
    return {"status": game.game_status, "status_text": game.game_status_text, "period": game.period}


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
