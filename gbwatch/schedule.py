"""Build the day's slate from nba.com and the statsdmz book URL."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime

import httpx

from gbwatch.config import DEFAULT_PREFIXES
from gbwatch.http import request
from gbwatch.models import Game
from gbwatch.timeutil import et_calendar, parse_utc

log = logging.getLogger("gbwatch.schedule")

GAMES_URL = "https://www.nba.com/games?date={date}"
BOX_URL = "https://www.nba.com/game/{slug}/box-score"


def book_url(date_et: date, away: str, home: str) -> str:
    ymd = date_et.strftime("%Y%m%d")
    return f"https://statsdmz.nba.com/pdfs/{ymd}/{ymd}_{away.upper()}{home.upper()}_book.pdf"


def teams_from_slug(slug: str) -> tuple[str, str]:
    away, _vs, home, *_rest = slug.split("-")
    return away.upper(), home.upper()


def find_slugs(html: str, prefixes: tuple[str, ...] = DEFAULT_PREFIXES) -> list[str]:
    prefix = "|".join(re.escape(item) for item in prefixes)
    pattern = re.compile(rf"\b([a-z]{{3}}-vs-[a-z]{{3}}-(?:{prefix})\d+)\b")
    found: list[str] = []
    for match in pattern.finditer(html):
        slug = match.group(1)
        if slug not in found:
            found.append(slug)
    return found


def parse_box_score(html: str, slug: str, slate: date) -> Game:
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError(f"no __NEXT_DATA__ on {slug}")
    payload = json.loads(match.group(1))
    game = payload["props"]["pageProps"]["game"]
    away = (game.get("awayTeam") or {}).get("teamTricode") or teams_from_slug(slug)[0]
    home = (game.get("homeTeam") or {}).get("teamTricode") or teams_from_slug(slug)[1]
    tip = parse_utc(game["gameTimeUTC"])
    date_et = _slate_date(game.get("gameEt"), tip, slate)
    status = game.get("gameStatus")
    status_text = game.get("gameStatusText") or ""
    ended = _actual_end(payload, game)
    if not is_final_status(int(status) if status is not None else None, status_text):
        ended = None
    return Game(
        game_id=str(game["gameId"]),
        date_et=date_et.isoformat(),
        away=away.upper(),
        home=home.upper(),
        slug=slug,
        book_url=book_url(date_et, away, home),
        tip_utc=tip,
        game_status=int(status) if status is not None else None,
        game_status_text=status_text,
        period=_period_number(game.get("period")),
        final_end_utc=ended,
    )


def _period_number(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _actual_end(payload: dict, game: dict) -> datetime | None:
    """Box-score end instant. Live pages put it on blueprintReport.game_header."""
    header = (
        (payload.get("props") or {})
        .get("pageProps", {})
        .get("blueprintReport", {})
        or {}
    )
    if isinstance(header, dict):
        header = header.get("game_header") or {}
    raw = None
    if isinstance(header, dict):
        raw = header.get("actualEndTimeUTC")
    raw = raw or game.get("actualEndTimeUTC")
    if not raw:
        return None
    try:
        return parse_utc(str(raw))
    except ValueError:
        return None


def _slate_date(game_et: str | None, tip: datetime, slate: date) -> date:
    """NBA's gameEt is an Eastern clock time that still carries a Z suffix.

    Take its calendar date. Fall back to converting gameTimeUTC into US/Eastern,
    which is the real tip instant.
    """
    if game_et:
        found = re.match(r"(\d{4}-\d{2}-\d{2})", game_et)
        if found:
            return date.fromisoformat(found.group(1))
    converted = et_calendar(tip)
    return converted or slate


def is_final_status(status: int | None, text: str | None) -> bool:
    if status == 3:
        return True
    return (text or "").startswith("Final")


def get_games(
    date_et: date,
    client: httpx.Client,
    prefixes: tuple[str, ...] = DEFAULT_PREFIXES,
    *,
    sleeper=None,
    timeout: float = 10.0,
    retries: int = 1,
    deadline: float | None = None,
    skip_slugs: set[str] | None = None,
) -> list[Game]:
    kwargs = {"sleeper": sleeper, "timeout": timeout, "retries": retries}
    if deadline is not None and time.monotonic() >= deadline:
        return []
    page = request(client, "GET", GAMES_URL.format(date=date_et.isoformat()), **kwargs)
    if page.status_code != 200:
        raise RuntimeError(f"games page HTTP {page.status_code} for {date_et.isoformat()}")
    games: list[Game] = []
    skip = skip_slugs or set()
    for slug in find_slugs(page.text, prefixes):
        if slug in skip:
            continue
        if deadline is not None and time.monotonic() >= deadline:
            log.warning("schedule refresh time budget exhausted")
            break
        box = request(client, "GET", BOX_URL.format(slug=slug), **kwargs)
        if box.status_code != 200:
            log.warning("box score HTTP %s for %s", box.status_code, slug)
            continue
        try:
            games.append(parse_box_score(box.text, slug, date_et))
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            log.warning("box score parse failed for %s: %s", slug, type(exc).__name__)
    return games
