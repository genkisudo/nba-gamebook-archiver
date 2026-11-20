"""Diff a later final book against the first final book."""

from __future__ import annotations

from typing import NamedTuple

STAT_ORDER = [
    "MIN",
    "FG",
    "FGA",
    "3P",
    "3PA",
    "FT",
    "FTA",
    "OR",
    "DR",
    "TOT",
    "A",
    "PF",
    "ST",
    "TO",
    "BS",
    "+/-",
    "PTS",
    "DNP",
]


class Change(NamedTuple):
    player: str
    stat: str
    old: object
    new: object

    def as_dict(self) -> dict:
        return {"player": self.player, "stat": self.stat, "old": self.old, "new": self.new}


def diff_stats(old: dict, new: dict) -> list[Change]:
    changes: list[Change] = []
    for player in sorted(set(old) | set(new)):
        before = old.get(player) or {}
        after = new.get(player) or {}
        stats = set(before) | set(after)
        for stat in sorted(stats, key=_stat_key):
            old_value = before.get(stat)
            new_value = after.get(stat)
            if old_value != new_value:
                changes.append(Change(player, stat, old_value, new_value))
    return changes


def _stat_key(stat: str) -> tuple:
    try:
        return (0, STAT_ORDER.index(stat))
    except ValueError:
        return (1, stat)
