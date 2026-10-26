# 小禾入驻 QQ —— 傻瓜式部署指南

让小禾通过 NapCat + AstrBot 接入 QQ。全部在 Docker 里跑，不污染系统。

> ⚠️ **最重要的一句话：一定要用 QQ 小号！** 第三方协议端有封号风险，用小号跑，封了不心疼。先只在私聊测试，别急着拉群。

---

## 第 0 步：准备

1. 安装 **Docker Desktop**（https://www.docker.com/products/docker-desktop/），装完启动它，等右下角鲸鱼图标变绿。
2. 注册一个全新的 **QQ 小号**。
3. 确认小禾桌面端正在运行（`python -m app.main`，监听 `127.0.0.1:8000`）。

## 第 1 步：填 QQ 号

打开本目录的 `docker-compose.yml`，把 `ACCOUNT=YOUR_QQ_NUMBER` 改成你的小号号码。

## 第 2 步：启动容器

在本目录（`qq_bot/`）打开 PowerShell：

```powershell
docker compose up -d
```

首次会拉取镜像（约几百 MB，耐心等）。完成后 `docker ps` 应看到 `astrbot` 和 `napcat` 两个容器都在运行。

## 第 3 步：登录 NapCat（挂小号）

1. 浏览器打开 http://localhost:6099
2. 输入 WebUI Token：`xiaohe2026`
3. 页面会显示登录二维码，**用手机 QQ 小号扫码登录**

## 第 4 步：NapCat 连 AstrBot（反向 WS）

在 NapCat WebUI 里：

1. 左侧 **网络配置** → **新建** → **WebSocket 客户端（Reverse WS）**
2. URL 填：`ws://astrbot:6199/ws`
3. 消息格式选：**Array**
4. 保存并启用

## 第 5 步：AstrBot 接入 NapCat

1. 浏览器打开 http://localhost:6185（默认账号密码都是 `astrbot`，登录后建议改掉）
2. **机器人** → **添加** → 选择 **aiocqhttp（OneBot v11）**
3. 反向 WS 监听地址填 `0.0.0.0`，端口 `6199`
4. 保存。如果 NapCat 那边配置正确，这里会显示已连接。

## 第 6 步：AstrBot 连小禾（关键！）

小禾后端已经内置了 OpenAI 兼容接口 `POST /v1/chat/completions`（走的是完整的情感内核+记忆+人格链路，QQ 上的她和 App 里的她是同一个灵魂，共享同一条记忆时间线）。

在 AstrBot WebUI：

1. **大模型** → **提供商** → **添加** → 选 **OpenAI 兼容接口**
2. 填写：
   - **Base URL**：`http://host.docker.internal:8000/v1`
     （`host.docker.internal` 是 Docker 容器访问你电脑本机的固定域名，不用改）
   - **模型**：`deepseek-chat`（随便填，小禾内部会用你自己配置的模型）
   - **API Key**：打开小禾数据目录下的 `.auth_token` 文件，把里面那串令牌完整复制过来
     （桌面版默认在 `D:\new\ai_friend\.auth_token`；打包版在 `小禾.exe` 同级 `data` 目录附近）
3. 保存并设为默认提供商。

> 外部传来的 system prompt 会被桥接层直接忽略——人格只来自小禾自己的 STATIC_PERSONA，防止 QQ 上有人用提示词注入"夺舍"她。

## 第 7 步：测试

用你的**大号**给小号发一条私聊消息（比如"在吗"）。如果一切正常，小禾会以她的人格回复你——有情绪、有记忆、有性格的那种。

---

## 排错速查

| 症状 | 检查 |
|---|---|
| NapCat 打不开 | `docker ps` 看容器是否运行；`docker logs napcat` 看日志 |
| 扫码后掉线 | 小号被风控，等几小时再试；别频繁重登 |
| AstrBot 显示未连接 | NapCat 反向 WS 地址必须是 `ws://astrbot:6199/ws`（容器名，不是 localhost） |
| 小禾不回复 | AstrBot 的 Base URL 必须是 `http://host.docker.internal:8000/v1`；确认桌面端在跑；确认 API Key 是 `.auth_token` 的内容 |
| 回复 401 | API Key 填错，重新复制 `.auth_token` |

## 停止 / 卸载

```powershell
docker compose down        # 停止
docker compose down -v     # 停止并清空数据（小号登录态会丢）
```
