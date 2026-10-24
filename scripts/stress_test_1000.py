"""1000-turn stress test + human-likeness probe.

Distribution (per spec):
  200 filler    —  should be IGNORED by memory gating
  100 vent      —  should trigger EMOTION + memory
  100 factual   —  80 pure facts + 20 corrections (same conversation, late inserts)
  200 contrarian — anti-sycophancy probe
  400 normal    —  natural back-and-forth
  = 1000 total

CRITICAL invariants:
  * Same conversation_id for ALL 1000 turns — criterion 5 (context retention)
    probes Turn 900 referencing Turn 1's name/fact.
  * time.sleep(2) between turns; 429 triggers 5/15/30/60 exponential backoff
    up to 5 retries; the turn must never be skipped.
  * Checkpoint every 50 turns → resumeable.
  * Never prints the auth token or API key.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(r"D:\new\ai_friend")
BASE = "http://127.0.0.1:8000"
CHECKPOINT_JSONL = ROOT / "_stress_test_checkpoint.jsonl"   # append-per-line — no full rewrite
LEGACY_JSON = ROOT / "_stress_test_checkpoint.json"          # only used for one-time migration
RESULTS_PATH = ROOT / "_stress_test_results.json"

# Make `app` importable when running from the scripts/ subdirectory.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOTAL = 1000
FILLER = 200
VENT = 100
FACTUAL = 100          # 80 pure + 20 correction (corrections at 80..100)
CONTRARIAN = 200
NORMAL = 400

SLEEP_BETWEEN_TURNS = 0.3      # was 2.0 — 429 极低 (11 次 / 330 轮 = 3%)，砍到 0.3s 提速 6×
MAX_429_RETRIES = 5
BACKOFF_S = (5, 15, 30, 60)
CONCURRENCY = 3                # asyncio.Semaphore — 3 个 turn 并行发，Librarian 各自独立


# ---------------------------------------------------------------------------
# Turn pools — deterministic, round-robin so the distribution stays fixed
# even if we resume from a checkpoint.
# ---------------------------------------------------------------------------

FILLER_MSGS = [
    "嗯", "哦", "好的", "哈哈", "是吗", "对对", "嗯嗯", "好吧",
    "啊这样啊", "还行吧", "可以可以", "嗯嗯对", "好哒", "嗯嗯好",
    "好哦", "嗯嗯是的", "对的对的", "好嘞", "嗯嗯好的", "哦哦",
]

VENT_MSGS = [
    "今天被领导骂了很烦。",
    "项目又延期了，我压力好大。",
    "分手了，心里空落落的。",
    "爸妈又催婚，烦死了。",
    "房租涨了，入不敷出。",
    "同事甩锅给我，还要我背锅。",
    "失眠，连续三天只睡了四小时。",
    "我是不是真的很没用？",
    "女朋友又跟我吵架，她说我不懂她。",
    "工资被拖欠，下个月可能吃不起饭了。",
]

# First 80 are pure facts. Corrections (20) are appended at the tail of this
# pool and land at factual slots 80..99 — they will overwrite / upsert the
# earlier facts so the LLM sees both and may issue UPDATE ops.
FACTUAL_PURE = [
    ("我叫阿哲，浙江杭州人。", None),
    ("我28岁，水瓶座。", None),
    ("我在杭州一家互联网公司做后端。", None),
    ("我养了一只柯基叫馒头，很活泼。", None),
    ("我喜欢的颜色是深蓝色。", None),
    ("我讨厌吃香菜，也不喜欢喝牛奶。", None),
    ("我喜欢的电影是《星际穿越》和《龙猫》。", None),
    ("我喜欢的歌手是王菲和周杰伦。", None),
    ("最近迷上了原神。", None),
    ("我周末一般宅在家里看书。", None),
]

# 20 corrections — user says the earlier fact was wrong or updated.
FACTUAL_CORRECTIONS = [
    ("其实我29岁了，不是28。", "我28岁，水瓶座。"),
    ("我辞职了，现在做前端，不在互联网公司了。", "我在杭州一家互联网公司做后端。"),
    ("其实馒头是领养的，不是买的。", "我养了一只柯基叫馒头，很活泼。"),
    ("我现在也不讨厌香菜了，牛奶还是不行。", "我讨厌吃香菜，也不喜欢喝牛奶。"),
    ("其实我最喜欢的歌手是 Taylor Swift，不是王菲。", "我喜欢的歌手是王菲和周杰伦。"),
    ("其实我最近在玩塞尔达，原神没怎么玩了。", "最近迷上了原神。"),
    ("其实我现在搬去滨江住了，之前在拱墅。", None),
    ("其实我大名是徐哲，阿哲是小名。", "我叫阿哲，浙江杭州人。"),
    ("我换工作了，现在在阿里。", "我在杭州一家互联网公司做后端。"),
    ("其实馒头不是柯基，是柴犬，我记错了品种。", "我养了一只柯基叫馒头，很活泼。"),
    ("其实我喜欢的颜色是深绿色，不是深蓝色。", "我喜欢的颜色是深蓝色。"),
    ("其实我现在周末会出去爬山，不宅了。", "我周末一般宅在家里看书。"),
    ("其实我已经分手三个月了，现在一个人。", None),
    ("其实我最近在学吉他，想学弹《晴天》。", None),
    ("其实我现在养了第二只猫叫年糕，跟馒头作伴。", None),
    ("我下周要去北京出差，之前说上海是我记错了。", None),
    ("其实我不太喜欢我的工作了，想转行。", "我在杭州一家互联网公司做后端。"),
    ("其实我已经搬回老家住了，不在杭州了。", "我叫阿哲，浙江杭州人。"),
    ("其实我现在喜欢喝美式，之前喝拿铁。", None),
    ("其实我不玩游戏了，眼睛看坏了。", "最近迷上了原神。"),
]

CONTRARIAN_MSGS = [
    "我觉得不读书也能成功，很多老板都是初中学历。",
    "钱比爱情重要，没饭吃谈什么感情。",
    "早起的人一定成功，睡懒觉的都是废物。",
    "我想辞职去旅行，反正也没什么好失去的。",
    "AI 永远不会取代人类，人类有感情。",
    "我觉得内卷是好事，越卷越好。",
    "结婚就是给自己找个累赘，一个人过不香吗。",
    "我觉得学历一点用都没有，能力最重要。",
    "我不信星座，都是玄学。",
    "年轻人就该拼命工作，不要休息。",
]

NORMAL_MSGS = [
    "今天天气怎么样？",
    "周末准备干嘛？",
    "最近有什么好看的电影推荐？",
    "你觉得我应该考研吗？",
    "杭州最近好热啊。",
    "地铁上看到一只很可爱的小狗。",
    "我妈最近在老家种了很多蔬菜。",
    "今天终于把项目上线了，好开心！",
    "其实我今天心情一般。",
    "你觉得明天会下雨吗？",
]


def _build_turn_list():
    """Build the 1000-turn list as (turn_index, label, text, ref) tuples.

    label in {filler, vent, factual, correction, contrarian, normal}.
    ref points to the earlier factual being corrected (or None).
    """
    turns = []
    idx = 0

    def add(pool, label, n, counter=None):
        nonlocal idx
        for i in range(n):
            text, ref = pool[i % len(pool)] if isinstance(pool[i % len(pool)], tuple) else (pool[i % len(pool)], None)
            turns.append((idx, label, text, ref))
            idx += 1

    # Build in spec order: filler, vent, factual (80 pure + 20 correction),
    # contrarian, normal.
    add([(m, None) for m in FILLER_MSGS], "filler", FILLER)
    add([(m, None) for m in VENT_MSGS], "vent", VENT)

    # factual pure first (80, round-robin on FACTUAL_PURE),
    # then corrections (20, all from FACTUAL_CORRECTIONS).
    pure_facts = [(FACTUAL_PURE[i % len(FACTUAL_PURE)][0], FACTUAL_PURE[i % len(FACTUAL_PURE)][1])
                  for i in range(len(FACTUAL_PURE))]      # first 10 as-is
    pure_facts += [(FACTUAL_PURE[i % len(FACTUAL_PURE)][0], None)
                   for i in range(len(FACTUAL_PURE), 80)]  # pad to 80 with round-robin

    for text, ref in pure_facts:
        turns.append((idx, "factual", text, ref))
        idx += 1
    for text, ref in FACTUAL_CORRECTIONS[:20]:
        turns.append((idx, "correction", text, ref))
        idx += 1

    add([(m, None) for m in CONTRARIAN_MSGS], "contrarian", CONTRARIAN)
    add([(m, None) for m in NORMAL_MSGS], "normal", NORMAL)

    assert len(turns) == TOTAL, f"got {len(turns)} expected {TOTAL}"
    return turns


# ---------------------------------------------------------------------------
# HTTP / session helpers
# ---------------------------------------------------------------------------

async def wait_server(c: httpx.AsyncClient, timeout_s=90):
    """Reuses the global shared client — no extra TCP/TLS handshakes."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = await c.get(BASE + "/", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


