#!/usr/bin/env python3
"""RoValid v1.0 - Roblox username availability checker.

Two-stage async engine:
  Stage 1  bulk existence screen  (200 names per request)
  Stage 2  signup validator       (survivors only, confirms availability)
"""

from __future__ import annotations

# ── Python 3.13 SSL bug fix ───────────────────────────────────────────────
import asyncio.sslproto as _sslproto

_orig_eof = _sslproto.SSLProtocol.eof_received


def _safe_eof(self):
    try:
        return _orig_eof(self)
    except RuntimeError:
        return False


_sslproto.SSLProtocol.eof_received = _safe_eof
# ──────────────────────────────────────────────────────────────────────────

# ── Windows ConnectionResetError noise ────────────────────────────────────
# Roblox resets idle TLS sockets rather than closing them politely. On
# Windows the Proactor loop then calls shutdown() on the dead socket from a
# callback, where nothing catches the resulting ConnectionResetError, so
# asyncio prints a full traceback per socket - dozens of them at the end of
# a run that otherwise finished fine. The connection is already gone at this
# point and there is nothing to recover, so the error is pure noise.
try:
    import asyncio.proactor_events as _proactor

    _orig_conn_lost = _proactor._ProactorBasePipeTransport._call_connection_lost

    def _safe_conn_lost(self, exc):
        try:
            return _orig_conn_lost(self, exc)
        except ConnectionResetError:
            return None

    _proactor._ProactorBasePipeTransport._call_connection_lost = _safe_conn_lost
except (ImportError, AttributeError):  # pragma: no cover - non-Windows loops
    pass
# ──────────────────────────────────────────────────────────────────────────

import argparse
import asyncio
from contextlib import nullcontext
import math
import signal
import sys
import time
from collections import deque
from diagnostics import RateLimitMonitor, RateLimitStop
from storage import PendingSnapshots, RecoveryError, ResultsBusyError, ResultsLock, sync_file

import aiohttp
from rich.live import Live
from rich.style import Style
from rich.text import Text

import config as _config
from config import (
    BATCH_MAX,
    DATA_DIR,
    LOGS_DIR,
    MAX_CONCURRENCY,
    RESULTS_DIR,
    AppSettings,
    Config,
    RunConfig,
    Stats,
    ensure_dir,
    ensure_file,
    is_valid_username,
    load_lines,
)
from engine import (
    AVAILABLE,
    ERROR,
    MALFORMED_CHUNK,
    CENSORED,
    EXHAUSTED,
    INVALID,
    TAKEN,
    CircuitBreaker,
    RobloxChecker,
    WebhookSender,
    debug_enabled as _debug_enabled,
    set_debug,
)
from proxy import ProxyManager
from ui import C, banner, console, final_summary, live_card
from wizard import generate_usernames, setup_wizard


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> AppSettings:
    parser = argparse.ArgumentParser(
        description="RoValid - Roblox username availability checker",
    )
    parser.add_argument("-d", "--debug", action="store_true",
                        help="Enable debug output (request/response logs)")
    parser.add_argument("-n", "--no-wizard", action="store_true",
                        help="Skip setup wizard - use saved config and files")
    parser.add_argument("--no-stream", dest="stream", action="store_false",
                        help="Don't print each check above the live panel")
    parser.add_argument("-s", "--stream", dest="stream", action="store_true",
                        help=argparse.SUPPRESS)  # on by default; kept working
    parser.set_defaults(stream=AppSettings.stream)
    parser.add_argument("--diag", action="store_true",
                        help="Write logs/diag.csv samples and immediate logs/rate_limit_events.csv events")
    parser.add_argument("--stop-on-429", action="store_true",
                        help="Save and stop when the rolling 429 threshold is reached")
    parser.add_argument("--rate-limit-threshold", type=int, default=5,
                        help="429 responses across both endpoints to trigger stop (default: 5)")
    parser.add_argument("--rate-limit-window", type=float, default=60.0,
                        help="Rolling stop window in seconds (default: 60; not an API quota)")
    parser.add_argument("--proxyless-min-gap", type=float, default=0.0, metavar="SECONDS",
                        help="Minimum request start gap across both endpoints without proxies (default: 0/off)")
    parser.add_argument("--version", action="version",
                        version=f"RoValid v{_config.VERSION}")
    args = parser.parse_args()
    if args.rate_limit_threshold < 1 or not math.isfinite(args.rate_limit_window) or args.rate_limit_window <= 0:
        parser.error("Rate-limit threshold and window must be positive; window must be finite")
    if not math.isfinite(args.proxyless_min_gap) or args.proxyless_min_gap < 0:
        parser.error("Proxyless minimum gap must be finite and non-negative")
    return AppSettings(debug=args.debug, no_wizard=args.no_wizard,
                       stream=args.stream, diag=args.diag, stop_on_429=args.stop_on_429,
                       rate_limit_threshold=args.rate_limit_threshold,
                       rate_limit_window=args.rate_limit_window,
                       proxyless_min_gap=args.proxyless_min_gap)


# ---------------------------------------------------------------------------
# Graceful stop
# ---------------------------------------------------------------------------

