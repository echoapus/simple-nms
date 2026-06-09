#!/usr/bin/env python3
"""Data retention cleanup — delete events older than N days.

Usage:
    python3 cleanup.py                  # default: 30 days
    python3 cleanup.py --days 7         # keep only last 7 days
    python3 cleanup.py --dry-run        # show what would be deleted

Cron example (daily at 03:00):
    0 3 * * * cd /opt/simple-nms && python3 cleanup.py --days 30 >> /var/log/simple-nms-cleanup.log 2>&1
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

DEFAULT_DB = "data/events.db"
DEFAULT_DAYS = 30


def main():
    parser = argparse.ArgumentParser(description="Simple NMS — data retention cleanup")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"Database path (default: {DEFAULT_DB})")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Delete events older than N days (default: {DEFAULT_DAYS})")
    parser.add_argument("--dry-run", action="store_true", help="Show count without deleting")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Error: database not found: {args.db}")
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    cutoff = f"datetime('now', '-{args.days} days')"

    # Count events to delete
    count = conn.execute(f"SELECT COUNT(*) FROM events WHERE ts < {cutoff}").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if args.dry_run:
        print(f"[{ts}] DRY RUN: {count} of {total} events older than {args.days} days would be deleted")
    elif count == 0:
        print(f"[{ts}] No events older than {args.days} days. Total: {total}")
    else:
        conn.execute(f"DELETE FROM events WHERE ts < {cutoff}")
        conn.commit()
        remaining = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        print(f"[{ts}] Deleted {count} events older than {args.days} days. Remaining: {remaining}")

        # Reclaim disk space
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print(f"[{ts}] WAL checkpoint completed")

    conn.close()


if __name__ == "__main__":
    main()
