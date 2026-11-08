"""Sequential, cancellation-aware X checks. Every Unknown latches a review stop."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from . import x
from .overnight import OvernightStore, transport_failure


def deadline(headers, finished, delay):
    dates = [finished + timedelta(seconds=delay)]
    value = headers.get("retry-after")
    if value is not None:
        try:
            dates.append(finished + timedelta(seconds=max(0, int(value))))
        except (ValueError, OverflowError):
            try:
                dates.append(parsedate_to_datetime(value).astimezone(UTC))
            except (TypeError, ValueError, OverflowError):
                dates.append(finished + timedelta(minutes=5))
    for prefix in ("x-rate-limit-", "x-ratelimit-"):
        if headers.get(prefix + "remaining", "").strip() == "0":
            try:
                dates.append(datetime.fromtimestamp(float(headers[prefix + "reset"]), UTC))
            except (KeyError, ValueError, OverflowError, OSError):
                dates.append(finished + timedelta(minutes=5))
    return max(dates).isoformat()


class XRunner:
    def __init__(self, repo, *, fetcher=None, clock=None, monotonic=None, wait=None, updated=lambda: None):
        self.repo = repo
        self.fetcher = fetcher
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.cancel = threading.Event()
        self.manual_stop = threading.Event()
        self.disable_overnight = threading.Event()
        self.wait = wait or self.cancel.wait
        self.updated = updated
        self.night = OvernightStore(repo.store, "X")

    def run(self, identity, *, automatic=False):
        state, error = "Paused", ""
        started = False
        try:
            if automatic and not self.night.may_resume(identity):
                return
            if self.cancel.is_set():
                return
            self.repo.start(identity)
            started = True
            self.night.begin(identity, automatic=automatic, clock=self.clock, cancel=self.cancel)
            delay = self.repo.progress(identity)["interval_seconds"]
            route = self.repo.connection_for_run(identity)
            while not self.cancel.is_set():
                if not self.repo.progress(identity)["pending"]:
                    state = "Completed"
                    break
                control = self.repo.control()
                if control.get("blocked"):
                    state, error = "Restricted", control["blocked"]
                    break
                wait_until = self.night.deadline(identity, control)
                remaining = (
                    max(0, (datetime.fromisoformat(wait_until) - self.clock()).total_seconds())
                    if wait_until
                    else 0
                )
                if remaining:
                    self.night.waiting(identity, wait_until, clock=self.clock)
                    self.updated()
                if remaining and self.wait(remaining):
                    break
                if self.cancel.is_set():
                    break
                task = self.repo.claim(identity)
                if not task:
                    state = "Completed"
                    break
                if self.cancel.is_set():
                    self.repo.release(task)
                    break
                request_at = self.clock().isoformat()
                self.night.requesting(task, clock=self.clock)
                start = self.monotonic()
                try:
                    response = (
                        self.fetcher(task["username"], 15.0)
                        if self.fetcher
                        else x.protocol.fetch(task["username"], 15.0, connection=route)
                    )
                except Exception as exc:
                    response = transport_failure(
                        x.protocol.Response(error=type(exc).__name__, complete=False), exc
                    )
                finished = self.clock()
                if self.night.retry(task, response, finished):
                    self.updated()
                    continue
                result = x.result_for(
                    task["username"],
                    response,
                    checked_at=finished.isoformat(),
                    latency=self.monotonic() - start,
                )
                result.update(
                    request_at=request_at, delivery_uncertain=bool(response.error), connection=route
                )
                blocked = result["reason"] if result["classification"] == "Unknown" else None
                self.repo.finish(
                    task, result, not_before=deadline(response.headers, finished, delay), blocked=blocked
                )
                if not blocked:
                    self.night.success(identity, clock=self.clock)
                self.updated()
                if blocked:
                    state, error = "Restricted", blocked
                    break
        except BaseException as exc:
            if started:
                self.repo.recover()
                state, error = "Error", type(exc).__name__
            else:
                raise
        finally:
            if started:
                if self.disable_overnight.is_set():
                    self.night.enable(identity, False)
                if self.manual_stop.is_set():
                    self.night.stop(identity)
                self.repo.end(identity, state, error)
                self.night.end(identity, state, error, clock=self.clock)
                self.updated()
