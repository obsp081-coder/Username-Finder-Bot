"""Offline-only Bulk capacity scenarios. No database, provider requests or job changes."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from username_finder.roblox_bulk_protocol import encoded  # noqa: E402
from username_finder.roblox_prefilter_estimates import capacity_plan  # noqa: E402

BATCHES = (20, 50, 100, 200, 500, 1000)
SCENARIOS = (
    ("default_illustration", 0.5, 10),
    ("one_second_illustration", 0.5, 1),
    ("fast_unverified_illustration", 0.25, 0.5),
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--names", type=int, default=2_000_000)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--local-seconds", type=float, default=0, help="Explicit local overhead assumption")
    parser.add_argument(
        "--safe-fraction", type=float, default=1, help="Unresolved outcomes never cause retries"
    )
    args = parser.parse_args(argv)
    try:
        rows = []
        for label, latency, delay in SCENARIOS:
            for batch in BATCHES:
                plan = capacity_plan(
                    args.names,
                    batch,
                    delay,
                    latency=latency,
                    hours=args.hours,
                    local_seconds_per_request=args.local_seconds,
                    safe_outcome_fraction=args.safe_fraction,
                )
                plan["scenario"] = label
                plan["maximum_ascii_request_json_bytes"] = len(
                    encoded({"usernames": ["X" * 20] * batch, "excludeBannedUsers": False})
                )
                rows.append(plan)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"kind": "offline_assumptions_not_live_measurements", "scenarios": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
