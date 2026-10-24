"""One-off maintenance script: remove short filler from the episodic memory layer.

Deletes rows in `memories` where:
    layer = 'episodic' AND length(trim(content)) < 8 AND fact_key IS NULL

Run from the project root:
    python clean_db.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "ai_friend.db"

SELECT_CONDITION = """
    layer = 'episodic'
    AND fact_key IS NULL
    AND length(trim(content)) < 8
"""


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"database not found: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    try:
        before = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        targets = conn.execute(
            f"SELECT id, content FROM memories WHERE {SELECT_CONDITION} ORDER BY id"
        ).fetchall()
        print(f"db: {DB_PATH}")
        print(f"memories before: {before}")
        print(f"matching rows: {len(targets)}")
        for row in targets[:50]:
            print(f"  id={row[0]} content={row[1]!r}")

        if targets:
            conn.execute(f"DELETE FROM memories WHERE {SELECT_CONDITION}")
            conn.commit()

        after = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        print(f"deleted: {before - after}")
        print(f"memories after: {after}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
