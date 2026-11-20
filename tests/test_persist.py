"""Manifests and the gamebooks branch round-trip without secrets or empty commits."""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gbwatch.ingest import ingest_bytes
from gbwatch.models import Game
from gbwatch.paths import relative_book_path, resolve_stored_path
from gbwatch.persist import SecretLeak, publish_data, restore_data, write_manifests
from gbwatch.schedule import book_url
from gbwatch.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF = (FIXTURES / "MEMCLE_final.pdf").read_bytes()
WHEN = datetime(2026, 11, 19, 3, 11, 41, tzinfo=timezone.utc)


def _game() -> Game:
    day = datetime(2026, 11, 18).date()
    return Game(
        game_id="0022600258",
        date_et=day.isoformat(),
        away="MEM",
        home="CLE",
        slug="mem-vs-cle-0022600258",
        book_url=book_url(day, "MEM", "CLE"),
        tip_utc=datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc),
        state="FINAL_SEEN",
        final_seen_utc=WHEN,
        game_status=3,
        game_status_text="Final",
    )


def _seed(data: Path) -> None:
    store = Store(data)
    game = _game()
    store.save_game(game)
    assert (
        ingest_bytes(
            store,
            game,
            PDF,
            url=game.book_url,
            fetched_at=WHEN,
            source="live",
            last_modified="Thu, 19 Nov 2026 03:11:56 GMT",
            etag='"abc"',
            size=len(PDF),
            now=WHEN,
        )
        == "stored"
    )
    version = store.versions(game.game_id)[0]
    store.enqueue_wayback(version.id, game.book_url, created_at=WHEN)
    write_manifests(store)
    store.close()


def test_manifest_records_fetch_headers_pdf_date_and_wayback(tmp_path):
    _seed(tmp_path)
    line = json.loads((tmp_path / "manifest.jsonl").read_text().splitlines()[0])
    assert line["fetched_utc"] == "2026-11-19T03:11:41Z"
    assert line["last_modified"] == "Thu, 19 Nov 2026 03:11:56 GMT"
    assert line["etag"] == '"abc"'
    assert line["pdf_created"] == "2026-11-19T03:11:41Z"
    assert line["late_capture"] is False
    assert line["first_final"] is True
    assert line["wayback_status"] == "pending"
    assert line["wayback_url"] is None
    assert line["wayback_mismatch_url"] is None
    assert line["path"].endswith(".pdf")
    assert not Path(line["path"]).is_absolute()
    per_game = json.loads((tmp_path / "20261118" / "MEMCLE" / "manifest.json").read_text())
    assert per_game["versions"][0]["sha256"] == line["sha256"]
    assert per_game["state"] == "FINAL_SEEN"
    raw = (tmp_path / "manifest.jsonl").read_bytes()
    store = Store(tmp_path)
    write_manifests(store)
    assert (tmp_path / "manifest.jsonl").read_bytes() == raw

    version = store.versions("0022600258")[0]
    store.set_late_capture(version.id)
    store.set_wayback_url(version.id, "https://web.archive.org/web/20261119031141/https://statsdmz.nba.com/x.pdf")
    store.set_wayback_mismatch_url(
        version.id, "https://web.archive.org/web/20261119040000/https://statsdmz.nba.com/x.pdf"
    )
    store.update_wayback_job(store.all_wayback_jobs()[0].id, status="done", wayback_url="https://web.archive.org/web/x")
    write_manifests(store)
    updated = json.loads((tmp_path / "manifest.jsonl").read_text().splitlines()[0])
    assert updated["late_capture"] is True
    assert updated["wayback_status"] == "done"
    assert updated["wayback_url"].startswith("https://web.archive.org/web/")
    assert updated["wayback_mismatch_url"].startswith("https://web.archive.org/web/")
    store.close()


def test_unchanged_save_does_not_touch_updated_utc(tmp_path):
    store = Store(tmp_path)
    store.save_game(_game())
    store.conn.execute("UPDATE games SET updated_utc = '2000-01-01T00:00:00Z'")
    store.conn.commit()
    store.save_game(store.get_game("0022600258"))
    assert store.conn.execute("SELECT updated_utc FROM games").fetchone()[0] == "2000-01-01T00:00:00Z"


def test_relative_and_foreign_absolute_paths_resolve(tmp_path):
    folder = tmp_path / "20261118" / "MEMCLE"
    folder.mkdir(parents=True)
    target = folder / "book.pdf"
    target.write_bytes(b"%PDF-1.4")
    assert resolve_stored_path(tmp_path, "20261118/MEMCLE/book.pdf") == target
    foreign = "/home/runner/work/nba/data/20261118/MEMCLE/book.pdf"
    assert relative_book_path(tmp_path, foreign) == "20261118/MEMCLE/book.pdf"
    assert resolve_stored_path(tmp_path, foreign) == target


def test_publish_roundtrip_and_second_push_is_a_no_op(tmp_path):
    repo, _bare = _git_repo(tmp_path)
    data = tmp_path / "data"
    _seed(data)
    assert publish_data(repo, data) is True
    assert publish_data(repo, data) is False
    restored = tmp_path / "restored"
    assert restore_data(repo, restored) is True
    store = Store(restored)
    version = store.versions("0022600258")[0]
    assert not Path(version.path).is_absolute()
    found = resolve_stored_path(restored, version.path)
    assert found.is_file()
    assert found.read_bytes() == PDF
    manifest = json.loads((restored / "manifest.jsonl").read_text().splitlines()[0])
    assert manifest["sha256"] == version.sha256
    assert manifest["pdf_created"] == "2026-11-19T03:11:41Z"
    assert manifest["fetched_utc"] == "2026-11-19T03:11:41Z"
    assert "IA_ACCESS" not in (restored / "gbwatch.sqlite").read_bytes().decode("utf-8", "replace")
    store.close()


def test_publish_refuses_secret_material_and_a_missed_restore(tmp_path, monkeypatch):
    repo, bare = _git_repo(tmp_path)
    data = tmp_path / "data"
    _seed(data)
    secret = "SUPERSECRETVALUE99"
    monkeypatch.setenv("IA_SECRET", secret)
    (data / "notes.txt").write_text(f"token {secret}\n", encoding="utf-8")
    with pytest.raises(SecretLeak) as exc:
        publish_data(repo, data)
    assert secret not in str(exc.value)
    missing = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "refs/heads/gamebooks"],
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0

    monkeypatch.setenv("GBWATCH_REQUIRE_RESTORE", "1")
    (data / "notes.txt").unlink()
    with pytest.raises(RuntimeError, match="restore did not finish"):
        publish_data(repo, data)


def _git_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    bare = tmp_path / "bare.git"
    repo.mkdir()
    subprocess.check_call(["git", "init", "-b", "main"], cwd=repo)
    subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=repo)
    subprocess.check_call(["git", "config", "user.name", "Test"], cwd=repo)
    subprocess.check_call(["git", "config", "commit.gpgsign", "false"], cwd=repo)
    (repo / "README.md").write_text("code\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "README.md"], cwd=repo)
    subprocess.check_call(["git", "commit", "-m", "init"], cwd=repo)
    subprocess.check_call(["git", "init", "--bare", str(bare)])
    subprocess.check_call(["git", "remote", "add", "origin", str(bare)], cwd=repo)
    return repo, bare
