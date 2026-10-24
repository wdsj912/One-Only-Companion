"""Chat routers — send a message, receive a streamed reply, trigger memory write + reflection + consolidation."""
from __future__ import annotations

import datetime as dt
import json
import logging
import random
from dataclasses import replace
from typing import AsyncGenerator, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, Field, field_validator

from app import models as m
from app.database import get_session
from app.services.emotion_kernel import Appraisal, EmotionState, Formulation, RelationStage
from app.services.feedback import (
    extract_feedback,
    apply_feedback_to_state,
    apply_feedback_to_memory_salience,
    transition_relation_stage,
)
from app.services.sycophancy_gate import (
    detect_sycophancy,
    flag_memory_sycophancy_creep,
)
from app.services.llm_service import (
    LLMError,
    chat_completion_stream_with_fallback,
    chat_completion_with_fallback,
)
from app.services import memory_agent
from app.services.memory_service import (
    retrieve_relevant_memories,
    run_consolidation,
    extract_open_threads,
    save_open_threads,
    get_latest_open_thread,
    cleanup_open_threads,
)
from app.services.persona_drift import get_or_create_persona
from app.services.prompt_builder import (
    build_dynamic_prompt,
    build_messages_array,
    build_static_prompt,
    check_sycophancy_failure,
    guard_reply,
)
from app.services.reflection import get_recent_reflections, run_reflection
from app.services.safety import (
    CRISIS_RESPONSE,
    DEPENDENCY_WARNING,
    classify_message,
)
from app.services.settings_service import get_llm_config, get_model_priority_list


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Returned as a normal 200 reply when the model endpoint is unreachable. The
# user's turn is already committed at that point, so nothing is lost.
LLM_UNAVAILABLE_REPLY = "我这边突然断线了…你帮我看下右上角的设置？"

MAX_MESSAGE_LEN = 4000

# 精力模型：清醒 8 小时掉完，休息 2 小时回满；离开超过 30 分钟算在休息。
ENERGY_DRAIN_HOURS = 8.0
ENERGY_RECOVER_HOURS = 2.0
ENERGY_REST_AFTER_MIN = 30

# A reply that has to defend an identity challenge or explain a stance needs
# room for the opinion plus its reason. Short small talk does not.
TOKEN_BUDGET_SHORT = 120
TOKEN_BUDGET_LONG = 300
LONG_MESSAGE_CHARS = 50
NEGATIVE_VALENCE_THRESHOLD = -0.2


def _reply_token_budget(text: str, state: EmotionState) -> int:
    """Dynamic max_tokens: heavy/negative/long turns get 300, small talk 120."""
    if state.formulation.valence < NEGATIVE_VALENCE_THRESHOLD or len(text) > LONG_MESSAGE_CHARS:
        return TOKEN_BUDGET_LONG
    return TOKEN_BUDGET_SHORT


def _sse_frame(text: str) -> str:
    """Encode a payload as one SSE event.

    A raw ``data: {text}`` line breaks the protocol when the text contains a
    newline, so every physical line gets its own ``data:`` field.
    """
    return "".join(f"data: {line}\n" for line in text.split("\n")) + "\n"


async def _expand_media(req: "SendMessageRequest") -> str:
    """把图片/视频首帧交给视觉模型转成文字，伪装成用户消息的一部分。

    失败时降级为一句"我发了张图"，绝不阻断正常聊天。
    """
    if not req.image:
        return req.text
    from app.services.llm_service import describe_image

    kind = "video" if (req.media_kind or "").lower() == "video" else "image"
    label = "视频" if kind == "video" else "图片"
    desc = await describe_image(req.image, kind)
    unit = "段" if kind == "video" else "张"
    if desc:
        return f"[用户发了一{unit}{label}，描述：{desc}] {req.text}"
    return f"[用户发了一{unit}{label}，但在你的视角里是模糊的，你可以自然地请对方用文字描述] {req.text}"


