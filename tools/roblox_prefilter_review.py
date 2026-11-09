"""Read-only restriction/compatibility report; never dispatches or changes evidence."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from username_finder.roblox_bulk import write_new  # noqa: E402
from username_finder.roblox_prefilter import Prefilter  # noqa: E402
from username_finder.roblox_prefilter_review import inspect_operation  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument(
        "--database", required=True, type=Path, help="Original shared database path; not opened"
    )
    parser.add_argument("--output", type=Path, help="New JSON file; existing files are never overwritten")
    args = parser.parse_args(argv)
    report = inspect_operation(Prefilter(args.run, args.database))
    if args.output:
        write_new(args.output, report)
        print(str(args.output))
    else:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
