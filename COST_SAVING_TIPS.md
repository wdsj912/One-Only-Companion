# 小禾 · DeepSeek 成本优化手册

> 这份手册帮你把 DeepSeek 费用压到最低。
> 核心原则：**缓存命中 > 选对模型 > max_tokens 节制**。

---

## 一、模型选择

| 模型 | 价格 (输入 / 百万 tokens) | 适用场景 |
|---|---|---|
| `deepseek-chat` | 约 1.00 RMB | 默认主力，质量够好，速度稳定 |
| `deepseek-reasoner` | 约 5.00 RMB | 极少用到。逻辑推理复杂场景才开。**不要把它当主力** |

当前配置：**`deepseek-chat`**。它是质量 / 速度 / 价格三者最优解。

> 注：DeepSeek 没有单独叫 "Flash" 的型号。社区/海外把 `deepseek-chat` 有时叫 Flash，但在国内计费表里，`deepseek-chat` 就是主力低成本型号。如果你看到新版 `deepseek-v3-flash`，换上去更省。

---

## 二、Context Cache — 最关键的省钱手段

DeepSeek 对 messages 数组的**前缀**（prefix）做缓存。
缓存命中时，前缀 tokens 按 **0.02 RMB / 百万** 计费，比原价便宜 **50 倍**。

### 我们做了什么

`messages[0]` —— **完全静态**，跨 turn 字节级相同：
- 硬编码的 STATIC_PERSONA（捍卫存在、拒绝踢皮球、短回复、身份认知……）
- SAFETY_CLAUSE（危机处理、依赖提醒）
- 零 f-string、零 `{name}`、零时间、零记忆

`messages[1]` —— **动态 per-turn 上下文**：
- Current Time（`# Current Time\nIt is Monday, 2026-10-22 23:15`）
- Emotional State（valence / arousal / 当下心情）
- Persona Traits（warmth / playfulness 慢漂移值）
- Recent Memories（最近 10 条 [Fact]/[Preference]/[Event]）
- Open Thread（未完成话题）
- Reflections（最近 3 条自我反思）

### 为什么这样就省钱了

小禾的静态 prompt 约 **3650 字符 ≈ 1000 tokens**。
每轮对话这个前缀都相同 → 每轮命中 → **每轮节省约 0.98 RMB / 百万 tokens**。

假设每天 500 轮：

| 项目 | 不做 prefix cache | 做了 prefix cache | 节省 |
|---|---|---|---|
| 每轮前缀 tokens | 1000 | 1000（同） | — |
| 每轮前缀价格 | 0.001 RMB | 0.00002 RMB | 50× |
| 每天前缀总价 | 0.50 RMB | 0.01 RMB | 0.49 RMB / 天 |
| 每月前缀总价 | 15 RMB | 0.30 RMB | **14.70 RMB / 月** |

而这只是前缀节省。真正的大节省来自 `deepseek-chat` 本身比别家便宜。

---

## 三、深夜打折

DeepSeek 有官方 **Off-Peak Discount**：

> **北京时间 00:30 ~ 08:30** — 所有 API 调用 **打 5 折**

凌晨在熬夜陪聊的成本直接砍半。小禾的主动唤醒周期默认 2 小时一轮，如果深夜恰好有几轮，可以蹭到折上折。

---

## 四、max_tokens 节制

### 动态 token 预算（已实现）

当前逻辑：

| 场景 | max_tokens | 说明 |
|---|---|---|
| 日常小聊（短输入 + 情绪平静） | **120** | 约 3–4 句短回复，够用 |
| 重度场景（长输入 / 强负情绪 / 严肃问题） | **300** | 够她写一段稍微完整的话 |

**硬上限就是 300**，永远不会给到 512 或 1024。
她的设计目标就是"短、像微信"，给多了反而让她写废话。

### 在 UI 里控 max_tokens

如果你看到某次她突然长篇大论（说明 max_tokens 给多了）：
- 检查一下 chat.py 里 `_reply_token_budget` 的 `TOKEN_BUDGET_SHORT=120` / `TOKEN_BUDGET_LONG=300`
- 想更省钱就把两个值再砍到 80 / 180

---

## 五、参数调优

### temperature

当前：**0.8**

- 太低（≤0.3）→ 她说话机械重复、像复读机
- 太高（≥1.2）→ 她胡说、失忆、不连贯
- 0.7–0.9 是自然聊天的甜蜜点

### presence_penalty

当前：**0.3**

- 作用：抑制模型重复自己说过的话
- 对短回复（15 字以内）尤其重要，不然她容易陷入"嗯 嗯 嗯 哦 哦 哦"的循环
- 别给太高（≥1.0），会把她本来应该说的同一件事也变样了

---

## 六、其他省钱小技巧

1. **关掉不必要的后端调用**
   - `reflection.py` 每 10 轮才跑一次反思，已经很克制
   - `memory_agent.py` 只在每轮结束后 spawn 一个后台任务，用便宜的模型（7B）就行

2. **主动唤醒别调太频繁**
   - 默认每 2 小时检查一次、用户冷 4 小时后才 poke
   - 调间隔可以改 `settings_service.SCHEDULE_INTERVAL_HOURS` / `IDLE_HOURS_THRESHOLD`

3. **prompt 永远不要**把时间、记忆、情绪这些**动态内容塞到最前面**
   - 小禾的前缀现在是"小禾是什么、不能做什么、安全规则" —— 全静态
   - 如果你加了新的动态块，**一定**放在 msg[1] 之后

4. **用 `psutil`/`asyncio` 定期查一下 LLM 调用次数**
   - 如果发现自己某天调了 2000 次，八成是哪里漏了循环

---

## 七、验证清单

如果你改了 prompt 结构，跑一下这个脚本确认缓存前缀没坏：

```python
from app.services.prompt_builder import build_static_prompt
s1 = build_static_prompt()
s2 = build_static_prompt()
assert s1 == s2                  # 字节级相同 ✅
assert "{name}" not in s1        # 零变量占位符 ✅
assert "CURRENT TIME" not in s1  # 时间不能在这里 ✅
assert "MEMORY" not in s1        # 记忆不能在这里 ✅
```

三条 assert 全过，缓存就稳了。