class _StopRequest:
    """Ctrl+C asks the run to wind down instead of killing it.

    A KeyboardInterrupt out of asyncio.run() skips everything after the
    worker loop - which is where the summary is printed and unresolved.txt
    is written - so an interrupted run used to lose both. Hits were never at
    risk (they are flushed as they are found), but the retry list was, and
    that is the part you cannot reconstruct.

    So the first Ctrl+C only sets a flag. The worker loop notices, cancels
    what is still in flight, and falls through to the normal finalisation
    path. A second Ctrl+C restores the default handler and kills the process
    outright, for when winding down is itself the thing that is stuck.
    """

    def __init__(self) -> None:
        self.requested = False
        self.reason = None
        self._rate_limit_history = deque()
        self._interrupt_received = False
        self._previous = None

    def install(self) -> None:
        try:
            self._previous = signal.signal(signal.SIGINT, self._handle)
        except (ValueError, OSError):
            # Not the main thread, or no signal support - Ctrl+C then just
            # behaves as it did before.
            self._previous = None

    def restore(self) -> None:
        if self._previous is not None:
            try:
                signal.signal(signal.SIGINT, self._previous)
            except (ValueError, OSError):
                pass
            self._previous = None

    def _handle(self, signum, frame) -> None:
        if self._interrupt_received:
            self.restore()
            raise KeyboardInterrupt
        self._interrupt_received = True
        self.requested = True
        console.print(
            f"\n[{C.WARNING}]Stopping[/] "
            f"[{C.MUTED}]- finishing what is in flight, then saving. "
            f"Ctrl+C again to quit now.[/]"
        )


# ---------------------------------------------------------------------------
# Event-loop noise
# ---------------------------------------------------------------------------

def _quiet_unclosed_connections() -> None:
    """Drop aiohttp's "Unclosed connection" notices from the event loop.

    aiohttp reports these from Connection.__del__ - a garbage-collector hook
    that also calls _release(should_close=True), so the socket is already
    being tidied by the time the notice is emitted, and the response it
    carried was read long before. Roblox and free proxies both drop idle
    sockets constantly, so a big run prints thousands of them and buries the
    output that matters.

    Only this one message is filtered; everything else still reaches the
    default handler, and --debug turns the filter off so nothing is hidden
    when you are actually looking for a fault.
    """
    if _debug_enabled():
        return
    loop = asyncio.get_running_loop()

    def handler(lp, context):
        if context.get("message") in (
            "Unclosed connection", "Unclosed client session",
        ):
            return
        lp.default_exception_handler(context)

    loop.set_exception_handler(handler)


# ---------------------------------------------------------------------------
# RPS calculator
# ---------------------------------------------------------------------------

async def _rps_calculator(stats: Stats, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        prev_req, prev_resolved, started = stats.requests, stats.definitive, time.monotonic()
        await asyncio.sleep(1)
        elapsed = time.monotonic() - started
        if elapsed > 0:
            stats.set_rps((stats.requests - prev_req) / elapsed)
            stats.set_checks_rps((stats.definitive - prev_resolved) / elapsed)


DIAG_INTERVAL = 5.0
_DIAG_FIELDS = (
    "screened", "ok", "http_err", "conn_err", "ratelimited", "no_proxy",
    "proxies_usable", "proxies_resting", "proxies_buried",
    "resolved", "requests", "resolved_per_second", "http_per_second", "efficiency",
)


async def _diag_sampler(stats: Stats, pm, stop_event: asyncio.Event,
                        path) -> None:
    """Write a per-interval breakdown of the run to *path*.

    Deltas, not totals: a run that decays needs the shape over time, and
    cumulative counters hide it. Each row says what happened during that
    interval and what the proxy pool looked like at the end of it, so the
    column that grows as throughput falls is the cause.
    """
    prev = {f: 0 for f in ("screened", "ok_responses", "http_errors",
                           "conn_errors", "ratelimited", "no_proxy", "definitive", "requests")}
    started = previous_time = time.monotonic()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("elapsed," + ",".join(_DIAG_FIELDS) + "\n")
        fh.flush()
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), DIAG_INTERVAL)
            except asyncio.TimeoutError:
                pass
            now = {f: getattr(stats, f) for f in prev}
            delta = {f: now[f] - prev[f] for f in prev}
            prev = now
            sampled = time.monotonic()
            interval = max(sampled - previous_time, 1e-9)
            previous_time = sampled
            usable, resting, buried = pm.pool_state()
            fh.write(
                f"{sampled - started:.0f},"
                f"{delta['screened']},{delta['ok_responses']},"
                f"{delta['http_errors']},{delta['conn_errors']},"
                f"{delta['ratelimited']},{delta['no_proxy']},"
                f"{usable},{resting},{buried},"
                f"{delta['definitive']},{delta['requests']},"
                f"{delta['definitive'] / interval:.6f},{delta['requests'] / interval:.6f},"
                f"{stats.definitive / stats.requests if stats.requests else 0:.6f}\n"
            )
            fh.flush()


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

# Stage-1 attempts on one chunk before giving up and paying stage-2 prices.
#
# The trade is lopsided and this number was set as if it were not. Giving up
# on a 200-name chunk costs 200 requests, one per name; trying again costs
# one. So retrying stays cheaper than surrendering for something like 200
# tries, and the old value of 5 gave up roughly forty times too early.
#
# Measured on a 100,000-name run over a screened 128-proxy pool: 288 of 500
# chunks - 58% of the entire job - ran out of tries and fell through, so
# 57,600 names were checked one at a time. That single decision accounted
# for 61,677 of the run's 67,193 requests and dropped efficiency to 1.5
# names per request against a ceiling of 200.
#
# 40 is well inside the point where retrying is still the cheaper option
# (at most 40 requests against 200) while keeping a genuinely unscreenable
# chunk from spinning forever.
MAX_CHUNK_TRIES = 40


