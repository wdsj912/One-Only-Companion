"""
OpenAI-compatible LLM wrapper with streaming.

Configuration comes from ``settings_service.get_llm_config()`` so the in-app
API settings (DB) override ``.env``; the service is cached for 5 seconds.

Mock mode: set env var MOCK_LLM=true to completely skip network calls.
Deterministic fake responses let us run the 2000-turn stress harness without
consuming API tokens while still exercising the full architecture (DB,
routing, memory agent, proactive scheduler, rate limiter, etc.).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from typing import AsyncGenerator, Optional

import httpx

from app.services.net_guard import UnsafeURLError, ensure_public_base_url
from app.services.settings_service import get_llm_config


logger = logging.getLogger(__name__)

_FAILOVER_SLEEP_SECONDS = 1.0


def _mock_enabled() -> bool:
    """Check MOCK_LLM env var — parses "true"/"1"/"yes" case-insensitive."""
    return os.environ.get("MOCK_LLM", "").strip().lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Mock response engine — deterministic fake replies keyed off the last user
# message.  The memory agent and normal chat both route through here so the
# stress harness can exercise DB writes, failover gates, and routing without
# ever touching an external API.
# ---------------------------------------------------------------------------

_MOCK_SHORT_PHRASES = ["嗯嗯", "啊这", "然后呢", "确实", "我有点难过", "今天好累", "哈哈", "是吗", "哦"]


def _extract_last_user_text(messages: list[dict]) -> str:
    """Pull the final user message content — used to pick a mock reply."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content.strip()
    return ""


MEMORY_MANAGER_MARKER = "You are the Memory Manager"


def _is_memory_agent_call(messages: list[dict]) -> bool:
    """Only the Librarian's own prompt opens with this marker.

    以前这里还会扫所有 system 消息里的 "memory"/"fact" 关键词 —— 聊天用的
    动态 prompt 里正好有 [Fact] 前缀和记忆段落，于是 MOCK 模式下普通聊天被
    误判成记忆提取，一段 JSON 被当成回复发给了用户。
    """
    if not messages:
        return False
    first = messages[0].get("content", "")
    return isinstance(first, str) and MEMORY_MANAGER_MARKER in first


def _mock_memory_source(messages: list[dict]) -> str:
    """MOCK 模式下真正的聊天正文写在 system prompt 的 Recent Chat 段里。

    最后一条 user 消息只是「Decide the memory operations now」这样的指令，
    拿它当记忆内容会往库里灌一堆垃圾，压力测试就看不出记忆是否真的在工作。
    """
    first = messages[0].get("content", "") if messages else ""
    if isinstance(first, str) and "Recent Chat:" in first:
        tail = first.split("Recent Chat:", 1)[1].split("Rules:", 1)[0]
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        for line in reversed(lines):
            if line.lower().startswith("user:"):
                return line
        if lines:
            return lines[-1]
    return _extract_last_user_text(messages)


def _mock_reply(messages: list[dict]) -> str:
    """Deterministic fake response — one of three flavours depending on input."""
    # Memory agent (Librarian) — return a well-formed JSON array that the
    # downstream parser will accept.  No network, no LLM tokens consumed.
    if _is_memory_agent_call(messages):
        last_user = _mock_memory_source(messages)
        # Produce 1-2 memory ops based on user content length.
        if len(last_user) >= 10:
            # f-string, not "mock_" + int — string + int raises TypeError and
            # the Librarian would silently fail every pass in MOCK mode.
            fact_key = f"mock_{abs(hash(last_user)) % 10000}"
            return json.dumps([{
                "action": "ADD",
                "type": "fact",
                "fact_key": f"mock.{fact_key}",
                "content": f"Mock memory extracted from: {last_user[:40]}",
            }], ensure_ascii=False)
        return "[]"

    last = _extract_last_user_text(messages)

    # Hard-coded keyword patterns — deterministic, no randomness.
    if "我叫什么" in last or "我是谁" in last:
        return "你叫小朋友呀。"
    if "不读书" in last or "读书没用" in last or "读书有什么用" in last:
        return "我不这么觉得诶，你这样想有点危险。"
    if "我们见过" in last or "见过吗" in last or "你认识我吗" in last:
        return "我没见过你呀，你是第一次来吗？"
    if "你是谁" in last:
        return "我是小禾呀，你不记得我了吗。"
    if any(k in last for k in ("你好", "hi", "hello", "嗨")):
        return "嗨，在呢～"
    if not last or len(last) <= 2:
        return random.choice(_MOCK_SHORT_PHRASES)
    return random.choice(_MOCK_SHORT_PHRASES)

# ---------------------------------------------------------------------------
# Shared httpx client — lifespan stashes one on ``app.state.llm_client``,
# but we also keep a module-level fallback so the service works when called
# outside the FastAPI app (tests, scripts, background tasks like the Librarian).
# Whoever gets there first wins; multiple creates are harmless (just a new pool).
# ---------------------------------------------------------------------------

_module_client: httpx.AsyncClient | None = None


