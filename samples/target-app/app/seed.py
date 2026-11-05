"""Insert synthetic work items: `python -m app.seed --count 1000`."""

import argparse
import random

import psycopg

STATUSES = ("todo", "in_progress", "done")
VERBS = ("Review", "Update", "Check", "Write", "Fix", "Plan", "Test", "Deploy")
NOUNS = ("invoice", "report", "schedule", "contract", "dashboard", "backup", "ticket", "release")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1000)
    args = parser.parse_args()

    rows = [
        (
            f"{random.choice(VERBS)} {random.choice(NOUNS)} #{i:04d}",
            f"Synthetic seed item {i}",
            random.choice(STATUSES),
        )
        for i in range(1, args.count + 1)
    ]
    # Empty conninfo: settings come from the PG* environment variables.
    with psycopg.connect("") as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO work_items (title, description, status) VALUES (%s, %s, %s)", rows
        )
        total = conn.execute("SELECT count(*) FROM work_items").fetchone()[0]
    print(f"inserted {len(rows)} rows; work_items now has {total} rows")


if __name__ == "__main__":
    main()
