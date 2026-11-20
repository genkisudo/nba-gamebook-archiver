"""One bounded GitHub Actions shift.

A shift uses the same poll intervals as the daemon: the book is quiet until
tip + QUIET_MINUTES, quarter 4, or Final, then every 60s (30s after Final).
Before any game is that close, the shift only refreshes the slate every five
minutes so a quarter-4 status still opens the book early, and so a shift that
starts with nothing inside its budget can exit immediately.

The process stops at DEFAULT_BUDGET (five hours) so checkout and the publish
step finish inside GitHub's six-hour job cap. The next scheduled run restores
SQLite from the gamebooks branch and continues games that are not DONE.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, datetime, timedelta

import httpx

from gbwatch.http import make_client
from gbwatch.models import Game
from gbwatch.persist import write_manifests
from gbwatch.polymarket import EventCache
from gbwatch.schedule import is_final_status
from gbwatch.store import Store
from gbwatch.timeutil import format_utc
from gbwatch.watcher import Watcher, state_for
from gbwatch.wayback import Wayback

log = logging.getLogger("gbwatch.session")

# Start the 60s/30s loop this long before tip. Slate refresh before that is
# enough to notice quarter 4; the book itself stays on the daemon's window.
POLL_LEAD = timedelta(minutes=15)

# Leave room under the six-hour Actions cap for setup and publishing.
DEFAULT_BUDGET = timedelta(hours=5)


def default_budget() -> timedelta:
    raw = os.environ.get("GBWATCH_BUDGET_SECONDS", "").strip()
    if not raw:
        return DEFAULT_BUDGET
    try:
        seconds = int(raw)
    except ValueError:
        return DEFAULT_BUDGET
    if seconds <= 0:
        return DEFAULT_BUDGET
    return timedelta(seconds=seconds)


def actionable_at(game: Game) -> datetime:
    """When the fast poll loop should be running for this game.

    The instant is in the past once the game is already in that window.
    """
    if game.final_seen_utc is not None:
        return game.final_seen_utc
    if game.final_end_utc is not None or is_final_status(game.game_status, game.game_status_text):
        return game.final_end_utc or game.tip_utc
    if _late_game(game):
        return game.tip_utc
    return game.tip_utc - POLL_LEAD


def is_actionable(game: Game, now: datetime) -> bool:
    """True when this game should be on the 60s/30s poll, not the five-minute slate refresh."""
    if game.state == "DONE":
        return False
    if _late_game(game):
        return True
    if game.final_seen_utc is not None or game.final_end_utc is not None:
        return True
    if is_final_status(game.game_status, game.game_status_text):
        return True
    return now >= game.tip_utc - POLL_LEAD


def needs_this_run(game: Game, now: datetime, deadline: datetime) -> bool:
    """True when this shift can still do useful work before it must exit.

    A game already inside the window has an actionable time in the past, which
    is before the deadline. A game the daemon would already have dropped
    (tip + 6h, or 12h after the live final, the box-score end, or discovery)
    does not keep the shift alive.
    """
    if game.state == "DONE":
        return False
    if (
        state_for(
            now, game.tip_utc, game.final_seen_utc, game.final_end_utc, game.final_discovered_utc
        )
        == "DONE"
    ):
        return False
    return actionable_at(game) <= deadline


def settle_expired(store, now: datetime) -> None:
    """Mark games the daemon would already have given up on, without HTTP."""
    for game in store.active_games():
        if (
            state_for(
                now, game.tip_utc, game.final_seen_utc, game.final_end_utc, game.final_discovered_utc
            )
            == "DONE"
        ):
            game.state = "DONE"
            store.save_game(game)


def run_session(
    config,
    *,
    client: httpx.Client | None = None,
    clock=None,
    sleep=time.sleep,
    budget: timedelta | None = None,
    deadline: datetime | None = None,
    slate_date: date | None = None,
    mode: str = "watch",
    max_ticks: int | None = None,
) -> str:
    """Watch one shift. Returns idle, complete, budget, backfill, ticks, or error."""
    from gbwatch.cli import SystemClock

    own_client = client is None
    client = client or make_client()
    clock = clock or SystemClock()
    store: Store | None = None
    outcome = "idle"
    try:
        store = Store(config.data_dir)
        if not (os.environ.get("IA_ACCESS") and os.environ.get("IA_SECRET")):
            log.warning(
                "Wayback skipped: add repository secrets IA_ACCESS and IA_SECRET to enable Save Page Now"
            )
        if mode not in {"watch", "backfill"}:
            log.error("mode must be watch or backfill")
            outcome = "error"
            return outcome
        if mode == "backfill":
            outcome = _backfill_shift(config, store, client, clock, sleep, slate_date)
            return outcome
        started = clock.now()
        shift_deadline = deadline or (started + (budget if budget is not None else default_budget()))
        log.info("actions shift deadline=%s", format_utc(shift_deadline))
        outcome = _watch_shift(
            config,
            store,
            client,
            clock,
            sleep,
            started,
            shift_deadline,
            slate_date,
            max_ticks,
        )
        return outcome
    finally:
        try:
            if store is not None:
                try:
                    write_manifests(store)
                except Exception:
                    log.exception("manifest write failed")
                    raise
                finally:
                    store.close()
        finally:
            if own_client:
                client.close()


def _backfill_shift(config, store, client, clock, sleep, slate_date: date | None) -> str:
    if slate_date is None:
        log.error("backfill requires a slate date")
        return "error"
    from gbwatch.backfill import backfill_date

    count = backfill_date(
        slate_date,
        store,
        client,
        prefixes=config.id_prefixes,
        sleeper=sleep,
        clock=clock,
    )
    log.info("backfill stored %s version(s) for %s", count, slate_date.isoformat())
    return "backfill"


def _watch_shift(
    config,
    store,
    client,
    clock,
    sleep,
    started: datetime,
    deadline: datetime,
    slate_date: date | None,
    max_ticks: int | None,
) -> str:
    from gbwatch.cli import _refresh_schedule

    http_sleeper = time.sleep if max_ticks is None else sleep
    watcher = Watcher(store, client, clock, sleeper=http_sleeper)
    cache = EventCache()
    extra = (slate_date,) if slate_date is not None else ()
    schedule_at: datetime | None = None
    ticks = 0
    polled = False
    outcome = "idle"
    while True:
        try:
            now = clock.now()
            settle_expired(store, now)
            if now >= deadline:
                outcome = _deadline_outcome(store, now, deadline, polled)
                break
            if schedule_at is None or (now - schedule_at) >= timedelta(minutes=5):
                _refresh_schedule(
                    store,
                    client,
                    config,
                    cache,
                    now,
                    watcher.sleeper,
                    started,
                    extra,
                )
                schedule_at = clock.now()
            now = clock.now()
            settle_expired(store, now)
            needed = [game for game in store.active_games() if needs_this_run(game, now, deadline)]
            if not needed:
                outcome = "complete" if polled else "idle"
                _log_idle(store, deadline, outcome)
                break
            if not any(is_actionable(game, now) for game in needed):
                delay = _wait_delay(needed, now, deadline, schedule_at)
                sleep(delay)
            else:
                watcher.tick(now)
                watcher.wayback.drain(budget_seconds=2)
                polled = True
                now = clock.now()
                settle_expired(store, now)
                if not store.active_games():
                    _flush_wayback(watcher, clock, sleep)
                    outcome = "complete"
                    break
                remaining = (deadline - clock.now()).total_seconds()
                if remaining <= 0:
                    outcome = "budget"
                    break
                try:
                    delay = min(30.0, watcher.seconds_until_due(clock.now()), remaining)
                except Exception:
                    delay = 5.0
                sleep(max(1.0, delay))
        except Exception:
            log.exception("actions iteration failed")
            sleep(5)
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            outcome = "ticks"
            break
    if outcome == "budget":
        try:
            watcher.wayback.drain(budget_seconds=2)
        except Exception:
            log.warning("wayback drain failed")
    finals = sum(1 for game in store.all_games() if _has_final(store, game))
    log.info("actions outcome=%s finals=%s active=%s", outcome, finals, len(store.active_games()))
    return outcome


def _deadline_outcome(store, now: datetime, deadline: datetime, polled: bool) -> str:
    if any(needs_this_run(game, now, deadline) for game in store.active_games()):
        return "budget"
    return "complete" if polled else "idle"


def _wait_delay(games: list[Game], now: datetime, deadline: datetime, schedule_at: datetime) -> float:
    wake = min(actionable_at(game) for game in games)
    refresh_in = 300.0 - (now - schedule_at).total_seconds()
    delay = min(
        60.0,
        max(0.0, (wake - now).total_seconds()),
        max(0.0, (deadline - now).total_seconds()),
        max(1.0, refresh_in),
    )
    return max(1.0, delay)


def _flush_wayback(watcher: Watcher, clock, sleep, *, window: timedelta = timedelta(seconds=30)) -> None:
    """Poll Save Page Now jobs that are already in flight. Do not wait out the long backoff."""
    wayback: Wayback = watcher.wayback
    if not wayback.enabled():
        return
    end = clock.now() + window
    for _ in range(15):
        if clock.now() >= end:
            return
        wayback.drain(budget_seconds=2)
        now = clock.now()
        if not watcher.store.wayback_ready(now, 10) and not watcher.store.wayback_by_status("submitted"):
            return
        sleep(2)


def _log_idle(store, deadline: datetime, outcome: str) -> None:
    if outcome != "idle":
        return
    upcoming = [
        (actionable_at(game), game.game_id)
        for game in store.all_games()
        if game.state != "DONE"
    ]
    if not upcoming:
        log.info("nothing to watch before %s", format_utc(deadline))
        return
    moment, game_id = min(upcoming)
    log.info(
        "nothing to watch before %s; next game %s opens at %s",
        format_utc(deadline),
        game_id,
        format_utc(moment),
    )


def _has_final(store, game: Game) -> bool:
    return any(version.kind == "final" for version in store.versions(game.game_id))


def _late_game(game: Game) -> bool:
    if game.period is not None and game.period >= 4:
        return True
    text = game.game_status_text or ""
    return bool(re.search(r"\bQ\s*[4-9]\b|\bOT\b|overtime", text, re.I))
