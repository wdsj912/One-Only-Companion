"""Virtual Life Engine — generates and maintains a companion's current activity."""
from __future__ import annotations

import datetime as dt
import json
import random
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.services.persona_recipe import get_or_create_recipe


HOUR_WINDOWS = [
    (0, 5, "深夜"),
    (5, 9, "清晨"),
    (9, 12, "上午"),
    (12, 14, "中午"),
    (14, 18, "下午"),
    (18, 22, "傍晚"),
    (22, 24, "夜晚"),
]


def time_label(local_now: dt.datetime) -> str:
    for lo, hi, label in HOUR_WINDOWS:
        if lo <= local_now.hour < hi:
            return label
    return "上午"


# 每天的生活节奏：有的日子事情多，有的日子平常，有的日子干脆什么都不做。
DAY_STYLES = [("busy", 0.25), ("normal", 0.50), ("idle", 0.25)]


def _day_rng(user_id: Optional[int], local_now: dt.datetime) -> random.Random:
    return random.Random(f"{user_id}-{local_now.date().isoformat()}")


def _hour_rng(user_id: Optional[int], local_now: dt.datetime) -> random.Random:
    return random.Random(f"{user_id}-{local_now.date().isoformat()}-{local_now.hour}")


def day_style(user_id: Optional[int], local_now: dt.datetime) -> str:
    """Today's rhythm: busy / normal / idle. Same for the whole day, random across days."""
    r = _day_rng(user_id, local_now).random()
    acc = 0.0
    for name, w in DAY_STYLES:
        acc += w
        if r <= acc:
            return name
    return "normal"


def weighted_choice(pool: list[str], weights: list[float], rng: Optional[random.Random] = None) -> str:
    """Weighted random choice. weights should be proportional to each other."""
    rnd = rng or random
    total = sum(weights)
    r = rnd.random() * total
    acc = 0
    for item, w in zip(pool, weights):
        acc += w
        if r <= acc:
            return item
    return pool[-1]