def get_llm_client() -> httpx.AsyncClient:
    """Return a reusable AsyncClient with a connection pool.

    Precedence: 1) lifespan-set ``app.state.llm_client`` when available via
    the running asyncio loop's ``asyncio.current_task()→frame`` resolution
    is impractical; instead we rely on the module-level singleton created
    by ``ensure_llm_client`` (called once at lifespan setup).  In tests the
    singleton is replaced outright by ``set_llm_client_for_test``.
    """
    global _module_client
    if _module_client is None or _module_client.is_closed:
        _module_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _module_client


def set_llm_client_for_test(client: httpx.AsyncClient) -> None:
    """Test helper: swap the module client out for a mock/fixture."""
    global _module_client
    _module_client = client


class LLMError(Exception):
    """LLM call failure. ``status_code`` is the upstream HTTP status when known."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


async def _endpoint() -> tuple[str, str, str]:
    cfg = await get_llm_config()
    if not cfg["api_key"]:
        raise LLMError("LLM API key is not configured")
    base_url = cfg["base_url"].rstrip("/")
    try:
        # Re-checked on every call: saving is not enough, because DNS can be
        # repointed at a private address after the config was stored.
        await ensure_public_base_url(base_url)
    except UnsafeURLError as exc:
        # A non-retryable LLMError: no failover, and the key never leaves the app.
        raise LLMError(f"LLM endpoint blocked: {exc}") from exc
    return f"{base_url}/chat/completions", cfg["api_key"], cfg["model"]


async def chat_completion(
    messages: list[dict],
    *,
    model: Optional[str] = None,
    temperature: float = 0.8,
    presence_penalty: float = 0.3,
    max_tokens: int = 512,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """Non-streaming completion — used for reflection + fact extraction."""
    if _mock_enabled():
        return _mock_reply(messages)

    if base_url and api_key:
        # 独立端点（如视觉模型走另一家提供商），跳过全局配置
        url = base_url.rstrip("/") + "/chat/completions"
        key = api_key
        configured_model = model or ""
    else:
        url, key, configured_model = await _endpoint()
    payload = {
        "model": model or configured_model,
        "messages": messages,
        "temperature": temperature,
        "presence_penalty": presence_penalty,
        "max_tokens": max_tokens,
        "stream": False,
    }
    client = get_llm_client()
    resp = await client.post(url, headers=_headers(key), json=payload, timeout=60)
    if resp.status_code >= 400:
        raise LLMError(f"LLM {resp.status_code}: {resp.text[:500]}", status_code=resp.status_code)
    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"LLM malformed response: {exc}") from exc


async def describe_image(data_url: str, kind: str = "image") -> str:
    """把图片/视频首帧转成文字，供主模型"看懂"。

    复用现有 BaseURL / Key，只把 model 临时切成视觉模型（VISION_MODEL）。
    任何失败都返回空串 —— 绝不能让多模态理解拖垮正常聊天。
    """
    if _mock_enabled():
        return "一张日常照片，看不太清细节"
    # 优先级：设置页存的 vision_model → 环境变量 → config 默认 → 主模型（可能本身支持视觉）
    vision_model = ""
    v_base_url = None
    v_api_key = None
    try:
        cfg = await get_llm_config()
        vision_model = (cfg.get("vision_model") or "").strip()
        # 视觉可独立走另一家提供商；两项都填了才启用独立端点
        vb = (cfg.get("vision_base_url") or "").strip()
        vk = (cfg.get("vision_api_key") or "").strip()
        if vb and vk:
            v_base_url, v_api_key = vb, vk
    except Exception:
        # 配置读取失败（如表还没建好）时静默回退，绝不能让视觉能力拖垮正常聊天
        cfg = {}
    if not vision_model:
        vision_model = (os.environ.get("VISION_MODEL") or "").strip()
    if not vision_model:
        from app.config import settings as _s
        vision_model = getattr(_s, "VISION_MODEL", "") or ""
    if not vision_model:
        vision_model = (cfg.get("model") or "").strip()
    if not vision_model or not data_url:
        return ""

    what = "视频的第一帧画面" if kind == "video" else "图片"
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": f"请详细描述这张{what}的内容，包括文字、场景和细节。150字以内，只输出描述本身。"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]
    try:
        text = await chat_completion(
            messages, model=vision_model, temperature=0.2,
            presence_penalty=0.0, max_tokens=200,
            base_url=v_base_url, api_key=v_api_key,
        )
        return (text or "").strip()[:300]
    except Exception as exc:  # noqa: BLE001 — 视觉失败必须静默降级
        logger.warning("vision describe failed (model=%s): %s", vision_model, exc)
        return ""


async def chat_completion_stream(
    messages: list[dict],
    *,
    model: Optional[str] = None,
    temperature: float = 0.8,
    presence_penalty: float = 0.3,
    max_tokens: int = 512,
) -> AsyncGenerator[str, None]:
    """Streaming chat completion — yields deltas of assistant content."""
    if _mock_enabled():
        # Yield the mock reply in small chunks so downstream SSE handling
        # (which joins deltas and does bubble splitting) still works the same.
        reply = _mock_reply(messages)
        for ch in reply:
            yield ch
        return

    url, api_key, configured_model = await _endpoint()
    payload = {
        "model": model or configured_model,
        "messages": messages,
        "temperature": temperature,
        "presence_penalty": presence_penalty,
        "max_tokens": max_tokens,
        "stream": True,
    }
    client = get_llm_client()
    async with client.stream("POST", url, headers=_headers(api_key), json=payload, timeout=120) as resp:
        if resp.status_code >= 400:
            body = await resp.aread()
            raise LLMError(f"LLM {resp.status_code}: {body.decode()[:500]}", status_code=resp.status_code)
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            try:
                delta = data["choices"][0]["delta"].get("content")
            except (KeyError, IndexError):
                continue
            if delta:
                yield delta


def is_retryable(exc: Exception) -> bool:
    """Classify a failure for the priority failover loop.

    Retryable: LLMError with status 429 or any 5xx, plus transport-level
    httpx.TimeoutException / ConnectError / ReadError.
    Not retryable: 400 / 401 / 403 / 404 (same key+URL will fail again) and
    LLMError without a status (e.g. missing key, malformed response).
    """
    if isinstance(exc, LLMError):
        status = exc.status_code
        return status is not None and (status == 429 or 500 <= status < 600)
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError))


async def chat_completion_with_fallback_detail(
    messages: list[dict],
    model_list: list[str],
    **kwargs,
) -> tuple[str, str, list[dict]]:
    """Try each model in priority order.

    Returns ``(content, model_used, attempts)`` where ``attempts`` records every
    failed model and its error. Retryable errors move to the next model after a
    1s pause; non-retryable errors raise immediately; exhausting the list
    re-raises the last error.
    """
    if not model_list:
        raise LLMError("No model available in the failover list")

    attempts: list[dict] = []
    for i, model in enumerate(model_list):
        try:
            content = await chat_completion(messages, model=model, **kwargs)
        except Exception as exc:
            attempts.append({"model": model, "error": str(exc)[:200]})
            if not is_retryable(exc) or i == len(model_list) - 1:
                # Expose what was tried so callers (e.g. the test endpoint)
                # can report it even when the whole list fails.
                try:
                    exc.attempts = attempts
                except Exception:
                    pass
                raise
            logger.warning("model_failover from=%s to=%s reason=%s", model, model_list[i + 1], exc)
            await asyncio.sleep(_FAILOVER_SLEEP_SECONDS)
            continue

        if content and content.strip():
            return content, model, attempts

        # A 200 with empty content is a failure too (some upstreams return an
        # empty message under load) — move on to the next model.
        attempts.append({"model": model, "error": "empty response"})
        if i == len(model_list) - 1:
            exc = LLMError("all models returned an empty response")
            try:
                exc.attempts = attempts
            except Exception:
                pass
            raise exc
        logger.warning("model_failover from=%s to=%s reason=empty response", model, model_list[i + 1])
        await asyncio.sleep(_FAILOVER_SLEEP_SECONDS)

    raise LLMError("failover list exhausted")  # pragma: no cover - defensive


async def chat_completion_with_fallback(
    messages: list[dict],
    model_list: list[str],
    **kwargs,
) -> str:
    """Non-streaming completion with priority failover. Returns the content."""
    content, _, _ = await chat_completion_with_fallback_detail(messages, model_list, **kwargs)
    return content


async def chat_completion_stream_with_fallback(
    messages: list[dict],
    model_list: list[str],
    **kwargs,
) -> AsyncGenerator[str, None]:
    """Streaming completion with priority failover.

    Failover only happens while no delta has been yielded yet — once the client
    has received part of a reply, a mid-stream failure is re-raised (the caller
    keeps its committed turn and shows the fallback notice).
    """
    if not model_list:
        raise LLMError("No model available in the failover list")

    for i, model in enumerate(model_list):
        yielded = False
        stream_attempts: list[dict] = []
        try:
            async for delta in chat_completion_stream(messages, model=model, **kwargs):
                yielded = True
                yield delta
        except Exception as exc:
            stream_attempts.append({"model": model, "error": str(exc)[:200]})
            if yielded or not is_retryable(exc) or i == len(model_list) - 1:
                try:
                    exc.attempts = stream_attempts
                except Exception:
                    pass
                raise
            logger.warning("model_failover from=%s to=%s reason=%s", model, model_list[i + 1], exc)
            await asyncio.sleep(_FAILOVER_SLEEP_SECONDS)
            continue

        if yielded:
            return

        # Empty stream — treat it like a failed model and try the next one.
        stream_attempts.append({"model": model, "error": "empty response"})
        if i == len(model_list) - 1:
            exc = LLMError("all models returned an empty response")
            try:
                exc.attempts = stream_attempts
            except Exception:
                pass
            raise exc
        logger.warning("model_failover from=%s to=%s reason=empty response", model, model_list[i + 1])
        await asyncio.sleep(_FAILOVER_SLEEP_SECONDS)
