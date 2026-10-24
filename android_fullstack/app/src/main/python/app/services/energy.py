"""精力（user.energy）的生物钟恢复 —— 纯函数，无 DB 依赖。

规则（用户指定的昼夜节律）：
  · 深夜（本地 23:00–06:00）：无论多久没聊，精力上限压到 40 —— 她一定犯困
  · 早晨（06:00–10:00）且沉默 ≥6 小时：睡到自然醒，精力 85~100
  · 白天（10:00–23:00）：沉默每小时 +5，封顶 90
  · 聊天消耗仍由 emotion kernel 按轮次负责，这里不管向下消耗（深夜压帽除外）

时间戳一律 naive UTC（与 models.utcnow 一致），换算本地边界时 +8h。
"""
from __future__ import annotations

import datetime as dt

NIGHT_CAP = 40.0            # 深夜精力上限
MORNING_ENERGY_BASE = 85.0  # 睡了一觉醒来
MORNING_ENERGY_BONUS = 15.0
SLEEP_GAP_HOURS = 6.0
DAY_RECOVER_PER_HOUR = 5.0
DAY_RECOVER_CAP = 90.0
REST_AFTER_HOURS = 0.5
LOCAL_OFFSET_HOURS = 8  # UTC+8


def recalc_energy(
    current: float | None,
    last: dt.datetime | None,
    now: dt.datetime | None = None,
) -> tuple[float, dt.datetime]:
    """返回 (新精力值, 本次计算时间)。同一 (current, last, now) 输入结果确定。"""
    now = (now or dt.datetime.now(dt.timezone.utc)).replace(tzinfo=None)
    cur = max(0.0, min(100.0, current if current is not None else 70.0))
    if last is None:
        last = now
    last = last.replace(tzinfo=None)
    gap_h = max(0.0, (now - last).total_seconds() / 3600.0)
    local_hour = (now + dt.timedelta(hours=LOCAL_OFFSET_HOURS)).hour

    # 深夜：生物钟犯困，超过上限直接压下来
    if local_hour >= 23 or local_hour < 6:
        return float(int(round(min(cur, NIGHT_CAP)))), now
    # 早晨且睡了一觉 → 85~100
    if 6 <= local_hour < 10 and gap_h >= SLEEP_GAP_HOURS:
        bonus = min(MORNING_ENERGY_BONUS, (gap_h - SLEEP_GAP_HOURS) * 3.0)
        return float(int(round(min(100.0, MORNING_ENERGY_BASE + bonus)))), now
    # 白天沉默缓慢回血，封顶 90
    if 10 <= local_hour < 23 and gap_h >= REST_AFTER_HOURS:
        cur = min(DAY_RECOVER_CAP, cur + gap_h * DAY_RECOVER_PER_HOUR)
    return float(int(round(cur))), now
