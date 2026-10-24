# "小禾" 装进手机 — 完整部署指南

你已经有了一个运行中的 Python FastAPI 后端 + 原生 PWA 前端。这份指南告诉你三条路径，从轻到重。

---

## 路径 1：PWA "添加到主屏幕"（5 分钟 · 零成本）

最简单，不需要任何打包工具。手机浏览器打开网页 → 安装到桌面 → 独立全屏运行。

**前提**：手机和电脑连同一个 Wi-Fi。

### 步骤

```bash
# 电脑上启动服务器（监听局域网）
cd ai_friend
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

手机浏览器打开 `http://[电脑局域网IP]:8000`。查电脑 IP：Windows 终端跑 `ipconfig` 看 IPv4。

### 安装到主屏幕

| 平台 | 操作 |
|---|---|
| **iOS Safari** | 底部分享按钮 → 向下滑 → "添加到主屏幕" → 桌面出现小禾图标 |
| **Android Chrome** | 右上角菜单 → "安装应用" 或 "添加到主屏幕" → 弹窗确认 |
| **Android Edge** | 右上角菜单 → "应用" → "将此站点作为应用安装" |

### 安装后

- 桌面图标 → 点开是全屏独立 App（无浏览器地址栏）
- 后台调度器继续跑：主动唤醒每 30min、虚拟生命活动每 1h
- **限制**：电脑关机，小禾就不在了；HTTP 明文（家庭 Wi-Fi 安全）

---

## 路径 2：Android APK（真正的安装包）

把 PWA 包成 `.apk`，可以分享给朋友安装、上 Google Play。用 Chrome 官方的 **Bubblewrap (TWA)**，一行命令搞定。

### 安装 Bubblewrap（需要 Node.js ≥ 18）

```bash
npm install -g @bubblewrap/cli
```

### 打包

```bash
# 先把服务部署到一个公网 HTTPS 地址（路径 3）
# 然后执行：
bubblewrap init --manifest https://你的域名/manifest.json
bubblewrap build
```

产物在 `app-release-signed.apk`，直接发到手机就能安装。

### 注意

- Bubblewrap 要求 manifest.json 里的 `start_url` 必须是 HTTPS 公网地址
- iOS 打包 IPA 需要 macOS + Xcode + 苹果开发者账号，在 Windows 上做不了
- 如果暂时没有公网 HTTPS，TWA 这条路走不通——先用路径 1，或者把服务器搬到云

---

## 路径 3：云部署 + HTTPS（终极形态 · 24h 在线）

让她不管你关不关电脑都在，手机全球访问。最便宜的 VPS（阿里云/腾讯云 1核 1G ≈ ¥50/月）就够用。

### 步骤概览

1. **买一台 VPS**（选 Linux Ubuntu，香港节点延迟低）
2. **装 Python + 克隆项目**
   ```bash
   apt update && apt install python3.11 python3-venv nginx -y
   git clone 你的仓库地址 /opt/ai_friend
   cd /opt/ai_friend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
   ```
3. **用 systemd 做守护进程**（防止崩溃自动重启）
   ```bash
   # /etc/systemd/system/ai_friend.service
   [Unit]
   Description=ai_friend companion AI
   After=network.target

   [Service]
   WorkingDirectory=/opt/ai_friend
   ExecStart=/opt/ai_friend/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   Restart=on-failure
   User=www-data

   [Install]
   WantedBy=multi-user.target
   ```
   ```bash
   systemctl daemon-reload && systemctl enable ai_friend && systemctl start ai_friend
   ```
4. **用 Nginx 反代 + Let's Encrypt 免费 HTTPS**
   ```nginx
   # /etc/nginx/sites-available/xiaohe.conf
   server {
       listen 80;
       server_name 你的域名;
       location / { proxy_pass http://127.0.0.1:8000; }
   }
   ```
   ```bash
   certbot --nginx -d 你的域名
   ```
5. **手机访问 `https://你的域名`** → "添加到主屏幕" → 永久在线的小禾

### 为什么需要 HTTPS？

- Service Worker（离线缓存）强制要求 HTTPS
- 浏览器允许 `navigator.serviceWorker.register()` 只在安全上下文
- Web Speech API 语音识别部分浏览器要求安全上下文

---

## 项目结构（部署时需要这些）

```
ai_friend/
├── app/                     # 全部后端代码
│   ├── main.py             # FastAPI 入口 + APScheduler
│   ├── config.py           # LLM_BASE_URL / PORT
│   ├── database.py         # SQLite 迁移
│   ├── models.py           # User / Conversation / Message / Memory / PersonaState
│   ├── routers/            # chat · companion · memories · settings · auth
│   ├── services/           # prompt_builder · memory_agent · emotion_kernel · life_engine
│   └── static/             # manifest.json · sw.js · 图标
├── templates/chat.html     # 全部前端（单文件 86KB）
├── ai_friend.db            # SQLite 数据库（含 .auth_token 写入的用户）
├── .auth_token             # 访问令牌（HttpOnly Cookie 鉴权）
├── .env                    # LLM_API_KEY（生产部署前填写）
├── requirements.txt        # pip 依赖
└── 启动.bat                # Windows 一键启动脚本
```

---

## 快速验证清单

| 步骤 | 命令 / 操作 | 预期 |
|---|---|---|
| 后端启动 | `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000` | HTTP 200 |
| manifest | `curl localhost:8000/manifest.json` | 合法 JSON + theme_color |
| service worker | `curl localhost:8000/sw.js` | 返回 JS 代码 |
| 手机浏览器打开 | `http://电脑IP:8000` | 页面加载 + 登录一次 |
| 添加到主屏幕 | Safari 分享 / Chrome 菜单 | 桌面出现小禾图标 |
| 图标启动 | 点击桌面图标 | 全屏独立 App（无地址栏） |

---

## 已知限制

- Windows 默认阻止 `0.0.0.0:8000` 入站——第一次启动会弹防火墙提示，点"允许"即可
- `.env` 里 `LLM_API_KEY` 为空时，服务器会返回 400 要求用户通过 UI 输入（安全）
- factory reset 会清空 DB 但保留 .auth_token，下次访问会生成新用户实例
- 手机上语音识别需要 HTTPS 或 localhost，HTTP 下 Web Speech API 可能静默禁用
