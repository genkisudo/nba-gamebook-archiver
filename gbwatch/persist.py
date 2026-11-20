"""Read and write the gamebooks branch.

Captured PDFs, manifests, alerts, and the SQLite resume file live on a
dedicated branch so a later Actions runner can continue a shift. Paths in
that branch are relative to the data directory. IA_ACCESS and IA_SECRET are
never written; publish refuses the commit if either value appears in a
non-PDF file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from gbwatch.models import Game, Version, WaybackJob
from gbwatch.paths import relative_book_path
from gbwatch.store import Store
from gbwatch.timeutil import format_utc, utcnow

log = logging.getLogger("gbwatch.persist")

DATA_BRANCH = "gamebooks"
_RESTORE_MARKER = ".restore-ok"
_URL_USERINFO = re.compile(r"(https://)([^/\s@]+)@")

DATA_README = """# NBA game book archive

This branch is data written by the GitHub Actions watcher. The application lives on the default branch.

| Path | What it is |
| --- | --- |
| `manifest.jsonl` | One JSON object per stored book version |
| `YYYYMMDD/AWAYHOME/manifest.json` | The same records for a single game |
| `YYYYMMDD/AWAYHOME/*.pdf` | PDF bytes. The live watcher keeps the first FINAL BOX, plus an in-game copy only when the box score was already final |
| `alerts.jsonl` | `first_final` and `late_first_capture` alerts |
| `gbwatch.sqlite` | Resume state: games, versions, alerts, Wayback queue |

`fetched_utc` is when the GET completed (UTC). `last_modified` and `etag` are the response headers from that GET. `pdf_created` is the PDF CreationDate from `pdfinfo -isodates`, stored in UTC. `late_capture` means the file arrived too late to prove it was the first post-game book. `wayback_status` is `pending`, `submitted`, `done`, `failed`, or `skipped` (Save Page Now was not attempted). `first_final` marks the first `kind=final` version for that game.

