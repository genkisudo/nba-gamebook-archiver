"""gbwatch command line."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from gbwatch.backfill import backfill_date
from gbwatch.config import load_config
from gbwatch.ingest import ensure_matchup, ingest_bytes, ingest_text
from gbwatch.http import make_client
from gbwatch.notify import render_diff, render_show, render_summary
from gbwatch.polymarket import EventCache, select_games
from gbwatch.redact import install_redaction
from gbwatch.schedule import get_games
from gbwatch.store import Store
from gbwatch.timeutil import et_calendar, parse_utc, utcnow
from gbwatch.watcher import Watcher, discovered_too_late, state_for

log = logging.getLogger("gbwatch")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def cli() -> None:
    sys.exit(main())


def main(argv: list[str] | None = None) -> int:
    install_redaction()
    parser = argparse.ArgumentParser(prog="gbwatch", description="Archive NBA official game books.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="poll the slate until each game is 12h past final")

    back = sub.add_parser("backfill", help="download Wayback copies for one ET date")
    back.add_argument("date", help="YYYY-MM-DD")

    show = sub.add_parser("show", help="print the first final and later diffs")
    show.add_argument("game", help="game id, or AWAYHOME such as MEMCLE")
    show.add_argument("date", nargs="?", help="ET date, required with AWAYHOME")

    diff = sub.add_parser("diff", help="print stat diffs versus the first final")
    diff.add_argument("game_id")

    alerts = sub.add_parser("alerts", help="print unacked alerts")
    alerts.add_argument("--since", type=int, default=0, help="only ids greater than this")

    ack = sub.add_parser("ack", help="mark an alert as read")
    ack.add_argument("id", type=int)

    summary = sub.add_parser("summary", help="markdown table for one ET date")
    summary.add_argument("date")

    imported = sub.add_parser("import", help="store a PDF or pdftotext capture for one game")
    imported.add_argument("--game", required=True, help="AWAYHOME, for example WASTOR")
    imported.add_argument("--date", required=True, help="ET game date YYYYMMDD")
    imported.add_argument("--fetched-utc", required=True, help="when this copy was fetched, UTC ISO-8601")
    imported.add_argument("--source", required=True, choices=("otb", "wayback", "manual"))
    body = imported.add_mutually_exclusive_group(required=True)
    body.add_argument("--pdf", help="path to a game-book PDF")
    body.add_argument("--text", help="path to pdftotext-style book text")
    imported.add_argument("--note", default=None, help="optional note stored with the version")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config()
    if args.cmd == "run":
        serve(config)
        return 0
    store = Store(config.data_dir)
    try:
        if args.cmd == "backfill":
            return _backfill(config, store, _parse_date(args.date))
        if args.cmd == "show":
            game = _resolve(store, args.game, args.date)
            if game is None:
                print("game not found", file=sys.stderr)
                return 1
            sys.stdout.write(render_show(store, game))
            return 0
        if args.cmd == "diff":
            game = store.get_game(args.game_id)
            if game is None:
                print("game not found", file=sys.stderr)
                return 1
            sys.stdout.write(render_diff(store, game))
            return 0
        if args.cmd == "alerts":
            for alert in store.unread_alerts(args.since):
                print(json.dumps(alert, separators=(",", ":")))
            return 0
        if args.cmd == "ack":
            if store.ack(args.id, utcnow()):
                print(f"acked {args.id}")
                return 0
            print(f"no such alert {args.id}", file=sys.stderr)
            return 1
        if args.cmd == "summary":
            sys.stdout.write(render_summary(store, args.date))
            return 0
        if args.cmd == "import":
            return _import(store, args)
    finally:
        store.close()
    return 2


def serve(config, *, client: httpx.Client | None = None, clock=None, sleep=time.sleep, max_ticks: int | None = None) -> None:
    """Long-lived poll. Tests pass max_ticks and a fake clock."""
    own_client = client is None
    client = client or make_client()
    clock = clock or SystemClock()
    store = Store(config.data_dir)
    # Production retries wait. A bounded test run uses the injected sleeper
    # so a fake clock can advance without a real pause.
    http_sleeper = time.sleep if max_ticks is None else sleep
    watcher = Watcher(store, client, clock, sleeper=http_sleeper)
    cache = EventCache()
    schedule_at: datetime | None = None
    started_at = clock.now()
    ticks = 0
    try:
        while True:
            now = clock.now()
            if schedule_at is None or (now - schedule_at) >= timedelta(minutes=5):
                _refresh_schedule(store, client, config, cache, now, watcher.sleeper, started_at)
                schedule_at = now
            watcher.tick(now)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            delay = min(30.0, watcher.seconds_until_due(clock.now()))
            sleep(delay)
    finally:
        store.close()
        if own_client:
            client.close()


def _refresh_schedule(store, client, config, cache: EventCache, now: datetime, sleeper, started_at: datetime) -> None:
    today = et_calendar(now)
    discovered = []
    for day in (today, today - timedelta(days=1)):
        try:
            discovered.extend(get_games(day, client, config.id_prefixes, sleeper=sleeper))
        except Exception:
            log.warning("schedule refresh failed date=%s", day.isoformat())
    try:
        if config.game_filter == "polymarket":
            events = cache.get(now, client, discovered, sleeper=sleeper)
            allowed = {game.game_id for game in select_games(discovered, "polymarket", events)}
        else:
            allowed = {game.game_id for game in select_games(discovered, config.game_filter)}
    except Exception:
        log.warning("game filter failed; keeping games already stored")
        allowed = set()
    for game in discovered:
        known = store.get_game(game.game_id)
        if known is None and game.game_id not in allowed:
            continue
        if known is None:
            # Do not stamp final_seen_utc = now. That made a book downloaded
            # hours after the game look like the first post-game version.
            if discovered_too_late(game, started_at):
                game.state = "DONE"
            else:
                game.state = state_for(now, game.tip_utc, None, game.final_end_utc)
        store.upsert_game(game)


def _backfill(config, store: Store, day: date) -> int:
    client = make_client()
    try:
        count = backfill_date(day, store, client, prefixes=config.id_prefixes, clock=SystemClock())
    finally:
        client.close()
    print(f"stored {count} new version(s) for {day.isoformat()}")
    return 0


def _resolve(store: Store, ident: str, when: str | None):
    if when:
        matchup = ident.strip().upper()
        if len(matchup) != 6 or not matchup.isalpha():
            print("matchup must look like MEMCLE", file=sys.stderr)
            return None
        return store.find_matchup(matchup[:3], matchup[3:], when)
    return store.get_game(ident)


def _parse_date(text: str) -> date:
    return date.fromisoformat(text)


def _import(store: Store, args) -> int:
    matchup = args.game.strip().upper()
    if len(matchup) != 6 or not matchup.isalpha():
        print("game must look like WASTOR", file=sys.stderr)
        return 1
    if len(args.date) != 8 or not args.date.isdigit():
        print("date must be YYYYMMDD", file=sys.stderr)
        return 1
    day = date(int(args.date[0:4]), int(args.date[4:6]), int(args.date[6:8]))
    try:
        fetched = parse_utc(args.fetched_utc)
    except ValueError:
        print("fetched-utc must be ISO-8601", file=sys.stderr)
        return 1
    game = ensure_matchup(store, matchup[:3], matchup[3:], day)
    if args.pdf:
        pdf_path = Path(args.pdf)
        if not pdf_path.is_file():
            print(f"no such file {pdf_path}", file=sys.stderr)
            return 1
        result = ingest_bytes(
            store,
            game,
            pdf_path.read_bytes(),
            url=game.book_url,
            fetched_at=fetched,
            source=args.source,
            last_modified=None,
            etag=None,
            size=None,
            now=fetched,
            note=args.note,
        )
    else:
        text_path = Path(args.text)
        if not text_path.is_file():
            print(f"no such file {text_path}", file=sys.stderr)
            return 1
        result = ingest_text(
            store,
            game,
            text_path.read_text(encoding="utf-8"),
            url=game.book_url,
            fetched_at=fetched,
            source=args.source,
            now=fetched,
            note=args.note,
        )
    print(f"{result} {game.matchup} {args.source} {format_utc_safe(fetched)}")
    return 0 if result in {"stored", "duplicate"} else 1


def format_utc_safe(moment: datetime) -> str:
    from gbwatch.timeutil import format_utc

    return format_utc(moment)
