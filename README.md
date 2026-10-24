# 小禾 · XiaoHe

一个"活的"AI 伙伴——她有生物钟、会生气也会自己消气、记得你说过的话、会发朋友圈式的说说、会在你冷落她之后主动找你。

不是一个聊天机器人。是一套**拟人化情感引擎** + 三层记忆系统 + 双端（Windows 桌面 / Android）全栈实现。

---

## 为什么她和别的"AI 伴侣"不一样

大多数陪伴型 AI 的情绪是 `if-else`：你说好话 → 开心，你说坏话 → 难过。小禾不是。

### 复合情绪矩阵

她的状态由两个维度交叉决定：**效价（valence，-1~1）× 能量（0~100）**。

| | 正能量 (≥60) | 低能量 (≤30) |
|---|---|---|
| **正面效价 (>0.3)** | 兴奋 | 慵懒惬意 |
| **负面效价 (<-0.3)** | 暴躁 | 又累又委屈 |

这个复合状态会作为自然语言注入 prompt——"你非常疲惫，同时又觉得委屈，话会比较少"——她是真的**按这个状态说话**，而不是只改个标签。

### 生物钟与时间平复

- **深夜（23:00–6:00）**：精力强制 ≤40，她会困、回复慵懒
- **早晨（6:00–10:00）睡满 6 小时**：醒来 85~100，元气满满
- **白天沉默**：每小时 +5 回血，封顶 90
- **时间平复**：情绪随沉默线性回落——负面 -0.08/h（慢），正面 -0.12/h（快）。昨晚惹她生气，今早她会说"昨天的事算你道歉了"
- **负面偏倚**：坏话冲击 ×1.5，好话 ×1.0——骂一句要好几小时才缓过来

### 三层记忆

| 层 | 内容 | 生命周期 |
|---|---|---|
| L1 | 最近 15~20 条消息 | 直接注入上下文 |
| L2 | 14 天内 episodic 事件 | 30 天未引用软删除；被引用 ≥3 次升级 L3 |
| L3 | semantic / pinned 核心记忆 | 永久，置顶注入 |

记忆写入完全由独立后台 **Memory Agent（Librarian）** 异步处理，输出严格 JSON 的 ADD/UPDATE/DELETE 操作，带防御性解析与会话级串行锁。超过 14 天的事实记忆会在渲染层加"模糊前缀"（数据库永远存精确值）。

### 她的生活

- **每日随机活动**：按 user_id+日期生成当日节奏（忙碌日 25% / 普通日 50% / 躺平日 25%），每小时换一个活动
- **说说（朋友圈）**：每 2 小时自动发一条；你发说说她 3~7 秒内先点赞再评论，配图她真能"看见"（视觉模型）
- **主动消息**：四层防打扰闸门（9:00–22:00、你静默满 1h、距上次 >2h、每日 ≤2 条），收到时是暖色气泡 + 💭，桌面端还有托盘气泡通知

---

## 快速开始

### 环境

