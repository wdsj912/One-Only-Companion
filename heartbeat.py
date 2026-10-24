"""Heartbeat monitor for the running stress_runner. Prints a status line every
INTERVAL_S seconds by tailing the stress log and querying the DB once."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\new\ai_friend")
STRESS_LOG = Path(r"C:\Users\Administrator\AppData\Local\Temp\trae-agent-toolhost\jobs\job-4eb477f3babc405982754498c85e3952\output.log")
INTERVAL_S = 120
MAX_TICKS = 1800  # max 1800*2 = 3600 min = 60h


def tail(path, n=5):
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        return lines[-n:]
    except Exception as e:
        return [f"<log read error: {e}>"]


def db_snap():
    try:
        c = sqlite3.connect(str(ROOT / "ai_friend.db")).cursor()
        mem = c.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0]
        sm = c.execute("SELECT COUNT(*) FROM memories WHERE layer='semantic' AND deleted_at IS NULL").fetchone()[0]
        em = c.execute("SELECT COUNT(*) FROM memories WHERE layer='episodic' AND deleted_at IS NULL").fetchone()[0]
        conv = c.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        msg = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        dup = c.execute(
            "SELECT fact_key, COUNT(*) FROM memories "
            "WHERE fact_key IS NOT NULL AND deleted_at IS NULL "
            "GROUP BY fact_key HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC LIMIT 3"
        ).fetchall()
        c.close()
        return f"mem={mem}(s={sm}e={em}) conv={conv} msg={msg} dup={dup}"
    except Exception as e:
        return f"<db err: {e}>"


def log_tail(n=3):
    log_path = ROOT / "_memory_agent.log"
    if not log_path.exists():
        return "(no Librarian log yet)"
    try:
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        if not lines:
            return "(empty Librarian log)"
        last = json.loads(lines[-1])
        ok = [r for r in last.get("results", []) if not r.startswith("skipped") and not r.startswith("parse")]
        err = last.get("error")
        return f"last={last.get('ts')[:19]} results={ok or '[none]'} err={err and err[:80] or '-'}"
    except Exception as e:
        return f"<log tail err: {e}>"


print(f"[heartbeat] monitor every {INTERVAL_S}s, ctrl+C to stop")
t0 = time.time()
prev_last_line = None
ticks = 0
while ticks < MAX_TICKS:
    time.sleep(INTERVAL_S)
    ticks += 1
    elapsed = time.time() - t0
    lines = tail(STRESS_LOG, 1)
    last = lines[0] if lines else "(log empty)"
    changed = last != prev_last_line
    prev_last_line = last
    print(f"[heartbeat @{elapsed/60:.0f}m] {last} | db: {db_snap()} | librarian: {log_tail()} | log_changed={changed}", flush=True)

print("[heartbeat] max ticks reached")