A shift that is still polling leaves games in `gbwatch.sqlite` with a state other than `DONE`. The next run restores this branch and continues.
"""


class SecretLeak(RuntimeError):
    """A file that would be committed contains an Archive.org secret."""


def write_manifests(store: Store) -> None:
    """Rewrite manifest files from SQLite. No versions means leave any existing manifest alone."""
    versions = store.all_versions()
    if not versions:
        return
    games = {game.game_id: game for game in store.all_games()}
    jobs = {job.version_id: job for job in store.all_wayback_jobs()}
    lines: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for version in versions:
        game = games.get(version.game_id)
        if game is None:
            continue
        record = version_record(store, game, version, jobs.get(version.id), versions)
        lines.append(json.dumps(record, separators=(",", ":")))
        grouped.setdefault(version.game_id, []).append(record)
    _write_if_changed(store.data_dir / "manifest.jsonl", "\n".join(lines) + "\n")
    for game_id, records in grouped.items():
        game = games[game_id]
        folder = store.data_dir / game.date_et.replace("-", "") / f"{game.away}{game.home}"
        folder.mkdir(parents=True, exist_ok=True)
        payload = {
            "game_id": game.game_id,
            "date_et": game.date_et,
            "away": game.away,
            "home": game.home,
            "book_url": game.book_url,
            "state": game.state,
            "versions": records,
        }
        _write_if_changed(folder / "manifest.json", json.dumps(payload, indent=2) + "\n")


def version_record(
    store: Store,
    game: Game,
    version: Version,
    job: WaybackJob | None,
    versions: list[Version],
) -> dict:
    finals = [item for item in versions if item.game_id == version.game_id and item.kind == "final"]
    return {
        "game_id": game.game_id,
        "date_et": game.date_et,
        "away": game.away,
        "home": game.home,
        "book_url": game.book_url,
        "kind": version.kind,
        "source": version.source,
        "fetched_utc": version.fetched_utc,
        "last_modified": version.last_modified,
        "etag": version.etag,
        "pdf_created": version.pdf_created,
        "sha256": version.sha256,
        "wayback_url": version.wayback_url,
        "wayback_status": job.status if job is not None else "skipped",
        "late_capture": bool(version.late_capture),
        "first_final": bool(finals) and finals[0].id == version.id,
        "path": relative_book_path(store.data_dir, version.path),
    }


def restore_data(repo: Path, dest: Path, *, branch: str = DATA_BRANCH, remote: str = "origin") -> bool:
    """Copy `branch` into `dest`. Returns False when the branch does not exist yet.

    A network failure raises. Publishing over a failed restore would drop books
    that are already on the branch.
    """
    dest.mkdir(parents=True, exist_ok=True)
    marker = dest / _RESTORE_MARKER
    marker.unlink(missing_ok=True)
    found = _fetch_branch(repo, remote, branch)
    if not found:
        log.info("no %s branch yet; starting with an empty archive", branch)
        marker.write_text("missing\n", encoding="utf-8")
        return False
    _clear_dir(dest)
    _extract_archive(repo, dest, f"refs/remotes/{remote}/{branch}")
    marker.write_text("ok\n", encoding="utf-8")
    log.info("restored %s into %s", branch, dest)
    return True


def publish_data(
    repo: Path,
    source: Path,
    *,
    branch: str = DATA_BRANCH,
    remote: str = "origin",
    push: bool = True,
) -> bool:
    """Commit `source` onto `branch` and push. Returns False when nothing changed.

    On GitHub Actions, restore must have finished (it writes `.restore-ok`).
    That stops a failed restore from replacing the branch with an empty tree.
    """
    if os.environ.get("GITHUB_ACTIONS") == "true" and not (source / _RESTORE_MARKER).is_file():
        raise RuntimeError("refusing to publish: restore did not finish")
    if not worth_publishing(source):
        log.info("nothing to publish")
        return False
    _ensure_identity(repo)
    work = Path(tempfile.mkdtemp(prefix="gbwatch-data-"))
    work.rmdir()
    try:
        _open_worktree(repo, remote, branch, work)
        previous = work / "gbwatch.sqlite"
        old_bytes = previous.read_bytes() if previous.is_file() else None
        old_logic = sqlite_snapshot(previous) if previous.is_file() else None
        _clear_dir(work)
        _copy_archive(source, work)
        relativize_tree(work)
        _write_if_changed(work / "README.md", DATA_README)
        leaks = find_secret_leaks(work)
        if leaks:
            raise SecretLeak(f"refusing to publish {leaks[0].relative_to(work)}: file contains secret material")
        if old_logic is not None and (work / "gbwatch.sqlite").is_file():
            if sqlite_snapshot(work / "gbwatch.sqlite") == old_logic and old_bytes is not None:
                (work / "gbwatch.sqlite").write_bytes(old_bytes)
        _git(work, ["add", "-A"])
        status = _git(work, ["status", "--porcelain"])
        if not status.stdout.strip():
            log.info("%s branch unchanged", branch)
            return False
        message = f"Archive NBA game books\n\nRecorded at {format_utc(utcnow())}."
        _git(work, ["commit", "-m", message])
        if push:
            _push(repo, work, remote, branch)
        log.info("published %s", branch)
        return True
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(work)],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(work, ignore_errors=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, check=False)


def worth_publishing(data_dir: Path) -> bool:
    if not data_dir.is_dir():
        return False
    alerts = data_dir / "alerts.jsonl"
    if alerts.is_file() and alerts.stat().st_size > 0:
        return True
    for path in data_dir.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in {".pdf", ".txt", ".json", ".jsonl"}:
            return True
    database = data_dir / "gbwatch.sqlite"
    if not database.is_file():
        return False
    snapshot = sqlite_snapshot(database)
    if snapshot is None:
        return False
    tables = json.loads(snapshot)
    return any(tables[name] for name in tables)


def find_secret_leaks(root: Path) -> list[Path]:
    secrets = [
        value
        for value in (os.environ.get("IA_ACCESS"), os.environ.get("IA_SECRET"))
        if value and len(value) >= 8
    ]
    if not secrets:
        return []
    leaks = []
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts or path.suffix.lower() == ".pdf":
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if any(secret.encode("utf-8") in data for secret in secrets):
            leaks.append(path)
    return leaks


def relativize_tree(data_dir: Path) -> None:
    """Store book paths relative to `data_dir` so the branch is portable."""
    database = data_dir / "gbwatch.sqlite"
    if database.is_file():
        store = Store(data_dir)
        try:
            for version in store.all_versions():
                rel = relative_book_path(data_dir, version.path)
                if version.path and rel != version.path:
                    store.update_version(version.id, path=rel)
            rows = store.conn.execute("SELECT id, payload FROM alerts ORDER BY id").fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                raw = payload.get("path") or ""
                if raw and Path(raw).is_absolute():
                    payload["path"] = relative_book_path(data_dir, raw)
                    encoded = json.dumps(payload, separators=(",", ":"), default=str)
                    store.conn.execute(
                        "UPDATE alerts SET payload = ? WHERE id = ?",
                        (encoded, row["id"]),
                    )
            store.conn.commit()
        finally:
            store.close()
    alerts = data_dir / "alerts.jsonl"
    if alerts.is_file():
        _relativize_jsonl(data_dir, alerts)


def sqlite_snapshot(database: Path) -> str | None:
    """Logical row contents. Used so opening SQLite does not create an empty commit."""
    if not database.is_file():
        return None
    uri = database.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {}
        queries = {
            "games": "SELECT * FROM games ORDER BY game_id",
            "versions": "SELECT * FROM versions ORDER BY id",
            "alerts": "SELECT * FROM alerts ORDER BY id",
            "wayback_jobs": "SELECT * FROM wayback_jobs ORDER BY id",
        }
        for name, sql in queries.items():
            try:
                rows = conn.execute(sql).fetchall()
            except sqlite3.Error:
                return None
            tables[name] = [dict(row) for row in rows]
        return json.dumps(tables, sort_keys=True, default=str)
    finally:
        conn.close()


def _relativize_jsonl(data_dir: Path, path: Path) -> None:
    lines = []
    changed = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            lines.append(line)
            continue
        raw = payload.get("path") or ""
        if raw and Path(raw).is_absolute():
            payload["path"] = relative_book_path(data_dir, raw)
            line = json.dumps(payload, separators=(",", ":"), default=str)
            changed = True
        lines.append(line)
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _copy_archive(source: Path, dest: Path) -> None:
    allowed_names = {"gbwatch.sqlite", "alerts.jsonl", "manifest.jsonl", "README.md"}
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.name not in allowed_names and path.suffix.lower() not in {".pdf", ".txt", ".json"}:
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _write_if_changed(path: Path, text: str) -> None:
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _clear_dir(path: Path) -> None:
    if not path.exists():
        return
    for child in list(path.iterdir()):
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _fetch_branch(repo: Path, remote: str, branch: str) -> bool:
    proc = _git(
        repo,
        ["fetch", "--depth=1", "--no-tags", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"],
        check=False,
    )
    if proc.returncode == 0:
        return True
    err = (proc.stderr or "").lower()
    if "couldn't find remote ref" in err or "unable to find" in err or "not found" in err:
        return False
    raise RuntimeError("git fetch failed: " + _scrub(proc.stderr).strip()[-500:])


def _open_worktree(repo: Path, remote: str, branch: str, work: Path) -> bool:
    """Check out `branch` into `work`. Returns True when the remote branch already existed."""
    ref = f"refs/remotes/{remote}/{branch}"
    existed = _git(repo, ["rev-parse", "--verify", ref], check=False).returncode == 0
    if not existed:
        # The remote may have the branch even when this clone has not fetched it.
        existed = _fetch_branch(repo, remote, branch)
    if existed:
        _git(repo, ["worktree", "add", "--force", "--detach", str(work), ref])
        _git(work, ["checkout", "-B", branch])
        return True
    _git(repo, ["worktree", "add", "--force", "-b", branch, "--orphan", str(work)])
    return False


def _push(repo: Path, work: Path, remote: str, branch: str) -> None:
    delay = 4
    last = ""
    for attempt in range(4):
        if attempt:
            time.sleep(delay)
            delay *= 2
            if _remote_has_branch(repo, remote, branch):
                rebase = _git(work, ["rebase", f"refs/remotes/{remote}/{branch}"], check=False)
                if rebase.returncode != 0:
                    _git(work, ["rebase", "--abort"], check=False)
                    raise RuntimeError("git rebase failed: " + _scrub(rebase.stderr or "").strip()[-500:])
        proc = _git(work, ["push", remote, f"HEAD:{branch}"], check=False)
        if proc.returncode == 0:
            return
        last = _scrub(proc.stderr or "").strip()[-500:]
    raise RuntimeError("git push failed: " + last)


def _remote_has_branch(repo: Path, remote: str, branch: str) -> bool:
    try:
        return _fetch_branch(repo, remote, branch)
    except RuntimeError:
        return False


def _ensure_identity(repo: Path) -> None:
    email = _git(repo, ["config", "user.email"], check=False)
    if email.returncode == 0 and email.stdout.strip():
        return
    _git(repo, ["config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    _git(repo, ["config", "user.name", "github-actions[bot]"])


def _extract_archive(repo: Path, dest: Path, ref: str) -> None:
    proc = subprocess.run(
        ["git", "-C", str(repo), "archive", ref],
        capture_output=True,
        env=_git_env(),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("git archive failed: " + _scrub(proc.stderr.decode("utf-8", "replace")).strip()[-500:])
    subprocess.run(["tar", "-x", "-C", str(dest)], input=proc.stdout, check=True)


def _git(cwd: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: " + _scrub(proc.stderr or "").strip()[-500:])
    return proc


def _git_env() -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _scrub(text: str) -> str:
    cleaned = _URL_USERINFO.sub(r"\1***@", text or "")
    for name in ("IA_ACCESS", "IA_SECRET", "GITHUB_TOKEN"):
        value = os.environ.get(name) or ""
        if len(value) >= 8:
            cleaned = cleaned.replace(value, "[redacted]")
    return cleaned
