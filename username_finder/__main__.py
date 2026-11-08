import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Username Finder Bot — local simulation only")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "runtime" / "username_history.db",
    )
    args = parser.parse_args()
    from .gui import run

    raise SystemExit(run(args.database))


if __name__ == "__main__":
    main()