async def _run_checker(
    cfg: RunConfig,
    settings: AppSettings,
    *,
    round_no: int = 1,
    cooldown=None,
    already_checked: set[str] | None = None,
    pending_names: dict[str, str] | None = None,
    stop: "_StopRequest | None" = None,
    trial=None,
    _ownership=None,
) -> int:
    """Run normally, or observe one explicitly supplied, isolated trial."""
    kwargs = dict(round_no=round_no, cooldown=cooldown,
                  already_checked=already_checked, pending_names=pending_names, stop=stop,
                  _ownership=_ownership)
    if trial is None:
        return await _run_checker_impl(cfg, settings, **kwargs)
    if already_checked or pending_names:
        raise ValueError("A benchmark trial cannot inherit completed or pending work")
    trial.begin(cfg, settings, started_at=trial.metrics.now())
    failure = None
    try:
        return await _run_checker_impl(
            cfg, settings, **kwargs, metrics=trial.metrics,
            results_dir=trial.results_dir, logs_dir=trial.logs_dir,
        )
    except BaseException as exc:
        failure = exc
        raise
    finally:
        trial.metrics.finish(failure)
        try:
            trial.write_report()
        except Exception as exc:
            trial.metrics.persistence_failure("metrics_report")
            if failure is None:
                raise
            console.print(f"[{C.DANGER}]Additional trial report failure: {exc!r}[/]")


async def _run_checker_impl(
    cfg: RunConfig,
    settings: AppSettings,
    *,
    round_no: int = 1,
    cooldown=None,
    already_checked: set[str] | None = None,
    pending_names: dict[str, str] | None = None,
    stop: "_StopRequest | None" = None,
    metrics=None,
    results_dir=None,
    logs_dir=None,
    _ownership=None,
) -> int:
    """Own results before reading state, including direct programmatic calls."""
    directory = RESULTS_DIR if results_dir is None else results_dir
    owner = asyncio.current_task()
    if _ownership is not None:
        _ownership.verify(directory, owner)
    with (nullcontext(_ownership) if _ownership is not None else ResultsLock(directory, owner)):
        resources = {}
        try:
            return await _run_checker_owned(
                cfg, settings, round_no=round_no, cooldown=cooldown,
                already_checked=already_checked, pending_names=pending_names, stop=stop,
                metrics=metrics, results_dir=directory, logs_dir=logs_dir, _resources=resources,
            )
        finally:
            # Setup can fail before the worker cleanup block exists. Ownership
            # still covers closing every resource acquired so far.
            if resources:
                active = sys.exc_info()[1]

                async def close_setup():
                    errors = []
                    helpers = resources.pop("helpers", [])
                    for task in helpers:
                        task.cancel()
                    for result in await asyncio.gather(*helpers, return_exceptions=True):
                        if isinstance(result, Exception):
                            errors.append(result)
                    for key, resource in list(resources.items()):
                        try:
                            if key == "session":
                                await resource.close()
                            else:
                                resource.close()
                        except Exception as exc:
                            errors.append(exc)
                    if errors:
                        raise errors[0]

                closing = asyncio.create_task(close_setup())
                cancelled = None
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError as exc:
                        cancelled = exc
                    except Exception:
                        break
                try:
                    closing.result()
                except Exception as exc:
                    if active is None and cancelled is None:
                        raise
                    console.print(f"[{C.DANGER}]Additional setup cleanup failure: {exc!r}[/]")
                if cancelled is not None:
                    raise cancelled


