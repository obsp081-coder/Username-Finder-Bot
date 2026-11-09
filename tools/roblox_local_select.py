"""Offline, optional Roblox file prioritization. Never submits candidates to a checker."""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from username_finder.roblox_local_selection import Cancelled, LocalSelection, Selection  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", type=Path, required=True, help="New scratch directory, or existing run with --resume"
    )
    parser.add_argument("--source", type=Path, help="Original UTF-8 TXT/CSV; never changed")
    parser.add_argument("--config", type=Path, help="Optional Selection JSON; defaults preserve source order")
    parser.add_argument("--resume", action="store_true", help="Use stored source and criteria, no overrides")
    parser.add_argument(
        "--export", type=Path, required=True, help="NEW output directory, including after cancellation"
    )
    args = parser.parse_args(argv)

    def forbidden(*args, **kwargs):
        raise AssertionError("Network access is forbidden in local selection")

    socket.socket.connect = socket.create_connection = forbidden
    tool = LocalSelection(args.run)
    try:
        if args.resume:
            if args.source or args.config:
                parser.error("Resume uses saved source/criteria; do not pass --source or --config")
        else:
            if not args.source:
                parser.error("A new run requires --source")
            config = (
                Selection.from_data(json.loads(args.config.read_text(encoding="utf-8-sig")))
                if args.config
                else Selection()
            )
            tool.create(args.source, config)
        tool.prepare()
        report = tool.export(args.export)
        print(
            json.dumps({"complete": True, "counts": report["counts"], "export": str(args.export)}, indent=2)
        )
        return 0
    except (Cancelled, KeyboardInterrupt):
        print(
            "Cancelled. Checkpoints retained. Resume explicitly with a NEW --export directory.",
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        print(f"Stopped safely: {exc}. Original source unchanged; no provider requests.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
