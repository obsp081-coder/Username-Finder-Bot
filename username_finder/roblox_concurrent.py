"""Opt-in individual workers with serialized durable state and shared admission."""

from __future__ import annotations

import math
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from . import roblox_protocol as protocol
from .overnight import transport_failure
from .roblox import result_for, retry_not_before


def validate_policy(value):
    if not isinstance(value, dict) or set(value) != {"version", "workers", "rate"}:
        raise ValueError("Invalid individual execution settings")
    workers, rate = value["workers"], value["rate"]
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or type(workers) is not int
        or not 1 <= workers <= 8
        or isinstance(rate, bool)
        or not isinstance(rate, (float, int))
        or not math.isfinite(rate)
        or not 0.25 <= rate <= 5
    ):
        raise ValueError("Use 1-8 workers and an aggregate rate of 0.25-5 requests/second")
    return dict(value)


def execution_policy(repo, identity):
    policies = repo.control().get("individual_execution", {})
    if not isinstance(policies, dict):
        raise ValueError("Invalid saved execution settings")
    return validate_policy(policies.get(identity, dict(version=1, workers=1, rate=1.0)))


def configure_execution(repo, identity, *, workers, rate):
    """Explicit local consent, stored in existing JSON; no schema migration."""
    policy = validate_policy(dict(version=1, workers=workers, rate=rate))
    with repo.store.transaction() as db:
        run = db.execute("SELECT state,interval_seconds FROM roblox_runs WHERE id=?", (identity,)).fetchone()
        if not run or run[0] == "Running" or (workers > 1 and run[1] != 0):
            raise ValueError("Concurrent checking requires an idle, zero-interval individual run")
        control = repo._control(db)
        policies = control.setdefault("individual_execution", {})
        if not isinstance(policies, dict):
            raise ValueError("Invalid saved execution settings")
        policies[identity] = policy
        repo._save_control(db, control)


class _NotDispatched(Exception):
    """The guarded connection did not call its underlying HTTP request method."""


class _ObservedConnection:
    """Observe restriction headers before a potentially slow body is consumed."""

    def __init__(self, connection, observe, send=None):
        self.connection, self.observe = connection, observe
        self.send = send

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def request(self, *args, **kwargs):
        if self.send is None:
            raise RuntimeError("HTTP dispatch requires the shared send gate")
        return self.send(self.connection.request, *args, **kwargs)

    def getresponse(self):
        response = self.connection.getresponse()
        self.observe(
            protocol.Response(
                status=response.status,
                headers={
                    key.lower(): value
                    for key, value in response.headers.items()
                    if key.lower() in {"retry-after", "x-ratelimit-remaining", "x-ratelimit-reset"}
                },
                challenge=any(key.lower().startswith("rblx-challenge-") for key in response.headers),
            )
        )
        return response