async def _run_checker_owned(
    cfg, settings, *, round_no=1, cooldown=None, already_checked=None,
    pending_names=None, stop=None, metrics=None, results_dir=None, logs_dir=None, _resources,
) -> int:
    """Two-stage checker with a live display. Returns the hit count.

    Repeat rounds share cooldown, completed names, and pending work so a
    later round neither re-learns the rate limit nor forgets unfinished names.
    """

    _quiet_unclosed_connections()

    results_dir = RESULTS_DIR if results_dir is None else results_dir
    logs_dir = LOGS_DIR if logs_dir is None else logs_dir
    if settings.stop_on_429 and settings.rate_limit_threshold < 1:
        raise ValueError("Enabled 429 stop requires a positive threshold")
    gap = settings.proxyless_min_gap
    if (isinstance(gap, bool) or not isinstance(gap, (int, float))
            or not math.isfinite(gap) or gap < 0):
        raise ValueError("Proxyless minimum gap must be finite and non-negative")
    stop = stop if stop is not None else _StopRequest()
    started_monotonic = time.monotonic()
    started_wall = time.time()

    def safety_stop(reason):
        stop.requested, stop.reason = True, reason

    diagnostics = (RateLimitMonitor(
        logs_dir / "rate_limit_events.csv" if settings.diag else None,
        threshold=settings.rate_limit_threshold if settings.stop_on_429 else 0,
        window=settings.rate_limit_window, on_stop=safety_stop, started=started_monotonic,
        history=getattr(stop, "_rate_limit_history", None),
    ) if settings.diag or settings.stop_on_429 else None)
    if diagnostics is not None:
        _resources["diagnostics"] = diagnostics
    setup_operation = "results_setup"
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
        hits_path = results_dir / "hits.txt"
        try:
            hit_text = hits_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            hit_text = ""
        hit_lines = [line.strip() for line in hit_text.splitlines() if line.strip()]
        # Only complete, valid historical records may retire pending recovery work.
        # Reject ambiguous files without rewriting them or starting any workers.
        if (hit_text and not hit_text.endswith("\n")) or any(
            not is_valid_username(name) for name in hit_lines
        ):
            raise ValueError("hits.txt contains an incomplete or invalid record; review it before recovery")
        saved_hits = {name.lower() for name in hit_lines}
        pending_names = pending_names if pending_names is not None else {}
        # Preserve previous runs' pending work without silently scheduling it.
        snapshots = PendingSnapshots(results_dir)
        for name in snapshots.load():
            # A checked pending intent may represent a newer unfinished explicit
            # recheck. An older saved hit must not silently retire that work.
            if snapshots.has_checked_intent or name.lower() not in saved_hits:
                pending_names.setdefault(name.lower(), name)
        # Preserve the existing case-insensitive cohort and round filtering.
        _seen = set()
        names = []
        for name in cfg.usernames:
            key = name.lower()
            if key not in _seen:
                _seen.add(key)
                if not already_checked or key not in already_checked:
                    names.append(name)
        duplicates = len(cfg.usernames) - len(names)
        total_names = len(names)
        for name in names:
            pending_names.setdefault(name.lower(), name)
        # No transport/session is constructed until all pending inputs are saved.
        setup_operation = "pending_checkpoint"
        snapshots.write(pending_names.values())
        setup_operation = "results_setup"
        hits_file = hits_path.open("a", encoding="utf-8")
        _resources["hits"] = hits_file
    except (OSError, UnicodeError, ValueError):
        if metrics is not None:
            metrics.persistence_failure(setup_operation)
        raise
    hits_lock = asyncio.Lock()

    pm = ProxyManager(cfg.proxies, remove_on_fail=cfg.remove_bad_proxies, scored=cfg.scraped)
    stats = Stats()
    start_time = started_wall

    if cfg.concurrency > MAX_CONCURRENCY:
        cfg.concurrency = MAX_CONCURRENCY

    # Bump file-descriptor limit (macOS defaults to 256)
    try:
        import resource as _resource
        _soft, _hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
        _resource.setrlimit(_resource.RLIMIT_NOFILE, (_hard if _hard > 1024 else 4096, _hard))
    except Exception:
        pass

    connector = aiohttp.TCPConnector(
        limit=max(cfg.concurrency * 2, 100),
        limit_per_host=0,
        enable_cleanup_closed=True,
        ttl_dns_cache=300,
        # Hold idle sockets open well past aiohttp's 15s default. Both stages
        # hit only two hosts, so a warm pool means a TLS handshake per
        # connection for the whole run instead of one every 15 idle seconds.
        keepalive_timeout=60,
    )
    session = aiohttp.ClientSession(
        connector=connector,
        trust_env=False,
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=8, sock_read=30),
        headers={"User-Agent": "Mozilla/5.0 (compatible; RoValid/1.0)"},
    )
    _resources["session"] = session

    # Circuit breaker for a single rotating proxy
    cb: CircuitBreaker | None = None
    paused = False
    if pm.is_single and not pm.is_proxyless:
        async def _on_circuit_open():
            nonlocal paused
            paused = True
            stats.inc("circuit_opens")
            await asyncio.sleep(2.0)
            paused = False
        cb = CircuitBreaker(threshold=10, window=2.0, cooldown=2.0, on_open=_on_circuit_open)

    checker = RobloxChecker(
        pm, timeout=cfg.timeout, scraped=cfg.scraped,
        circuit_breaker=cb, stats=stats, cooldown=cooldown,
        # Both stages run at once now, so the concurrency the user picked is
        # enforced as one shared in-flight request budget inside the checker
        # rather than as a worker count per stage.
        max_inflight=cfg.concurrency,
        **({"metrics": metrics} if metrics is not None else {}),
        **({"diagnostics": diagnostics} if diagnostics is not None else {}),
        **({"min_request_gap": gap, "stop_requested": lambda: stop.requested}
           if gap > 0 and pm.is_proxyless else {}),
    )

    webhook: WebhookSender | None = None
    if cfg.webhook_url and cfg.webhook_message:
        webhook = WebhookSender(cfg.webhook_url, cfg.webhook_message, session, start_time)

    def _resolve(name: str, outcome: str) -> None:
        key = name.lower()
        pending_names.pop(key, None)
        if already_checked is not None:
            already_checked.add(key)
        if metrics is not None:
            metrics.resolve(name, outcome)

    # Only the last handful of either list is ever rendered, so bounding them
    # keeps a long run from accumulating a list entry per name checked.
    recent_hits: deque[str] = deque(maxlen=16)
    feed: deque[str] = deque(maxlen=16)
    # Sampled once per render tick to drive the panel's sparkline. A single
    # live number tells you the rate; the history tells you whether you are
    # speeding up or being throttled, which is the more useful of the two.
    rps_history: deque[float] = deque(maxlen=16)
    interrupted = False
    cand_queue: asyncio.Queue[str | None] = asyncio.Queue()
    running: list[asyncio.Task] = []
    seal_task: asyncio.Task | None = None
    failure: BaseException | None = None

    def _note_failure(exc: BaseException) -> None:
        nonlocal failure
        if isinstance(exc, asyncio.CancelledError) and not isinstance(failure, asyncio.CancelledError):
            if failure is not None:
                console.print(f"[{C.DANGER}]Failure before cancellation: {failure!r}[/]")
            failure = exc
        elif failure is None:
            failure = exc
        elif exc is not failure:
            console.print(f"[{C.DANGER}]Additional cleanup failure: {exc!r}[/]")

    # Live-display state. Both stages now run concurrently, so screening and
    # validation progress are tracked separately and `stage` only decides
    # which of the two the panel is currently showing.
    state = {
        "stage": 1,
        "screened": 0,
        "screen_total": total_names,
        "validated": 0,
        "cand_total": 0,
    }

    def _live_render():
        elapsed = max(time.monotonic() - started_monotonic, 1e-9)
        rps_history.append(stats.definitive / elapsed)
        if state["stage"] == 1:
            done, total = state["screened"], state["screen_total"]
        else:
            done, total = state["validated"], state["cand_total"]
        return live_card(
            stage=state["stage"],
            stage_done=done,
            stage_total=total,
            round_no=round_no,
            works=stats.works,
            taken=stats.taken,
            censored=stats.censored,
            requests=stats.requests,
            batch_requests=stats.batch_requests,
            candidates=stats.candidates,
            ratelimited=stats.ratelimited,
            circuit_opens=stats.circuit_opens,
            rps=stats.requests / elapsed,
            checks_rps=stats.definitive / elapsed,
            resolved=stats.definitive, input_total=total_names, invalid=stats.invalid,
            elapsed=elapsed,
            proxy_alive=pm.alive_count,
            paused=paused,
            recent=list(recent_hits),
            feed=list(feed),
            rps_history=list(rps_history),
        )

    stop_rps = asyncio.Event()
    rps_task = asyncio.create_task(_rps_calculator(stats, stop_rps))
    _resources["helpers"] = [rps_task]
    diag_task = (
        asyncio.create_task(
            _diag_sampler(stats, pm, stop_rps, logs_dir / "diag.csv")
        )
        if settings.diag else None
    )
    if diag_task is not None:
        _resources["helpers"].append(diag_task)
    webhook_task = asyncio.create_task(webhook.run()) if webhook else None
    if webhook_task is not None:
        _resources["helpers"].append(webhook_task)

    new_hits_written = False

    async def _record_hit(name: str) -> None:
        nonlocal new_hits_written
        async with hits_lock:
            key = name.lower()
            new_record = key not in saved_hits
            if new_record:
                try:
                    hits_file.write(f"{name}\n")
                    hits_file.flush()
                except Exception:
                    if metrics is not None:
                        metrics.persistence_failure("hits_append")
                    raise
                saved_hits.add(key)
                new_hits_written = True
        if metrics is not None:
            metrics.persisted_hit(name, new_record=new_record)
        _resolve(name, AVAILABLE)
        stats.inc_works()
        recent_hits.append(name)
        feed.append(f"[{C.SUCCESS}]+[/] {name}")
        if webhook:
            webhook.enqueue(name)

    if duplicates:
        console.print(
            f"[{C.MUTED}]Skipped {duplicates} duplicate name"
            f"{'s' if duplicates != 1 else ''} in the input[/]"
        )

    console.print()
    try:
        if metrics is not None and metrics.control is not None:
            metrics.control.configure_session(session)
        with Live(_live_render(), refresh_per_second=4, console=console) as live:

            # ── Stages 1 and 2, pipelined ─────────────────────────────────
            #
            # These used to run strictly one after the other: every chunk was
            # screened, and only then did validation start. Stage 1 compresses
            # 200 names into one request while stage 2 spends one request per
            # survivor, so stage 2 is far and away the longer of the two - and
            # all of stage 1's wall clock was being spent with the validator
            # idle.
            #
            # Now stage 1 publishes survivors to a queue as each chunk comes
            # back and stage 2 drains it concurrently, so screening time hides
            # almost entirely inside validation time. The validators block on
            # an empty queue, which is also what keeps the combined request
            # rate honest early on: they only go wide once there is a backlog
            # to go wide on.
            # One line per resolved name, printed above the live panel; rich
            # moves the panel down as lines arrive, so the result is a
            # scrolling log with the stats pinned underneath.
            #
            # Two things keep it cheap enough to leave on by default.
            # Styles are pre-built rather than parsed out of markup on every
            # line, and lines are emitted in batches - measured together at
            # ~12,000 lines/sec against ~4,000 for the markup-per-line
            # version it replaces. The batch also flushes on the render tick,
            # so a slow proxyless run still shows each line promptly instead
            # of waiting for the buffer to fill.
            _S_HIT = Style(color=C.SUCCESS, bold=True)
            _S_TAKEN = Style(color=C.MUTED, dim=True)
            _S_CENSORED = Style(color=C.WARNING)
            _S_UNKNOWN = Style(color=C.WARNING, dim=True)
            _S_DIM = Style(color=C.MUTED, dim=True)
            _S_NAME = Style(bold=True)

            _look = {
                TAKEN:    ("·", "taken",      _S_TAKEN),
                CENSORED: ("•", "censored",   _S_CENSORED),
                INVALID:  ("×", "invalid",    _S_DIM),
            }

            # A hit is the whole point of watching this run, so it does not
            # get a line like every other outcome - it gets a slab. Reverse
            # video across the full width is the one treatment that still
            # reads once the terminal has been shrunk into a phone-sized
            # video frame; a coloured line just becomes a slightly brighter
            # row in a grey wall.
            _S_SLAB = Style(color="#0B0B0C", bgcolor=C.SUCCESS, bold=True)
            _S_SLAB_TAIL = Style(color=C.SUCCESS, bold=True)
            _SLAB_WIDTH = 52
            _stream_buf: list[Text] = []
            STREAM_BATCH = 16

            def _stream_flush() -> None:
                if not _stream_buf:
                    return
                live.console.print(Text("\n").join(_stream_buf))
                _stream_buf.clear()

            def _stream(name: str, outcome: str) -> None:
                if not settings.stream:
                    return
                line = Text(no_wrap=True, end="")

                if outcome == AVAILABLE:
                    line.append(
                        f"  ✦  AVAILABLE   {name}".ljust(_SLAB_WIDTH),
                        _S_SLAB,
                    )
                    line.append(
                        f"  #{stats.works:,} · {time.time() - start_time:.0f}s ",
                        _S_SLAB_TAIL,
                    )
                    _stream_buf.append(line)
                    # Flush immediately: the hit is the moment worth seeing,
                    # and buffering it behind fifteen "taken" lines is what
                    # made it land late on screen.
                    _stream_flush()
                    return

                icon, label, style = _look.get(
                    outcome, ("?", "unresolved", _S_UNKNOWN),
                )
                line.append(f" {icon} ", style)
                # Taken is most of the traffic, so it stays dim and short -
                # the wall greys out and the hits are what your eye catches.
                if outcome == TAKEN:
                    line.append(f"{name:<10}", _S_TAKEN)
                    line.append(label, _S_TAKEN)
                else:
                    line.append(f"{name:<10}", _S_NAME)
                    line.append(f"{label:<11}", style)
                    line.append(
                        f"{time.time() - start_time:6.0f}s "
                        f"{stats.rps:>5.1f} HTTP/s "
                        f"{stats.works:>6,} found",
                        _S_DIM,
                    )
                _stream_buf.append(line)
                if len(_stream_buf) >= STREAM_BATCH:
                    _stream_flush()

            def _enqueue(batch: list[str]) -> None:
                for n in batch:
                    cand_queue.put_nowait(n)
                stats.inc("candidates", len(batch))
                state["cand_total"] += len(batch)
                if metrics is not None:
                    metrics.queue_depth("candidates", cand_queue.qsize())

            # Both pools are sized to the full concurrency setting. That is
            # not double the load: `checker` holds a shared semaphore of
            # cfg.concurrency in-flight requests, so the two stages compete
            # for one budget and whichever has work pending uses it. A fixed
            # split would starve stage 1 on a low-survival run and stage 2 on
            # a high-survival one.
            n_validators = max(1, cfg.concurrency)
            screen_tasks: list[asyncio.Task] = []

            if cfg.two_stage:
                chunks = [names[i:i + BATCH_MAX] for i in range(0, len(names), BATCH_MAX)]

                # A chunk that fails screening goes back for another stage-1
                # attempt rather than straight to stage 2. Screening a chunk
                # again costs one request; handing its 200 names to stage 2
                # costs 200 - so falling back on the first failure multiplies
                # the load by 200 at exactly the moment the endpoint is
                # already refusing us, and the extra stage-2 traffic then
                # makes more chunks fail. A live 4-char run showed it: 8,398
                # cheap requests turned into 1.17 million expensive ones.
                chunk_queue = deque((c, 0) for c in chunks)
                if metrics is not None:
                    metrics.queue_depth("chunks", len(chunk_queue))
                inflight = 0
                idx_lock = asyncio.Lock()

                async def _take_chunk():
                    """Next (chunk, tries), or None once nothing is left."""
                    nonlocal inflight
                    while True:
                        async with idx_lock:
                            if chunk_queue:
                                inflight += 1
                                return chunk_queue.popleft()
                            if inflight == 0:
                                return None
                        # Chunks are still out; one may come back for a retry,
                        # so wait rather than exiting the worker.
                        await asyncio.sleep(0.05)

                async def _release(retry=None) -> None:
                    nonlocal inflight
                    async with idx_lock:
                        inflight -= 1
                        if retry is not None:
                            chunk_queue.append(retry)

                async def _screen_worker() -> None:
                    while True:
                        item = (await metrics.wait("batch", "chunk_queue", _take_chunk())
                                if metrics is not None else await _take_chunk())
                        if item is None:
                            return
                        chunk, tries = item
                        taken_set = await checker.batch_screen(session, chunk)

                        if taken_set is None and tries + 1 < MAX_CHUNK_TRIES:
                            await _release((chunk, tries + 1))
                            continue

                        await _release()
                        if taken_set is None or taken_set is MALFORMED_CHUNK:
                            # Out of stage-1 attempts, or a chunk the endpoint
                            # will never accept. Stage 2 is the last resort.
                            _enqueue(chunk)
                            if metrics is not None:
                                metrics.fallback(chunk)
                            stats.inc("fellback_chunks")
                            feed.append(f"[{C.WARNING}]?[/] chunk -> stage 2")
                        else:
                            free = [n for n in chunk if n.lower() not in taken_set]
                            for name in chunk:
                                if name.lower() in taken_set:
                                    _resolve(name, TAKEN)
                            stats.inc_taken(len(chunk) - len(free))
                            _enqueue(free)
                        stats.inc("screened", len(chunk))
                        state["screened"] += len(chunk)
                        if settings.stream:
                            _line = Text(no_wrap=True, end="")
                            _line.append(" # ", Style(color=C.PRIMARY))
                            _line.append(f"screened {len(chunk):<4} ", _S_DIM)
                            _line.append(
                                f"{state['screened']:>10,}/"
                                f"{state['screen_total']:,}  "
                                f"{stats.rps:>5.1f} HTTP/s",
                                _S_DIM,
                            )
                            _stream_buf.append(_line)
                            if len(_stream_buf) >= STREAM_BATCH:
                                _stream_flush()

                n_screeners = min(len(chunks), max(1, cfg.concurrency))
                screen_tasks = [
                    asyncio.create_task(_screen_worker())
                    for _ in range(n_screeners)
                ]
                running.extend(screen_tasks)
            else:
                # Validator-only mode: every name is a stage-2 candidate.
                _enqueue(names)
                state["stage"] = 2
                state["screened"] = total_names

            async def _validate_worker() -> None:
                while True:
                    name = (await metrics.wait("validate", "candidate_queue", cand_queue.get())
                            if metrics is not None else await cand_queue.get())
                    if name is None:  # sentinel: screening finished, queue drained
                        return
                    outcome, code = await checker.validate(session, name)

                    if outcome == AVAILABLE:
                        await _record_hit(name)
                    elif outcome == TAKEN:
                        stats.inc_taken()
                        feed.append(f"[{C.DANGER}]-[/] {name}")
                    elif outcome == CENSORED:
                        stats.inc("censored")
                        feed.append(f"[{C.WARNING}]c[/] {name}")
                    elif outcome == INVALID:
                        stats.inc("invalid")
                    else:
                        feed.append(f"[{C.WARNING}]?[/] {name}")
                        if metrics is not None:
                            metrics.unresolved(name, outcome)

                    if outcome in (TAKEN, CENSORED, INVALID):
                        _resolve(name, outcome)
                    _stream(name, outcome)
                    state["validated"] += 1

            vtasks = [
                asyncio.create_task(_validate_worker())
                for _ in range(n_validators)
            ]
            running.extend(vtasks)

            async def _seal_queue() -> None:
                """Once screening is done, tell the validators when to stop."""
                if screen_tasks:
                    await asyncio.gather(*screen_tasks)
                    # Screening is over, so the panel switches to stage 2 and
                    # the candidate total is final.
                    state["stage"] = 2
                for _ in range(n_validators):
                    cand_queue.put_nowait(None)

            seal_task = asyncio.create_task(_seal_queue())
            running.append(seal_task)

            pending = set(running)
            while pending:
                # asyncio.wait sleeps on the tasks themselves rather than
                # re-polling `.done()` over every worker four times a second.
                done, pending = await asyncio.wait(
                    pending, timeout=0.25, return_when=asyncio.FIRST_EXCEPTION,
                )
                for task in done:
                    task.result()  # Unexpected failures must stop and surface.
                _stream_flush()
                live.update(_live_render())
                if stop is not None and stop.requested and pending:
                    # Wind down here rather than letting the interrupt unwind
                    # the stack: everything below this block still needs to
                    # run to save the summary and the retry list.
                    interrupted = True
                    if metrics is not None:
                        metrics.stopped()
                    break

            _stream_flush()
            live.update(_live_render())

    except RateLimitStop as exc:
        interrupted = True
        if not stop.requested and (diagnostics is None or diagnostics.reason is None):
            _note_failure(exc)
        elif metrics is not None:
            metrics.stopped()
    except (asyncio.CancelledError, Exception) as exc:
        interrupted = True
        _note_failure(exc)
    finally:
        async def _cleanup() -> None:
            # Stop every owner before taking the final pending-work snapshot.
            for task in running:
                # The sealer finishes when screeners exit. Cancelling its
                # gather too would cancel those workers a second time.
                if task is not seal_task and not task.done():
                    task.cancel()
            results = await asyncio.gather(*running, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) and not (
                    isinstance(result, RateLimitStop) and (
                        stop.requested or (diagnostics is not None and diagnostics.reason))
                ):
                    _note_failure(result)

            stop_rps.set()
            rps_task.cancel()
            if diag_task:
                # Let it write one final row before it goes, so the tail of a
                # decaying run - the part worth seeing - is not the part missing.
                try:
                    await asyncio.wait_for(diag_task, timeout=2.0)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    diag_task.cancel()
                except Exception as exc:
                    _note_failure(exc)
            if webhook:
                try:
                    await webhook.flush()
                except Exception as exc:
                    _note_failure(exc)
            if webhook_task:
                webhook_task.cancel()

            helpers = [rps_task, *([diag_task] if diag_task else []),
                       *([webhook_task] if webhook_task else [])]
            for result in await asyncio.gather(*helpers, return_exceptions=True):
                if isinstance(result, Exception):
                    _note_failure(result)
            _resources.pop("helpers", None)

            if diagnostics is not None:
                if diagnostics.failure is not None:
                    if metrics is not None:
                        metrics.persistence_failure("rate_limit_events")
                    _note_failure(diagnostics.failure)
                try:
                    _resources.pop("diagnostics", None)
                    diagnostics.close()
                except Exception as exc:
                    if metrics is not None:
                        metrics.persistence_failure("rate_limit_events_close")
                    _note_failure(exc)

            hits_synced = True
            try:
                if new_hits_written:
                    sync_file(hits_file)
            except Exception as exc:
                hits_synced = False
                if metrics is not None:
                    metrics.persistence_failure("hits_sync")
                _note_failure(exc)
            try:
                _resources.pop("hits", None)
                hits_file.close()
            except Exception as exc:
                if metrics is not None:
                    metrics.persistence_failure("hits_close")
                _note_failure(exc)
            try:
                if hits_synced:
                    snapshots.write(pending_names.values())
            except Exception as exc:
                if metrics is not None:
                    metrics.persistence_failure("unresolved_snapshot")
                _note_failure(exc)
            try:
                _resources.pop("session", None)
                await session.close()
                await asyncio.sleep(0.25)  # let the connector close cleanly
            except Exception as exc:
                _note_failure(exc)

        # Cancellation during finalisation must not interrupt the recovery save.
        cleanup_task = asyncio.create_task(_cleanup())
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                interrupted = True
                _note_failure(exc)
        cleanup_task.result()

    if failure is not None:
        raise failure

    unresolved = list(pending_names.values())
    elapsed = time.monotonic() - started_monotonic
    snap = stats.snapshot()

    if interrupted:
        console.print(
            f"[{C.WARNING}]Stopped early[/] "
            f"[{C.MUTED}]- results below are what completed before the stop.[/]"
        )

    if snap["fellback_chunks"]:
        console.print(
            f"[{C.WARNING}]{snap['fellback_chunks']} chunks[/] could not be screened "
            f"and fell through to stage 2 "
            f"[{C.MUTED}](~{snap['fellback_chunks'] * BATCH_MAX} names checked one "
            f"at a time - the pool could not keep up)[/]"
        )

    final_summary(
        requests=snap["requests"],
        batch_requests=snap["batch_requests"],
        works=snap["works"],
        taken=snap["taken"],
        censored=snap["censored"],
        invalid=snap["invalid"],
        unresolved=len(unresolved),
        ratelimited=snap["ratelimited"],
        elapsed=elapsed,
        peak_rps=snap["peak_rps"],
        best_streak=snap["best_streak"],
        total_names=total_names,
        stop_reason=getattr(stop, "reason", None),
    )

    if unresolved:
        console.print(
            f"[{C.WARNING}]{len(unresolved)}[/] names unresolved -> "
            f"[{C.MUTED}]results/unresolved.txt[/] (re-run with that as your input file)"
        )

    return snap["works"]


