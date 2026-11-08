"""Orchestrate acquisition, offline data processing, and persistence."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import math
import signal
import threading
import time

from normalizer import timestamp
from facebook_crawler import FacebookCrawler, CollectionFailure
from data_filter import process_collection

LOG = logging.getLogger(__name__)
SECURITY_STOPS = {"authentication_failed", "session_expired", "checkpoint_required", "rate_limited", "account_restricted"}


def processing_failure_code(raw):
    errors = raw.get("errors", []) if isinstance(raw, dict) else []
    if not isinstance(errors, list):
        return "parse_error"
    return next((error.get("code") for error in errors if isinstance(error, dict)
                 and isinstance(error.get("code"), str) and error["code"] in SECURITY_STOPS), "parse_error")


class FacebookCollector(FacebookCrawler):
    """Compatibility facade: callers receive normalized data as before.

    New integrations use FacebookCrawler then data_filter.process_collection
    explicitly so acquiring and processing data remain independent.
    """
    def __call__(self, run):
        return process_collection(super().__call__(run))


class Worker:
    """Claim and finish queued runs in order; only one worker owns the DB lock."""

    def __init__(self, db, collector=None, interval_seconds=0, lookback_hours=72,
                 poll_seconds=2, *, clock=time.monotonic, now=None, processor=None):
        if not math.isfinite(interval_seconds) or interval_seconds < 0:
            raise ValueError("interval_seconds must be nonnegative")
        if not math.isfinite(poll_seconds) or not 0.01 <= poll_seconds <= 60:
            raise ValueError("poll_seconds must be between 0.01 and 60")
        self.db = db
        self.collector = collector or FacebookCrawler(lookback_hours=lookback_hours)
        self.processor = processor or process_collection
        self.interval_seconds, self.poll_seconds = interval_seconds, poll_seconds
        self.clock = clock
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._last_scheduled = {}
        self._stop = threading.Event()
        if isinstance(self.collector, FacebookCrawler):
            self.collector.cancel_event = self._stop

    def process_one(self) -> bool:
        if self.db.state().get("paused_code"):
            return False
        run = self.db.claim_next()
        if run is None:
            return False
        try:
            raw = self.collector(run)
        except CollectionFailure as exc:
            self.db.fail_run(run["id"], exc.code)
        except Exception:
            # Never emit exception text: upstream errors can contain credentials.
            self.db.fail_run(run["id"], "unknown_error")
        else:
            try:
                envelope = self.processor(raw)
            except Exception:
                self.db.fail_run(run["id"], processing_failure_code(raw), raw=raw)
                return True
            try:
                self.db.complete_run(run["id"], envelope, raw=raw)
            except (ValueError, TypeError, KeyError):
                self.db.fail_run(run["id"], processing_failure_code(raw), raw=raw)
            except Exception:
                # An uncertain storage result must not start another collection.
                # If the transaction rolled back, mark it safely; if the DB is
                # unavailable, recover_runs will mark it interrupted on restart.
                self.stop()
                try:
                    self.db.fail_run(run["id"], "unknown_error")
                except Exception:
                    pass
                raise CollectionFailure("unknown_error") from None
        return True

    def _schedule(self) -> None:
        if not self.interval_seconds or self.db.state().get("paused_code"):
            return
        tick = self.clock()
        for group in self.db.list_groups():
            if not group.get("enabled"):
                continue
            previous = self._last_scheduled.get(group["id"])
            if previous is not None and tick - previous < self.interval_seconds:
                continue
            value = group.get("last_success_at")
            last_success = timestamp(value)
            if isinstance(value, datetime) and value.tzinfo is not None:
                last_success = value.astimezone(timezone.utc).isoformat()
            if last_success:
                published = datetime.fromisoformat(last_success.replace("Z", "+00:00"))
                if (self.now() - published).total_seconds() < self.interval_seconds:
                    continue
            self._last_scheduled[group["id"]] = tick
            try:
                self.db.enqueue(group["id"])
            except Exception as exc:
                if type(exc).__name__ not in {"Conflict", "NotFound", "InvalidInput"}:
                    raise

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self, stop_event=None) -> bool:
        self._stop = stop_event if stop_event is not None else threading.Event()
        if isinstance(self.collector, FacebookCrawler):
            self.collector.cancel_event = self._stop
        handlers = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.signal(sig, lambda *_args: self.stop())
        heartbeat_stop = threading.Event()
        heartbeat_thread = None
        owns_heartbeat = False
        try:
            with self.db.worker_lock() as acquired:
                if not acquired:
                    LOG.warning("worker_already_running")
                    return False
                try:
                    # If the lock session died rather than shutting down cleanly,
                    # the old child needs time to notice and terminate. Do not emit
                    # our heartbeat or recover its running job until its lease is
                    # stale. The old owner's heartbeat checks that exact lock session.
                    while self.db.state().get("worker_active"):
                        if self._stop.wait(self.poll_seconds):
                            return True
                    self.db.recover_runs()
                    self.db.set_worker_heartbeat()
                    owns_heartbeat = True

                    def heartbeat():
                        while not heartbeat_stop.wait(5):
                            try:
                                self.db.set_worker_heartbeat()
                            except Exception:
                                LOG.error("worker_heartbeat_failed")
                                self.stop()
                                return

                    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
                    heartbeat_thread.start()
                    while not self._stop.is_set():
                        try:
                            self._schedule()
                            worked = self.process_one()
                        except Exception:
                            LOG.error("worker_database_error")
                            self.stop()
                            worked = False
                        if not worked:
                            self._stop.wait(self.poll_seconds)
                    return True
                finally:
                    # Stop heartbeats and clear our lease before releasing the
                    # advisory lock, so a clean restart need not wait 30 seconds.
                    heartbeat_stop.set()
                    if heartbeat_thread is not None:
                        heartbeat_thread.join(timeout=15)
                    heartbeat_alive = heartbeat_thread is not None and heartbeat_thread.is_alive()
                    if owns_heartbeat and not heartbeat_alive and hasattr(self.db, "clear_worker_heartbeat"):
                        try:
                            # The DB method verifies the same lock session before
                            # clearing. Lost ownership leaves the stale lease intact.
                            self.db.clear_worker_heartbeat()
                        except Exception:
                            LOG.warning("worker_heartbeat_not_cleared")
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)

    run = run_forever