class ConcurrentExecution:
    """Exactly K long-lived workers, no per-name futures or in-memory backlog.

    Store's existing RLock/FULL-WAL connection serializes all transactions.
    The admission lock covers intent+deadline persistence, never a network wait.
    Each admitted request owns its transport until it has durably settled.
    """

    def __init__(self, parent, identity, policy, activity, *, monotonic=time.monotonic, wall=None, wait=None):
        self.parent, self.repo, self.identity = parent, parent.repository, identity
        self.policy, self.activity = validate_policy(policy), activity
        self.monotonic, self.wall = monotonic, wall or parent.clock
        self.halt = threading.Event()
        self.wait = wait or self.halt.wait
        self.admission = threading.Lock()
        self.send_gate = threading.Lock()
        self.results = threading.Lock()
        self.monitor = threading.Lock()
        self.header_failures = threading.local()
        self.marker_lock = threading.Lock()
        self.gap = 1 / self.policy["rate"]
        self.next_start = monotonic() + self.gap  # No initial burst.
        self.next_send = self.next_start
        self.started = monotonic()
        self.ended = None
        self.state, self.reason = "Paused", ""
        self.restricted = False
        self.attempts = self.http = self.accepted = self.active = self.outstanding = 0
        self.http_ok = 0
        self.peak = 0
        self.counts = Counter()
        self.waiting = False

    def snapshot(self):
        with self.monitor:
            elapsed = max(0, (self.ended if self.ended is not None else self.monotonic()) - self.started)
            return dict(
                workers=self.policy["workers"],
                rate=self.policy["rate"],
                elapsed=elapsed,
                attempts=self.attempts,
                http_responses=self.http,
                http_200=self.http_ok,
                attempts_per_second=self.attempts / elapsed if elapsed else 0,
                accepted=self.accepted,
                accepted_per_second=self.accepted / elapsed if elapsed else 0,
                results_per_minute=60 * self.accepted / elapsed if elapsed else 0,
                active=self.active,
                outstanding=self.outstanding,
                peak=self.peak,
                counts=dict(self.counts),
                waiting=self.waiting and self.ended is None and not self.stopped(),
                reason=self.reason,
            )

    def stopped(self):
        return self.halt.is_set() or self.parent.cancel.is_set()

    def restrict(self, reason):
        with self.monitor:
            self.restricted = True
            self.state = "Restricted"
            self.reason = self.reason or reason
            self.halt.set()
        # The existing shared marker also protects old clients and failed SQL
        # writes. Its existing contents are authoritative and never overwritten.
        try:
            with self.marker_lock:
                self.activity.restrict("individual_review_required")
        except BaseException as exc:
            self.header_failures.error = exc
            try:
                with self.repo.store.transaction() as db:
                    control = self.repo._control(db)
                    control["blocked"] = True
                    self.repo._save_control(db, control)
            except BaseException as fallback_error:
                raise exc from fallback_error
            raise

    def observe_headers(self, response):
        if self.restricted_response(response):
            self.restrict("provider_restricted")
            # Persist the stop even if the body/worker/process never completes.
            try:
                with self.results, self.repo.store.transaction() as db:
                    control = self.repo._control(db)
                    control["blocked"] = True
                    self.repo._save_control(db, control)
            except BaseException as exc:
                self.header_failures.error = exc
                raise

    def admit(self):
        with self.admission:
            while not self.stopped():
                if self.activity.block.exists():
                    self.restrict("shared_restriction")
                    return None
                control = self.repo.control()
                if control.get("blocked"):
                    self.restrict("provider_review_required")
                    return None
                delay = max(0, self.next_start - self.monotonic())
                if control.get("not_before"):
                    deadline = datetime.fromisoformat(control["not_before"])
                    if deadline.tzinfo is None:
                        raise ValueError("Invalid saved admission deadline")
                    delay = max(delay, (deadline - self.wall()).total_seconds())
                if delay > 0:
                    with self.monitor:
                        self.waiting = True
                    self.wait(min(delay, 0.05))
                    continue
                # Queue intent and the next admissible time share one FULL transaction.
                task = self.repo.claim_next(
                    self.identity, not_before=(self.wall() + timedelta(seconds=self.gap)).isoformat()
                )
                if task is None:
                    with self.monitor:
                        self.waiting = False
                    return None
                if self.stopped():
                    self.repo.release_unstarted(task)
                    return None
                self.next_start = self.monotonic() + self.gap
                with self.monitor:
                    self.waiting = False
                    self.attempts += 1
                    self.active += 1
                    self.outstanding += 1
                    self.peak = max(self.peak, self.active)
                return task
        return None

    def transmit(self, request, *args, **kwargs):
        # TLS setup/reconnection can reorder workers after durable admission.
        # Serialize actual HTTP starts as well, holding no SQL or response lock.
        with self.send_gate:
            while not self.stopped():
                if self.activity.block.exists() or self.repo.control().get("blocked"):
                    self.restrict("shared_restriction")
                    break
                delay = self.next_send - self.monotonic()
                if delay > 0:
                    self.wait(min(delay, 0.05))
                    continue
                self.next_send = self.monotonic() + self.gap
                return request(*args, **kwargs)
        raise _NotDispatched()

    @staticmethod
    def restricted_response(response, result=None):
        return (
            response.status in (401, 403, 429)
            or (response.status is not None and 300 <= response.status < 400)
            or response.challenge
            or "retry-after" in response.headers
            or response.headers.get("x-ratelimit-remaining") == "0"
            or (result is not None and result["reason"] == "restriction_message")
        )

    def finish(self, task, response, request_at, finished):
        if self.restricted_response(response):
            self.restrict("provider_restricted")  # Before parsing/SQLite/callback work.
        result = result_for(task["username"], response, checked_at=finished.isoformat())
        result["request_at_utc"] = request_at
        if self.restricted_response(response, result):
            self.restrict("provider_restricted")
        with self.results:
            control = self.repo.control()
            streak = control.get("unknown_streak", 0) + 1 if result["classification"] == "Unknown" else 0
            updates = {"unknown_streak": streak}
            if self.restricted or streak >= 2 or control.get("blocked"):
                self.restrict("consecutive_unknown_review_required" if streak >= 2 else "provider_restricted")
                updates["blocked"] = True  # A later successful response cannot clear the latch.
            if self.restricted_response(response, result):
                updates["restriction_streak"] = control.get("restriction_streak", 0) + 1
                updates["not_before"] = retry_not_before(response.headers.get("retry-after"), at=finished)
            if response.headers.get("x-ratelimit-remaining") == "0":
                updates["not_before"] = max(
                    updates.get("not_before", ""),
                    retry_not_before(response.headers.get("x-ratelimit-reset"), at=finished),
                )
            self.repo.finish_check(task, result, control_update=updates, response_finished_at=finished)
            with self.monitor:
                self.outstanding -= 1
                self.counts[result["classification"]] += 1
                if result["classification"] != "Unknown":
                    self.accepted += 1
        self.parent.updated()

    def worker(self):
        transport = None
        try:
            if self.parent.fetcher is None:
                transport = protocol.PersistentTransport()
                factory = transport.connection_factory
                transport.connection_factory = lambda: _ObservedConnection(
                    factory(), self.observe_headers, self.transmit
                )
            fetch = self.parent.fetcher or transport.fetch
            while not self.stopped():
                task = self.admit()
                if task is None:
                    return
                requested = self.wall().isoformat()
                try:
                    response = fetch(protocol.request_url(task["username"]))
                except _NotDispatched:
                    self.repo.release_unstarted(task)
                    with self.monitor:
                        self.active -= 1
                        self.outstanding -= 1
                    return
                except Exception as exc:
                    response = transport_failure(
                        protocol.Response(error="unexpected_transport_error", complete=False), exc
                    )
                if getattr(self.header_failures, "error", None) is not None:
                    raise self.header_failures.error
                finished = self.wall()
                with self.monitor:
                    self.active -= 1
                    self.http += int(response.status is not None)
                    self.http_ok += int(response.status == 200)
                self.finish(task, response, requested, finished)
        except BaseException:
            self.halt.set()
            raise
        finally:
            if transport is not None:
                try:
                    transport.close()
                except BaseException:
                    self.halt.set()
                    raise

    def run(self, *, automatic=False):
        from .roblox_restriction import checking_permission

        if automatic or (self.parent.night.get(self.identity) or {}).get("enabled"):
            raise ValueError("Concurrent individual checking requires manual start without overnight retries")
        status = checking_permission(self.repo, at=self.wall(), lock_held=True, purpose="individual")
        if not status["allowed"] or self.repo.control().get("restriction_streak", 0):
            raise ValueError("Saved Roblox restrictions require review before concurrent checking")
        if self.parent.cancel.is_set() or not self.repo.start(self.identity):
            return
        failure = None
        try:
            with self.repo.store.lock:
                first_check = self.repo.store.db.execute(
                    "SELECT COALESCE(MAX(id),0) FROM roblox_checks"
                ).fetchone()[0]
            with ThreadPoolExecutor(
                max_workers=self.policy["workers"], thread_name_prefix="roblox-individual"
            ) as pool:
                futures = []
                try:
                    for _ in range(self.policy["workers"]):
                        futures.append(pool.submit(self.worker))
                except BaseException as exc:
                    self.halt.set()
                    failure = exc
                for future in futures:
                    try:
                        future.result()
                    except BaseException as exc:
                        self.halt.set()
                        failure = failure or exc
            if failure is not None:
                self.state, self.reason = "Error", type(failure).__name__
                try:
                    if self.restricted and not self.activity.block.exists():
                        with self.repo.store.transaction() as db:
                            control = self.repo._control(db)
                            control["blocked"] = True
                            self.repo._save_control(db, control)
                    self.repo.recover()  # All workers joined; uncertain intents become Unknown, no replay.
                except BaseException as recovery_error:
                    raise failure from recovery_error
                # Reconcile even an exception AFTER a successful SQL commit.
                with self.repo.store.lock:
                    rows = self.repo.store.db.execute(
                        "SELECT classification,completed,COUNT(*) FROM roblox_checks "
                        "WHERE id>? AND run_id=? GROUP BY classification,completed",
                        (first_check, self.identity),
                    ).fetchall()
                with self.monitor:
                    self.counts = Counter()
                    self.accepted = 0
                    for classification, completed, count in rows:
                        self.counts[classification] += count
                        if completed and classification != "Unknown":
                            self.accepted += count
                    self.outstanding = self.active = 0
            elif not self.restricted and not self.repo.has_pending(self.identity):
                self.state = "Completed"
        except BaseException as exc:
            self.halt.set()
            self.state, self.reason = "Error", type(exc).__name__
            raise
        finally:
            self.ended = self.monotonic()
            self.repo.end(self.identity, self.state, self.reason)
            self.parent.updated()
        if failure is not None:
            raise failure
