"""Classify a game book and pull the player stat table out of pdftotext -layout."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from gbwatch.models import ParsedBook

STAT_COLS = ["FG", "FGA", "3P", "3PA", "FT", "FTA", "OR", "DR", "TOT", "A", "PF", "ST", "TO", "BS", "+/-", "PTS"]

PERIOD_RE = re.compile(
    r"\b(?:1st|2nd|3rd|4th)\s+QUARTER\b|\bOVERTIME\b",
    re.IGNORECASE,
)
PLAYER_RE = re.compile(
    r"^\s*(\d{1,2})\s+(.+?)\s+(?:([FCG])\s+)?(\d{1,3}:\d{2})\s+(-?\d.*)$"
)
DNP_RE = re.compile(r"^\s*(\d{1,2})\s+(.+?)\s+(DNP|DND|NWT)\s*-\s*(.*?)\s*$")
TOTAL_RE = re.compile(r"^\s{2,}(\d{1,3}:\d{2})\s+(-?\d.*)$")
TEAM_RE = re.compile(r"^(VISITOR|HOME):\s+(.+?)\s+\(\d+-\d+\)\s*$")


def classify_kind(text: str) -> str:
    """in_game when attendance is uncounted or a period header has no final score."""
    if "Attendance: Not Yet Counted" in text:
        return "in_game"
    if _has_final_score(text):
        return "final"
    if PERIOD_RE.search(text):
        return "in_game"
    return "final"


def _has_final_score(text: str) -> bool:
    if re.search(r"\bFINAL BOX\b", text):
        return True
    match = re.search(r"SCORE BY PERIOD([^\n]+)", text)
    if not match:
        return False
    columns = set(match.group(1).split())
    return {"1", "2", "3", "4"}.issubset(columns)


def pdftotext(path: Path) -> str:
    try:
        return subprocess.check_output(
            ["pdftotext", "-layout", str(path), "-"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("pdftotext is not installed (poppler-utils)") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"pdftotext failed for {path}") from exc


def pdf_metadata(path: Path) -> dict:
    """Read page count and PDF dates.

    `pdfinfo` without `-isodates` prints CreationDate in the process timezone
    (CET on a Madrid box) and does not include a numeric offset. `-isodates`
    keeps the PDF's own offset, so the result does not depend on TZ.
    """
    try:
        out = subprocess.check_output(
            ["pdfinfo", "-isodates", str(path)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {"pages": None, "pdf_created": None, "pdf_modified": None}
    pages = created = modified = None
    for line in out.splitlines():
        if line.startswith("Pages:"):
            try:
                pages = int(line.split(":", 1)[1].strip())
            except ValueError:
                pages = None
        elif line.startswith("CreationDate:"):
            created = _pdfinfo_date(line.split(":", 1)[1].strip())
        elif line.startswith("ModDate:"):
            modified = _pdfinfo_date(line.split(":", 1)[1].strip())
    return {"pages": pages, "pdf_created": created, "pdf_modified": modified}


def _pdfinfo_date(text: str) -> str | None:
    """Convert a pdfinfo -isodates value to UTC `YYYY-MM-DDTHH:MM:SSZ`."""
    raw = text.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # An ISO string with no offset is not a local time. Do not apply TZ.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_pdf(path: Path) -> ParsedBook:
    return parse_text(pdftotext(path))


def load_parsed(path: Path) -> ParsedBook:
    """Parse a stored PDF or a text capture (.txt is pdftotext-style text)."""
    path = Path(path)
    if path.suffix.lower() == ".txt":
        return parse_text(path.read_text(encoding="utf-8"))
    return parse_pdf(path)


def normalize_text(text: str) -> str:
    """Stable bytes for hashing a text capture: LF, trimmed lines, one trailing newline."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def parse_text(text: str) -> ParsedBook:
    kind = classify_kind(text)
    section = _box_section(text)
    players, teams, score = _parse_section(section)
    return ParsedBook(kind=kind, players=players, teams=teams, score=score)


def _box_section(text: str) -> str:
    """Full-game FINAL BOX when the book has one; otherwise the first box."""
    parts = re.split(r"\f", text)
    for part in parts:
        if "FINAL BOX" in part[:1500]:
            return part
    return parts[0] if parts else text


def _parse_section(section: str) -> tuple[dict, dict, dict]:
    players: dict[str, dict] = {}
    teams: dict[str, dict] = {}
    team: str | None = None
    lines = section.splitlines()
    score_at = None
    for index, line in enumerate(lines):
        if "SCORE BY PERIOD" in line:
            score_at = index
            break
        team_match = TEAM_RE.match(line.strip())
        if team_match:
            team = team_match.group(2).strip()
            continue
        dnp = DNP_RE.match(line)
        if dnp:
            name = _clean_name(dnp.group(2))
            players[name] = {"DNP": (dnp.group(4) or "").strip()}
            continue
        player = PLAYER_RE.match(line)
        if player:
            stats = _stat_map(player.group(5))
            if stats is None:
                continue
            stats["MIN"] = player.group(4)
            players[_clean_name(player.group(2))] = stats
            continue
        total = TOTAL_RE.match(line)
        if total and team and "%" not in line:
            stats = _stat_map(total.group(2))
            if stats is None:
                continue
            stats["MIN"] = total.group(1)
            teams[team] = stats
    score = _parse_score(lines[score_at:]) if score_at is not None else {}
    return players, teams, score


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


def _stat_map(rest: str) -> dict | None:
    tokens = re.findall(r"-?\d+", rest)
    if len(tokens) < len(STAT_COLS):
        return None
    return {column: int(token) for column, token in zip(STAT_COLS, tokens[: len(STAT_COLS)])}


def _parse_score(lines: list[str]) -> dict[str, int]:
    score: dict[str, int] = {}
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("Inactive", "Points", "Technical", "Flagrant", "MEMO", "Copyright")):
            break
        match = re.match(r"(.+?)\s+(-?\d+(?:\s+-?\d+)*)\s*$", stripped)
        if not match:
            break
        numbers = [int(piece) for piece in match.group(2).split()]
        if not numbers:
            break
        score[match.group(1).strip()] = numbers[-1]
    return score
