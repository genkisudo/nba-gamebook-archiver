"""Book paths stored in SQLite.

The gamebooks branch keeps them relative to the data directory so a later
runner, with a different checkout path, can still open the file.
"""

from __future__ import annotations

import re
from pathlib import Path

_BOOK = re.compile(r"(\d{8}/[A-Za-z0-9]+/.+)$")


def relative_book_path(data_dir: Path, path: str) -> str:
    """Return `path` relative to `data_dir` when it points at a stored book."""
    if not path:
        return path
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(Path(data_dir).resolve()).as_posix()
    except ValueError:
        match = _BOOK.search(path.replace("\\", "/"))
        if match:
            return match.group(1)
        return path


def resolve_stored_path(data_dir: Path, path: str) -> Path:
    """Find a stored book on this machine. Absolute paths from another host still resolve."""
    if not path:
        return Path(path)
    candidate = Path(path)
    if candidate.is_file():
        return candidate
    rel = relative_book_path(data_dir, path)
    alt = Path(data_dir) / rel
    if alt.is_file() or not candidate.is_absolute():
        return alt
    return candidate
