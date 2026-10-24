"""Stress runner — 10000 chat turns against the real server with full
429/5xx exponential backoff, per-conversation reuse, and periodic checkpoints.

Every turn: send -> wait for Librarian (up to 60s with 429 retries) -> log.
After N runs: dump DB summary + _memory_agent.log tail + latency percentiles.

Never prints the .auth_token or the server's API key.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

BASE = "http://127.0.0.1:8000"
ROOT = Path(r"D:\new\ai_friend")
RUNS = 1000
PROGRESS_EVERY = 10       # print live progress every N turns
SNAPSHOT_EVERY = 100      # dump DB / latency snapshot every N turns


def wait_server(timeout_s=60):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE + "/", timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


class HttpClient:
    def __init__(self):
        self.cj = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))

    def req(self, method, path, payload=None, timeout=120):
        data = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(
            BASE + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with self.opener.open(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())

    def post_with_backoff(self, path, payload, *, max_attempts=10):
        attempt = 0
        backoff = 2.0  # seconds
        last_err = None
        while attempt < max_attempts:
            try:
                status, body = self.req("POST", path, payload)
                # HTTP 5xx or explicit rate-limit flag — retry
                if status >= 500 or body.get("llm_unavailable"):
                    time.sleep(backoff)
                    backoff = min(backoff * 1.6, 120)
                    attempt += 1
                    continue
                return True, status, body, attempt
            except urllib.error.HTTPError as e:
                # 429 specifically — wait longer
                if e.code == 429:
                    time.sleep(backoff * 2)
                    backoff = min(backoff * 2, 120)
                else:
                    time.sleep(backoff)
                backoff = min(backoff * 1.6, 120)
                attempt += 1
                last_err = f"HTTP {e.code}"
            except Exception as e:
                time.sleep(backoff)
                backoff = min(backoff * 1.6, 120)
                attempt += 1
                last_err = repr(e)
        return False, None, {"error": last_err}, attempt


# ---------------------------------------------------------------------------
# Content pool — diverse prompts so the Librarian has real curation work.
# Mixed Chinese/English, names, facts, preferences, corrections, emotions.
# ---------------------------------------------------------------------------

PROMPTS = [
    # identity anchors
    "我叫阿哲，浙江杭州人。",
    "我28岁，喜欢的颜色是深蓝色。",
    "我在一家互联网公司做后端开发。",
    "其实我本名不叫阿哲，那是小名，我大名是徐哲。",   # 更正
    "我属蛇，水瓶座。",
    # pets / facts
    "我养过一只叫豆豆的猫，现在已经走了，我很伤心。",
    "后来我又养了只柯基叫馒头，特别活泼。",
    "其实豆豆其实是被我送到老家给爸妈养了，没有走，是我记错了。",  # 更正
    # preferences
    "我讨厌吃香菜，也不喜欢喝牛奶。",
    "其实香菜我现在可以接受了，牛奶还是不行。",                    # 更正
    "我喜欢的电影是《星际穿越》和《龙猫》。",
    "我喜欢的歌手是王菲、周杰伦和 Taylor Swift。",
    "最近迷上了原神，一直在抽角色。",
    # emotions
    "今天上班被领导骂了，很烦。",
    "周五终于把项目上线了，真开心！",
    "最近失眠，压力很大。",
    "今天去西湖散步，心情好多了。",
    # corrections / updates
    "其实我不喜欢原神，那是我表弟在玩，我喜欢的是塞尔达。",
    "我换工作了，现在做前端。",
    "我搬家了，现在住在滨江。",
    # generic conversation
    "周末一般宅在家里看书。",
    "最近在读《三体》，已经读到第三部了。",
    "AI 技术发展得好快啊。",
    "你觉得我应该考研吗？",
    "我最近想学吉他。",
    "杭州最近好热。",
    "今天地铁上看到一只很可爱的小狗。",
    "我妈最近在老家种了很多蔬菜。",
    "下周要去上海出差。",
    "其实我下周去的是北京，不是上海，记错了。",                    # 更正
]


def rand_prompt(i: int) -> str:
    """Deterministic round-robin so every run is reproducible."""
    return PROMPTS[i % len(PROMPTS)]


def db_summary():
    conn = sqlite3.connect(str(ROOT / "ai_friend.db"))
    c = conn.cursor()
    n_mem = c.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0]
    n_sem = c.execute("SELECT COUNT(*) FROM memories WHERE layer='semantic' AND deleted_at IS NULL").fetchone()[0]
    n_epi = c.execute("SELECT COUNT(*) FROM memories WHERE layer='episodic' AND deleted_at IS NULL").fetchone()[0]
    n_pinned = c.execute("SELECT COUNT(*) FROM memories WHERE pinned=1 AND deleted_at IS NULL").fetchone()[0]
    n_conv = c.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    n_msg = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    # top fact_keys to spot duplicates
    dup = c.execute(
        "SELECT fact_key, COUNT(*) c FROM memories "
        "WHERE fact_key IS NOT NULL AND deleted_at IS NULL "
        "GROUP BY fact_key HAVING c > 1 ORDER BY c DESC LIMIT 5"
    ).fetchall()
    # soft deletes
    n_del = c.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NOT NULL").fetchone()[0]
    # recent 5 Librarian log entries
    log_tail = ""
    log_path = ROOT / "_memory_agent.log"
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        log_tail = "\n".join(lines[-5:])
    conn.close()
    return {
        "mem": n_mem, "sem": n_sem, "epi": n_epi, "pinned": n_pinned,
        "conv": n_conv, "msg": n_msg, "deleted": n_del,
        "dup_keys": dup, "log_tail": log_tail,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def main():
    if not wait_server(60):
        print("server not reachable on 127.0.0.1:8000", file=sys.stderr)
        sys.exit(2)

    client = HttpClient()
    token = (ROOT / ".auth_token").read_text(encoding="utf-8").strip()
    _, _, login, _ = client.post_with_backoff("/api/auth/login", {"token": token})
    if not login.get("ok"):
        print(f"login failed: {login}", file=sys.stderr)
        sys.exit(3)

    conv_id = None
    successes = 0
    failures = 0
    llm_unavail = 0
    latencies: list[float] = []
    backoff_total = 0.0
    t0 = time.time()
    print(f"[stress] starting {RUNS} turns, {len(PROMPTS)}-prompt pool, same conversation, real LLM + backoff")

    for i in range(RUNS):
        prompt = rand_prompt(i)
        payload = {"user_id": 1, "text": prompt}
        if conv_id is not None:
            payload["conversation_id"] = conv_id

        t_start = time.time()
        ok, status, body, attempts = client.post_with_backoff("/api/chat/send", payload)
        t_end = time.time()
        lat = t_end - t_start
        latencies.append(lat)
        backoff_total += max(0, attempts - 1)

        if ok and status == 200:
            conv_id = body.get("conversation_id", conv_id)
            successes += 1
            if body.get("llm_unavailable"):
                llm_unavail += 1
        else:
            failures += 1

        # progress
        if (i + 1) % PROGRESS_EVERY == 0 or (i + 1) == RUNS:
            elapsed = t_end - t0
            p50 = percentile(latencies, 0.5)
            p95 = percentile(latencies, 0.95)
            p99 = percentile(latencies, 0.99)
            eta = (RUNS - i - 1) * (elapsed / (i + 1))
            print(
                f"[{i+1:>5}/{RUNS}] ok={successes} fail={failures} llm_unavail={llm_unavail} "
                f"lat p50={p50:.2f}s p95={p95:.2f}s p99={p99:.2f}s "
                f"backoff_attempts={backoff_total} "
                f"elapsed={elapsed:.0f}s eta~{eta:.0f}s ({eta/60:.1f}m)",
                flush=True,
            )

        # checkpoint snapshot
        if (i + 1) % SNAPSHOT_EVERY == 0:
            # Wait for the Librarian to finish the latest pass (it runs
            # asynchronously; DB growth should appear within a few seconds).
            time.sleep(6)
            s = db_summary()
            print(f"  [snapshot @{i+1}] mem={s['mem']} (sem={s['sem']} epi={s['epi']} pinned={s['pinned']}) "
                  f"deleted={s['deleted']} conv={s['conv']} msg={s['msg']} dup_keys={s['dup_keys']}", flush=True)
            if s["log_tail"]:
                # last 200 chars of log to confirm Librarian is alive
                print(f"    log_tail: {s['log_tail'][-200:]}", flush=True)

    elapsed = time.time() - t0
    s = db_summary()
    print("\n=== FINAL SUMMARY ===")
    print(f"runs: {RUNS}  ok: {successes}  fail: {failures}  llm_unavail: {llm_unavail}")
    print(f"elapsed: {elapsed:.1f}s ({elapsed/60:.1f}m)  avg_turn: {elapsed/RUNS:.2f}s")
    print(f"latency p50={percentile(latencies,0.5):.2f}s  "
          f"p95={percentile(latencies,0.95):.2f}s  "
          f"p99={percentile(latencies,0.99):.2f}s")
    print(f"chat_retry_attempts_total: {backoff_total}")
    print(f"memories total: {s['mem']}  semantic={s['sem']} episodic={s['epi']} pinned={s['pinned']}  "
          f"deleted={s['deleted']}")
    print(f"conversations: {s['conv']}  messages: {s['msg']}")
    print(f"duplicate fact_keys: {s['dup_keys']}")
    print(f"\nLibrarian log tail (last 5 entries):")
    print(s["log_tail"] or "(no log)")

    # server still up?
    try:
        urllib.request.urlopen(BASE + "/", timeout=2).read()
        print("\nserver alive: YES  (port 8000 still serving)")
    except Exception as e:
        print(f"\nserver alive: NO  ({e})")

    # exit non-zero only if failures are abnormal (>1%)
    if failures > RUNS * 0.01:
        sys.exit(1)


if __name__ == "__main__":
    main()
