"""One sequential, bounded collector worker for the private Phase 1 app."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from normalizer import normalize_run, timestamp
from scripts import phase0

LOG = logging.getLogger(__name__)
ERROR_CODES = {
    "authentication_failed", "session_expired", "checkpoint_required", "rate_limited",
    "account_restricted", "group_not_accessible", "group_not_found", "network_error",
    "parse_error", "configuration_error", "dependency_missing", "unknown_error",
    "pagination_stalled", "invalid_cursor", "interrupted",
}


def safe_code(code: object) -> str:
    return code if isinstance(code, str) and code in ERROR_CODES else "unknown_error"


class CollectionFailure(Exception):
    """Only a fixed code crosses the collector/application boundary."""

    def __init__(self, code: object):
        self.code = safe_code(code)
        super().__init__(self.code)


class FacebookCollector:
    """Invoke the validated Go adapter without logging or keeping session data.

    The lookback is bounded; a page cap can produce an incomplete window. The
    resulting envelope keeps that distinction instead of claiming full coverage.
    """

    def __init__(self, lookback_hours=72, timeout=300, *, root=None,
                 execute=None, resolve_group=None, load_env=None, now=None):
        if not math.isfinite(lookback_hours) or not 0 < lookback_hours <= 8760:
            raise ValueError("lookback_hours must be between 0 and 8760")
        if not math.isfinite(timeout) or not 1 <= timeout <= 3600:
            raise ValueError("timeout must be between 1 and 3600 seconds")
        self.lookback_hours, self.timeout = lookback_hours, timeout
        self.root = Path(root) if root is not None else phase0.ROOT
        self.execute = execute
        self.cancel_event = threading.Event()
        self.resolve_group = resolve_group or phase0.resolve_group
        self.load_env = load_env or phase0.load_local_env
        self.now = now or (lambda: datetime.now(timezone.utc))
        # Exported values at worker launch retain priority. File-sourced values
        # are re-read for each collection, so updating .env then resuming clears
        # a stale session without restarting the worker.
        self._session_baseline = {key: os.environ[key] for key in
                                  ("FACEBOOK_COOKIE", "FACEBOOK_COOKIE_FILE", "FACEBOOK_USER_ID")
                                  if key in os.environ}

    def _execute(self, command, *, cwd, timeout, **_kwargs):
        """Poll the child so shutdown/lock loss can stop and reap it promptly."""
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if self.cancel_event.is_set():
                    raise CollectionFailure("interrupted")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    return subprocess.CompletedProcess(command, process.wait(timeout=min(0.25, remaining)))
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def __call__(self, run: dict) -> dict:
        try:
            if self.cancel_event.is_set():
                raise CollectionFailure("interrupted")
            for key in ("FACEBOOK_COOKIE", "FACEBOOK_COOKIE_FILE", "FACEBOOK_USER_ID"):
                if key in self._session_baseline:
                    os.environ[key] = self._session_baseline[key]
                else:
                    os.environ.pop(key, None)
            self.load_env()
            if not os.environ.get("FACEBOOK_COOKIE"):
                raise CollectionFailure("authentication_failed")
            group_url = run.get("group_url") or run.get("url")
            group = str(self.resolve_group(group_url, timeout=self.timeout))
            if self.cancel_event.is_set():
                raise CollectionFailure("interrupted")
            if not group.isdigit() or int(group) <= 0:
                raise CollectionFailure("group_not_found")
            binary = self.root / ".bin/facebook-go"
            if not binary.is_file():
                raise CollectionFailure("configuration_error")
            end = self.now().astimezone(timezone.utc)
            start = end - timedelta(hours=self.lookback_hours)
            lower, upper = (instant.isoformat().replace("+00:00", "Z") for instant in (start, end))
            collector_run_id = "phase1-" + uuid.uuid4().hex
            with tempfile.TemporaryDirectory(prefix="guitar-radar-") as temporary:
                output = Path(temporary) / "output.json"
                command = [str(binary), "--group", group, "--from", lower, "--to", upper,
                           "--pages", "50", "--page-size", "20", "--timeout", str(self.timeout),
                           "--run-id", collector_run_id, "--output", str(output)]
                timed_out, returncode = False, 0
                try:
                    result = (self.execute or self._execute)(command, cwd=self.root, check=False,
                                          timeout=self.timeout + 10,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    returncode = result.returncode
                except subprocess.TimeoutExpired:
                    timed_out = True
                if not output.is_file():
                    raise CollectionFailure("network_error" if timed_out else "parse_error")
                if output.stat().st_size > 64 * 1024 * 1024:
                    raise CollectionFailure("parse_error")
                try:
                    raw = json.loads(output.read_text(encoding="utf-8"))
                    if not isinstance(raw, dict) or raw.get("run_id") != collector_run_id or raw.get("group_id") != group:
                        raise ValueError
                    if raw.get("collector") != "facebook-go" or raw.get("source") != "live":
                        raise ValueError
                    bounds = raw.get("window") or {}
                    if timestamp(bounds.get("from")) != timestamp(lower) or timestamp(bounds.get("to")) != timestamp(upper):
                        raise ValueError
                    normalized = normalize_run(raw, "facebook-go")
                    if returncode and normalized["status"] == "success":
                        raise ValueError
                except (ValueError, TypeError, KeyError, AttributeError):
                    raise CollectionFailure("parse_error") from None
                # Keep partial rows from a killed adapter only when its current
                # envelope is complete JSON with the expected identity/window.
                if timed_out:
                    normalized["status"] = "partial" if normalized["posts"] else "failed"
                    normalized["errors"].append({"code": "network_error"})
                    normalized["window"].update(complete=False, stop_reason="error")
                normalized["errors"] = [{"code": safe_code(error.get("code"))}
                                        for error in normalized["errors"]]
                return normalized
        except CollectionFailure:
            raise
        except phase0.RunError as exc:
            raise CollectionFailure(exc.code) from None
        except ImportError:
            raise CollectionFailure("dependency_missing") from None
        except OSError:
            raise CollectionFailure("network_error") from None
        except Exception:
            raise CollectionFailure("unknown_error") from None


class Worker:
    """Claim and finish queued runs in order; only one worker owns the DB lock."""

    def __init__(self, db, collector=None, interval_seconds=0, lookback_hours=72,
                 poll_seconds=2, *, clock=time.monotonic, now=None):
        if not math.isfinite(interval_seconds) or interval_seconds < 0:
            raise ValueError("interval_seconds must be nonnegative")
        if not math.isfinite(poll_seconds) or not 0.01 <= poll_seconds <= 60:
            raise ValueError("poll_seconds must be between 0.01 and 60")
        self.db = db
        self.collector = collector or FacebookCollector(lookback_hours=lookback_hours)
        self.interval_seconds, self.poll_seconds = interval_seconds, poll_seconds
        self.clock = clock
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._last_scheduled = {}
        self._stop = threading.Event()
        if isinstance(self.collector, FacebookCollector):
            self.collector.cancel_event = self._stop

    def process_one(self) -> bool:
        if self.db.state().get("paused_code"):
            return False
        run = self.db.claim_next()
        if run is None:
            return False
        try:
            envelope = self.collector(run)
        except CollectionFailure as exc:
            self.db.fail_run(run["id"], exc.code)
        except Exception:
            # Never emit exception text: upstream errors can contain credentials.
            self.db.fail_run(run["id"], "unknown_error")
        else:
            try:
                self.db.complete_run(run["id"], envelope)
            except (ValueError, TypeError, KeyError):
                self.db.fail_run(run["id"], "parse_error")
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
        if isinstance(self.collector, FacebookCollector):
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