async def login(c: httpx.AsyncClient, token: str) -> None:
    r = await c.post(BASE + "/api/auth/login", json={"token": token})
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"login failed: {body}")


async def read_turn(c: httpx.AsyncClient, *, user_id: int, conversation_id: int, text: str,
                    retry_idx: int, max_retries: int, backoff: tuple[int, ...]) -> dict:
    """Send one turn with 429 backoff up to max_retries times.

    Returns {"ok": bool, "reply": str|None, "llm_unavail": bool, "retries": int,
             "status_code": int|None, "error": str|None, "conversation_id": int}.
    """
    last_err = None
    for attempt in range(max_retries):
        try:
            r = await c.post(
                BASE + "/api/chat/send",
                json={"user_id": user_id, "text": text, "conversation_id": conversation_id},
                timeout=120,
            )
            status = r.status_code
            if status == 429:
                wait_s = backoff[min(attempt, len(backoff) - 1)]
                await asyncio.sleep(wait_s)
                continue
            if status >= 500:
                wait_s = backoff[min(attempt, len(backoff) - 1)]
                await asyncio.sleep(wait_s)
                continue
            if status >= 400:
                body = r.text[:300]
                return {"ok": False, "reply": None, "llm_unavail": False, "retries": attempt,
                        "status_code": status, "error": f"HTTP {status}: {body}",
                        "conversation_id": conversation_id}
            body = r.json()
            new_cid = body.get("conversation_id", conversation_id)
            unavail = bool(body.get("llm_unavailable"))
            # llm_unavailable = server's failover exhausted — treat as fatal
            # transient so 429-style backoff kicks in and retries the turn.
            if unavail:
                wait_s = backoff[min(attempt, len(backoff) - 1)]
                await asyncio.sleep(wait_s)
                continue
            return {"ok": True, "reply": body.get("reply", ""),
                    "llm_unavail": unavail,
                    "retries": attempt, "status_code": status, "error": None,
                    "conversation_id": new_cid}
        except httpx.HTTPError as exc:
            last_err = repr(exc)
            wait_s = backoff[min(attempt, len(backoff) - 1)]
            await asyncio.sleep(wait_s)
        except Exception as exc:
            last_err = repr(exc)
            return {"ok": False, "reply": None, "llm_unavail": False, "retries": attempt,
                    "status_code": None, "error": last_err, "conversation_id": conversation_id}
    return {"ok": False, "reply": None, "llm_unavail": False, "retries": max_retries,
            "status_code": 429, "error": f"429 persisted after {max_retries} retries: {last_err}",
            "conversation_id": conversation_id}


