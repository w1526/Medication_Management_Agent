import argparse
import sys

from .http import serve


def main():
    if sys.version_info < (3, 10):
        raise SystemExit(
            "Medication Reminder with DeepSeek Harness requires Python 3.10+. "
            "Use .venv/bin/python -m medication_reminder ..."
        )
    parser = argparse.ArgumentParser(description="Medication Reminder MVP")
    parser.add_argument("--db", default="data/medication.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    serve(args.db, args.host, args.port, args.interval)


if __name__ == "__main__":
    main()

