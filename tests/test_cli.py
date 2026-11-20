import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from gbwatch.cli import main, serve
from gbwatch.config import Config
from gbwatch.ingest import ingest_bytes
from gbwatch.models import Game
from gbwatch.schedule import book_url
from gbwatch.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
WHEN = datetime(2026, 11, 19, 3, 11, 41, tzinfo=timezone.utc)


def _game():
    return Game(
        game_id="0022600258",
        date_et="2026-11-18",
        away="MEM",
        home="CLE",
        slug="mem-vs-cle-0022600258",
        book_url=book_url(WHEN.date(), "MEM", "CLE"),
        tip_utc=datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc),
        state="FINAL_SEEN",
        final_seen_utc=WHEN,
        game_status=3,
        game_status_text="Final",
    )


def test_show_alerts_ack_summary_and_hash_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("IA_ACCESS", raising=False)
    monkeypatch.delenv("IA_SECRET", raising=False)
    store = Store(tmp_path)
    game = _game()
    store.save_game(game)
    body = (FIXTURES / "MEMCLE_final.pdf").read_bytes()
    assert (
        ingest_bytes(
            store,
            game,
            body,
            url=game.book_url,
            fetched_at=WHEN,
            source="live",
            last_modified="Thu, 19 Nov 2026 03:11:56 GMT",
            etag='"abc"',
            size=len(body),
            now=WHEN,
        )
        == "stored"
    )
    assert (
        ingest_bytes(
            store,
            game,
            body,
            url=game.book_url,
            fetched_at=WHEN,
            source="live",
            last_modified="Thu, 19 Nov 2026 03:11:56 GMT",
            etag='"abc"',
            size=len(body),
            now=WHEN,
        )
        == "duplicate"
    )
    version = store.versions(game.game_id)[0]
    assert version.kind == "final"
    assert version.pdf_created == "2026-11-19T03:11:41Z"
    assert Path(version.path).is_file()
    assert "/20261118/MEMCLE/" in version.path
    assert version.path.endswith(f"_{version.sha256[:8]}.pdf")
    store.close()

    assert main(["show", "MEMCLE", "2026-11-18"]) == 0
    shown = capsys.readouterr().out
    assert "server Last-Modified: Thu, 19 Nov 2026 03:11:56 GMT" in shown
    assert "pdf CreationDate: 2026-11-19T03:11:41Z" in shown
    assert version.sha256 in shown
    assert "first final:" in shown

    assert main(["diff", game.game_id]) == 0
    assert "no later finals" in capsys.readouterr().out

    assert main(["summary", "2026-11-18"]) == 0
    summary = capsys.readouterr().out
    assert "MEM@CLE" in summary
    assert "0022600258" in summary

    assert main(["alerts", "--since", "0"]) == 0
    alert = json.loads(capsys.readouterr().out.strip())
    assert alert["type"] == "first_final"
    assert alert["diffs"] == []
    assert main(["ack", str(alert["id"])]) == 0
    assert capsys.readouterr().out.strip() == f"acked {alert['id']}"
    assert main(["alerts", "--since", "0"]) == 0
    assert capsys.readouterr().out == ""
    assert main(["show", "does-not-exist"]) == 1


def test_redact_filter_scrubs_secret(caplog, monkeypatch):
    monkeypatch.setenv("IA_SECRET", "SUPERSECRETVALUE")
    caplog.set_level(logging.INFO)
    logging.getLogger("gbwatch.test").info("leaked %s", "SUPERSECRETVALUE")
    assert "SUPERSECRETVALUE" not in caplog.text
    assert "[redacted]" in caplog.text


def test_exception_traceback_scrubs_secret(caplog, monkeypatch):
    monkeypatch.setenv("IA_ACCESS", "SUPERSECRETVALUE")
    caplog.set_level(logging.ERROR)
    try:
        raise RuntimeError("header SUPERSECRETVALUE")
    except RuntimeError:
        logging.getLogger("gbwatch.test").exception("wayback failed")
    assert "SUPERSECRETVALUE" not in caplog.text
    assert "[redacted]" in caplog.text