class SendMessageRequest(BaseModel):
    user_id: int = Field(default=1, ge=1)
    conversation_id: Optional[int] = Field(default=None, ge=1)
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_LEN)
    # 多模态：data URL（图片压缩后 / 视频首帧）。后端先用视觉模型转文字再交给主模型。
    image: Optional[str] = Field(default=None, max_length=2_500_000)
    media_kind: Optional[str] = Field(default=None, max_length=16)

    @field_validator("text")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("text must not be blank")
        return cleaned


class ConversationResponse(BaseModel):
    id: int
    started_at: dt.datetime
    ended_at: Optional[dt.datetime]


class MessageResponse(BaseModel):
    id: int
    conversation_id: int
    role: str
    content: str
    created_at: dt.datetime
    proactive: Optional[bool] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _ensure_conversation(
    session: AsyncSession, user_id: int, conversation_id: Optional[int]
) -> m.Conversation:
    if conversation_id is not None:
        conv = (await session.execute(
            select(m.Conversation).where(m.Conversation.id == conversation_id, m.Conversation.user_id == user_id)
        )).scalar_one_or_none()
        if conv is not None:
            return conv

    conv = m.Conversation(user_id=user_id)
    session.add(conv)
    await session.flush()
    return conv


# ---------------------------------------------------------------------------
# Filler short-circuit — 1-4 char 语气词 don't always need the LLM.
# 25% of the time we reply with a canned micro-response directly, saving
# tokens and feeling like a real person (she doesn't always over-process
# a single "嗯"). The rest goes through the normal pipeline.
# ---------------------------------------------------------------------------
_FILLER_INPUTS = {
    "嗯", "哦", "噢", "哈", "好", "好的", "ok", "OK", "Ok", "哈哈哈", "哈哈", "hehe",
    "嘿", "嗨", "呀", "嘛", "呗", "呢", "嗯嗯", "哦哦", "好好好", "okok", "OKOK",
}
_FILLER_REPLIES = [
    "嗯", "哦", "哈哈", "好呀", "嘿", "嗯嗯", "好~", "喔", "哈哈哈", "ok", "ok呀",
    "哦？", "嗯？", "哈？", "好嘞", "哈哈好", "行", "行嘞",
]


def _try_filler_short_circuit(text: str) -> Optional[str]:
    """Return a canned reply if `text` is a trivial filler, else None.

    Only fires ~25% of the time so she doesn't feel pre-scripted.
    """
    stripped = text.strip().lower()
    if len(stripped) <= 4 and stripped in _FILLER_INPUTS and random.random() < 0.25:
        return random.choice(_FILLER_REPLIES)
    return None