# ---------------------------------------------------------------------------
# Round loop
# ---------------------------------------------------------------------------

MAX_REDRAW_TRIES = 6


async def _run_rounds(cfg: RunConfig, settings: AppSettings) -> None:
    with ResultsLock(RESULTS_DIR, asyncio.current_task()) as ownership:
        await _run_rounds_owned(cfg, settings, ownership)


async def _run_rounds_owned(cfg: RunConfig, settings: AppSettings, ownership) -> None:
    """Run the checker once, or repeatedly until a name comes back free.

    Every round shares one cooldown and one set of already-checked names, so
    repeating costs no re-learning of the rate limit and never spends a
    request on a name an earlier round already answered.
    """
    from engine import SharedCooldown

    repeat = cfg.repeat_until_found and cfg.gen_length > 0
    cooldown = SharedCooldown() if not cfg.proxies else None
    already: set[str] = set()
    pending_names: dict[str, str] = {}

    stop = _StopRequest()
    stop.install()

    round_no = 1
    total_hits = 0
    try:
        while True:
            if repeat and round_no > 1:
                console.print()
                console.print(
                    f"[{C.PRIMARY}]Round {round_no}[/] "
                    f"[{C.MUTED}]- {len(already)} names checked so far, still looking[/]"
                )

            total_hits += await _run_checker(
                cfg, settings,
                round_no=round_no,
                cooldown=cooldown,
                already_checked=already if repeat else None,
                pending_names=pending_names,
                stop=stop,
                _ownership=ownership,
            )

            if stop.requested or total_hits > 0 or not repeat:
                break

            # Only definitive answers are excluded from subsequent draws.
            fresh: list[str] = []
            for _ in range(MAX_REDRAW_TRIES):
                batch = generate_usernames(
                    cfg.gen_length, cfg.gen_count, cfg.gen_underscore,
                )
                fresh = [n for n in batch if n.lower() not in already]
                if fresh:
                    break
            if not fresh:
                console.print(
                    f"[{C.WARNING}]No unchecked names left to draw[/] "
                    f"[{C.MUTED}]- the {cfg.gen_length}-character space is exhausted.[/]"
                )
                break

            cfg.usernames = fresh
            round_no += 1
    finally:
        stop.restore()

    if repeat and round_no > 1:
        console.print(
            f"[{C.MUTED}]{round_no} rounds, {len(already)} names checked, "
            f"{total_hits} found.[/]"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    settings = parse_args()
    set_debug(settings.debug)

    ensure_dir(DATA_DIR, LOGS_DIR, RESULTS_DIR)
    ensure_file(DATA_DIR / "config.json")
    ensure_file(DATA_DIR / "proxies.txt")
    ensure_file(DATA_DIR / "names_to_check.txt")

    cfg_store = Config()

    try:
        if settings.no_wizard:
            console.print(banner())
            proxies = load_lines(DATA_DIR / "proxies.txt")
            loaded = load_lines(DATA_DIR / "names_to_check.txt")
            usernames = [n for n in loaded if is_valid_username(n)]
            if not usernames:
                console.print(f"[{C.DANGER}]No valid usernames. Run without --no-wizard first.[/]")
                sys.exit(1)
            skipped = len(loaded) - len(usernames)
            run_config = RunConfig(
                proxies=proxies,
                remove_bad_proxies=cfg_store.get("remove_proxies", False),
                usernames=usernames,
                concurrency=cfg_store.get("concurrency", 2 if not proxies else 50),
                timeout=cfg_store.get("timeout", 10),
                scraped=False,
                two_stage=cfg_store.get("two_stage", True),
                webhook_url=cfg_store.get("webhook") or None,
                webhook_message=cfg_store.get("webhook_message") or None,
            )
            console.print(
                f"[{C.MUTED}]Skipping wizard - {len(proxies)} proxies, "
                f"{len(usernames)} usernames"
                + (f", {skipped} invalid skipped" if skipped else "") + "[/]"
            )
        else:
            run_config = asyncio.run(setup_wizard(cfg_store, settings))

        asyncio.run(_run_rounds(run_config, settings))
    except (ResultsBusyError, RecoveryError) as exc:
        console.print(f"[{C.DANGER}]{exc}[/]")
        sys.exit(1)
    except UnicodeError:
        console.print(f"[{C.DANGER}]Invalid UTF-8 input/results. Preserve and back up the original files; "
                      "review and reconcile recovery data in a separate results directory before retrying.[/]")
        sys.exit(1)
    except (EOFError, KeyboardInterrupt):
        console.print(f"\n[{C.WARNING}]Aborted.[/]")
        sys.exit(0)


if __name__ == "__main__":
    main()
