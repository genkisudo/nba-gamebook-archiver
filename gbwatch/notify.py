"""Append alert lines and render show / summary text."""

from __future__ import annotations

from gbwatch import classify
from gbwatch.diff import diff_stats
from gbwatch.models import Game, Version
from gbwatch.paths import resolve_stored_path
from gbwatch.store import Store
from gbwatch.timeutil import format_utc, format_zones, parse_utc


def emit_first_final(store: Store, game: Game, version: Version, created_utc: str) -> dict:
    return store.add_alert(_base(game, version, "first_final", created_utc, version.fetched_utc, []))


def emit_late_first_capture(
    store: Store, game: Game, version: Version, created_utc: str, gap_minutes: int
) -> dict:
    payload = _base(game, version, "late_first_capture", created_utc, version.fetched_utc, [])
    payload["gap_minutes"] = gap_minutes
    return store.add_alert(payload)


def _base(game: Game, version: Version, alert_type: str, created_utc: str, first_fetched: str, diffs: list) -> dict:
    return {
        "created_utc": created_utc,
        "type": alert_type,
        "game_id": game.game_id,
        "matchup": game.matchup,
        "first_final_fetched_utc": first_fetched,
        "server_last_modified": version.last_modified,
        "pdf_created_utc": version.pdf_created,
        "path": version.path,
        "sha256": version.sha256,
        "wayback_url": version.wayback_url,
        "diffs": diffs,
    }


def render_show(store: Store, game: Game) -> str:
    lines = [
        f"{game.away} @ {game.home}  {game.game_id}  {game.date_et}  state={game.state}",
        f"book: {game.book_url}",
    ]
    versions = store.versions(game.game_id)
    finals = [version for version in versions if version.kind == "final"]
    if not finals:
        lines.append("first final: not captured")
    else:
        first = finals[0]
        lines.append("first final:")
        if first.late_capture:
            lines.append("  LATE: not proof of first book")
        lines.append(f"  fetched_utc: {format_zones(parse_utc(first.fetched_utc))}")
        lines.append(f"  source: {first.source}")
        lines.append(f"  server Last-Modified: {first.last_modified}")
        lines.append(f"  pdf CreationDate: {first.pdf_created}")
        lines.append(f"  sha256: {first.sha256}")
        lines.append(f"  wayback: {_wayback_label(first)}")
        lines.append(f"  path: {first.path}")
        if first.note:
            lines.append(f"  note: {first.note}")
        if len(finals) > 1:
            lines.append("later versions:")
            first_stats = _stats(store, first)
            for version in finals[1:]:
                lines.append(
                    f"  {version.fetched_utc}  source={version.source}  sha256={version.sha256}  "
                    f"Last-Modified={version.last_modified}  wayback={version.wayback_url}"
                )
                if version.note:
                    lines.append(f"    note: {version.note}")
                changes = diff_stats(first_stats, _stats(store, version))
                if not changes:
                    lines.append("    diffs: none")
                for change in changes:
                    lines.append(f"    {change.player} {change.stat} {change.old} -> {change.new}")
    others = [version for version in versions if version.kind != "final"]
    if others:
        lines.append("other versions:")
        for version in others:
            lines.append(
                f"  {version.fetched_utc}  source={version.source}  kind={version.kind}  sha256={version.sha256}"
            )
    return "\n".join(lines) + "\n"


def render_diff(store: Store, game: Game) -> str:
    finals = [version for version in store.versions(game.game_id) if version.kind == "final"]
    if not finals:
        return f"{game.matchup}: no final captured\n"
    first = finals[0]
    lines = [f"{game.matchup} first final {first.fetched_utc} {first.sha256}"]
    if len(finals) == 1:
        lines.append("no later finals")
        return "\n".join(lines) + "\n"
    base = _stats(store, first)
    for version in finals[1:]:
        lines.append(f"vs {version.fetched_utc} {version.sha256}")
        changes = diff_stats(base, _stats(store, version))
        if not changes:
            lines.append("  none")
        for change in changes:
            lines.append(f"  {change.player} {change.stat} {change.old} -> {change.new}")
    return "\n".join(lines) + "\n"


def render_summary(store: Store, date_et: str) -> str:
    games = store.games_on(date_et)
    lines = [f"# NBA game books {date_et}", ""]
    if not games:
        lines.append("No games stored for this date.")
        lines.append("")
        return "\n".join(lines)
    lines.append("| Matchup | Game ID | State | Versions | First final (UTC) | Late |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for game in games:
        versions = store.versions(game.game_id)
        finals = [version for version in versions if version.kind == "final"]
        first = finals[0].fetched_utc if finals else ""
        late = "yes" if finals and finals[0].late_capture else ""
        lines.append(
            f"| {game.matchup} | {game.game_id} | {game.state} | {len(versions)} | {first} | {late} |"
        )
    lines.append("")
    return "\n".join(lines)


def _wayback_label(version: Version) -> str:
    if version.wayback_url:
        return f"{version.wayback_url} (verified)"
    if version.wayback_mismatch_url:
        return f"MISMATCH {version.wayback_mismatch_url}"
    return "None"


def _stats(store: Store, version: Version) -> dict:
    path = resolve_stored_path(store.data_dir, version.path)
    if not path.is_file():
        return {}
    try:
        return classify.load_parsed(path).stats()
    except Exception:
        return {}