def test_run_rotates_its_own_log(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GAME_FILTER", "all")
    monkeypatch.setattr("gbwatch.cli.serve", lambda *_args, **_kwargs: None)
    from logging.handlers import RotatingFileHandler

    root = logging.getLogger()
    before = list(root.handlers)
    try:
        assert main(["run"]) == 0
        added = [
            handler
            for handler in root.handlers
            if isinstance(handler, RotatingFileHandler) and handler not in before
        ]
        assert len(added) == 1
        assert added[0].maxBytes == 10 * 1024 * 1024
        assert added[0].backupCount == 5
        assert added[0].baseFilename == str((tmp_path / "gbwatch.log").resolve())
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()


def test_actions_logs_on_stderr_not_the_rotating_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("gbwatch.session.run_session", lambda *_args, **_kwargs: "idle")
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        assert main(["actions"]) == 0
        from logging.handlers import RotatingFileHandler

        added = [handler for handler in root.handlers if handler not in before]
        assert not any(isinstance(handler, RotatingFileHandler) for handler in added)
        assert not (tmp_path / "gbwatch.log").exists()
        assert "gbwatch actions: idle" in capsys.readouterr().out
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()


def test_run_sh_discards_stdout_and_keeps_stderr(tmp_path):
    root = tmp_path / "app"
    (root / "scripts").mkdir(parents=True)
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "gbwatch"
    stub.write_text("#!/bin/sh\necho STDOUT_MARKER\necho STDERR_MARKER >&2\nexec sleep 30\n")
    stub.chmod(0o755)
    source = Path(__file__).resolve().parents[1] / "scripts" / "run.sh"
    script = root / "scripts" / "run.sh"
    script.write_text(source.read_text())
    script.chmod(0o755)
    data = tmp_path / "data"
    env = os.environ.copy()
    env["DATA_DIR"] = str(data)
    result = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=10
    )
    pid_path = data / "gbwatch.pid"
    pid = int(pid_path.read_text()) if pid_path.exists() else None
    try:
        assert result.returncode == 0, result.stderr
        log = ""
        deadline = time.time() + 5
        while time.time() < deadline:
            if (data / "gbwatch.log").exists():
                log = (data / "gbwatch.log").read_text()
            if "STDERR_MARKER" in log or "STDOUT_MARKER" in log:
                break
            time.sleep(0.05)
        assert "STDERR_MARKER" in log
        assert "STDOUT_MARKER" not in log
    finally:
        if pid:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass


def test_run_sh_lock_is_not_fooled_by_a_reused_pid(tmp_path):
    root = tmp_path / "app"
    (root / "scripts").mkdir(parents=True)
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "gbwatch"
    stub.write_text("#!/bin/sh\nexec sleep 60\n")
    stub.chmod(0o755)
    source = Path(__file__).resolve().parents[1] / "scripts" / "run.sh"
    script = root / "scripts" / "run.sh"
    script.write_text(source.read_text())
    script.chmod(0o755)
    data = tmp_path / "data"
    env = os.environ.copy()
    env["DATA_DIR"] = str(data)

    def start():
        return subprocess.run(
            ["bash", str(script)], env=env, capture_output=True, text=True, timeout=10
        )

    first = start()
    assert first.returncode == 0, first.stderr
    pid = int((data / "gbwatch.pid").read_text())
    second = start()
    assert second.returncode == 0
    assert "already running" in second.stdout
    os.kill(pid, 15)
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    # The pidfile now names this live pytest process, which is not gbwatch.
    (data / "gbwatch.pid").write_text(str(os.getpid()))
    third = start()
    new_pid = None
    try:
        assert third.returncode == 0, third.stderr
        assert "started pid" in third.stdout
        new_pid = int((data / "gbwatch.pid").read_text())
        assert new_pid != os.getpid()
    finally:
        if new_pid and new_pid != os.getpid():
            try:
                os.kill(new_pid, 15)
            except ProcessLookupError:
                pass


def test_serve_one_tick_offline(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if "games?date=" in str(request.url):
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(404)

    class Clock:
        def now(self):
            return datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    serve(
        Config(data_dir=tmp_path, game_filter="all"),
        client=client,
        clock=Clock(),
        sleep=lambda _seconds: None,
        max_ticks=1,
    )
    store = Store(tmp_path)
    assert store.games_on("2026-11-19") == []
    store.close()
