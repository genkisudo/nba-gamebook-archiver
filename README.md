# gbwatch

Captures every version of an NBA official game book PDF, keeps the **first** post-game book (the one Polymarket player props resolve on), asks the Wayback Machine to snapshot it, and writes a JSON line when a later final changes a stat.

No LLM calls. Python 3.11+, `httpx`, poppler (`pdftotext`, `pdfinfo`), and the stdlib `sqlite3`.

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

## Run

```bash
./scripts/run.sh                 # nohup daemon, log at $DATA_DIR/gbwatch.log
gbwatch backfill 2026-11-18     # Wayback CDX, then wayback/available
gbwatch show 0022600258
gbwatch show MEMCLE 2026-11-18
gbwatch diff 0022600258
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

Alerts fire for the first `final` book and for each later `final` whose stat table differs from that first one. `gbwatch alerts` prints alerts that have not been `ack`ed. `gbwatch show` prints the first final's fetch time, server `Last-Modified`, PDF `CreationDate`, sha256, Wayback URL, and diffs for every later final.

Timestamps are UTC. `show` also prints Europe/Madrid and America/Mexico_City.

## Tests

```bash
pytest
```

The suite is offline: HTTP is faked, the clock is faked, and the PDFs under `tests/fixtures/` are the only books that are parsed.

## Behavior notes

- A book is `in_game` when it contains `Attendance: Not Yet Counted`, or when it has a quarter/overtime header and no final score. A final score is a `FINAL BOX` page or a score table that includes periods 1–4. Later quarter pages inside a finished book do not flip it back to `in_game`. Only the `FINAL BOX` table is parsed for finals.
- `gameEt` on nba.com is an Eastern clock time with a `Z` suffix. The slate date is that calendar date. `gameTimeUTC` is the real tip.
- Gamma's `tag_slug=nba&closed=false` listing is futures-first, so the client also sends `series_slug=nba-{year}` and, if needed, looks up `nba-{away}-{home}-{date}`.
- Player-prop questions on Gamma say `3-Pointers Made` as well as the `3PM` form in the spec. Both count.
- Wayback jobs are queued (at most 10 submitted at once) and retried with backoff on HTTP 429. A Save Page Now failure leaves the local PDF in place.
- Backfill stores CDX captures with `source=wayback` and `fetched_utc` set to the capture time, oldest first, so the earliest final stays the first final.
- `gbwatch import` stores a PDF or pdftotext-style text from another system (`--source otb`, `wayback`, or `manual`). Text versions are hashed from normalized text (LF, trimmed lines) and classified with the same parser. First-final selection and diffs compare parsed stats, so a text capture and a later PDF of the same game diff against each other. `show` prints each version's source.
