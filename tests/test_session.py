"""Bounded Actions shifts: when a run has work, when it waits, and when it resumes."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from gbwatch.cli import main
from gbwatch.config import Config
from gbwatch.models import Game
from gbwatch.schedule import book_url
from gbwatch.session import (
    DEFAULT_BUDGET,
    POLL_LEAD,
    actionable_at,
    is_actionable,
    needs_this_run,
    run_session,
    settle_expired,
)
from gbwatch.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROOT = Path(__file__).resolve().parent.parent
PDF = (FIXTURES / "MEMCLE_final.pdf").read_bytes()
TIP = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now += timedelta(seconds=seconds)


def _game(**overrides) -> Game:
    game = Game(
        game_id="0022600258",
        date_et="2026-11-18",
        away="MEM",
        home="CLE",
        slug="mem-vs-cle-0022600258",
        book_url=book_url(datetime(2026, 11, 18).date(), "MEM", "CLE"),
        tip_utc=TIP,
        state="SCHEDULED",
        game_status=1,
        game_status_text="7:00 pm ET",
    )
    for key, value in overrides.items():
        setattr(game, key, value)
    return game


def _offline(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="offline")


def test_window_opens_at_the_lead_quarter_four_or_final():
    now = TIP - timedelta(hours=3)
    scheduled = _game(tip_utc=TIP)
    assert actionable_at(scheduled) == TIP - POLL_LEAD
    assert not is_actionable(scheduled, now)
    assert not needs_this_run(scheduled, now, TIP - POLL_LEAD - timedelta(seconds=1))
    assert needs_this_run(scheduled, now, TIP - POLL_LEAD)

    early = _game(tip_utc=TIP, period=4, game_status=2, game_status_text="Q4 6:00")
    assert is_actionable(early, TIP + timedelta(minutes=90))
    overtime = _game(tip_utc=TIP, game_status=2, game_status_text="OT 1:00")
    assert is_actionable(overtime, TIP + timedelta(minutes=90))

    final = _game(
        tip_utc=TIP,
        game_status=3,
        game_status_text="Final",
        final_seen_utc=TIP + timedelta(minutes=40),
        state="FINAL_SEEN",
    )
    assert is_actionable(final, TIP + timedelta(minutes=40))
    assert needs_this_run(final, TIP + timedelta(minutes=40), TIP + timedelta(hours=2))
    done = _game(state="DONE")
    assert not needs_this_run(done, now, now + timedelta(hours=5))


def test_settle_expired_drops_only_games_the_daemon_would_drop(tmp_path):
    store = Store(tmp_path)
    now = TIP + timedelta(hours=8)
    stale = _game(state="LIVE")
    store.save_game(stale)
    fresh = _game(
        game_id="0022600999",
        away="BOS",
        home="LAL",
        state="FINAL_SEEN",
        final_seen_utc=now - timedelta(hours=1),
        game_status=3,
        game_status_text="Final",
    )
    store.save_game(fresh)
    settle_expired(store, now)
    assert store.get_game(stale.game_id).state == "DONE"
    assert store.get_game(fresh.game_id).state == "FINAL_SEEN"
    fresh.final_seen_utc = now - timedelta(hours=12)
    store.save_game(fresh)
    settle_expired(store, now)
    assert store.get_game(fresh.game_id).state == "DONE"


def test_discovered_final_without_an_end_time_lasts_twelve_hours(tmp_path):
    """Already final at discovery, and the box score has no actualEndTimeUTC.

    The daemon keeps that game for 12h after discovery. Dropping it at tip + 6h
    ends the shift while the watcher would still be polling the book.
    """
    discovered = TIP + timedelta(hours=5)
    game = _game(
        state="FINAL_SEEN",
        game_status=3,
        game_status_text="Final",
        final_seen_utc=None,
        final_end_utc=None,
        final_discovered_utc=discovered,
    )
    # Past tip + 6h, still inside 12h of discovery.
    now = TIP + timedelta(hours=7)
    assert needs_this_run(game, now, now + timedelta(hours=1))
    store = Store(tmp_path)
    store.save_game(game)
    settle_expired(store, now)
    assert store.get_game(game.game_id).state == "FINAL_SEEN"
    later = discovered + timedelta(hours=12)
    assert not needs_this_run(game, later, later + timedelta(hours=1))
    settle_expired(store, later)
    assert store.get_game(game.game_id).state == "DONE"


def test_shift_with_nothing_in_budget_exits_without_waiting(tmp_path, monkeypatch):
    monkeypatch.delenv("IA_ACCESS", raising=False)
    monkeypatch.delenv("IA_SECRET", raising=False)
    now = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
    store = Store(tmp_path)
    # Quiet window is 23:45 UTC, after a five-hour shift that starts at 15:00.
    store.save_game(_game(tip_utc=datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc), state="SCHEDULED"))
    store.close()
    slept = []
    clock = Clock(now)
    outcome = run_session(
        Config(data_dir=tmp_path, game_filter="all"),
        client=httpx.Client(transport=httpx.MockTransport(_offline)),
        clock=clock,
        sleep=lambda seconds: slept.append(seconds),
        deadline=now + DEFAULT_BUDGET,
    )
    assert outcome == "idle"
    assert slept == []


def test_shift_waits_until_the_lead_without_polling_the_book(tmp_path, monkeypatch):
    monkeypatch.delenv("IA_ACCESS", raising=False)
    monkeypatch.delenv("IA_SECRET", raising=False)
    now = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
    books = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "statsdmz" in str(request.url):
            books.append(request.method)
        return httpx.Response(500, text="offline")

    store = Store(tmp_path)
    store.save_game(_game(tip_utc=now + timedelta(minutes=40), state="SCHEDULED"))
    store.close()
    clock = Clock(now)

    def sleep(seconds):
        clock.advance(seconds)

    outcome = run_session(
        Config(data_dir=tmp_path, game_filter="all"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock,
        sleep=sleep,
        deadline=now + timedelta(hours=3),
        max_ticks=3,
    )
    assert outcome == "ticks"
    assert books == []
    assert clock.now() == now + timedelta(minutes=3)


def test_book_stays_quiet_until_tip_plus_105_then_polls(tmp_path, monkeypatch):
    monkeypatch.delenv("IA_ACCESS", raising=False)
    monkeypatch.delenv("IA_SECRET", raising=False)
    books = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "statsdmz" in str(request.url):
            books.append(request.method)
            return httpx.Response(404)
        return httpx.Response(500, text="offline")

    store = Store(tmp_path)
    store.save_game(_game(state="LIVE", game_status=2, game_status_text="Q3 5:00"))
    store.close()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    early = Clock(TIP + timedelta(minutes=104))
    assert (
        run_session(
            Config(data_dir=tmp_path, game_filter="all"),
            client=client,
            clock=early,
            sleep=lambda _seconds: None,
            deadline=early.now() + timedelta(hours=1),
            max_ticks=1,
        )
        == "ticks"
    )
    assert books == []
    later = Clock(TIP + timedelta(minutes=105))
    run_session(
        Config(data_dir=tmp_path, game_filter="all"),
        client=client,
        clock=later,
        sleep=lambda _seconds: None,
        deadline=later.now() + timedelta(hours=1),
        max_ticks=1,
    )
    assert books == ["HEAD"]


def test_budget_exit_resumes_and_keeps_only_the_first_final(tmp_path, monkeypatch):
    monkeypatch.delenv("IA_ACCESS", raising=False)
    monkeypatch.delenv("IA_SECRET", raising=False)
    phase = {"pdf": False}
    gets = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "statsdmz" not in url:
            return httpx.Response(500, text="offline")
        if request.method == "GET":
            gets.append(url)
            if not phase["pdf"]:
                return httpx.Response(404)
            return httpx.Response(
                200,
                content=PDF,
                headers={"ETag": '"final"', "Last-Modified": "Thu, 19 Nov 2026 02:10:00 GMT"},
            )
        if not phase["pdf"]:
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={
                "ETag": '"final"',
                "Last-Modified": "Thu, 19 Nov 2026 02:10:00 GMT",
                "Content-Length": str(len(PDF)),
            },
        )

    start = datetime(2026, 11, 19, 2, 0, tzinfo=timezone.utc)
    store = Store(tmp_path)
    store.save_game(
        _game(
            state="FINAL_SEEN",
            game_status=3,
            game_status_text="Final",
            final_seen_utc=start,
        )
    )
    store.close()
    clock = Clock(start)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = Config(data_dir=tmp_path, game_filter="all")

    def sleep(seconds):
        clock.advance(seconds)

    assert (
        run_session(config, client=client, clock=clock, sleep=sleep, deadline=start + timedelta(seconds=50))
        == "budget"
    )
    mid = Store(tmp_path)
    assert mid.versions("0022600258") == []
    assert mid.get_game("0022600258").state != "DONE"
    mid.close()
    assert gets == []

    phase["pdf"] = True
    assert run_session(config, client=client, clock=clock, sleep=sleep, deadline=clock.now() + timedelta(hours=1)) == (
        "complete"
    )
    done = Store(tmp_path)
    versions = done.versions("0022600258")
    assert [row.kind for row in versions] == ["final"]
    assert done.get_game("0022600258").state == "DONE"
    assert len(gets) == 1
    done.close()

    def explode(request: httpx.Request) -> httpx.Response:
        if "statsdmz" in str(request.url):
            raise AssertionError("book polled after the first final")
        return httpx.Response(500, text="offline")

    assert (
        run_session(
            config,
            client=httpx.Client(transport=httpx.MockTransport(explode)),
            clock=clock,
            sleep=sleep,
            deadline=clock.now() + timedelta(hours=1),
        )
        == "idle"
    )
    assert len(gets) == 1


def test_backfill_mode_requires_a_date_and_does_not_poll_forever(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("IA_ACCESS", raising=False)
    monkeypatch.delenv("IA_SECRET", raising=False)
    assert main(["actions", "--date", "tomorrow"]) == 2
    assert main(["actions", "--mode", "backfill"]) == 2

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    outcome = run_session(
        Config(data_dir=tmp_path, game_filter="all"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=Clock(TIP),
        sleep=lambda _seconds: None,
        mode="backfill",
        slate_date=TIP.date(),
    )
    assert outcome == "backfill"


def test_schedule_covers_early_tips_and_late_endings_including_delays_and_drops():
    """A runner must be up from the poll lead through 20 minutes after a 06:30 UTC ending.

    Runs that cannot reach that window exit in a couple of minutes. A run that
    overlaps it stays until its budget or the window ends. Concurrency keeps
    one pending run, which starts as soon as the current one exits.
    """
    text = (ROOT / ".github" / "workflows" / "watch.yml").read_text()
    assert "cancel-in-progress: false" in text
    assert "concurrency:" in text
    assert "secrets.IA_ACCESS" in text
    assert "secrets.IA_SECRET" in text
    assert "set -x" not in text
    assert "echo " not in text
    assert 'timeout-minutes: 340' in text
    assert "workflow_dispatch:" in text
    minutes = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- cron:"):
            continue
        expr = stripped.split(":", 1)[1].strip().strip("'\"")
        minutes |= _expand_cron(expr)
    budget = int(DEFAULT_BUDGET.total_seconds() // 60)
    assert budget == 300
    assert 340 > budget
    # Weekend noon ET (17:00 UTC in winter, 16:00 UTC in summer) through a
    # 06:30 UTC ending, plus 20 minutes for the book to land.
    weekend = (1 * 1440 + 16 * 60 + 45, 2 * 1440 + 6 * 60 + 50)
    # 7:00 pm ET tip (00:00 UTC) through the same late ending.
    weeknight = (23 * 60 + 45, 1 * 1440 + 6 * 60 + 50)
    for start, end in (weekend, weeknight):
        assert _gaps(minutes, budget, start, end, delay=0) == []
        assert _gaps(minutes, budget, start, end, delay=45) == []
        for dropped in sorted(minutes):
            kept = minutes - {dropped}
            missing = _gaps(kept, budget, start, end, delay=0)
            assert missing == [], f"dropping {dropped // 60:02d}:{dropped % 60:02d} leaves {missing[:3]}"


def _expand_cron(expr: str) -> set[int]:
    minute, hour, dom, month, dow = expr.split()
    assert dom == month == dow == "*"
    return {item * 60 + bit for item in _field(hour, 23) for bit in _field(minute, 59)}


def _field(field: str, hi: int) -> set[int]:
    values = set()
    for part in field.split(","):
        if "-" in part:
            start, stop = part.split("-")
            values.update(range(int(start), int(stop) + 1))
        else:
            values.add(int(part))
    assert values and min(values) >= 0 and max(values) <= hi
    return values


def _gaps(minutes: set[int], budget: int, required_start: int, required_end: int, delay: int) -> list[int]:
    fires = {day * 1440 + minute + delay for day in range(4) for minute in minutes}
    missing = []
    running_until = -10**9
    pending = None
    covered = set()
    for minute in range(4 * 1440):
        if minute in fires:
            if minute < running_until:
                pending = minute
            else:
                pending = None
                running_until = minute + _duration(minute, required_start, required_end, budget)
        elif pending is not None and minute >= running_until:
            pending = None
            running_until = minute + _duration(minute, required_start, required_end, budget)
        if minute < running_until:
            covered.add(minute)
    for minute in range(required_start, required_end):
        if minute not in covered:
            missing.append(minute)
    return missing


def _duration(start: int, required_start: int, required_end: int, budget: int) -> int:
    if start >= required_end or start + budget < required_start:
        return 2
    if required_end <= start + budget:
        return max(2, required_end - start)
    return budget
