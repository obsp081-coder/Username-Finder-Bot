"""One durable Roblox worker with optional bounded transport recovery."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from . import roblox_protocol as protocol
from .overnight import OvernightStore, transport_failure
from .roblox import result_for, retry_not_before
from .roblox_activity import RobloxActivity


class RobloxRunner:
    def __init__(self, repository, *, fetcher=None, wait=None, updated=lambda: None, clock=None):
        self.repository = repository
        self.fetcher = fetcher
        self.wait = wait
        self.updated = updated
        self.clock = clock or (lambda: datetime.now(UTC))
        self.cancel = threading.Event()
        self.manual_stop = threading.Event()
        self.disable_overnight = threading.Event()
        self.night = OvernightStore(repository.store, "Roblox")

    def run(self, run_id, *, automatic=False):
        # This guard also covers overnight and scheduled entry points.
        with RobloxActivity(self.repository.store.path):
            return self._run(run_id, automatic=automatic)

    def _run(self, run_id, *, automatic=False):
        repo = self.repository
        started = False
        transport = None
        state, error = "Paused", ""
        try:
            if automatic and not self.night.may_resume(run_id):
                return
            if self.cancel.is_set() or not repo.start(run_id):
                return
            started = True
            if self.fetcher is None:
                transport = protocol.PersistentTransport()
            fetch = self.fetcher if self.fetcher is not None else transport.fetch
            self.night.begin(run_id, automatic=automatic, clock=self.clock, cancel=self.cancel)
            while not self.cancel.is_set():
                if not repo.has_pending(run_id):
                    state = "Completed"
                    break
                control = repo.control()
                if control.get("blocked"):
                    state, error = "Restricted", "provider_review_required"
                    break
                delay = 0.0
                deadline = self.night.deadline(run_id, control)
                if deadline:
                    delay = max(0, (datetime.fromisoformat(deadline) - self.clock()).total_seconds())
                if delay:
                    self.night.waiting(run_id, deadline, clock=self.clock)
                    self.updated()
                    # Progress callbacks also consume part of the post-response interval.
                    delay = max(0, (datetime.fromisoformat(deadline) - self.clock()).total_seconds())
                    if delay and (self.wait or self.cancel.wait)(delay):
                        break
                if self.cancel.is_set():
                    break
                task = repo.claim_next(run_id, night=self.night, clock=self.clock)
                if task is None:
                    state = "Completed"
                    break
                if self.cancel.is_set():
                    repo.release_unstarted(task, night=self.night)
                    break
                request_at = self.clock().isoformat()
                try:
                    response = fetch(protocol.request_url(task["username"]))
                except Exception as exc:
                    # Preserve an uncertain outcome instead of repeating the external request.
                    response = transport_failure(
                        protocol.Response(error="unexpected_transport_error", complete=False), exc
                    )
                response_finished_at = self.clock()
                if self.night.retry(task, response, response_finished_at):
                    self.updated()
                    continue
                result = result_for(task["username"], response, checked_at=response_finished_at.isoformat())
                result["request_at_utc"] = request_at
                updates = {}
                unknown_streak = (
                    control.get("unknown_streak", 0) + 1 if result["classification"] == "Unknown" else 0
                )
                updates["unknown_streak"] = unknown_streak
                restricted = (
                    response.status in (401, 403, 429)
                    or response.challenge
                    or result["reason"] == "restriction_message"
                    or "retry-after" in response.headers
                )
                if restricted:
                    restrictions = control.get("restriction_streak", 0) + 1
                    updates.update(
                        restriction_streak=restrictions,
                        not_before=retry_not_before(
                            response.headers.get("retry-after"), at=response_finished_at
                        ),
                    )
                    permanent = (
                        response.status in (401, 403)
                        or response.challenge
                        or result["reason"] == "restriction_message"
                        or restrictions >= 2
                    )
                    updates["blocked"] = permanent
                    state, error = "Restricted", "provider_restricted"
                elif result["classification"] != "Unknown":
                    updates["restriction_streak"] = 0
                if response.headers.get("x-ratelimit-remaining") == "0":
                    updates["not_before"] = max(
                        updates.get("not_before", ""),
                        retry_not_before(response.headers.get("x-ratelimit-reset"), at=response_finished_at),
                    )
                if unknown_streak >= 2:
                    updates["blocked"] = True
                    state, error = "Restricted", "consecutive_unknown_review_required"
                repo.finish_check(
                    task,
                    result,
                    control_update=updates,
                    response_finished_at=response_finished_at,
                    night=self.night,
                    clock=self.clock,
                )
                night = self.night.get(run_id)
                if result["classification"] == "Unknown" and night and night["enabled"]:
                    state, error = "Restricted", "overnight_unknown_review_required"
                self.updated()
                if restricted or unknown_streak >= 2 or state == "Restricted":
                    break
        except Exception as exc:
            state, error = "Error", type(exc).__name__
            if started:
                repo.recover()
            else:
                raise
        finally:
            if transport is not None:
                transport.close()
            if started:
                if self.disable_overnight.is_set():
                    self.night.enable(run_id, False)
                if self.manual_stop.is_set():
                    self.night.stop(run_id)
                repo.end(run_id, state, error)
                self.night.end(run_id, state, error, clock=self.clock)
                self.updated()
