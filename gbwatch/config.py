"""Runtime configuration. Archive.org keys stay in the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PREFIXES = ("00226", "00426", "00526")


@dataclass
class Config:
    data_dir: Path
    game_filter: str = "polymarket"
    id_prefixes: tuple[str, ...] = DEFAULT_PREFIXES

    def ia_access(self) -> str | None:
        return os.environ.get("IA_ACCESS") or None

    def ia_secret(self) -> str | None:
        return os.environ.get("IA_SECRET") or None


def load_config() -> Config:
    data = Path(os.environ.get("DATA_DIR", "./data")).expanduser().resolve()
    filt = os.environ.get("GAME_FILTER", "polymarket").strip() or "polymarket"
    raw = os.environ.get("GAME_ID_PREFIXES", ",".join(DEFAULT_PREFIXES))
    prefixes = tuple(part.strip() for part in raw.split(",") if part.strip())
    return Config(data_dir=data, game_filter=filt, id_prefixes=prefixes or DEFAULT_PREFIXES)
