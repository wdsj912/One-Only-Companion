"""手动触发一条假的 proactive 消息 —— 让前端 poll 拉到 → 弹托盘气泡。

用法：
  桌面版正在运行时，在单独的 cmd/PowerShell 里跑：
    cd D:\new\ai_friend
    .venv\Scripts\python scripts\poke_proactive.py

脚本会自动找到桌面版正在用的 DB（%LOCALAPPDATA%\\XiaoHe\\ai_friend.db），
插一条 role=assistant, proactive=True 的消息到该用户最近的 conversation。
前端 15 秒内 poll 就能拉到 → window.pywebview.api.notify() → 气泡。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
from pathlib import Path


def _find_db() -> Path:
    """优先桌面版数据目录，fallback 项目根。"""
    candidates = []
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "XiaoHe" / "ai_friend.db")
    candidates.append(Path(__file__).resolve().parent.parent / "ai_friend.db")
    for p in candidates:
        if p.exists() and p.stat().st_size > 1024:
            return p
    raise FileNotFoundError(f"ai_friend.db 没找到 — 搜过: {candidates}")


# 三条不同风格的假 proactive 文案（模拟 LLM 输出）
FAKE_TEXTS = [
    "刚才路过一家很好看的花店，突然就想到你啦~",
    "哼，居然这么久都不理我？",
    "你知道我一直在等你吗",
    "突然想听听你的声音了",
    "我今天好乖哦，有没有奖励呀",
]


def main() -> int:
    db = _find_db()
    print(f"DB: {db}")

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 确保 proactive 列存在（老库可能没迁移过）
    cur.execute("PRAGMA table_info(messages)")
    cols = {r[1] for r in cur.fetchall()}
    if "proactive" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN proactive BOOLEAN DEFAULT 0")
        conn.commit()
        print("已补 proactive 列")

    # 找 user（优先 1 号，fallback 任意）
    cur.execute("SELECT id FROM users LIMIT 1")
    user = cur.fetchone()
    if user is None:
        print("DB 里还没有用户 — 请先在桌面版完成一次登录/对话")
        return 1
    user_id = user["id"]
    print(f"user_id: {user_id}")

    # 找该用户最近的 conversation
    cur.execute(
        "SELECT id FROM conversations WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    conv = cur.fetchone()
    if conv is None:
        cur.execute(
            "INSERT INTO conversations (user_id, started_at) VALUES (?, ?)",
            (user_id, dt.datetime.utcnow().isoformat()),
        )
        conv_id = cur.lastrowid
        print(f"新建 conversation_id: {conv_id}")
    else:
        conv_id = conv["id"]
        print(f"conversation_id: {conv_id}")

    text = FAKE_TEXTS[dt.datetime.now().second % len(FAKE_TEXTS)]
    cur.execute(
        "INSERT INTO messages (conversation_id, role, content, proactive, created_at) "
        "VALUES (?, 'assistant', ?, 1, ?)",
        (conv_id, text, dt.datetime.utcnow().isoformat()),
    )
    msg_id = cur.lastrowid
    conn.commit()
    conn.close()

    print()
    print(f"✅ 已插入假 proactive 消息 id={msg_id}:")
    print(f"   内容: 「{text}」")
    print()
    print("前端 poll 间隔 15 秒 ——")
    print("  如果桌面版窗口被最小化/失焦 → 托盘气泡应该在几秒内弹出")
    print("  如果窗口在前台聚焦 → 消息只会出现在聊天列表里")
    return 0


if __name__ == "__main__":
    sys.exit(main())
