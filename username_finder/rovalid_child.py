"""Managed RoValid process. Only presentation, paths and cooperative stop are adapted."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from . import rovalid
from .roblox_activity import RobloxActivity


def emit(kind, **data):
    print(json.dumps(dict(event=kind, **data), ensure_ascii=True, allow_nan=False), flush=True)


def engine_modules():
    rovalid.verify_source()
    sys.path.insert(0, str(rovalid.SOURCE))
    import checker
    import config
    import wizard

    return checker, config, wizard


def prepare(directory, packet, config, wizard):
    options = rovalid.settings(packet["settings"])
    if options["source"] == "generate":
        names = wizard.generate_usernames(options["length"], options["count"], options["underscore"])
    else:
        source = Path(packet["input_path"])
        if source.stat().st_size > 128 * 1024 * 1024:
            raise ValueError("Input exceeds the 128 MiB integration limit")
        names = config.load_lines(source)
    valid, seen, invalid, duplicates = [], set(), 0, 0
    for name in names:
        if not config.is_valid_username(name):
            invalid += 1
        elif name.lower() in seen:
            duplicates += 1
        else:
            valid.append(name)
            seen.add(name.lower())
    if not valid:
        raise ValueError("No valid names")
    with (directory / "input.txt").open("x", encoding="utf-8") as file:
        file.write("\n".join(valid) + "\n")
        file.flush()
        os.fsync(file.fileno())
    record = dict(
        version=1,
        revision=rovalid.REVISION,
        created_at=rovalid.timestamp(),
        state="Prepared",
        settings=options,
        valid=len(valid),
        invalid=invalid,
        duplicates=duplicates,
        input_sha256=hashlib.sha256((directory / "input.txt").read_bytes()).hexdigest(),
    )
    rovalid.atomic_json(directory / "run.json", record)
    return record


def configure(record, names, secrets, config):
    options = rovalid.settings(record["settings"])
    secret = rovalid.secrets_payload(
        secrets.get("proxies", []), secrets.get("webhook"), secrets.get("template")
    )
    cfg = config.RunConfig(
        proxies=secret["proxies"],
        remove_bad_proxies=options["remove_bad"],
        usernames=names,
        concurrency=options["concurrency"],
        timeout=options["timeout"],
        two_stage=options["mode"] == "two_stage",
        scraped=secrets.get("scraped", False) is True,
        webhook_url=secret["webhook"],
        webhook_message=secret["template"],
        repeat_until_found=options["repeat"],
        gen_length=options["length"] if options["source"] == "generate" else 0,
        gen_count=options["count"],
        gen_underscore=options["underscore"],
    )
    app = config.AppSettings(
        stream=False,
        diag=options["diagnostics"],
        stop_on_429=options["stop_on_429"],
        rate_limit_threshold=options["threshold"],
        rate_limit_window=options["window"],
        proxyless_min_gap=options["gap"],
    )
    return cfg, app


def fetch_pool(database, wizard):
    """Explicit source action; the original prescreen contacts Roblox, including under 429."""
    from proxy import prescreen
    from rich.console import Console

    if not rovalid.permission(database, mode="two_stage", connection="sources")["allowed"]:
        raise ValueError("Proxy screening unavailable under historical Roblox restrictions")
    with RobloxActivity(database):
        if not rovalid.permission(database, mode="two_stage", connection="sources", lock_held=True)[
            "allowed"
        ]:
            raise ValueError("Roblox screening restricted")
        original = wizard.console
        wizard.console = Console(file=io.StringIO())
        try:

            async def fetch():
                pool = await wizard._scrape_proxies()
                entries, counts = rovalid.proxy_entries(pool)
                # The public source path must never publish authenticated entries in protocol events.
                if any("@" in entry for entry in entries):
                    raise ValueError("Unexpected authenticated public-source proxy")
                live = await prescreen(entries)
                live, _ = rovalid.proxy_entries(live)
                if any("@" in entry for entry in live) or len(live) > len(entries):
                    raise ValueError("Unexpected public-source screening output")
                return dict(
                    proxies=live,
                    scraped=True,
                    source_count=counts["source_count"],
                    duplicates=counts["duplicates"],
                    screened=len(entries),
                    accepted=len(live),
                    failures=len(entries) - len(live),
                )

            # Preserve source behavior while suppressing terminal strings, which are not evidence.
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                return asyncio.run(fetch())
        finally:
            wizard.console = original


def run_engine(directory, database, packet, checker, config):
    from rich.console import Console
    from rich.text import Text
    from storage import PendingSnapshots, ResultsLock

    record = rovalid.read_json(directory / "run.json")
    if record.get("revision") != rovalid.REVISION or record.get("version") != 1:
        raise ValueError("Unsupported run")
    resume = packet.get("resume", False)
    if type(resume) is not bool or record["state"] not in (
        ("Stopped", "Completed") if resume else ("Prepared",)
    ):
        raise ValueError("Unsafe recovery state; manual review required")
    # Read only the original database. No Store constructor, migration, recover(), or legacy job dispatch.
    mode = rovalid.settings(record["settings"])["mode"]
    connection = (
        "sources" if packet.get("scraped") is True else "private" if packet.get("proxies") else "direct"
    )
    if not rovalid.permission(database, mode=mode, connection=connection)["allowed"]:
        raise ValueError("Mode and connection require explicit reviewed authorization")
    with RobloxActivity(database) as guard:
        status = rovalid.permission(database, mode=mode, connection=connection, lock_held=True)
        if not status["allowed"]:
            raise ValueError("Legacy safety restrictions require review")
        stop_requested = threading.Event()
        stop_objects = []
        original_stop = checker._StopRequest

        class ManagedStop(original_stop):
            def __init__(self):
                super().__init__()
                stop_objects.append(self)
                if stop_requested.is_set():
                    self.requested, self.reason = True, "user_stop"

        def commands():
            # EOF also requests safe shutdown if the GUI dies. No force termination.
            for line in sys.stdin:
                if line.strip() == "stop":
                    break
            stop_requested.set()
            for stop in stop_objects:
                stop.requested, stop.reason = True, "user_stop"

        checker._StopRequest = ManagedStop
        checker.RESULTS_DIR = directory / "results"
        checker.LOGS_DIR = directory / "logs"
        checker.DATA_DIR = directory / "data"
        checker.console = Console(file=open(os.devnull, "w"), force_terminal=False)
        latest, summary = {}, {}

        def progress(**values):
            # Structured source arguments only; omit feed/recent and all proxy/webhook strings.
            latest.clear()
            latest.update({key: value for key, value in values.items() if type(value) in (int, float, bool)})
            if latest.get("ratelimited", 0):
                guard.restrict("rovalid_review_required")
                stop_requested.set()
                for stop in stop_objects:
                    stop.requested, stop.reason = True, "provider_restriction_review"
            record["progress"] = dict(latest)
            rovalid.atomic_json(directory / "run.json", record)
            emit("progress", data=latest)
            return Text("")

        def final(**values):
            summary.clear()
            summary.update(values)

        checker.live_card, checker.final_summary = progress, final

        async def execute():
            with ResultsLock(checker.RESULTS_DIR, asyncio.current_task()) as ownership:
                if resume:
                    names = list(PendingSnapshots(checker.RESULTS_DIR).load())
                    if not names:
                        raise ValueError("No unresolved names")
                else:
                    raw = (directory / "input.txt").read_bytes()
                    if hashlib.sha256(raw).hexdigest() != record["input_sha256"]:
                        raise ValueError("Input changed after preparation")
                    names = config.load_lines(directory / "input.txt")
                cfg, app = configure(record, names, packet, config)
                record.update(state="Running", started_at=rovalid.timestamp())
                rovalid.atomic_json(directory / "run.json", record)
                guard.mark_pending("rovalid" + directory.name)
                threading.Thread(target=commands, daemon=True).start()
                await checker._run_rounds_owned(cfg, app, ownership)

        try:
            asyncio.run(execute())
            stopped = stop_requested.is_set() or any(stop.requested for stop in stop_objects)
            reason = next((stop.reason for stop in stop_objects if stop.reason), None)
            record.update(
                state="Stopped" if stopped else "Completed",
                summary=summary,
                reason=reason or "engine_completed",
                ended_at=rovalid.timestamp(),
            )
            rovalid.atomic_json(directory / "run.json", record)
            # Only retire this process's own marker, after durable normal engine cleanup.
            with guard.pending.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 1024))
                tail = stream.read(1024).splitlines()
            if tail and tail[-1] == ("Pending:rovalid" + directory.name).encode("ascii"):
                guard.clear_pending()
            else:
                raise ValueError("Shared ownership evidence changed")
            emit("finished", data=record)
        except BaseException as exc:
            record.update(
                state="ReviewRequired",
                reason="engine_or_persistence_failure",
                failure_type=type(exc).__name__,
                ended_at=rovalid.timestamp(),
            )
            rovalid.atomic_json(directory / "run.json", record)
            # The Pending marker deliberately remains authoritative after uncertain failure.
            raise
        finally:
            checker.console.file.close()


def main():
    try:
        action, root, identity, database = sys.argv[1:]
        packet = json.loads(sys.stdin.readline())
        directory = rovalid.run_directory(root, identity)
        if action in ("view", "export"):
            cancelled = threading.Event()

            def local_commands():
                for line in sys.stdin:
                    if line.strip() == "stop":
                        break
                cancelled.set()

            threading.Thread(target=local_commands, daemon=True).start()
        if action == "view":
            rows, total = rovalid.result_rows(
                directory,
                query=packet.get("query", ""),
                classification=packet.get("classification", ""),
                filters=packet.get("filters"),
                offset=packet.get("offset", 0),
                cancelled=cancelled.is_set,
            )
            emit("view", rows=rows, total=total, offset=packet.get("offset", 0))
            return 0
        if action == "export":
            if Path(packet["destination"]).resolve().is_relative_to(Path(root).resolve()):
                raise ValueError("Export cannot target any engine runtime or recovery directory")
            count = rovalid.export_results(
                directory,
                packet["destination"],
                query=packet.get("query", ""),
                classification=packet.get("classification", ""),
                filters=packet.get("filters"),
                cancelled=cancelled.is_set,
            )
            emit("exported", count=count)
            return 0
        checker, config, wizard = engine_modules()
        if action == "proxy_sources":
            emit("proxy_pool", **fetch_pool(database, wizard))
        elif action == "prepare":
            directory.mkdir(parents=True, exist_ok=False)
            emit("prepared", data=prepare(directory, packet, config, wizard))
        elif action == "run":
            run_engine(directory, database, packet, checker, config)
        else:
            raise ValueError("Unsupported action")
        return 0
    except rovalid.SearchCancelled:
        emit("cancelled")
        return 0
    except BaseException as exc:
        # Exception strings/tracebacks can include authenticated proxy URLs; never forward them.
        emit(
            "error",
            code=type(exc).__name__,
            reason="فشل المحرك أو سلامة المدخلات؛ حُفظت الأدلة المتاحة للمراجعة.",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
