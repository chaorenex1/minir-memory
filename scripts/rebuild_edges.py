"""Rebuild the page-sourced edges from the knowledge files.

Dry-run by default. On --apply it copies the database beside itself, then replaces every
``src_kind='page'`` edge with the edges the pages currently declare. Manual edges
(``minir_link``) are untouched: the files are the source, the graph is the index.

    python scripts/rebuild_edges.py --db <memory.db> --knowledge <knowledge root>
    python scripts/rebuild_edges.py --db <memory.db> --knowledge <root> --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from minir import knowledge  # noqa: E402
from minir.store import MinirStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--knowledge", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args()

    db = Path(args.db)
    root = Path(args.knowledge)
    if not db.is_file():
        print("no database at %s" % db)
        return 2
    if not root.is_dir():
        print("no knowledge root at %s" % root)
        return 2

    edges = knowledge.derived_edges(root)
    print("pages declare %d edges" % len(edges))
    if not args.apply:
        print("dry run; pass --apply to replace the page-sourced edges")
        return 0

    backup_dir = Path(args.backup_dir) if args.backup_dir else db.parent
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / ("%s.pre-edge-rebuild-%s"
                           % (db.name, time.strftime("%Y%m%d-%H%M%S")))
    shutil.copy2(db, backup)
    print("backup: %s" % backup)

    store = MinirStore(db)
    try:
        written = store.rebuild_page_edges(edges)
    finally:
        store.close()
    print("rebuilt %d page edges" % written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
