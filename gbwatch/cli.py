"""gbwatch command line."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx

from gbwatch.backfill import backfill_date
from gbwatch.config import load_config
from gbwatch.ingest import ensure_matchup, ingest_bytes, ingest_text
from gbwatch.http import make_client
from gbwatch.notify import render_diff, render_show, render_summary
from gbwatch.polymarket import EventCache, select_games
from gbwatch.redact import install_redaction
from gbwatch.schedule import get_games, is_final_status
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

    actions = sub.add_parser(
        "actions",
        help="one GitHub Actions shift: watch until every first final is stored or the budget ends",
    )
    actions.add_argument("--date", default=None, help="extra ET slate date YYYY-MM-DD")
    actions.add_argument("--mode", choices=("watch", "backfill"), default=None)

    sub.add_parser("restore", help="copy the gamebooks branch into DATA_DIR")
    sub.add_parser("publish", help="commit DATA_DIR to the gamebooks branch and push")

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
    config = load_config()
    if args.cmd == "run":
        # The box daemon writes a rotating file. Actions logs on stderr instead,
        # which is what the workflow captures.
        _configure_run_logging(config.data_dir)
        serve(config)
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.cmd == "actions":
        return _actions(config, args)
    if args.cmd == "restore":
        return _restore(config)
    if args.cmd == "publish":
        return _publish(config)
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


def _actions(config, args) -> int:
    from gbwatch.session import run_session

    mode = (args.mode or os.environ.get("GBWATCH_MODE") or "watch").strip() or "watch"
    raw_date = args.date
    if raw_date is None:
        raw_date = os.environ.get("GBWATCH_SLATE_DATE", "")
    raw_date = (raw_date or "").strip()
    slate = None
    if raw_date:
        try:
            slate = date.fromisoformat(raw_date)
        except ValueError:
            print("date must be YYYY-MM-DD", file=sys.stderr)
            return 2
    if mode not in {"watch", "backfill"}:
        print("mode must be watch or backfill", file=sys.stderr)
        return 2
    outcome = run_session(config, slate_date=slate, mode=mode)
    if outcome == "error":
        return 2
    print(f"gbwatch actions: {outcome}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"## gbwatch\n\n- Outcome: `{outcome}`\n")
    return 0


def _restore(config) -> int:
    from gbwatch.persist import restore_data

    restore_data(_repo_root(), config.data_dir)
    return 0


def _publish(config) -> int:
    from gbwatch.persist import SecretLeak, publish_data

    try:
        publish_data(_repo_root(), config.data_dir)
    except SecretLeak as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _repo_root() -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return Path.cwd()


def _configure_run_logging(data_dir: Path) -> None:
    """Rotate the daemon log in-process. run.sh does not restart a live daemon.

    Not used for `gbwatch actions`: that command logs to stderr so the
    workflow log still receives the lines.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = str((data_dir / "gbwatch.log").resolve())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == path:
            return
    handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)


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
            try:
                now = clock.now()
                if schedule_at is None or (now - schedule_at) >= timedelta(minutes=5):
                    _refresh_schedule(store, client, config, cache, now, watcher.sleeper, started_at)
                    schedule_at = clock.now()
                watcher.tick(clock.now())
                watcher.wayback.drain(budget_seconds=2)
            except Exception:
                log.exception("serve iteration failed")
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            try:
                delay = min(30.0, watcher.seconds_until_due(clock.now()))
            except Exception:
                delay = 5.0
            sleep(delay)
    finally:
        store.close()
        if own_client:
            client.close()


def _refresh_schedule(
    store,
    client,
    config,
    cache: EventCache,
    now: datetime,
    sleeper,
    started_at: datetime,
    extra_dates: tuple[date, ...] = (),
) -> None:
    today = et_calendar(now)
    discovered = []
    deadline = time.monotonic() + 20
    skip = {game.slug for game in store.all_games() if game.state == "DONE" and game.slug}
    days: list[date] = []
    for day in (today, today - timedelta(days=1), *extra_dates):
        if day not in days:
            days.append(day)
    for day in days:
        if time.monotonic() >= deadline:
            log.warning("schedule refresh time budget exhausted")
            break
        try:
            discovered.extend(
                get_games(
                    day,
                    client,
                    config.id_prefixes,
                    sleeper=sleeper,
                    timeout=10.0,
                    retries=1,
                    deadline=deadline,
                    skip_slugs=skip,
                )
            )
        except Exception:
            log.warning("schedule refresh failed date=%s", day.isoformat())
    try:
        if config.game_filter == "polymarket":
            events = cache.get(now, client, discovered, sleeper=sleeper)
            if not events:
                log.warning("no polymarket events; watching every game on the slate")
                allowed = {game.game_id for game in discovered}
            else:
                allowed = {game.game_id for game in select_games(discovered, "polymarket", events)}
        else:
            allowed = {game.game_id for game in select_games(discovered, config.game_filter)}
    except Exception:
        log.warning("game filter failed; watching every game on the slate")
        allowed = {game.game_id for game in discovered}
    for game in discovered:
        known = store.get_game(game.game_id)
        if known is None and game.game_id not in allowed:
            continue
        finished = is_final_status(game.game_status, game.game_status_text)
        if known is None:
            if discovered_too_late(game, started_at):
                game.state = "DONE"
            else:
                # Not a live observation. final_seen_utc stays empty so lateness
                # cannot treat discovery time as the moment the game ended.
                if finished:
                    game.final_discovered_utc = now
                game.state = state_for(
                    now,
                    game.tip_utc,
                    game.final_seen_utc,
                    game.final_end_utc,
                    game.final_discovered_utc,
                )
        elif (
            finished
            and known.final_seen_utc is None
            and known.final_discovered_utc is None
            and not discovered_too_late(game, started_at)
        ):
            game.final_discovered_utc = now
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
