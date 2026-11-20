# gbwatch

This README is the spec.

Captures the first post-game NBA official game book PDF (the one Polymarket player props resolve on), asks the Wayback Machine to snapshot it, and writes a JSON line when that first final is late. The live watcher stores one book per game: the first page-1 `FINAL BOX`.

No LLM calls. Python 3.11+, `httpx`, poppler (`pdftotext`, `pdfinfo`), and the stdlib `sqlite3`.

Production runs on GitHub Actions. A local daemon is optional and only works on a host that stays awake.

The book URL is overwritten in place:

`https://statsdmz.nba.com/pdfs/YYYYMMDD/YYYYMMDD_AWAYHOME_book.pdf`

## Setup

```bash
sudo apt-get install -y poppler-utils python3-venv
./scripts/install.sh
source .venv/bin/activate
```

Archive.org Save Page Now keys are read from the environment only. They are never printed or written to the database. If they are unset, local copies are still kept.

```bash
export IA_ACCESS=...
export IA_SECRET=...
export DATA_DIR=./data          # default ./data
export GAME_FILTER=polymarket   # polymarket | all | comma-separated game ids
```

`GAME_FILTER=polymarket` (the default) watches a game only when Gamma has a player-prop market for it (`Points`, `Rebounds`, `Assists`, `Steals`, `Blocks`, `3PM`, or `3-Pointers Made`, with `O/U`). A game already being watched is not dropped if the market later closes.

Slate and box-score pages come from `www.nba.com`. The book PDF is on `statsdmz.nba.com`. Both get a browser User-Agent. `cdn.nba.com` and `stats.nba.com` return 403 and are not used.

## GitHub Actions

Workflow: `.github/workflows/watch.yml`. It runs only after it is on the default branch. Schedule and `workflow_dispatch` do not run from a pull request.

### Schedule

Cron is UTC. One shift stays up for at most five hours (`GBWATCH_BUDGET_SECONDS` overrides that; the job timeout is 340 minutes) so setup and the publish step finish inside GitHub's six-hour cap. The next fire restores SQLite from the `gamebooks` branch and continues any game that is not `DONE`.

| UTC | Why it is there |
| --- | --- |
| hourly `15:00`–`23:00` and `00:00`–`10:00` | A noon-ET weekend tip is ~16:00 UTC in summer and ~17:00 UTC in winter. A late West Coast ending is ~06:30 UTC. The hourly grid covers both, plus the resume after a shift hits the five-hour cap. |
| `:30` past `15`, `16`, `5`, and `6` | A delayed top-of-hour fire still starts before the early quiet window and around the late endings. |

GitHub sometimes delays or drops a cron fire. The concurrency group `gbwatch` (`cancel-in-progress: false`) means a second fire waits, and GitHub keeps only the newest pending run. A late or duplicate fire does not cancel the shift that is already polling. When the running shift exits, the pending one starts with a fresh five-hour budget.

Before any watched game is within 15 minutes of tip, the shift only refreshes the slate every five minutes (that refresh is what notices quarter 4 and opens the book early). It does not poll the PDF on the 60-second loop yet. If no game enters that window before the shift's deadline, the watcher exits after one slate check, without sleeping. A shift whose games do fall inside the five hours waits, then uses the same intervals as the daemon: book quiet until tip+105 minutes, quarter 4, or Final; every 60 seconds through the live window; every 30 seconds after Final; stop when the first `FINAL BOX` is stored.

### What a night costs

This repository is public, so GitHub-hosted Actions minutes are free. The figures are wall-clock time, not a bill.

A shift that starts hours before tip and whose quiet window is still inside its five-hour budget waits on the runner. That is deliberate: a dropped later cron must not leave a gap. Concurrency keeps it to one runner. Typical wall clock:

- Off night, or a fire with nothing due before its deadline: the job is checkout, install, one slate check, and exit. A few minutes.
- Weeknight (about 7:00 pm ET through a ~06:30 UTC ending): one runner from about 19:00 UTC until the last book is stored, roughly 11–12 hours, plus about an hour of short jobs that exit immediately.
- Weekend with a noon-ET tip: the chain starts around 15:00 UTC, roughly 15–16 hours.
- A month of the regular season is on the order of 300–400 hours. Nothing is billed for a public repository.

