from datetime import datetime
from zoneinfo import ZoneInfo

from username_finder.domain import JobState
from username_finder.scheduling import ScheduleConfig, Scheduler

ZONE = ZoneInfo("Asia/Riyadh")


def dt(day=5, hour=9, minute=0, second=0):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=ZONE)


def test_independent_start_stop_midnight_and_blocked():
    s = Scheduler(
        ScheduleConfig(start_enabled=True, start_time="23:59", stop_enabled=True, stop_time="00:01")
    )
    assert s.tick(dt(hour=23, minute=58, second=59), JobState.PAUSED, 100) == []
    assert s.tick(dt(hour=23, minute=59), JobState.PAUSED, 100) == ["start"]
    assert s.tick(dt(6, 0, 0, 59), JobState.RUNNING, 110) == []
    assert s.tick(dt(6, 0, 1), JobState.RUNNING, 111) == ["stop"]
    blocked = Scheduler(ScheduleConfig(start_enabled=True))
    blocked.tick(dt(hour=8, minute=59, second=59), JobState.BLOCKED, 0)
    assert blocked.tick(dt(), JobState.BLOCKED, 0) == []


def test_duration_pause_resume_persisted_and_stop_priority():
    s = Scheduler(ScheduleConfig(duration_enabled=True, duration_minutes=1))
    assert s.tick(dt(), JobState.RUNNING, 59) == []
    assert s.tick(dt(second=1), JobState.PAUSED, 59) == []
    assert s.tick(dt(second=2), JobState.RUNNING, 60) == ["stop"]
    journal = s.serialize()["journal"]
    resumed = Scheduler(s.config, journal)
    assert resumed.tick(dt(second=3), JobState.RUNNING, 60) == ["stop"]
    simultaneous = Scheduler(ScheduleConfig(start_enabled=True, stop_enabled=True, stop_time="09:00"))
    simultaneous.tick(dt(hour=8, minute=59, second=59), JobState.RUNNING, 0)
    assert simultaneous.tick(dt(), JobState.RUNNING, 1) == ["stop"]


def test_missed_times_no_replay_and_backward_clock():
    s = Scheduler(ScheduleConfig(start_enabled=True))
    s.tick(dt(hour=8), JobState.READY, 0)
    assert s.tick(dt(hour=10), JobState.READY, 0) == []
    assert "missed_since" in s.journal
    assert s.tick(dt(hour=8), JobState.READY, 0) == []
    # Polling the next day at the actual crossing starts one new session.
    s.tick(dt(6, 8, 59, 59), JobState.PAUSED, 500)
    assert s.tick(dt(6), JobState.PAUSED, 500) == ["start"]
    assert s.journal["duration_baseline"] == 500
    assert s.tick(dt(6, second=1), JobState.RUNNING, 501) == []
    assert s.next_events(dt(6))["start"].startswith("2026-11-07")


def test_disabled_duration_can_keep_zero_without_blocking_independent_daily_options():
    import pytest

    ScheduleConfig(start_enabled=True, duration_enabled=False, duration_minutes=0).validate()
    with pytest.raises(ValueError):
        ScheduleConfig(duration_enabled=True, duration_minutes=0).validate()
