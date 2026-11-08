"""Local Qt pagination timings on isolated, simulated datasets; never opens user data."""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import platform
import pstats
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from username_finder.domain import Classification, SearchConfig, utc_now
from username_finder.gui import MainWindow
from username_finder.storage import Store, dumps


def seed(path: Path, size: int) -> str:
    store = Store(path)
    config = SearchConfig(limit=size)
    job = store.create_job(config, {"rules": "source-v1"})
    now = utc_now()
    with store.transaction() as db:
        for index in range(size):
            name = f"name{index:06d}"
            status = list(Classification)[index % 5].value
            kind = {
                "Available": "explicitly_available",
                "Taken": "explicitly_taken",
                "Reserved": "explicitly_reserved",
                "Invalid": "explicitly_invalid",
                "Unknown": "simulated_ambiguous",
            }[status]
            task = db.execute(
                "INSERT INTO tasks(job_id,username,state) VALUES(?,?,'Completed')", (job, name)
            ).lastrowid
            attempt = db.execute(
                """INSERT INTO attempts(task_id,username,classification,kind,provider,
                provider_version,mode,trustworthy,checked_at,latency_ms,error,trace_id)
                VALUES(?,?,?,?, 'local-mock','1','simulated',?,?,5,'',?)""",
                (task, name, status, kind, status != "Unknown", now, f"profile-{index}"),
            ).lastrowid
            db.execute("UPDATE tasks SET result_id=? WHERE id=?", (attempt, task))
            db.execute(
                "INSERT INTO username_state VALUES(?,?,?,?) ON CONFLICT(username,mode) DO UPDATE SET "
                "latest_id=excluded.latest_id,trusted_id=COALESCE(excluded.trusted_id,username_state.trusted_id)",
                (name, "simulated", attempt, attempt if status != "Unknown" else None),
            )
        counters = {key: "0" for key in store.job(job)["counters"]}
        for key in ("lines", "unique", "completed", "dispatched"):
            counters[key] = str(size)
        db.execute(
            "UPDATE jobs SET state='Completed',source_done=1,counters=? WHERE id=?", (dumps(counters), job)
        )
    store.close()
    return job


def measure(size: int, directory: Path, app: QApplication) -> dict:
    path = directory / f"pagination-{size}.db"
    job = seed(path, size)
    window = MainWindow(path)
    window.resize(1180, 850)
    window.timer.stop()
    window.load_job(job)
    window.search_tabs.setCurrentIndex(2)
    window.show()
    app.processEvents()
    timings = []
    sql_timings = []
    fill_timings = []
    original_results = window.store.results
    original_fill = window.fill_table

    def sql(*args, **kwargs):
        started = time.perf_counter()
        result = original_results(*args, **kwargs)
        sql_timings.append((time.perf_counter() - started) * 1000)
        return result

    def fill(*args, **kwargs):
        started = time.perf_counter()
        result = original_fill(*args, **kwargs)
        fill_timings.append((time.perf_counter() - started) * 1000)
        return result

    window.store.results = sql
    window.fill_table = fill
    profile = cProfile.Profile()
    profile.enable()
    for offset in (0, 100, 0, 100, 0, 100):
        started = time.perf_counter()
        window.offset = offset
        window.refresh_results()
        app.processEvents()
        window.result_table.viewport().repaint()
        timings.append((time.perf_counter() - started) * 1000)
    profile.disable()
    output = io.StringIO()
    pstats.Stats(profile, stream=output).sort_stats("cumulative").print_stats(18)
    (directory / f"profile-{size}.txt").write_text(output.getvalue(), encoding="utf-8")
    traced = []
    window.store.db.set_trace_callback(traced.append)
    original_results(job, mode="simulated", offset=100)
    window.store.db.set_trace_callback(None)
    query = next(sql for sql in traced if sql.startswith("SELECT"))
    plan = [list(r) for r in window.store.db.execute("EXPLAIN QUERY PLAN " + query)]
    rows = window.result_table.rowCount()
    window.close()
    app.processEvents()
    return {
        "size": size,
        "page_size": 100,
        "offsets": [0, 100, 0, 100, 0, 100],
        "navigation_including_paint_ms": timings,
        "sql_ms": sql_timings,
        "table_fill_ms": fill_timings,
        "final_rows": rows,
        "query_plan": plan,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="before")
    parser.add_argument("--sizes", nargs="+", type=int, default=[20, 200, 10000])
    args = parser.parse_args()
    directory = Path("runtime") / (
        "pagination-" + args.label + "-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    )
    directory.mkdir(parents=True)
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Segoe UI", 10))
    report = {
        "label": args.label,
        "platform": platform.platform(),
        "python": sys.version,
        "window": "1180x850 logical pixels, visible Qt window, main thread, no running engine",
        "results": [],
    }
    for size in args.sizes:
        row = measure(size, directory, app)
        report["results"].append(row)
        (directory / "measurements.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(row), flush=True)
    (directory / "measurements.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("REPORT=" + str(directory / "measurements.json"))


if __name__ == "__main__":
    main()
