"""Private-field-free 429 events and an optional local rolling-window stop."""

import csv
import math
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class RateLimitStop(Exception):
    """Stop new dispatch and let the checker save pending work normally."""


class DiagnosticsError(Exception):
    """A diagnostic write failed; never turn it into a transport retry."""


def valid_delay(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        delay = float(value)
    except (ValueError, OverflowError):
        return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def safe_retry_after(value):
    """Log only normalized delay/date syntax, never an arbitrary header string."""
    if value is None:
        return ""
    delay = valid_delay(value)
    if delay is not None:
        return str(delay)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is not None:
            return date.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    return "invalid"


class RateLimitMonitor:
    FIELDS = ("run_started_utc", "event_id", "phase", "utc_timestamp", "elapsed_seconds", "recorded_elapsed_seconds",
              "endpoint", "retry_after_header", "body_retry_delay_seconds",
              "effective_cooldown_seconds", "cooldown_state", "stop_reason")

    def __init__(self, path=None, *, threshold=0, window=60.0, on_stop=None,
                 clock=time.monotonic, wall_clock=time.time, started=None, history=None):
        if type(threshold) is not int or threshold < 0:
            raise ValueError("429 threshold must be a nonnegative integer")
        if not math.isfinite(window) or window <= 0:
            raise ValueError("429 window must be positive and finite")
        self.path, self.threshold, self.window = path, threshold, window
        self.clock, self.wall_clock, self.on_stop = clock, wall_clock, on_stop
        self.started = clock() if started is None else started
        self.run_started_utc = self._utc(wall_clock())
        self.reason = None
        self.failure = None
        self._recent = history if history is not None else deque()
        self._sequence = 0
        self._file = self._writer = None

    @staticmethod
    def _utc(stamp):
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="microseconds")

    def check(self):
        if self.failure is not None:
            raise self.failure
        if self.reason:
            raise RateLimitStop(self.reason)

    def begin(self, endpoint, header):
        if endpoint not in ("bulk", "validator"):
            raise ValueError("Unexpected diagnostic endpoint")
        now = self.clock()
        self._sequence += 1
        if self.threshold:
            while self._recent and self._recent[0] <= now - self.window:
                self._recent.popleft()
            self._recent.append(now)
            if len(self._recent) >= self.threshold and self.reason is None:
                self.reason = f"sustained_429: {self.threshold} responses within {self.window:g}s"
                if self.on_stop:
                    self.on_stop(self.reason)
        event = dict(run_started_utc=self.run_started_utc, event_id=self._sequence,
                     phase="received", utc_timestamp=self._utc(self.wall_clock()),
                     elapsed_seconds=f"{max(0, now - self.started):.6f}", endpoint=endpoint,
                     recorded_elapsed_seconds=f"{max(0, now - self.started):.6f}",
                     retry_after_header=safe_retry_after(header), body_retry_delay_seconds="",
                     effective_cooldown_seconds="", cooldown_state="pending",
                     stop_reason=self.reason or "")
        self._write(event)  # Write-ahead row survives cancellation while reading the body.
        return event

    def finish(self, event, *, body=None, effective=None, state="not_applied"):
        row = dict(event, phase="finalized", cooldown_state=state,
                   recorded_elapsed_seconds=f"{max(0, self.clock() - self.started):.6f}",
                   stop_reason=self.reason or "")
        delay = valid_delay(body.get("retry_after")) if isinstance(body, dict) else None
        row["body_retry_delay_seconds"] = delay if delay is not None else ""
        duration = valid_delay(effective)
        row["effective_cooldown_seconds"] = duration if duration is not None else ""
        self._write(row)

    def _write(self, row):
        if self.path is None:
            return
        try:
            if self._file is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._file = self.path.open("a", newline="", encoding="utf-8")
                self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDS)
                if self._file.tell() == 0:
                    self._writer.writeheader()
            self._writer.writerow(row)
            self._file.flush()
        except (OSError, UnicodeError) as exc:
            self.failure = DiagnosticsError("Failed to persist HTTP 429 diagnostics")
            raise self.failure from exc

    def close(self):
        if self._file is not None:
            self._file.close()
