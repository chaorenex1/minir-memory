"""Drop the pre-v5 marker inventory and the extraction rows it produced.

Dry-run by default. On --apply it copies the database beside itself, then in one transaction
clears the marker copies, the artifact arrays and the extraction queue, and rebuilds the
base_refreshes FTS index. Knowledge pages are never touched.

    python scripts/drop_legacy_markers.py --db <memory.db>
    python scripts/drop_legacy_markers.py --db <memory.db> --apply
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import time
from pathlib import Path

EMPTY_MARKERS = '{"value_markers": []}'


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0] if row else 0)


def _marker_artifacts(conn: sqlite3.Connection) -> int:
    # EMPTY_MARKERS holds no single quote, so the literal is safe to inline.
    sql = ("SELECT COUNT(*) FROM base_refreshes WHERE value_markers NOT IN ('', '%s')"
           % EMPTY_MARKERS)
    return _count(conn, sql)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.is_file():
        print("no database at %s" % db)
        return 2

    conn = sqlite3.connect(str(db))
    try:
        before = {
            "marker_conclusions": _count(
                conn, "SELECT COUNT(*) FROM conclusions WHERE kind = 'base.value_markers'"),
            "artifact_rows": _marker_artifacts(conn),
            "extraction_rows": _count(conn, "SELECT COUNT(*) FROM session_extractions"),
        }
        print("before: %s" % before)
        if not args.apply:
            print("dry run; pass --apply to delete the rows above")
            return 0

        backup_dir = Path(args.backup_dir) if args.backup_dir else db.parent
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / ("%s.pre-marker-reset-%s"
                               % (db.name, time.strftime("%Y%m%d-%H%M%S")))
        shutil.copy2(db, backup)
        print("backup: %s" % backup)

        with conn:
            conn.execute("DELETE FROM conclusions WHERE kind = 'base.value_markers'")
            conn.execute("UPDATE base_refreshes SET value_markers = ? "
                         "WHERE value_markers NOT IN ('', ?)",
                         (EMPTY_MARKERS, EMPTY_MARKERS))
            conn.execute("DELETE FROM session_extractions")
            has_fts = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'base_refreshes_fts'").fetchone()
            if has_fts:
                conn.execute("INSERT INTO base_refreshes_fts(base_refreshes_fts) VALUES('rebuild')")

        after = {
            "marker_conclusions": _count(
                conn, "SELECT COUNT(*) FROM conclusions WHERE kind = 'base.value_markers'"),
            "artifact_rows": _marker_artifacts(conn),
            "extraction_rows": _count(conn, "SELECT COUNT(*) FROM session_extractions"),
        }
        print("after: %s" % after)
        return 0 if not any(after.values()) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
