"""Store one PDF body, classify it, and alert when a final changes."""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

from gbwatch import classify
from gbwatch.diff import diff_stats
from gbwatch.models import Game
from gbwatch.notify import emit_diff, emit_first_final
from gbwatch.schedule import book_url
from gbwatch.store import Store, sha256_bytes
from gbwatch.timeutil import ET, format_utc

log = logging.getLogger("gbwatch.ingest")


def ingest_bytes(
    store: Store,
    game: Game,
    body: bytes,
    *,
    url: str,
    fetched_at: datetime,
    source: str,
    last_modified: str | None,
    etag: str | None,
    size: int | None,
    wayback=None,
    wayback_url: str | None = None,
    parse_pdf=classify.parse_pdf,
    now: datetime | None = None,
    note: str | None = None,
) -> str:
    """Return 'stored', 'duplicate', or 'invalid'.

    A duplicate hash never creates another row. Wayback errors stay inside
    the wayback queue and do not change the return value.
    """
    if not body.startswith(b"%PDF"):
        log.warning("skipping non-pdf body game=%s", game.game_id)
        return "invalid"
    digest = sha256_bytes(body)
    existing = store.version_by_hash(game.game_id, digest)
    if existing is not None:
        latest = store.latest_version(game.game_id)
        if latest is not None and latest.sha256 == digest:
            store.touch_validators(existing.id, last_modified, etag, size if size is not None else existing.size)
        log.info("duplicate book game=%s sha=%s", game.game_id, digest[:12])
        return "duplicate"

    path = store.pdf_path(game, fetched_at, digest)
    path.write_bytes(body)
    kind = "unknown"
    try:
        parsed = parse_pdf(path)
        kind = parsed.kind
    except Exception:
        log.warning("parse failed game=%s sha=%s", game.game_id, digest[:12])
    meta = classify.pdf_metadata(path)
    stored_size = size if size is not None else len(body)
    version = store.insert_version(
        game_id=game.game_id,
        url=url,
        fetched_utc=format_utc(fetched_at),
        last_modified=last_modified,
        etag=etag,
        size=stored_size,
        sha256=digest,
        pdf_created=meta.get("pdf_created"),
        pdf_modified=meta.get("pdf_modified"),
        pages=meta.get("pages"),
        kind=kind,
        path=str(path),
        wayback_url=wayback_url,
        source=source,
        note=note,
    )
    if wayback is not None and source == "live":
        try:
            wayback.enqueue(version.id, url)
            wayback.drain()
        except Exception:
            log.warning("wayback queue failed game=%s", game.game_id)
        version = store.get_version(version.id)
    if kind == "final":
        _alert(store, game, version, parse_pdf, now or fetched_at)
    return "stored"


def ingest_text(
    store: Store,
    game: Game,
    text: str,
    *,
    url: str,
    fetched_at: datetime,
    source: str,
    now: datetime | None = None,
    note: str | None = None,
) -> str:
    """Store pdftotext-style book text. The hash is of the normalized text."""
    normalized = classify.normalize_text(text)
    digest = sha256_bytes(normalized.encode("utf-8"))
    if store.version_by_hash(game.game_id, digest) is not None:
        log.info("duplicate text game=%s sha=%s", game.game_id, digest[:12])
        return "duplicate"
    path = store.version_path(game, fetched_at, digest, ".txt")
    path.write_text(normalized, encoding="utf-8")
    try:
        parsed = classify.parse_text(normalized)
        kind = parsed.kind
    except Exception:
        log.warning("text parse failed game=%s sha=%s", game.game_id, digest[:12])
        kind = "unknown"
    version = store.insert_version(
        game_id=game.game_id,
        url=url,
        fetched_utc=format_utc(fetched_at),
        last_modified=None,
        etag=None,
        size=len(normalized.encode("utf-8")),
        sha256=digest,
        pdf_created=None,
        pdf_modified=None,
        pages=None,
        kind=kind,
        path=str(path),
        wayback_url=None,
        source=source,
        note=note,
    )
    if kind == "final":
        _alert(store, game, version, classify.parse_pdf, now or fetched_at)
    return "stored"


def ensure_matchup(store: Store, away: str, home: str, day: date) -> Game:
    """Use the stored game for this matchup, or a finished shell row for an import."""
    existing = store.find_matchup(away, home, day.isoformat())
    if existing is not None:
        return existing
    game = Game(
        game_id=f"book-{day.strftime('%Y%m%d')}-{away}{home}",
        date_et=day.isoformat(),
        away=away,
        home=home,
        slug="",
        book_url=book_url(day, away, home),
        tip_utc=datetime(day.year, day.month, day.day, tzinfo=ET),
        state="DONE",
    )
    store.save_game(game)
    return game


def _stats_of(path: Path, parse_pdf) -> dict:
    if path.suffix.lower() == ".txt":
        return classify.parse_text(path.read_text(encoding="utf-8")).stats()
    return parse_pdf(path).stats()


def _alert(store: Store, game: Game, version, parse_pdf, now: datetime) -> None:
    finals = [item for item in store.versions(game.game_id) if item.kind == "final"]
    if not finals:
        return
    created = format_utc(now)
    if finals[0].id == version.id:
        emit_first_final(store, game, version, created)
        return
    first = finals[0]
    try:
        old = _stats_of(Path(first.path), parse_pdf)
        new = _stats_of(Path(version.path), parse_pdf)
    except Exception:
        log.warning("diff parse failed game=%s", game.game_id)
        return
    changes = diff_stats(old, new)
    if changes:
        emit_diff(store, game, first, version, changes, created)
