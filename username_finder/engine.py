from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

from .domain import RULES_VERSION, Classification, Evidence, JobState, SearchConfig
from .providers import DisabledSnapchatVerifier, MockVerifier, ProviderFailure, Verifier
from .sources import CandidateSource
from .storage import Store


class ExecutionEngine:
    """Runs on one background event loop; SQLite persists every accepted source cursor."""

    def __init__(
        self, store: Store, provider: Verifier | None = None, on_update: Callable[[dict], None] | None = None
    ):
        self.store = store
        self.provider = provider
        self.on_update = on_update or (lambda _: None)
        self._control = threading.Event()
        self._reason = JobState.STOPPED
        self.running = False
        self.max_inflight = 0
        self.max_pending = 0
        self.failures = 0
        self.error = ""

    def request_pause(self):
        self._reason = JobState.PAUSED
        self._control.set()

    def request_stop(self):
        self._reason = JobState.STOPPED
        self._control.set()

    def _block(self, reason: str):
        self.error = reason
        self._reason = JobState.BLOCKED
        self._control.set()

    async def _interruptible_delay(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._control.is_set():
                raise asyncio.CancelledError
            await asyncio.sleep(min(0.05, max(0, end - time.monotonic())))

    async def run(self, job_id: str):
        if self.running:
            return
        self.running = True
        self.error = ""
        self.failures = 0
        source = None
        active: dict[asyncio.Task, int] = {}
        started = time.monotonic()
        last_report = started
        baseline = 0.0
        state = JobState.INTERRUPTED
        preserve_state = False
        try:
            job = self.store.job(job_id)
            baseline = job["active_seconds"]
            if job["state"] in {JobState.COMPLETED, JobState.BLOCKED}:
                preserve_state = True
                return
            if job["rules_version"] != RULES_VERSION:
                raise ValueError("RULES_VERSION_CHANGED")
            config = SearchConfig.from_dict(job["config"])
            provider = self.provider or (
                MockVerifier() if config.provider == "mock" else DisabledSnapchatVerifier()
            )
            if config.provider == "real" or provider.mode != "simulated":
                self._block("LIVE_VERIFICATION_NOT_VALIDATED")
                state = JobState.BLOCKED
                return
            if self.store.provider_quarantined(provider.name, provider.version):
                self._block("PROVIDER_QUARANTINED")
                state = JobState.BLOCKED
                return
            source = CandidateSource(config, job["snapshot"], job["cursor"] or None)
            if not self.store.begin_job(job_id):
                preserve_state = True
                return
            started = time.monotonic()
            self.config = config
            self.current_provider = provider
            self.global_retries = job["counters"]["retries"]
            self.rate_lock = asyncio.Lock()
            self.next_request = 0.0
            self.target_workers = min(
                config.workers, provider.safe_concurrency, 5 if config.auto_workers else 1000
            )
            self.successes = 0
            self.source_exhausted = bool(job["source_done"])
            while not self._control.is_set():
                for future in [item for item in active if item.done()]:
                    future.result()
                    active.pop(future)
                # Keep both memory and durable unfinished work bounded, independently of total history size.
                capacity = max(
                    0,
                    min(
                        config.queue_size - self.store.pending_count(job_id),
                        self.target_workers + 16 - len(active),
                    ),
                )
                produced = 0
                while not self.source_exhausted and capacity > 0 and not self._control.is_set():
                    current = self.store.job(job_id)
                    if config.limit is not None and current["counters"]["unique"] >= config.limit:
                        self.source_exhausted = True
                        self.store.source_done(job_id)
                        break
                    record = source.next_record()
                    if record is None:
                        self.source_exhausted = True
                        self.store.source_done(job_id)
                        break
                    outcome = self.store.ingest(
                        job_id, record, config, provider.mode, provider.name, provider.version
                    )
                    if outcome == "candidate":
                        capacity -= 1
                    produced += 1
                    if produced % 32 == 0:
                        await asyncio.sleep(0)
                    if produced >= 128:
                        break
                available = self.target_workers - len(active)
                active_ids = set(active.values())
                if available > 0:
                    for task in self.store.pending(job_id, config.queue_size):
                        if task["id"] not in active_ids:
                            future = asyncio.create_task(self._check(task))
                            active[future] = task["id"]
                            available -= 1
                            if available <= 0:
                                break
                self.max_inflight = max(self.max_inflight, len(active))
                self.max_pending = max(self.max_pending, self.store.pending_count(job_id))
                refill = (
                    not self.source_exhausted
                    and len(active) < self.target_workers
                    and self.store.pending_count(job_id) < config.queue_size
                    and produced >= 128
                )
                if refill:
                    await asyncio.sleep(0)
                elif active:
                    done, _ = await asyncio.wait(active, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
                    for future in done:
                        future.result()
                        active.pop(future)
                elif self.source_exhausted:
                    state = JobState.COMPLETED
                    break
                else:
                    await asyncio.sleep(0)
                now = time.monotonic()
                if now - last_report >= 0.25:
                    self.store.update_active(job_id, baseline + now - started)
                    self.on_update(self.snapshot(job_id, baseline + now - started))
                    last_report = now
            if self._control.is_set():
                state = self._reason
        except asyncio.CancelledError:
            state = JobState.INTERRUPTED
        except Exception as exc:
            # Persist only a safe error code; file names, tokens, or arbitrary provider text are not logged.
            self._block(type(exc).__name__)
            state = JobState.BLOCKED
        finally:
            try:
                if active:
                    _, pending = await asyncio.wait(active, timeout=0.5)
                    for future in pending:
                        future.cancel()
                    await asyncio.gather(*active, return_exceptions=True)
                    for task_id in active.values():
                        self.store.interrupt(task_id)
                if source:
                    source.close()
                # An early return for an already final job must not change that job.
                original = self.store.job(job_id)
                if not preserve_state and (
                    original["state"] not in {JobState.COMPLETED, JobState.BLOCKED} or self.error
                ):
                    self.store.set_state(job_id, state, self.error, baseline + time.monotonic() - started)
                self.on_update(self.snapshot(job_id))
            finally:
                self.running = False
                self._control.clear()

    async def _check(self, task: dict):
        provider = self.current_provider
        config = self.config
        task_id = task["id"]
        attempts = task["attempts"]
        while not self._control.is_set():
            self.store.claim(task_id)
            attempts += 1
            started = time.monotonic()
            try:
                async with self.rate_lock:
                    while self.next_request > time.monotonic():
                        await self._interruptible_delay(min(0.05, self.next_request - time.monotonic()))
                    self.next_request = time.monotonic() + provider.min_interval
                result = await asyncio.wait_for(
                    provider.verify(task["username"]), timeout=config.timeout_seconds
                )
                result.validate()
                if (result.provider, result.provider_version, result.mode) != (
                    provider.name,
                    provider.version,
                    provider.mode,
                ):
                    raise ProviderFailure("PROVIDER_IDENTITY_MISMATCH", retryable=False)
                self.store.finish(task_id, result)
                self.failures = 0
                self.successes += 1
                if config.auto_workers and self.successes % 32 == 0:
                    self.target_workers = min(
                        config.workers,
                        provider.safe_concurrency,
                        max(self.target_workers + 1, self.target_workers * 2),
                    )
                return
            except asyncio.CancelledError:
                self.store.interrupt(task_id)
                raise
            except (ProviderFailure, TimeoutError, OSError, ValueError) as exc:
                if isinstance(exc, ProviderFailure):
                    code, retry_after, retryable = exc.code, exc.retry_after, exc.retryable
                else:
                    code = "SEMANTIC_MISMATCH" if isinstance(exc, ValueError) else type(exc).__name__
                    retry_after, retryable = 0, not isinstance(exc, ValueError)
                # Whitelist safe codes; arbitrary exception messages never enter evidence.
                safe_codes = {
                    "429",
                    "403",
                    "SEMANTIC_MISMATCH",
                    "PROVIDER_IDENTITY_MISMATCH",
                    "TimeoutError",
                    "OSError",
                    "ValueError",
                }
                safe_code = code if code in safe_codes else "PROVIDER_ERROR"
                self.failures += 1
                if config.auto_workers:
                    self.target_workers = max(1, self.target_workers // 2)
                if code in {"SEMANTIC_MISMATCH", "PROVIDER_IDENTITY_MISMATCH"}:
                    self.store.quarantine(provider.name, provider.version)
                should_block = not retryable or code == "403" or self.failures >= config.circuit_threshold
                retry = (
                    not should_block
                    and attempts <= config.max_retries
                    and self.global_retries < config.retry_budget
                    and not self._control.is_set()
                )
                evidence = Evidence(
                    Classification.UNKNOWN,
                    "network_error",
                    provider.name,
                    provider.version,
                    provider.mode,
                    False,
                    latency_ms=(time.monotonic() - started) * 1000,
                    error=safe_code,
                )
                self.store.finish(task_id, evidence, completed=not retry)
                if should_block:
                    self._block(safe_code)
                    return
                if not retry:
                    return
                self.global_retries += 1
                delay = max(retry_after, min(30, 0.1 * (2 ** (attempts - 1))))
                # Retry-After applies to the provider, including other workers.
                self.next_request = max(self.next_request, time.monotonic() + delay)
                await self._interruptible_delay(delay)
        self.store.interrupt(task_id)

    def snapshot(self, job_id: str, active_seconds: float | None = None) -> dict:
        job = self.store.job(job_id)
        seconds = job["active_seconds"] if active_seconds is None else active_seconds
        metrics = self.store.metrics(job_id)
        return {
            "job_id": job_id,
            "state": job["state"],
            "error": job["error"],
            "counters": job["counters"],
            "active_seconds": seconds,
            "decisive_per_minute": metrics["decisive"] * 60 / seconds if seconds > 0 else 0,
            "pending": self.store.pending_count(job_id),
            "effective_workers": getattr(self, "target_workers", 0)
            if job["state"] == JobState.RUNNING
            else 0,
            "simulated": True,
            "storage": self.store.health(),
        }
