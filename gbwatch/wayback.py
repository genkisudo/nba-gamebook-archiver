"""Save Page Now. Failures are logged and never block a local capture.

Credentials are read from IA_ACCESS / IA_SECRET at call time and are not logged.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta

import httpx

from gbwatch.models import WaybackJob
from gbwatch.store import Store, sha256_bytes
from gbwatch.timeutil import format_utc, parse_utc

# Network failures back off up to half an hour and stop after a day.
MAX_BACKOFF_SECONDS = 30 * 60
GIVE_UP_AFTER = timedelta(hours=24)
# serve() can call drain about once a second. Don't hit status that often.
STATUS_POLL_INTERVAL = timedelta(seconds=15)

log = logging.getLogger("gbwatch.wayback")

SAVE_URL = "https://web.archive.org/save"
STATUS_URL = "https://web.archive.org/save/status/{job_id}"


class Wayback:
    def __init__(
        self,
        store: Store,
        client: httpx.Client,
        clock,
        *,
        sleeper=None,
        max_concurrent: int = 10,
    ):
        self.store = store
        self.client = client
        self.clock = clock
        self.sleeper = sleeper or (lambda _seconds: None)
        self.max_concurrent = max_concurrent

    def enabled(self) -> bool:
        return bool(os.environ.get("IA_ACCESS") and os.environ.get("IA_SECRET"))

    def enqueue(self, version_id: int, target_url: str) -> None:
        if not self.enabled():
            log.info("wayback skipped: IA credentials not set")
            return
        self.store.enqueue_wayback(version_id, target_url, created_at=self.clock.now())

    def drain(self, budget_seconds: float = 2.0) -> None:
        """Submit and poll queued saves without blocking the watcher for long."""
        if not self.enabled():
            return
        deadline = time.monotonic() + budget_seconds
        now = self.clock.now()
        submitted = self.store.wayback_by_status("submitted")
        slots = self.max_concurrent - len(submitted)
        if slots > 0:
            for job in self.store.wayback_ready(now, slots):
                if time.monotonic() >= deadline:
                    return
                if not self._submit(job):
                    break
        for job in self.store.wayback_submitted_due(now):
            if time.monotonic() >= deadline:
                return
            self._poll(job)

    def _auth(self) -> dict:
        access = os.environ.get("IA_ACCESS") or ""
        secret = os.environ.get("IA_SECRET") or ""
        return {
            "Authorization": f"LOW {access}:{secret}",
            "Accept": "application/json",
        }

    def _submit(self, job: WaybackJob) -> bool:
        """Return False when a 429 says to stop starting new saves."""
        ms = int(self.clock.now().timestamp() * 1000)
        target = f"{job.target_url}?t={ms}"
        try:
            response = self.client.post(
                SAVE_URL,
                headers=self._auth(),
                data={"url": target, "capture_all": "1"},
            )
        except httpx.HTTPError:
            self._defer(job, "network")
            return True
        if response.status_code == 429:
            self._defer(job, "429")
            return False
        if response.status_code >= 400:
            self._defer(job, f"status {response.status_code}")
            return True
        try:
            body = response.json()
        except Exception:
            self._defer(job, "bad json")
            return True
        snapshot = snapshot_url(body)
        job_id = body.get("job_id")
        if snapshot and not job_id:
            self._finish(job, snapshot)
            return True
        if not job_id:
            self._defer(job, "no job_id")
            return True
        self.store.update_wayback_job(job.id, status="submitted", job_id=str(job_id), last_error=None)
        return True

    def _poll(self, job: WaybackJob) -> None:
        if not job.job_id:
            return
        try:
            response = self.client.get(STATUS_URL.format(job_id=job.job_id), headers=self._auth())
        except httpx.HTTPError:
            self._defer(job, "status network")
            return
        if response.status_code == 429:
            self._defer(job, "status 429")
            return
        if response.status_code >= 400:
            self._defer(job, f"status {response.status_code}")
            return
        try:
            body = response.json()
        except Exception:
            self._defer(job, "status bad json")
            return
        state = str(body.get("status") or "").lower()
        if state in {"pending", "processing"}:
            # _defer is the only other give-up check, and this path never calls it.
            # A job that stays pending would hold a max_concurrent slot forever.
            created = parse_utc(job.created_utc) if job.created_utc else self.clock.now()
            if self.clock.now() - created >= GIVE_UP_AFTER:
                reason = f"status {state} >24h"
                self.store.update_wayback_job(job.id, status="failed", last_error=reason)
                log.warning("wayback gave up version=%s reason=%s", job.version_id, reason)
                return
            nxt = self.clock.now() + STATUS_POLL_INTERVAL
            self.store.update_wayback_job(job.id, next_try_utc=format_utc(nxt))
            return
        snapshot = snapshot_url(body)
        if state in {"success", "complete"} or snapshot:
            if snapshot:
                self._finish(job, snapshot)
            else:
                self._defer(job, "success without url")
            return
        self._defer(job, state or "error")

    def _finish(self, job: WaybackJob, url: str) -> None:
        """Attach the snapshot only when its archived bytes are this version.

        Save Page Now fetches the live book URL when the job runs, which can
        be hours later. The NBA overwrites that URL, so the capture is proof
        only if it hashes to the copy we stored.
        """
        raw = raw_snapshot_url(url)
        if raw is None:
            self.store.update_wayback_job(
                job.id, status="failed", wayback_url=url, last_error="unverified snapshot url"
            )
            log.warning("wayback snapshot url not verifiable version=%s", job.version_id)
            return
        try:
            response = self.client.get(raw)
        except httpx.HTTPError:
            # Stay submitted and retry the download. A new save would fetch the
            # live URL again and could archive a later book.
            self._defer(job, "verify network", status="submitted")
            return
        if response.status_code != 200:
            self._defer(job, f"verify status {response.status_code}", status="submitted")
            return
        version = self.store.get_version(job.version_id)
        if sha256_bytes(response.content) != version.sha256:
            self.store.update_wayback_job(
                job.id, status="failed", wayback_url=url, last_error="sha256 mismatch"
            )
            self.store.set_wayback_mismatch_url(job.version_id, url)
            log.warning("wayback snapshot mismatch version=%s", job.version_id)
            return
        self.store.update_wayback_job(job.id, status="done", wayback_url=url, last_error=None)
        self.store.set_wayback_url(job.version_id, url)
        log.info("wayback stored version=%s", job.version_id)

    def _defer(self, job: WaybackJob, reason: str, stop: bool = False, *, status: str = "pending") -> None:
        attempts = job.attempts + 1
        created = parse_utc(job.created_utc) if job.created_utc else self.clock.now()
        if self.clock.now() - created >= GIVE_UP_AFTER:
            self.store.update_wayback_job(
                job.id,
                status="failed",
                attempts=attempts,
                last_error=reason[:200],
            )
            log.warning("wayback gave up version=%s reason=%s", job.version_id, reason)
            return
        delay = min(MAX_BACKOFF_SECONDS, 5 * (2 ** min(attempts - 1, 12)))
        next_try = self.clock.now() + timedelta(seconds=delay)
        self.store.update_wayback_job(
            job.id,
            status=status,
            attempts=attempts,
            next_try_utc=format_utc(next_try),
            last_error=reason[:200],
        )
        if attempts == 1 and status == "pending":
            log.warning(
                "wayback unreachable version=%s reason=%s; retrying for up to 24h",
                job.version_id,
                reason,
            )
        elif attempts == 1:
            log.warning(
                "wayback verify failed version=%s reason=%s; retrying",
                job.version_id,
                reason,
            )
        # Never sleep here. next_try_utc is what the next drain honors, including 429.


_RAW_SNAPSHOT = re.compile(r"^(https://web\.archive\.org/web/)(\d+)(/.*)$")


def raw_snapshot_url(url: str) -> str | None:
    """Insert id_ so archive.org returns the captured file, not the replay page."""
    match = _RAW_SNAPSHOT.match(url)
    if not match:
        return None
    return f"{match.group(1)}{match.group(2)}id_{match.group(3)}"


def snapshot_url(body: dict) -> str | None:
    for key in ("wayback_url", "snapshot_url"):
        value = body.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    direct = body.get("url")
    if isinstance(direct, str) and "web.archive.org/web/" in direct:
        return direct
    timestamp = body.get("timestamp")
    original = body.get("original_url") or body.get("original")
    if timestamp and isinstance(original, str) and original.startswith("http"):
        return f"https://web.archive.org/web/{timestamp}/{original}"
    return None
