"""UTC timestamps. NBA slate dates are US/Eastern."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(moment: datetime) -> str:
    """Filesystem-safe UTC timestamp (colons are awkward in paths)."""
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def et_calendar(moment: datetime) -> date:
    return moment.astimezone(ET).date()


def format_zones(moment: datetime) -> str:
    """UTC plus Madrid and Mexico City, for the optional display zones."""
    utc = format_utc(moment)
    try:
        madrid = moment.astimezone(ZoneInfo("Europe/Madrid")).strftime("%Y-%m-%d %H:%M %Z")
        mexico = moment.astimezone(ZoneInfo("America/Mexico_City")).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return utc
    return f"{utc} (Madrid {madrid}; Mexico City {mexico})"