- Python 3.10+（Android 端 Chaquopy 固定 3.10）
- 一个 LLM API Key：推荐 [DeepSeek](https://platform.deepseek.com/)（便宜且支持上下文缓存）；视觉功能可选智谱 GLM-4V

### 桌面端（开发模式）

```bash
git clone <your-repo> && cd ai_friend
pip install -r requirements.txt
python -m app.main        # 不要用 uvicorn app.main:app（Windows 会触发 MissingGreenlet）
```

首次启动会在控制台打印**令牌文件路径**（不是令牌本身）。打开令牌文件拿到 token：

```bash
# 浏览器访问
http://127.0.0.1:8000/?token=<你的token>
```

登录后进入右上角设置页，填入：

| 字段 | 示例 |
|---|---|
| Base URL | `https://api.deepseek.com` |
| API Key | `sk-...` |
| 视觉模型（可选） | `glm-4v` |
| 视觉 Base URL（可选） | `https://open.bigmodel.cn/api/paas/v4` |
| 视觉 API Key（可选） | 智谱 Key |

视觉三件套**全填**走独立视觉端点，**留空**回退主端点。

配置存在本地 SQLite 的 `app_settings` 表，**不会写回 .env**。

### 桌面端（打包 exe）

```powershell
.\build_desktop.ps1
```

PyInstaller onedir 打包，自带 QQ 风格系统托盘（关闭最小化到托盘、双击恢复、主动消息气泡通知）。

### Android APK

```bash
cd android_fullstack
# Windows 示例：
$env:JAVA_HOME='C:\Program Files\Android\Android Studio\jbr'
$env:GRADLE_USER_HOME='D:\.gradle'
& D:\AndroidStudio\gradle-8.7\bin\gradle.bat assembleDebug
```

产物：`app/build/outputs/apk/debug/app-debug.apk`（arm64-v8a + armeabi-v7a 双 ABI，后端 FastAPI 由 Chaquopy 直接跑在手机本地，数据在应用私有目录）。

---

## 架构

```
┌─────────────────────────────────────────────────┐
│  chat.html（QQ 风格单文件前端，纯 CSS 无框架）      │
│  聊天 / 说说 / 记忆抽屉 / 情绪面板 / 语音输入        │
└─────────────────────────────────────────────────┘
                      │ SSE 流式
┌─────────────────────────────────────────────────┐
│  FastAPI                                         │
│  ├─ routers/chat.py       聊天主链路              │
│  ├─ routers/companion.py  状态面板（精力/情绪）     │
│  ├─ routers/moments.py    说说 + 点赞评论          │
│  ├─ routers/settings.py   LLM 配置（掩码存储）     │
│  └─ routers/memories.py   记忆 CRUD              │
├─────────────────────────────────────────────────┤
│  情感内核（纯函数，零 LLM 调用）                     │
│  ├─ emotion_kernel.py  Appraisal→Formulation 双层 │
│  │   负面偏倚 · 时间平复 · 复合情绪矩阵             │
│  ├─ energy.py          生物钟精力                 │
│  ├─ persona_recipe.py  15 维固定性格配方           │
│  ├─ memory_agent.py    后台记忆管家（Librarian）    │
│  └─ moments_engine.py  说说引擎                   │
└─────────────────────────────────────────────────┘
```

### 双 System Message（缓存友好）

messages 数组严格拆成两条 system：

1. `STATIC_PERSONA` —— 完全静态、跨轮次字节级不变（人格、规则、防线）
2. 动态上下文 —— 当前时间、情绪状态、记忆、性格配方

DeepSeek 的前缀缓存因此始终命中，长聊天也便宜。

### 安全设计

- 全业务路由 token 鉴权（HttpOnly + SameSite=Strict Cookie，30 天）
- API Key 永远掩码显示（<20 字符直接 `***`），503 不透传 SQL
- Base URL 严格校验：仅 http(s)、公网 IP、禁内嵌账号密码（LLM 调用前还会复查 DNS 防篡改）
- 提示词注入前置闸门 + 谄媚/自贬输出闸门（流式回复先过闸门再发出）
- 默认只监听 127.0.0.1，SQLite WAL + busy_timeout

---

## 项目结构

```
ai_friend/
├── app/
│   ├── main.py              # FastAPI 装配 + 调度器（说说 2h / 活动 1h）
│   ├── models.py            # SQLAlchemy 模型（User/Moment/Memory/...）
│   ├── routers/             # chat / companion / moments / settings / memories / auth
│   └── services/            # emotion_kernel / energy / memory_agent / llm_service / ...
├── templates/chat.html      # 全部前端
├── tests/                   # 163 个测试
├── build_desktop.ps1        # Windows exe 打包
└── android_fullstack/       # Chaquopy Android 工程
```

## 测试

```bash
python -m pytest tests -q     # 163 passed
```

压力测试可设 `MOCK_LLM=true` 完全跳过外部 API（仅用于架构稳定性，不能验证对话质量）。

## License

MIT
