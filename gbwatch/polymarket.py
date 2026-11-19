"""Keep games that have an open Polymarket NBA player-prop market."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

import httpx

from gbwatch.http import request
from gbwatch.models import Game

log = logging.getLogger("gbwatch.polymarket")

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"

# Question shape from Gamma: "Cedric Coward: Points O/U 16.5".
# Threes are written "3-Pointers Made" on the live API; "3PM" is accepted too.
PROP_RE = re.compile(
    r":\s*(?:Points|Rebounds|Assists|Steals|Blocks|3PM|3-Pointers Made)\b.*\bO/U\b",
    re.IGNORECASE,
)

# City and nickname, lowercased, for title matching.
TEAMS: dict[str, tuple[str, ...]] = {
    "ATL": ("atlanta", "hawks"),
    "BOS": ("boston", "celtics"),
    "BKN": ("brooklyn", "nets"),
    "CHA": ("charlotte", "hornets"),
    "CHI": ("chicago", "bulls"),
    "CLE": ("cleveland", "cavaliers"),
    "DAL": ("dallas", "mavericks"),
    "DEN": ("denver", "nuggets"),
    "DET": ("detroit", "pistons"),
    "GSW": ("golden state", "warriors"),
    "HOU": ("houston", "rockets"),
    "IND": ("indiana", "pacers"),
    "LAC": ("la clippers", "clippers"),
    "LAL": ("los angeles l", "lakers"),
    "MEM": ("memphis", "grizzlies"),
    "MIA": ("miami", "heat"),
    "MIL": ("milwaukee", "bucks"),
    "MIN": ("minnesota", "timberwolves"),
    "NOP": ("new orleans", "pelicans"),
    "NYK": ("new york", "knicks"),
    "OKC": ("oklahoma city", "thunder"),
    "ORL": ("orlando", "magic"),
    "PHI": ("philadelphia", "76ers", "sixers"),
    "PHX": ("phoenix", "suns"),
    "POR": ("portland", "trail blazers", "blazers"),
    "SAC": ("sacramento", "kings"),
    "SAS": ("san antonio", "spurs"),
    "TOR": ("toronto", "raptors"),
    "UTA": ("utah", "jazz"),
    "WAS": ("washington", "wizards"),
}


def is_player_prop(question: str) -> bool:
    return bool(PROP_RE.search(question or ""))


def event_has_player_prop(event: dict) -> bool:
    for market in event.get("markets") or []:
        if is_player_prop(market.get("question") or ""):
            return True
    return False


def event_slug_for(game: Game) -> str:
    return f"nba-{game.away.lower()}-{game.home.lower()}-{game.date_et}"


def event_matches_game(event: dict, game: Game) -> bool:
    slug = (event.get("slug") or "").lower()
    if slug == event_slug_for(game):
        return True
    if _event_date(event) != game.date_et:
        return False
    blob = f"{event.get('title') or ''} {slug}".lower()
    return _mentioned(game.away, blob) and _mentioned(game.home, blob)


def select_games(games: list[Game], mode: str, events: list[dict] | None = None) -> list[Game]:
    """mode is 'all', 'polymarket', or a comma-separated list of game ids."""
    mode = (mode or "polymarket").strip()
    if mode == "all":
        return list(games)
    if mode != "polymarket":
        wanted = {part.strip() for part in mode.split(",") if part.strip()}
        return [game for game in games if game.game_id in wanted]
    events = events or []
    kept = []
    for game in games:
        for event in events:
            if event_matches_game(event, game) and event_has_player_prop(event):
                kept.append(game)
                break
    return kept


class EventCache:
    """Gamma responses, refreshed every 30 minutes while a game is still pre-tip."""

    def __init__(self) -> None:
        self.fetched_at: datetime | None = None
        self.events: list[dict] = []

    def get(self, now: datetime, client: httpx.Client, games: list[Game], *, sleeper=None) -> list[dict]:
        waiting = any(game.tip_utc > now for game in games)
        stale = self.fetched_at is None or (now - self.fetched_at) >= timedelta(minutes=30)
        if waiting and stale:
            self.events = fetch_events(client, games, sleeper=sleeper)
            self.fetched_at = now
        elif self.fetched_at is None:
            self.events = fetch_events(client, games, sleeper=sleeper)
            self.fetched_at = now
        return self.events


def fetch_events(client: httpx.Client, games: list[Game], *, sleeper=None) -> list[dict]:
    """Spec endpoint, narrowed to the daily NBA series so futures don't crowd it out.

    The bare `tag_slug=nba&closed=false` listing is futures-first. Game events for a
    calendar year live under series_slug `nba-{year}` (confirmed for 2026-11-18).
    Any slate game missing from that page is loaded by its own slug.
    """
    kwargs = {"sleeper": sleeper} if sleeper else {}
    events: list[dict] = []
    seen: set[str] = set()
    ok = False
    years = sorted({game.date_et[:4] for game in games})
    for year in years:
        params = {
            "tag_slug": "nba",
            "closed": "false",
            "limit": "500",
            "series_slug": f"nba-{year}",
        }
        try:
            response = request(client, "GET", GAMMA_EVENTS, params=params, timeout=10.0, retries=1, **kwargs)
        except httpx.HTTPError as exc:
            log.warning("gamma series fetch failed: %s", type(exc).__name__)
            continue
        if response.status_code != 200:
            log.warning("gamma series HTTP %s", response.status_code)
            continue
        ok = True
        for event in _as_events(response.json()):
            slug = event.get("slug") or ""
            if slug not in seen:
                seen.add(slug)
                events.append(event)
    for game in games:
        expected = event_slug_for(game)
        if expected in seen:
            continue
        try:
            response = request(
                client, "GET", GAMMA_EVENTS, params={"slug": expected}, timeout=10.0, retries=1, **kwargs
            )
        except httpx.HTTPError as exc:
            log.warning("gamma slug fetch failed: %s", type(exc).__name__)
            continue
        if response.status_code != 200:
            continue
        ok = True
        for event in _as_events(response.json()):
            slug = event.get("slug") or ""
            if slug and slug not in seen:
                seen.add(slug)
                events.append(event)
    if not ok and not events:
        raise RuntimeError("gamma unavailable")
    return events


def _as_events(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        inner = payload.get("events") or payload.get("data") or []
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict)]
        if payload.get("slug"):
            return [payload]
    return []


def _event_date(event: dict) -> str | None:
    raw = event.get("eventDate")
    if isinstance(raw, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    slug = event.get("slug") or ""
    match = re.search(r"(\d{4}-\d{2}-\d{2})$", slug)
    if match:
        return match.group(1)
    return None


def _mentioned(tricode: str, blob: str) -> bool:
    names = TEAMS.get(tricode.upper(), (tricode.lower(),))
    return any(name in blob for name in names)