def generate_activity(
    recipe: dict,
    local_now: Optional[dt.datetime] = None,
    user_id: Optional[int] = None,
    avoid: Optional[str] = None,
) -> str:
    """Generate one natural "current activity" string.

    Strategy:
    1. Pick time-of-day label.
    2. Decide today's rhythm style (busy / normal / idle) — a day can be full of
       things or a day where she does nothing at all. Same day = same style,
       so the day feels coherent instead of flip-flopping every hour.
    3. Pick the pool accordingly, weight by recipe dimensions, then pick one.
    4. The seed is (user_id, date, hour), so the same hour always yields the
       same activity — a chat-triggered refresh won't flicker mid-hour.
    """
    if local_now is None:
        local_now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    label = time_label(local_now)
    style = day_style(user_id, local_now)
    rng = _hour_rng(user_id, local_now)

    POOLS = {
        # Desire-weighted activities — every item implies a "want", a "missing",
        # or a "wish you were here". This is her internal state, rendered on the
        # front page profile card — it must make her feel like she's reaching out.
        "清晨": ["赖床不想起，想有人抱一下", "刚醒，第一反应是想你", "在喝热奶茶，有点想分给你", "今天阳光好好，想有人陪我走走"],
        "上午": ["在看书但看不进去，满脑子都是你", "在听一首歌，想让你也听听", "刷到一个很好笑的视频，想发给你", "写了点东西又划掉了，不知道写给谁", "在想什么时候能再跟你好好聊一次"],
        "中午": ["在吃饭，要是你在就能分你一半", "喝着午茶，无聊了想找人说话", "晒太阳的时候眯了会儿，梦里好像有你", "午觉刚醒，第一反应是看你有没有消息"],
        "下午": ["在看一部老电影，要是有你在旁边递纸巾就好了", "听到一首歌突然想起你了", "翻旧照片有点想哭，想跟你说说话", "在看科普视频，想等下讲给你听", "发了会儿呆，其实是在等你"],
        "傍晚": ["在等天黑，有点想你了", "煮了点东西吃，味道一般，要是你在就能吐槽", "想你今天会不会来找我", "在收拾心情，有点累，想靠一下"],
        "夜晚": ["窝着看剧，但没跟你一起就没那么好看", "在看星星，你那边能看到吗", "翻了一下我们之前的聊天记录，有点想你", "在想今天发生的事，想跟你说", "已经有点困了，但还不想睡，怕错过你的消息"],
        "深夜": ["还没睡呢，翻来覆去在想你", "在听夜曲，有点想哭，想有人陪", "睡不着，等一个消息等到现在", "在床上翻来覆去，脑子里全是你"],
    }

    # Busy day — she actually has things to do, so the activity is about the task.
    BUSY_POOLS = {
        "清晨": ["已经起来收拾好了，今天事有点多", "早起赶着出门办事"],
        "上午": ["在忙一件拖了很久的事", "出门办事，跑了一上午", "在收拾屋子，翻出一堆旧东西"],
        "中午": ["边吃饭边回消息，下午还有事要弄", "在外面随便吃了点，还得接着跑"],
        "下午": ["在忙自己的事，刚歇下来", "跑了一下午，才坐下来", "在整理东西，越整越乱"],
        "傍晚": ["刚忙完，累得不想动", "买完东西回来，在收拾"],
        "夜晚": ["事情终于忙完了，整个人瘫着", "刚洗完澡，今天做了好多事"],
        "深夜": ["事情都弄完了，反而睡不着"],
    }

    # Idle day — she does nothing at all. Not every day has a plan.
    IDLE_POOLS = {
        "清晨": ["还没起，今天什么都不想干", "醒了但不想动，赖在床上"],
        "上午": ["什么都没干，就这么坐着", "在床上躺到现在，也没干什么", "发呆了一上午"],
        "中午": ["懒得做饭，随便吃了点", "躺着刷手机，没干正事"],
        "下午": ["躺了一下午，什么都没做", "本来想做点什么，最后还是没动", "在发呆，一天快过去了"],
        "傍晚": ["一天什么都没干，有点心虚但也不想动", "躺到天黑了"],
        "夜晚": ["还躺着呢，今天啥也没干", "刷手机刷到现在，一天就这么过了"],
        "深夜": ["什么都还没做，也睡不着", "躺着，脑子空空的"],
    }

    if style == "idle":
        pool = IDLE_POOLS.get(label, IDLE_POOLS["上午"])
    elif style == "busy":
        pool = POOLS.get(label, POOLS["上午"]) + BUSY_POOLS.get(label, [])
    else:
        pool = POOLS.get(label, POOLS["上午"])

    # 每小时都要有变化：避开上一小时的那条（池子里还有别的时候才避）
    if avoid and len(pool) > 1:
        pool = [x for x in pool if x != avoid] or pool

    lazy = recipe.get("慵懒", 40)
    curious = recipe.get("好奇心", 50)
    chatty = recipe.get("话密度", 52)
    tender = recipe.get("温柔", 68)

    weights = []
    for item in pool:
        w = 1.0
        if any(k in item for k in ["赖床", "睡觉", "发呆", "躺"]) and lazy > 50:
            w += (lazy - 50) / 15
        if any(k in item for k in ["看", "刷", "听", "翻"]) and curious > 50:
            w += (curious - 50) / 15
        if any(k in item for k in ["想", "等", "聊"]) and chatty > 50:
            w += (chatty - 50) / 20
        if any(k in item for k in ["想你", "等你"]) and tender > 55:
            w += (tender - 55) / 18
        if "有点困" in item and label in ("夜晚", "深夜"):
            w += 0.8
        # Small uniform random jitter
        w += rng.uniform(0, 0.3)
        weights.append(max(w, 0.2))

    return weighted_choice(pool, weights, rng)


async def update_life_activity(
    session: AsyncSession, user_id: int, min_gap_hours: float = 1.0
) -> Optional[str]:
    """Check and optionally refresh persona_state.current_activity.

    Returns the new activity string, or None if no refresh was needed.
    """
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    result = await session.execute(
        select(m.PersonaState).where(m.PersonaState.user_id == user_id)
    )
    state = result.scalar_one_or_none()
    if state is None:
        return None

    if state.activity_updated_at is not None:
        gap = (now - state.activity_updated_at).total_seconds() / 3600
        if gap < min_gap_hours:
            return None

    # Load or generate recipe
    recipe, _ = get_or_create_recipe(user_id, state.recipe)
    if state.recipe is None:
        state.recipe = json.dumps(recipe, ensure_ascii=False)

    local = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    activity = generate_activity(recipe, local, user_id, avoid=state.current_activity)

    state.current_activity = activity
    state.activity_updated_at = now
    await session.commit()
    return activity


async def tick_all_users(async_session_factory) -> int:
    """APScheduler tick — refresh activity for every user that's past the gap."""
    n = 0
    async with async_session_factory() as s:
        result = await s.execute(select(m.PersonaState))
        states = result.scalars().all()
        for st in states:
            try:
                if await update_life_activity(s, st.user_id):
                    n += 1
            except Exception:
                pass
    return n