async def _active_user(
    session: AsyncSession, user_id: int
) -> m.User:
    result = await session.execute(
        select(m.User).options(selectinload(m.User.persona_state)).where(m.User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        # Assign the relationship directly — reading it after flush would emit
        # implicit lazy IO, which is illegal in SQLAlchemy's async session.
        user = m.User(id=user_id)
        user.persona_state = m.PersonaState()
        session.add(user)
        await session.flush()
        return user
    if user.persona_state is None:
        user.persona_state = m.PersonaState(user_id=user.id)
        session.add(user.persona_state)
    return user


async def _load_emotion_state(session: AsyncSession, user: m.User) -> EmotionState:
    """Rehydrate the dual-layer state from the persisted JSON columns.

    Emotional momentum only exists if the previous state is actually restored —
    a fresh default state each turn would make every message a cold start.
    """
    import json as _json

    # 精力按时间恢复：新的一天过了 6 点 / 离开 8 小时以上 → 满血；
    # 离开 30 分钟以上 → 部分恢复。只向上调，聊天消耗仍由 emotion kernel 按轮次扣。
    from app.services.energy import recalc_energy
    user.energy, _ = recalc_energy(user.energy, user.last_interaction_at)

    state = EmotionState(
        intimacy=user.intimacy if user.intimacy is not None else 30,
        energy=user.energy if user.energy is not None else 70,
        last_interaction_at=user.last_interaction_at,
        session_turns=user.session_turns or 0,
    )

    if user.appraisal_json:
        try:
            d = _json.loads(user.appraisal_json)
            state = replace(
                state,
                appraisal=Appraisal(
                    relevance=d["relevance"],
                    implication=d["implication"],
                    coping_potential=d["coping_potential"],
                    norm_compatibility=d["norm_compatibility"],
                    self_involvement=d["self_involvement"],
                    valence_raw=0.0,
                    arousal_raw=0.0,
                    dominance_raw=0.0,
                    valence_momentum=d["valence_momentum"],
                    arousal_momentum=d["arousal_momentum"],
                    dominance_momentum=d["dominance_momentum"],
                ),
            )
        except (ValueError, KeyError, TypeError):
            pass

    if user.formulation_json:
        try:
            d = _json.loads(user.formulation_json)
            state = replace(
                state,
                formulation=Formulation(
                    valence=d["valence"],
                    arousal=d["arousal"],
                    dominance=d["dominance"],
                    suppression_gate=d["suppression_gate"],
                ),
            )
        except (ValueError, KeyError, TypeError):
            pass

    # 时间平复效应：沉默期间情绪向 0 回落（小时级，先于每日衰减）
    from app.services.emotion_kernel import decay_emotion_by_time
    state = decay_emotion_by_time(state)
    return state.with_daily_decay()


async def _persist_emotion(session: AsyncSession, user: m.User, state: EmotionState) -> None:
    import json as _json
    user.appraisal_json = _json.dumps({
        "relevance": state.appraisal.relevance,
        "implication": state.appraisal.implication,
        "coping_potential": state.appraisal.coping_potential,
        "norm_compatibility": state.appraisal.norm_compatibility,
        "self_involvement": state.appraisal.self_involvement,
        "valence_momentum": state.appraisal.valence_momentum,
        "arousal_momentum": state.appraisal.arousal_momentum,
        "dominance_momentum": state.appraisal.dominance_momentum,
    })
    user.formulation_json = _json.dumps({
        "valence": state.formulation.valence,
        "arousal": state.formulation.arousal,
        "dominance": state.formulation.dominance,
        "suppression_gate": state.formulation.suppression_gate,
    })
    user.mood = state.formulation.mood_label()
    user.energy = state.energy
    user.intimacy = state.intimacy
    user.last_interaction_at = state.last_interaction_at
    user.session_turns = state.session_turns


async def _update_self_energy(session: AsyncSession, persona: m.PersonaState) -> None:
    """她自己的精力 —— 纯时间驱动，不再按消息条数扣。

    设定（用户指定）：
      · 清醒状态下 8 小时从 100 掉到 0（约 -12.5/小时）
      · 休息超过 30 分钟进入恢复状态，2 小时回满（约 +50/小时）

    所以连着聊一整天她会真的累，睡一觉/离开两小时又满血。
    说话长短、情绪激烈程度不再额外扣能量 —— 那会让精力掉得太快。
    """
    try:
        import datetime as _dt

        # 与全库其他时间列一致：naive UTC（models.utcnow）。混用本地时间会让
        # energy_updated_at 比其他字段快 8 小时，排查时全是假象。
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        current = max(0.0, min(100.0, getattr(persona, "self_energy", 70.0) or 0.0))
        last = getattr(persona, "energy_updated_at", None)

        if last is not None:
            try:
                # gap 为负（旧版本写过本地时间）时按 0 处理：宁可这一轮不恢复，
                # 也不要凭一个错位的时间戳瞬间回满。
                gap_hours = max(0.0, (now - last.replace(tzinfo=None)).total_seconds() / 3600.0)
            except Exception:
                gap_hours = 0.0

            if gap_hours >= ENERGY_REST_AFTER_MIN / 60.0:
                current += gap_hours * (100.0 / ENERGY_RECOVER_HOURS)
            else:
                current -= gap_hours * (100.0 / ENERGY_DRAIN_HOURS)

        # 每天 06:00（本地时间，UTC+8）一觉醒来满血复活。
        # 存储是 naive UTC，换算边界时 +8h。跨过早6点的第一次交互直接回满。
        local_now = now + _dt.timedelta(hours=8)
        six_am_local = local_now.replace(hour=6, minute=0, second=0, microsecond=0)
        if local_now >= six_am_local:
            last_local = (last.replace(tzinfo=None) + _dt.timedelta(hours=8)) if last is not None else None
            if last_local is None or last_local < six_am_local:
                current = 100.0

        current = max(0.0, min(100.0, current))
        persona.self_energy = round(current, 1)
        persona.energy_updated_at = now
        session.add(persona)
        await session.flush()
    except Exception as exc:
        logger.warning("self_energy update failed: %s", exc)


async def _memory_consolidation_and_reflection(
    session: AsyncSession, user: m.User, conv: m.Conversation
) -> None:
    """Post-turn bookkeeping (consolidation + reflection). Also commits.

    The conversation stays open across turns — ending it here would force a new
    conversation_id on the next request and silently drop the working context.
    """
    # Consolidation — best-effort, doesn't fail the response.
    try:
        await run_consolidation(session, user.id)
    except Exception as exc:
        logger.warning("post-turn consolidation failed: %s", exc)
    # Reflection — requires a real LLM. With a long-lived conversation it runs
    # every 10 turns (the original "once per session" trigger no longer exists).
    if user.total_turns % 10 == 0:
        try:
            llm_available = bool((await get_llm_config()).get("api_key"))
            await run_reflection(
                session,
                user.id,
                user.companion_name,
                conv.id,
                llm_available=llm_available,
            )
        except Exception as exc:
            logger.warning("post-turn reflection failed: %s", exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/send")
async def send_message(
    req: SendMessageRequest, session: AsyncSession = Depends(get_session)
):
    """Non-streaming send — blocks until the full reply is ready."""
    user = await _active_user(session, req.user_id)
    conv = await _ensure_conversation(session, req.user_id, req.conversation_id)

    # 0. 多模态：图片/视频先由视觉模型转成描述，再当作文字消息继续。
    if req.image:
        req.text = await _expand_media(req)

    # 1. Safety gates. Crisis remains a hard short-circuit — real-world safety
    # beats roleplay. Identity questions deliberately do NOT: she answers those
    # herself, in character (asks_identity flips a prompt hint + output guard).
    safety = classify_message(req.text)
    if safety["is_crisis"]:
        content = CRISIS_RESPONSE
        user_msg = m.Message(conversation_id=conv.id, role="user", content=req.text)
        asst_msg = m.Message(conversation_id=conv.id, role="assistant", content=content)
        session.add_all([user_msg, asst_msg])
        await session.commit()
        return {"reply": content, "message_id": asst_msg.id, "conversation_id": conv.id, "crisis": True}

    # 1b. Filler short-circuit — 25% chance we reply directly without LLM
    # when the input is a trivial 语气词. Saves tokens + feels human.
    filler = _try_filler_short_circuit(req.text)
    if filler is not None:
        user_msg = m.Message(conversation_id=conv.id, role="user", content=req.text)
        asst_msg = m.Message(conversation_id=conv.id, role="assistant", content=filler)
        session.add_all([user_msg, asst_msg])
        await session.commit()
        return {"reply": filler, "message_id": asst_msg.id, "conversation_id": conv.id, "filler": True}

    # 2. Emotional kernel transition.  Save pre-transition state for CTEM feedback.
    state = await _load_emotion_state(session, user)
    prev_valence = state.appraisal.valence_momentum
    prev_len = user.last_message_len or len(req.text)
    stage = RelationStage(user.relation_stage)
    state = state.transition(req.text, stage=stage)
    await _persist_emotion(session, user, state)

    # 3. Save user message. Long-term memory extraction is NOT done here —
    # the background Memory Agent (Librarian) re-reads the log after the turn.
    user_msg = m.Message(conversation_id=conv.id, role="user", content=req.text)
    session.add(user_msg)
    await session.flush()  # get id

    # Open threads bypass the Librarian (deterministic, zero-LLM) and are saved
    # with the user turn — a later LLM failure must never lose the user's plan.
    await save_open_threads(session, user.id, extract_open_threads(req.text), source_message_id=user_msg.id)

    # 4. Build prompt — includes sycophancy gate (deterministic, zero-LLM).
    persona = await get_or_create_persona(session, user.id)
    reflections = await get_recent_reflections(session, user.id, limit=3)
    memories = await retrieve_relevant_memories(session, user.id, limit=10)
    # Open threads: stale ones die here, the freshest one enters the prompt.
    await cleanup_open_threads(session, user.id)
    open_thread = await get_latest_open_thread(session, user.id)

    # Pull only the last 20 messages we actually need — the full-session pull
    # was O(N) memory on every turn. SQL limit + desc+reversed wins.
    msgs_in_conv = list((await session.execute(
        select(m.Message).where(m.Message.conversation_id == conv.id).order_by(m.Message.created_at.desc()).limit(20)
    )).scalars().all())
    msgs_in_conv.reverse()

    static_prompt = build_static_prompt()
    dynamic_prompt = build_dynamic_prompt(
        companion_name=user.companion_name,
        emotion_state=state,
        persona=persona,
        reflections=reflections,
        memories=memories,
        relation_stage=stage,
        current_user_text=req.text,
        open_thread=open_thread,
        recent_messages=msgs_in_conv,
        asks_identity=safety["asks_identity"],
    )
    messages = build_messages_array(
        static_prompt=static_prompt,
        dynamic_prompt=dynamic_prompt,
        working_memory=msgs_in_conv,
    )

    # Sycophancy gate: count how many retrieved memories would have triggered
    # agreement-justification.  At prompt-build time we already injected one
    # challenge — we also increment challenge_counter here so the rolling
    # rate stays honest.
    sycophancy_hits = sum(1 for mem in memories if detect_sycophancy(mem.content, req.text).agreement_detected)
    if sycophancy_hits:
        user.challenge_counter += 1

    # 5. Persist the user turn (message + extracted facts + memories) BEFORE the
    # LLM call. A network failure must never roll back what the user said.
    await session.commit()

    # 5a. LLM call with priority failover: configured model first, then the
    # saved list in order (deduped). Total failure still returns HTTP 200.
    cfg = await get_llm_config()
    priority = await get_model_priority_list(session)
    model_list = [x for x in dict.fromkeys([cfg.get("model") or "", *priority]) if x]
    try:
        full_reply = await chat_completion_with_fallback(
            messages, model_list, max_tokens=_reply_token_budget(req.text, state)
        )
        full_reply = full_reply or "（小禾暂时没说话）"
        if check_sycophancy_failure(full_reply):
            logger.info("ego guard: replaced a submissive reply (conv=%s)", conv.id)
            full_reply = guard_reply(full_reply)
    except (LLMError, httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
        logger.warning("LLM call failed, turn preserved: %s", exc)
        # The user turn is committed — the Librarian can still mine it.
        memory_agent.spawn(conv.id)
        return {
            "reply": LLM_UNAVAILABLE_REPLY,
            "message_id": None,
            "conversation_id": conv.id,
            "llm_unavailable": True,
        }

    # 6. Post-assistant safety (dependency watch fires AFTER reply).
    if safety["is_dependency"]:
        full_reply += "\n\n" + DEPENDENCY_WARNING

    asst_msg = m.Message(conversation_id=conv.id, role="assistant", content=full_reply)
    session.add(asst_msg)

    # 6b. Her own energy — drains on long replies, venting, fragile activity.
    await _update_self_energy(session, persona)

    # 7. CTEM closed loop — post-turn, never on hot path (no LLM calls here).
    signal = extract_feedback(
        prev_valence=prev_valence,
        current_valence=state.appraisal.valence_momentum,
        prev_len=prev_len,
        current_len=len(req.text),
        user_asked_followup=False,          # MVP: follow-up detection requires conversation context
        topic_switch=False,                  # MVP: topic detection requires LLM or embeddings
    )
    # Update neutral streak
    if signal.is_neutral:
        user.neutral_streak += 1
    else:
        user.neutral_streak = 0
    # Revise Layer B from feedback
    state = apply_feedback_to_state(state, signal, user.neutral_streak)
    # Revise memory salience from feedback
    await apply_feedback_to_memory_salience(
        session, user.id,
        reward=signal.follow_up or signal.sentiment_shift > 0.2,
        penalty=signal.topic_abandoned or signal.length_delta < -0.25,
        neutral_streak=user.neutral_streak,
    )
    # Persist CTEM bookkeeping
    await _persist_emotion(session, user, state)
    user.last_valence = state.appraisal.valence_momentum
    user.last_message_len = len(req.text)
    user.total_turns += 1

    # RelationStage 八档进阶 — 只看累计对话轮数，单轮情绪不跳档
    user.relation_stage = transition_relation_stage(stage, user.total_turns).value

    # Sycophancy creep flag (best-effort, logged, never fails the response)
    creep_warning = flag_memory_sycophancy_creep(challenge_counter=user.challenge_counter, total_turns=user.total_turns)

    # 8. Post-turn consolidation + reflection (conversation stays open).
    await _memory_consolidation_and_reflection(session, user, conv)
    await session.commit()

    # 9. Hand the turn to the Librarian — fully non-blocking, own DB session.
    memory_agent.spawn(conv.id)

    return {
        "reply": full_reply,
        "message_id": asst_msg.id,
        "conversation_id": conv.id,
        "relation_stage": user.relation_stage,
        "creep_warning": creep_warning,
    }


@router.post("/stream")
async def send_message_stream(
    req: SendMessageRequest, session: AsyncSession = Depends(get_session)
):
    """SSE streaming endpoint — mirrors the send handler's 8-step bookkeeping exactly,
    except the LLM reply is streamed to the client as it arrives."""
    user = await _active_user(session, req.user_id)
    conv = await _ensure_conversation(session, req.user_id, req.conversation_id)

    # 0. 多模态：图片/视频 → 视觉描述（复用同一 BaseURL/Key，仅临时切 model）
    if req.image:
        req.text = await _expand_media(req)

    # 1. Safety gates — short-circuit before LLM.
    safety = classify_message(req.text)

    async def event_generator() -> AsyncGenerator[str, None]:
        # 0. Tell the client which conversation this turn belongs to, so it can
        # send the same id back on the next request and keep the context.
        yield f"event: meta\ndata: {json.dumps({'conversation_id': conv.id})}\n\n"

        # 1. Crisis short-circuit — real-world safety beats roleplay.
        if safety["is_crisis"]:
            content = CRISIS_RESPONSE
            user_msg = m.Message(conversation_id=conv.id, role="user", content=req.text)
            asst_msg = m.Message(conversation_id=conv.id, role="assistant", content=content)
            session.add_all([user_msg, asst_msg])
            await session.commit()
            yield _sse_frame(content)
            yield f"data: [DONE]\n\n"
            return

        # 1b. Filler short-circuit — same logic as the send handler, but yields
        # a single frame (no streaming needed — it's a canned micro-response).
        filler = _try_filler_short_circuit(req.text)
        if filler is not None:
            user_msg = m.Message(conversation_id=conv.id, role="user", content=req.text)
            asst_msg = m.Message(conversation_id=conv.id, role="assistant", content=filler)
            session.add_all([user_msg, asst_msg])
            await session.commit()
            yield _sse_frame(filler)
            yield f"data: [DONE]\n\n"
            return

        # 2. Emotional kernel — save pre-transition state for CTEM feedback.
        state = await _load_emotion_state(session, user)
        prev_valence = state.appraisal.valence_momentum
        prev_len = user.last_message_len or len(req.text)
        stage = RelationStage(user.relation_stage)
        state = state.transition(req.text, stage=stage)
        await _persist_emotion(session, user, state)

        # 3. Save user message — memory extraction belongs to the Librarian.
        user_msg = m.Message(conversation_id=conv.id, role="user", content=req.text)
        session.add(user_msg)
        await session.flush()

        # Open threads bypass the Librarian (deterministic, zero-LLM) — saved
        # with the user turn so an LLM failure can never lose the user's plan.
        await save_open_threads(session, user.id, extract_open_threads(req.text), source_message_id=user_msg.id)

        # 4. Build prompt — sycophancy gate + relation_stage hint included.
        persona = await get_or_create_persona(session, user.id)
        reflections = await get_recent_reflections(session, user.id, limit=3)
        memories = await retrieve_relevant_memories(session, user.id, limit=10)
        # Open threads: stale ones die here, the freshest one enters the prompt.
        await cleanup_open_threads(session, user.id)
        open_thread = await get_latest_open_thread(session, user.id)

        msgs_in_conv = list((await session.execute(
            select(m.Message).where(m.Message.conversation_id == conv.id)
            .order_by(m.Message.created_at.desc()).limit(20)
        )).scalars().all())
        msgs_in_conv.reverse()

        static_prompt = build_static_prompt()
        dynamic_prompt = build_dynamic_prompt(
            companion_name=user.companion_name,
            emotion_state=state,
            persona=persona,
            reflections=reflections,
            memories=memories,
            relation_stage=stage,
            current_user_text=req.text,
            open_thread=open_thread,
            recent_messages=msgs_in_conv,
            asks_identity=safety["asks_identity"],
        )
        messages = build_messages_array(
            static_prompt=static_prompt,
            dynamic_prompt=dynamic_prompt,
            working_memory=msgs_in_conv,
        )

        # ---- Memory Agent spawn happens AFTER the LLM call (non-blocking) ----

        # Sycophancy creep counter — same as send handler.
        sycophancy_hits = sum(1 for mem in memories if detect_sycophancy(mem.content, req.text).agreement_detected)
        if sycophancy_hits:
            user.challenge_counter += 1

        # 5. Persist the user turn BEFORE contacting the model — same guarantee
        # as the send handler: a connection failure never rolls the turn back.
        await session.commit()

        # 5a. Stream LLM with priority failover (configured model first).
        cfg = await get_llm_config()
        priority = await get_model_priority_list(session)
        model_list = [x for x in dict.fromkeys([cfg.get("model") or "", *priority]) if x]
        try:
            # Buffered, not forwarded on arrival: the ego guard only works on a
            # complete reply — "我是算法" cannot be detected while half of it is
            # still in flight, and forwarding early would leak the very text
            # the guard exists to replace.
            full_reply_parts: list[str] = []
            async for delta in chat_completion_stream_with_fallback(
                messages, model_list, max_tokens=_reply_token_budget(req.text, state)
            ):
                full_reply_parts.append(delta)
            full_reply = "".join(full_reply_parts) or "（小禾暂时没说话）"
            if check_sycophancy_failure(full_reply):
                logger.info("ego guard: replaced a submissive streamed reply (conv=%s)", conv.id)
                full_reply = guard_reply(full_reply)
        except (LLMError, httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
            logger.warning("LLM stream failed, turn preserved: %s", exc)
            # The user turn is committed — the Librarian can still mine it.
            memory_agent.spawn(conv.id)
            yield _sse_frame(LLM_UNAVAILABLE_REPLY)
            yield "data: [DONE]\n\n"
            return

        # 6. Post-assistant safety (dependency watch fires AFTER reply).
        if safety["is_dependency"]:
            full_reply += "\n\n" + DEPENDENCY_WARNING

        # Emit the vetted reply as a single frame — the client renders it in
        # the streaming bubble exactly as it would render incremental deltas.
        yield _sse_frame(full_reply)

        asst_msg = m.Message(conversation_id=conv.id, role="assistant", content=full_reply)
        session.add(asst_msg)

        # 6b. Her own energy — 纯时间驱动（8h 掉完 / 2h 回满）。
        await _update_self_energy(session, persona)

        # 7. CTEM closed loop — post-turn, never on hot path.
        signal = extract_feedback(
            prev_valence=prev_valence,
            current_valence=state.appraisal.valence_momentum,
            prev_len=prev_len,
            current_len=len(req.text),
            user_asked_followup=False,
            topic_switch=False,
        )
        if signal.is_neutral:
            user.neutral_streak += 1
        else:
            user.neutral_streak = 0
        state = apply_feedback_to_state(state, signal, user.neutral_streak)
        await apply_feedback_to_memory_salience(
            session, user.id,
            reward=signal.follow_up or signal.sentiment_shift > 0.2,
            penalty=signal.topic_abandoned or signal.length_delta < -0.25,
            neutral_streak=user.neutral_streak,
        )
        await _persist_emotion(session, user, state)
        user.last_valence = state.appraisal.valence_momentum
        user.last_message_len = len(req.text)
        user.total_turns += 1

        # RelationStage 八档进阶
        user.relation_stage = transition_relation_stage(stage, user.total_turns).value

        # Sycophancy creep flag (best-effort)
        _ = flag_memory_sycophancy_creep(challenge_counter=user.challenge_counter, total_turns=user.total_turns)

        # 8. End + consolidation + reflection.
        await _memory_consolidation_and_reflection(session, user, conv)
        await session.commit()

        # 9. Hand the turn to the Librarian — fully non-blocking, own DB session.
        memory_agent.spawn(conv.id)

        yield f"data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/conversations")
async def list_conversations(user_id: int = 1, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(m.Conversation)
        .where(m.Conversation.user_id == user_id)
        .order_by(m.Conversation.started_at.desc())
        .limit(20)
    )
    convs = list(result.scalars().all())
    return [ConversationResponse(**c.__dict__) for c in convs]


@router.get("/conversations/{conv_id}/messages")
async def get_messages(conv_id: int, user_id: int = 1, session: AsyncSession = Depends(get_session)):
    conv = await session.get(m.Conversation, conv_id)
    if conv is None or conv.user_id != user_id:
        raise HTTPException(404, "conversation not found")
    result = await session.execute(
        select(m.Message).where(m.Message.conversation_id == conv_id).order_by(m.Message.created_at)
    )
    msgs = list(result.scalars().all())
    return [MessageResponse(**m.__dict__) for m in msgs]


class PollRequest(BaseModel):
    since_id: int = 0
    conversation_id: Optional[int] = None
    user_id: int = 1


@router.get("/poll")
async def poll_messages(
    since_id: int = 0,
    conversation_id: Optional[int] = None,
    user_id: int = 1,
    session: AsyncSession = Depends(get_session),
):
    """Lightweight long-poll-style pull for new messages.

    Returns any messages newer than since_id, filtered by conversation_id
    (if given) and user_id. Used by the frontend's 60s interval to sneak
    proactive bubbles into view without forcing a full reload.
    """
    stmt = select(m.Message).where(m.Message.id > since_id)
    if conversation_id is not None:
        stmt = stmt.where(m.Message.conversation_id == conversation_id)
    # Guard: only show messages from the user's conversations.
    stmt = (
        stmt.join(m.Conversation, m.Conversation.id == m.Message.conversation_id)
        .where(m.Conversation.user_id == user_id)
        .order_by(m.Message.created_at.asc(), m.Message.id.asc())
        .limit(20)
    )
    result = await session.execute(stmt)
    msgs = list(result.scalars().all())
    return {
        "messages": [MessageResponse(**m.__dict__) for m in msgs],
        "now": m.utcnow().isoformat(),
    }