### Data branch `gamebooks`

Each shift restores this branch into `data/`, watches, then commits it back. The branch is created on the first night there is something to store. `github-actions[bot]` needs permission to push it (the workflow sets `contents: write`). Do not protect the branch in a way that blocks that push.

```
gamebooks
├── README.md
├── manifest.jsonl                          # one JSON object per stored version
├── alerts.jsonl                            # first_final and late_first_capture
├── gbwatch.sqlite                          # games, versions, alerts, Wayback queue
└── YYYYMMDD/AWAYHOME/
    ├── manifest.json                       # the same records for this game
    └── YYYYMMDDTHHMMSSZ_<sha8>.pdf
```

`fetched_utc` is when the GET completed, in UTC. `last_modified` and `etag` are that response's headers. `pdf_created` is CreationDate from `pdfinfo -isodates`, stored in UTC. `late_capture: true` means the file arrived too late to prove it was the first post-game book. `wayback_status` is `pending`, `submitted`, `done`, `failed`, or `skipped`. `first_final` marks the first `kind=final` row. Paths in the branch are relative to the branch root.

A game that is still being polled is not `DONE` in `gbwatch.sqlite`. The next shift continues it. Opening SQLite without a real change does not produce an empty commit.

`data/` on the default branch is gitignored. Do not commit captures there.

### Secrets

Save Page Now uses repository secrets `IA_ACCESS` and `IA_SECRET` (Settings → Secrets and variables → Actions). The request header is `Authorization: LOW $IA_ACCESS:$IA_SECRET`. The values are never printed, never written to the database, and a commit that contains either value is refused.

If either secret is missing, the shift logs a warning, skips Wayback, and still stores the PDF. Add both secrets before relying on the archive URL.

### Manual run

Actions → Watch game books → Run workflow.

- `mode=watch` (default) polls the live book. `date` (`YYYY-MM-DD`, Eastern slate date) adds that date to today and yesterday. A game whose final is already more than six hours old the first time this run sees it is not watched; the PDF at that URL is no longer the first book.
- `mode=backfill` requires `date` and reads Wayback CDX (then `wayback/available`) for that slate.

## Local daemon

Same commands as the workflow, for a machine that stays awake. `gbwatch run` is the long-lived daemon. `gbwatch actions` is one bounded shift.

```bash
./scripts/run.sh                 # nohup daemon, log at $DATA_DIR/gbwatch.log
./scripts/watchdog.sh            # call run.sh every 5 minutes; it is idempotent
gbwatch actions                  # one shift, then exit (what Actions runs)
gbwatch restore                  # copy the gamebooks branch into $DATA_DIR
gbwatch publish                  # commit $DATA_DIR to the gamebooks branch and push
gbwatch backfill 2026-11-18     # Wayback CDX, then wayback/available
gbwatch show 0022600258
gbwatch show MEMCLE 2026-11-18
gbwatch diff 0022600258          # manual stat compare of stored finals
gbwatch alerts --since 0
gbwatch ack 1
gbwatch summary 2026-11-18
gbwatch import --game WASTOR --date 20261118 --fetched-utc 2026-11-19T03:09:00Z \
  --source otb --text capture.txt --note "resolver artifact"
```

`scripts/run.sh` rotates `gbwatch.log` past 10MB (keeps five archives) and refuses to start a second copy. A systemd unit for the same command:

```ini
[Service]
WorkingDirectory=/opt/gbwatch
Environment=DATA_DIR=/opt/gbwatch/data
Environment=GAME_FILTER=polymarket
EnvironmentFile=-/opt/gbwatch/gbwatch.env
ExecStart=/opt/gbwatch/.venv/bin/gbwatch run
Restart=always
```

Put `IA_ACCESS` and `IA_SECRET` in `gbwatch.env` if you use systemd. Do not commit that file.

## What it stores

- PDFs at `data/YYYYMMDD/AWAYHOME/<fetched_utc>_<sha8>.pdf` and text captures at the same path with a `.txt` suffix (`fetched_utc` is `YYYYMMDDTHHMMSSZ`)
- SQLite at `data/gbwatch.sqlite` (games, versions, alerts, wayback jobs)
- `data/alerts.jsonl`, one JSON object per line
- `data/manifest.jsonl` and `data/YYYYMMDD/AWAYHOME/manifest.json`, rewritten from SQLite by an Actions shift

