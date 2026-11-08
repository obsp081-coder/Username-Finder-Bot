from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .domain import JobState


@dataclass(frozen=True)
class ScheduleConfig:
    start_enabled: bool = False
    start_time: str = "09:00"
    stop_enabled: bool = False
    stop_time: str = "17:00"
    duration_enabled: bool = False
    duration_minutes: int = 60
    timezone: str = "Asia/Riyadh"

    def validate(self):
        time.fromisoformat(self.start_time)
        time.fromisoformat(self.stop_time)
        ZoneInfo(self.timezone)
        if self.duration_minutes < 0 or (self.duration_enabled and self.duration_minutes == 0):
            raise ValueError("مدة التشغيل يجب أن تكون أكبر من صفر.")


class Scheduler:
    """No catch-up starts on launch; short crossings only; stop wins simultaneous triggers."""

    def __init__(self, config: ScheduleConfig, journal: dict | None = None):
        config.validate()
        self.config = config
        self.journal = dict(journal or {})
        self.previous: datetime | None = None

    def tick(self, now: datetime, state: JobState | str, active_seconds: float) -> list[str]:
        now = now.astimezone(ZoneInfo(self.config.timezone))
        if self.previous is None:
            self.previous = now
            previous_seen = self.journal.get("last_seen")
            if previous_seen:
                previous = datetime.fromisoformat(previous_seen)
                if (now - previous).total_seconds() > 120:
                    self.journal["missed_since"] = previous_seen
            self.journal["last_seen"] = now.isoformat()
            return self._duration(state, active_seconds)
        previous = self.previous
        self.previous = now
        self.journal["last_seen"] = now.isoformat()
        actions = self._duration(state, active_seconds)
        elapsed = (now - previous).total_seconds()
        if elapsed < 0 or elapsed > 120:
            self.journal["missed_since"] = previous.isoformat()
            return actions
        for kind, enabled, value in [
            ("stop", self.config.stop_enabled, self.config.stop_time),
            ("start", self.config.start_enabled, self.config.start_time),
        ]:
            if not enabled:
                continue
            for date in {previous.date(), now.date()}:
                due = datetime.combine(date, time.fromisoformat(value), tzinfo=now.tzinfo)
                key = f"last_{kind}_date"
                if previous < due <= now and self.journal.get(key) != date.isoformat():
                    self.journal[key] = date.isoformat()
                    if kind == "stop" and state == JobState.RUNNING:
                        actions = ["stop"]
                    elif (
                        kind == "start"
                        and "stop" not in actions
                        and state not in {JobState.RUNNING, JobState.COMPLETED, JobState.BLOCKED}
                    ):
                        self.journal["duration_baseline"] = active_seconds
                        actions.append("start")
        return actions

    def _duration(self, state, active_seconds) -> list[str]:
        baseline = self.journal.get("duration_baseline", 0.0)
        if (
            self.config.duration_enabled
            and state == JobState.RUNNING
            and active_seconds - baseline >= self.config.duration_minutes * 60
        ):
            return ["stop"]
        return []

    def next_events(self, now: datetime) -> dict[str, str]:
        local = now.astimezone(ZoneInfo(self.config.timezone))
        result = {}
        for kind, enabled, value in [
            ("start", self.config.start_enabled, self.config.start_time),
            ("stop", self.config.stop_enabled, self.config.stop_time),
        ]:
            if enabled:
                due = datetime.combine(local.date(), time.fromisoformat(value), tzinfo=local.tzinfo)
                if due <= local:
                    due += timedelta(days=1)
                result[kind] = due.isoformat()
        return result

    def serialize(self) -> dict:
        return {"config": asdict(self.config), "journal": self.journal}
