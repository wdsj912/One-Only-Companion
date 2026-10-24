# SYSTEM ISSUE INVENTORY — pre-fix plan for post-stress-run

Identified 7 concrete issues (severity-tagged), with code locations, root cause, and
fix direction.  Execute in this order after the 1000-run stress completes.

---

## P0 — Hot-path correctness / perf bottleneck

### [high] chat.py L311-313 — full-session message pull
**位置** `app/routers/chat.py` `send_message` / `event_generator`
```python
msgs_in_conv = list((await session.execute(
    select(m.Message).where(m.Message.conversation_id == conv.id).order_by(m.Message.created_at)
)).scalars().all())
```
全量加载整会话所有消息（1000 次对话后 = 2000+ 行），然后 L325 只 slice `working_memory[-20:]`。
**修复** 直接 SQL `order_by(created_at.desc()).limit(20)` + `reversed()`。影响：每 turn 省一个 O(N) 内存分配 + N+10 个 ORM object。

### [high] chat.py L341-342 — get_llm_config 重复调用 + model_list 额外 DB 查询
```python
cfg = await get_llm_config()          # ← _endpoint() 里会再调一次！
priority = await get_model_priority_list(session)
model_list = [x for x in dict.fromkeys([cfg.get("model") or "", *priority]) if x]
```
- `chat_completion_with_fallback` 内部 `chat_completion` → `_endpoint()` → `get_llm_config()` — 每 turn **2 次** get_llm_config 调用（有 5s 缓存，偶尔撞缓存失效）
- `get_model_priority_list` 没有缓存，每次 chat send 多一次 DB 查询
**修复** 
  1. 把 model_list 从 chat.py 内部算好，传入一个带 cache 的函数（`resolve_model_list(session)`），5s TTL 与 get_llm_config 对齐
  2. 让 `_endpoint` 接受可选 `model_override`，避免再调一次 get_llm_config

### [high] llm_service.py L71 / L95 — 每次 LLM 调用新建 httpx.AsyncClient
```python
async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
    resp = await client.post(url, ...)
```
AsyncClient 自带连接池。每 turn 至少 chat × 1 + failover × 0-3 + Librarian × 1-5 = 1-9 个 client 创建销毁。
**修复** FastAPI lifespan 里 `httpx.AsyncClient()` 单例，chat_completion/stream 共用，shutdown 时 `await client.aclose()`。

---

## P1 — Retry / 退避策略

### [medium] llm_service.py L118-129 is_retryable 不区分 GLM 错误码
当前只看 HTTP status：429 一律 retry。但 GLM 有 3 种 429：
- **1302 账户级限流**（"您的账户已达到速率限制"）→ 换模型没用，必须长退避
- **1305 模型级限流**（"该模型当前访问量过大"）→ 换模型可能有用（我们就在做）
- **1305 模型过载**（ thinking 模型特殊情况）→ 应该直接 skip 这个模型，不等

当前 1s failover sleep 对 1302 太短，会连续 3 个模型全 429 白耗配额。
**修复** 从 `exc.message` JSON 里解析 `"code"`，区分：
- 1302 → 跳过 failover 循环，直接 raise（让 Librarian 的 5/15/30/60s 退避来处理）
- 其他 429 → 保持 failover（1s）或把 sleep 从 1s 调到 3s

### [medium] memory_agent.py L284 — Librarian 退避对账户级限流反应相同
`_BACKOFF_S = (5, 15, 30, 60)` 对所有 429 一视同仁。如果 chat 层遇到 1302 账户级限流后 return，接着 spawn 的 Librarian 也会连续被 1302 拒 5 次。但这**其实 OK**：它会跑 5+15+30+60 = 110s 退避，再 raise → 下一轮的 6-message 窗口自愈。**暂不修**，但可以加个优化：Librarian 第一次调就遇 1302，直接把退避跳到 30s 起步，省一次 5s 的空等。

---

## P2 — Prompt-build 热路径

### [medium] chat.py L331 + prompt_builder._format_memories L157 — sycophancy 双扫
`_format_memories` 里对每条 memory 调一次 `detect_sycophancy` 做 prompt block，然后 chat.py L331 又对同样的 memories 再跑一次做 `challenge_counter`。
**修复** 让 `_format_memories` 返回一个 `(prompt_block, hit_count)` tuple，调用处用 hit_count 直接更新 challenge_counter。或者更简单：用一个类来收集中间状态。

### [low] prompt_builder.py L147 — 函数内部 import
```python
def _format_memories(...):
    from app.services.sycophancy_gate import detect_sycophancy, augment_memories_prompt_block
```
每次 build_system_prompt 都要触发 import 名字查找（虽然 Python 有缓存，影响小）。
**修复** 移到文件顶部。

---

## P3 — Memory

### [low] memory_agent.py _CONV_LOCKS dict 无回收
```python
_CONV_LOCKS: dict[int, asyncio.Lock] = {}
```
新 conversation_id 会一直 append，永不回收。长期运行 10000+ conv 后 dict 变大（条目是 Lock 对象，几个 KB 一个，10000 = ~50MB）。
**修复** 
1. 用 `collections.OrderedDict` + `popitem(last=False)` LRU，上限 200 个
2. 或者更简单：conversations 是有限集合（真实用户不会创建无限 conv），1000 级规模可忽略
**结论** 先不修，stress 结束后看实际 conv 数量再决定。

---

## Execution order
1. Fix #2 (get_llm_config 重复 + model_list) — 影响面大，最容易修
2. Fix #1 (全量拉消息) — SQL 改一句，立竿见影
3. Fix #3 (AsyncClient 共享) — lifespan 改动，注意 shutdown 顺序
4. Fix #4 (GLM 1302 分类) — 影响退避策略稳定性
5. Fix #5 (Librarian 1302 快速退避起点) — 可选优化
6. Fix #6 (sycophancy 双扫) — prompt 路径微优化
7. Fix #7 (prompt_builder 内部 import) — 代码卫生

P3 (#8) 看 stress 实际 conv 数量决定要不要修。