On GitHub Actions that tree is the `gamebooks` branch. That is the durable copy.

The live watcher stores one book per game: the first `final`. Before the box score says Final, an in-game body is checked with HEAD and not written to disk. Once the box score says Final, a body that is not yet `FINAL BOX` is kept as evidence. Once the final is stored and a Wayback save is queued, the game is `DONE` and polling stops. Alerts are `first_final` or `late_first_capture`. `gbwatch alerts` prints alerts that have not been `ack`ed. `gbwatch show` prints the first final's fetch time, server `Last-Modified`, PDF `CreationDate`, sha256, Wayback URL, and source. `gbwatch diff` compares later imported or backfilled finals against that first one.

Timestamps are UTC. `show` also prints Europe/Madrid and America/Mexico_City.

## Tests

```bash
pytest
```

The suite is offline: HTTP is faked, the clock is faked, and the PDFs under `tests/fixtures/` are the only books that are parsed.

## Behavior notes

- `gbwatch actions` runs the same poll loop as the daemon for one shift. Until 15 minutes before tip it only refreshes the slate every five minutes. It exits when every watched game is `DONE`, or five hours after the process started.
- A book is `final` only when page 1 says `FINAL BOX`. A running header such as `4th QUARTER (12:00) -` or a 1–4 score table without that header is `in_game`, even if attendance has already been counted. `FINAL BOX` wins over `Attendance: Not Yet Counted`; the attendance line is stored on the version note.
- `gameEt` on nba.com is an Eastern clock time with a `Z` suffix. The slate date is that calendar date. `gameTimeUTC` is the real tip.
- PDF CreationDate and ModDate come from `pdfinfo -isodates`, which keeps the file's offset, and are stored as UTC. Plain `pdfinfo` prints those instants in the process timezone.
- The book is not polled from tip-off. Until `QUIET_MINUTES` after tip (default 105, about the length of a game) the box score is checked at most every 10 minutes and the PDF is not requested. From then until Final, both are checked every 60 seconds. Quarter 4 or later starts that active window early. If the game is still pre-game 30 minutes after the listed tip, that tip is ignored and the book waits for quarter 4 or a revised tip. Once Final is seen, even before the quiet window ends, the book is checked every 30 seconds until the first final PDF is stored.
- A first `final` fetched more than `LATE_CAPTURE_MINUTES` (default 20) after its anchor is late, measured from the earlier of the live final observation and the box score's `actualEndTimeUTC`. It is stored as `final` with `late_capture=1` and alert type `late_first_capture`. A live book for a game discovered already final, with no live observation and no `actualEndTimeUTC`, is late as well: the final was not seen. `show` labels it `LATE: not proof of first book`. Games whose final was more than 6 hours before the daemon started are not watched; `backfill` can still archive them.
- Wayback network failures back off up to 30 minutes and are marked failed after 24 hours. One warning is logged when the job starts failing, and one when it gives up.
- Gamma's `tag_slug=nba&closed=false` listing is futures-first, so the client also sends `series_slug=nba-{year}` and, if needed, looks up `nba-{away}-{home}-{date}`. If that fetch fails or returns no events, every game on the slate is watched.
- `scripts/watchdog.sh` calls `scripts/run.sh` every five minutes. `run.sh` does nothing when the daemon is already running.
- Player-prop questions on Gamma say `3-Pointers Made` as well as the `3PM` form in the spec. Both count.
- Wayback jobs are queued (at most 10 submitted at once) and retried with backoff on HTTP 429. A Save Page Now failure leaves the local PDF in place.
- Backfill stores CDX captures with `source=wayback` and `fetched_utc` set to the capture time, oldest first, so the earliest final stays the first final.
- `gbwatch import` stores a PDF or pdftotext-style text from another system (`--source otb`, `wayback`, or `manual`). Text versions are hashed from normalized text (LF, trimmed lines). Among imported and backfilled copies, the earliest `final` by fetch time is the first final. `show` prints each version's source.
