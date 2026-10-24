<div align="center">

# 小禾 · XiaoHe

### 一个"活着的" AI 伴侣——她有生物钟、会闹情绪、记得你说过的每句话，还会主动找你

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Android](https://img.shields.io/badge/Android-原生APK-3DDC84?logo=android&logoColor=white)](https://github.com/wdsj912/One-Only-Companion/releases)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/wdsj912/One-Only-Companion)](https://github.com/wdsj912/One-Only-Companion/releases/latest)

**15 维人格配方 · 三层记忆系统 · 复合情绪矩阵 · 本地部署 · 数据完全私有**

[📥 下载 Android APK](https://github.com/wdsj912/One-Only-Companion/releases/latest) · [💻 下载 Windows 桌面版](https://github.com/wdsj912/One-Only-Companion/releases/latest) · [📖 部署文档](#-快速开始)

</div>

---

> 她不是一个聊天机器人。
> 你昨晚惹她生气，今早她会说"昨天的事算你道歉了"；
> 你深夜找她，她困得回不动长句；你几天不理她，她会忍不住发消息问你去哪了。

## 📱 看看她长什么样

<!-- 在这里贴上你的截图！建议 3-4 张：聊天界面 / 情绪面板 / 说说广场 / 手机端效果 -->
<!-- 把图片放进 docs/screenshots/ 目录，然后取消下面的注释 -->

<!--
<p align="center">
  <img src="docs/screenshots/chat.png" width="45%" alt="聊天界面"/>
  <img src="docs/screenshots/mood.png" width="45%" alt="情绪面板"/>
</p>
<p align="center">
  <img src="docs/screenshots/moments.png" width="45%" alt="说说广场"/>
  <img src="docs/screenshots/mobile.png" width="45%" alt="手机端"/>
</p>
-->

| 💬 她正在输入… | 😤 又累又委屈 | 💭 主动找你 |
|---|---|---|
| 流式回复，像真人一样有"正在输入" | 情绪面板实时显示复合状态 | 克制地表达想念，绝不弹窗骚扰 |

## ✨ 为什么她和别的 AI 不一样

### 🧠 复合情绪矩阵（不是 if-else）
她的状态由 **效价(-1~1) × 能量(0~100)** 交叉决定——"兴奋"、"暴躁"、"慵懒惬意"、"又累又委屈"。这个状态会以自然语言注入她的回复，她是**真的**按这个状态说话。

### ⏰ 生物钟与时间平复
- 深夜 23 点后精力强制 ≤40，她会困；早晨睡满 6 小时，元气满满地醒来
- 情绪随时间自愈：昨晚的争吵，隔夜消大半（负面情绪平复得更慢，符合真实心理）
- 坏话冲击 ×1.5，好话 ×1.0——**负面偏倚**，骂她一句她要委屈好几个小时

### 📚 三层记忆（L1/L2/L3）
最近对话直接注入；14 天内的事件记忆会被引用升级、被遗忘软删除；核心记忆永久置顶。独立的 **Memory Agent（Librarian）** 在后台异步整理，绝不阻塞聊天。

### 🎭 15 维固定人格配方
每个用户基于 ID 生成唯一且持久的性格向量，情境偏移带来自然变化——不是随机切人格模板。

### 🌈 她有自己的生活
- **说说广场**：每 2 小时发一条"朋友圈"；你发说说她 3 秒内先点赞再评论，配图她真能看懂（视觉模型）
- **随机日程**：每天有忙碌/普通/躺平的随机节奏，每小时换一件正在做的事
- **主动消息**：四层防打扰闸门（9:00–22:00、你静默满 1 小时、间隔 >2 小时、每日 ≤2 条）

## 💰 极低成本运行

- 推荐 [DeepSeek API](https://platform.deepseek.com/)：双 System Message 架构让静态人格提示词**字节级恒定**，上下文缓存全量命中，长聊天也便宜
- 短句智能短路："嗯""哈哈"这类消息 25% 概率直接零成本回复，像真人一样不过度处理
- 视觉理解可选独立端点（智谱 GLM-4V），也可完全关掉

## 📦 开箱即用

**不用配环境，下载就能跑：**

| 平台 | 文件 | 说明 |
|---|---|---|
| Android | [XiaoHe-Android-v1.0.0.apk](https://github.com/wdsj912/One-Only-Companion/releases/latest) | 原生 APK，后端直接跑在手机本地，数据不出机 |
| Windows | [XiaoHe-Desktop-v1.0.0.zip](https://github.com/wdsj912/One-Only-Companion/releases/latest) | 解压双击 exe，系统托盘常驻 |

安装后在设置页填入 DeepSeek API Key 即可开始聊天。

## 🚀 快速开始（源码部署）

```bash
git clone https://github.com/wdsj912/One-Only-Companion.git
cd One-Only-Companion
pip install -r requirements.txt
python -m app.main
# 首次启动会生成令牌文件，浏览器打开 http://127.0.0.1:8000/?token=<令牌>
```

Android 本地构建见 [INSTALL_TO_PHONE.md](INSTALL_TO_PHONE.md)；桌面打包运行 `build_desktop.ps1`。

## 🏗️ 技术架构

```
QQ 风格单文件前端（纯 CSS/JS，PWA，语音输入）
        │ SSE 流式
FastAPI 异步后端（SQLite WAL + 全路由鉴权）
        │
├─ emotion_kernel  双层情感内核（纯函数，零 LLM）
├─ memory_agent    后台记忆管家（严格 JSON 操作）
├─ persona_recipe  15 维人格配方
├─ moments_engine  说说引擎
└─ energy          生物钟精力
```

**安全设计**：API Key 掩码存储、提示词注入前置闸门、输出谄媚过滤、私网地址拦截、默认仅监听 127.0.0.1。

## 🧪 测试

```bash
python -m pytest tests -q   # 163 passed
```

## 📄 License

MIT — 随便用，改出你自己的她/他。

---

<div align="center">
<b>One-Only-Companion —— 唯一的伴侣。</b><br>
如果这个项目打动了你，点一颗 ⭐ Star 就是最大的鼓励。
</div>