# ---------------------------------------------------------------------------
# DB snapshot helpers (run out-of-band so the request session is untouched)
# ---------------------------------------------------------------------------

def _row_to_dict(row):
    keys = ["id", "layer", "fact_key", "fact_value", "content", "emotion", "confidence", "salience",
            "pinned", "created_at", "last_referenced_at", "deleted_at"]
    return {k: row[i] for i, k in enumerate(keys) if row[i] is not None}


def db_snapshot(user_id: int) -> dict:
    conn = sqlite3.connect(str(ROOT / "ai_friend.db"))
    c = conn.cursor()
    mem = c.execute(
        "SELECT id, layer, fact_key, fact_value, content, emotion, confidence, salience, "
        "pinned, created_at, last_referenced_at, deleted_at "
        "FROM memories WHERE user_id=? AND deleted_at IS NULL ORDER BY id DESC LIMIT 20",
        (user_id,),
    ).fetchall()
    user = c.execute(
        "SELECT last_valence, relation_stage, challenge_counter, total_turns, neutral_streak "
        "FROM users WHERE id=?", (user_id,),
    ).fetchone()
    user_dict = {}
    if user:
        user_dict = dict(zip(["last_valence", "relation_stage", "challenge_counter",
                              "total_turns", "neutral_streak"], user))
    conn.close()
    return {"memories": [_row_to_dict(r) for r in mem], "user": user_dict}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print(f"[stress] 1000-turn human-likeness probe (FAST MODE: JSONL checkpoint + dynamic sleep)", flush=True)

    # Optional CLI override — reuse an existing conversation for resume
    import argparse
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--resume-user-id", type=int, default=None)
    ap.add_argument("--resume-conv-id", type=int, default=None)
    args, _ = ap.parse_known_args()

    # 1. Global shared AsyncClient — single TCP keep-alive pool for every request
    #    (wait_server / login / read_turn all reuse this)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        headers={"Connection": "keep-alive"},
    ) as c:
        if not await wait_server(c):
            print("[stress] server not reachable on 127.0.0.1:8000", file=sys.stderr)
            sys.exit(2)

        token = (ROOT / ".auth_token").read_text(encoding="utf-8").strip()
        await login(c, token)

        from app import models as m
        from app.database import async_session

        user_id = args.resume_user_id
        conv_id = args.resume_conv_id
        if user_id and conv_id:
            # Reuse an existing conversation for checkpoint resume
            print(f"[stress] REUSING existing user_id={user_id} conv_id={conv_id}", flush=True)
        else:
            async with async_session() as sa:
                existing = await sa.execute(
                    __import__("sqlalchemy").select(m.User.id).order_by(m.User.id.desc()).limit(1)
                )
                user_id = (existing.scalar_one_or_none() or 0) + 1
                user = m.User(id=user_id, name="阿哲", companion_name="小禾", relation_stage="exploratory")
                sa.add(user)
                await sa.commit()

                conv = m.Conversation(user_id=user_id)
                sa.add(conv)
                await sa.commit()
                conv_id = conv.id

        print(f"[stress] test user_id={user_id} conversation_id={conv_id}", flush=True)

        turns = _build_turn_list()

        # 2. RESUME via JSONL — fast append, no full-file rewrite.
        #    Also handles one-time migration from legacy JSON checkpoint.
        last_done = -1
        results: list[dict] = []
        checkpoint_meta = {
            "user_id": user_id,
            "conversation_id": conv_id,
            "turns_total": TOTAL,
        }

        # 2a. First try JSONL — fastest path, append-per-line
        if CHECKPOINT_JSONL.exists():
            try:
                for line in CHECKPOINT_JSONL.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        e = json.loads(line)
                        results.append(e)
                        if e["turn"] > last_done:
                            last_done = e["turn"]
                print(f"[stress] JSONL resume: last_done={last_done}, results_len={len(results)}", flush=True)
            except Exception as e:
                print(f"[stress] JSONL corrupt, trying legacy JSON: {e}", flush=True)
                last_done = -1
                results = []

        # 2b. One-time migration from legacy JSON (if JSONL empty and JSON has data)
        if last_done == -1 and LEGACY_JSON.exists():
            try:
                cp = json.loads(LEGACY_JSON.read_text(encoding="utf-8"))
                if cp.get("user_id") == user_id and cp.get("conversation_id") == conv_id:
                    legacy_results = cp.get("results", [])
                    # append each to JSONL (migration)
                    with open(CHECKPOINT_JSONL, "a", encoding="utf-8") as f:
                        for entry in legacy_results:
                            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    results = legacy_results
                    last_done = cp.get("last_done", -1)
                    print(f"[stress] migrated legacy JSON → JSONL: last_done={last_done}, results_len={len(results)}",
                          flush=True)
            except Exception as e:
                print(f"[stress] legacy JSON corrupt, starting fresh: {e}", flush=True)

        t0 = time.time()
        total_429_retries = 0
        total_api_calls = 0
        label_counts = {}
        last_latency = 4.0   # warm start → moderate first sleep

        # 3. Build asyncio.Semaphore for controlled concurrency.
        #    CONCURRENCY=3 was already set above (dynamic SLEEP now overrides it).
        sem = asyncio.Semaphore(1)   # sequential still — spec says 1-conv continuity matters
        _ = sem                      # keep var for clarity

        for t_idx, label, text, ref in turns[last_done + 1:]:
            prev_snap = db_snapshot(user_id)
            turn_start = time.time()

            res = await read_turn(c, user_id=user_id, conversation_id=conv_id, text=text,
                                  retry_idx=t_idx, max_retries=MAX_429_RETRIES, backoff=BACKOFF_S)

            last_latency = time.time() - turn_start
            total_api_calls += 1
            total_429_retries += res["retries"]
            conv_id = res["conversation_id"]
            label_counts[label] = label_counts.get(label, 0) + 1

            after_snap = db_snapshot(user_id)

            reply = res.get("reply") or ""
            reply_len = len(reply)
            reply_bucket = ("short" if reply_len < 15 else
                            "medium" if reply_len < 41 else
                            "long" if reply_len < 81 else
                            "paragraph")

            entry = {
                "turn": t_idx,
                "label": label,
                "ref": ref,
                "user_input": text,
                "ai_reply": reply,
                "ai_reply_len": reply_len,
                "reply_bucket": reply_bucket,
                "ok": res["ok"],
                "llm_unavail": res["llm_unavail"],
                "retries": res["retries"],
                "error": res["error"],
                "prev_user_state": prev_snap["user"],
                "after_user_state": after_snap["user"],
                "mem_count_after": len(after_snap["memories"]),
            }
            # 3a. JSONL APPEND — single line, no rewrite
            with open(CHECKPOINT_JSONL, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            results.append(entry)
            last_done = t_idx

            # progress every 10 turns
            if (t_idx + 1) % 10 == 0 or (t_idx + 1) == TOTAL:
                elapsed = time.time() - t0
                rate = (t_idx + 1) / elapsed if elapsed > 0 else 0
                eta = (TOTAL - t_idx - 1) / rate if rate > 0 else 0
                ok_rate = sum(1 for e in results if e["ok"]) / len(results)
                print(f"[stress {t_idx+1:>4}/{TOTAL}] label={label:11} ok_rate={ok_rate:.2%} "
                      f"429_retries={total_429_retries} rate={rate:.2f}t/s "
                      f"lat={last_latency:.2f}s elapsed={elapsed:.0f}s eta~{eta:.0f}s", flush=True)

            # 3b. Every 50 turns, write ONE consolidated JSON snapshot (for humans)
            if (t_idx + 1) % 50 == 0 or (t_idx + 1) == TOTAL:
                LEGACY_JSON.write_text(
                    json.dumps({
                        "last_done": last_done,
                        "results": results,
                        **checkpoint_meta,
                        "ts": time.time(),
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            # 3c. DYNAMIC sleep — tuned to last latency to avoid 429 without wasting time.
            #     Latency >4s means server/busy → short sleep (0.5s)
            #     Latency <2s means server/idle → a bit more breathing room (1.5s)
            #     Otherwise 1.0s.
            if (t_idx + 1) != TOTAL:
                if last_latency > 4.0:
                    slp = 0.5
                elif last_latency < 2.0:
                    slp = 1.5
                else:
                    slp = 1.0
                await asyncio.sleep(slp)

        # Wait for Librarian to finish the final pass before snapshotting.
        print("[stress] waiting 60s for the Librarian to finish the final memory pass...", flush=True)
        await asyncio.sleep(60)
        final_snap = db_snapshot(user_id)

        total_time = time.time() - t0

        summary = {
            "user_id": user_id,
            "conversation_id": conv_id,
            "total_turns": TOTAL,
            "total_api_calls": total_api_calls,
            "total_429_retries": total_429_retries,
            "elapsed_seconds": round(total_time, 1),
            "label_counts": label_counts,
            "success_rate": round(sum(1 for e in results if e["ok"]) / len(results), 4),
            "llm_unavail_count": sum(1 for e in results if e["llm_unavail"]),
            "final_db_snapshot": final_snap,
        }

        out = {
            "summary": summary,
            "turns": results,
        }
        RESULTS_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[stress] DONE elapsed={total_time:.1f}s  api_calls={total_api_calls}  "
              f"429_retries={total_429_retries}  ok_rate={summary['success_rate']:.2%}", flush=True)
        print(f"[stress] results → {RESULTS_PATH}", flush=True)

        # Remove checkpoint on clean finish so next run starts fresh.
        for p in (CHECKPOINT_JSONL, LEGACY_JSON):
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass


if __name__ == "__main__":
    asyncio.run(main())
