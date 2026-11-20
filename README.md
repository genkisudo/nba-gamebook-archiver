# NBA game book archive

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
