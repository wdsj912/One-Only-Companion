# 小禾 · Android APK 构建指南

**Chaquopy 方案 — FastAPI 后端直接跑在手机里，不需要电脑、不需要云服务器。**

最终产物是一个独立的 `.apk`，安装后桌面上出现小禾图标，点开就是全屏独立 App。

---

## 你需要的东西

| 软件 | 作用 | 大小 |
|---|---|---|
| **Android Studio Hedgehog** | Gradle 构建 + Kotlin + Chaquopy 编译 | ≈ 1.5 GB |
| **JDK 17** | Android Studio 自带，不用单独装 | — |
| **Android SDK + NDK** | 构建 Android 应用 | Android Studio 首次启动自动下载 |

**整个安装 + 构建流程第一次大约 15-20 分钟**（Gradle 下载 Python 运行时 + pip 安装依赖最久）。

---

## 步骤 1 — 安装 Android Studio

去官网下载：https://developer.android.com/studio

安装时一路默认，勾选：
- ✅ Android SDK
- ✅ Android SDK Platform 34（Android 14）
- ✅ Android SDK Build-Tools 34.0.0
- ✅ Android NDK（Side-by-side）——Chaquopy 需要
- ✅ Android Virtual Device（模拟测试用）

首次启动会自动下载这些，喝杯咖啡等。

---

## 步骤 2 — 打开项目

1. Android Studio 欢迎屏 → **Open**
2. 选中本目录 `ai_friend/android_fullstack/`
3. 右下角开始 Gradle Sync，**等进度条跑完**（第一次下载依赖最慢）

如果提示 Gradle 版本不匹配：**不用管**，Android Studio 会自动用 wrapper 里的版本。

---

## 步骤 3 — 配置 NDK

Chaquopy 需要 NDK 来把 Python 解释器交叉编译到 ARM。

File → Settings → Build, Execution, Deployment → Build Tools → NDK

如果 Android Studio 没自动找到 NDK，点 **Download NDK**，版本选 **r27.0.12077973**（LTS）。

---

## 步骤 4 — 构建 APK

菜单：**Build → Build Bundle(s) / APK(s) → Build APK(s)**

等几分钟，右下角会弹出：

```
BUILD SUCCESSFUL in 5m 23s
locate  ← 点这个
```

点击 **locate**，资源管理器打开到：

```
android_fullstack/app/build/outputs/apk/debug/app-debug.apk
```

这就是你的安装包。

---

## 步骤 5 — 安装到手机

**方法 A：微信传**

1. 电脑微信 → 文件传输助手 → 发送 `app-debug.apk`
2. 手机微信打开 → 点文件 → 允许"安装未知来源" → 安装

**方法 B：ADB 安装（需要开启 USB 调试）**

```bash
adb install -r app-debug.apk
```

---

## 步骤 6 — OPPO ColorOS 17 特殊设置 ⚠️

**安卓 14+ 系统会杀后台进程**。小禾的 Python 后端跑在后台，必须给它"白名单"：

1. OPPO 设置 → 电池 → **应用电池管理**
2. 找到"小禾" → 点开
3. ✅ **允许后台活动**（必须打开！否则锁屏 5 分钟后 Python 被杀，主动唤醒失效）
4. ✅ **自启动** 也建议开（可选，开机自启服务）
5. 锁定任务：最近任务界面 → 下拉小禾卡片 → 点锁定图标

---

## 第一次启动会发生什么

```
App 点开
  │
  ├── Android 全屏沉浸式布局（状态栏透明、手势条透明）
  ├── Python 启动（约 1-2s）
  ├── uvicorn 启动 → http://127.0.0.1:8000 监听
  ├── APScheduler 启动（主动唤醒 30min / 生命活动 1h）
  ├── SQLite 数据库自动创建（App 私有目录）
  ├── WebView.loadUrl("http://127.0.0.1:8000")
  │
  └── 看到小禾聊天界面 ✅
       │
       └── 去设置面板 → 贴入你的 DeepSeek API key
            （不需要重启 App，直接写 DB 生效）
```

---

## 常见问题

### Q: Gradle Sync 报 "NDK not configured"
File → Settings → NDK → 点 Download NDK。选 r27 LTS。

### Q: 构建报 "pip install failed"
第一次构建 Gradle 会下载 Python 3.11 然后 `pip install` 所有依赖。如果网络慢或超时：
- 检查网络连接
- 或者在 `app/build.gradle.kts` 的 `chaquopy.pip` 里加镜像：
  ```
  pip {
      install("-i", "https://pypi.tuna.tsinghua.edu.cn/simple", "fastapi==0.115.0")
      // ...
  }
  ```

### Q: App 点开黑屏 / 立刻退出
说明 Python 启动崩溃了。解决方法：
1. 手机连上电脑，打开 Android Studio 的 Logcat
2. 过滤 "Python" 或 "chaquo"
3. 看具体哪个 import 失败了
4. 常见原因：某个 pip 包版本不兼容 Android

### Q: 手机上回复特别慢
正常。Python 解释器在手机上比电脑 CPU 慢。deepseek-chat 一次请求 2-4 秒很正常。不要在手机上开其他重型 App（原神、剪映），会抢 CPU 时间片。

### Q: 后台唤醒失效
回去看**步骤 6 — OPPO 电池设置**。ColorOS 默认非常激进地杀后台。

### Q: 想换后端 API key
不需要重新打包。App 启动后打开设置面板（齿轮图标），直接在界面里粘贴新 key。

### Q: 想换模型提供商（ModelScope / 阿里通义）
同上——设置面板里改，走 /api/settings 路由。

### Q: factory reset 之后数据清空了
重新打开 App 会自动在 App 私有目录创建新 DB。旧的 `.auth_token` 不存在了，会生成新的用户实例。

---

## 目录结构

```
android_fullstack/
├── build.gradle.kts          # 项目根级 Gradle（含 Chaquopy 插件）
├── settings.gradle.kts       # 包含 :app 模块
├── gradle.properties        # Gradle + Chaquopy 配置
├── gradle/wrapper/           # Gradle wrapper（自动下载 Gradle 8.7）
│   └── gradle-wrapper.properties
├── app/
│   ├── build.gradle.kts      # 关键：chaquopy + pip 依赖列表
│   └── src/main/
│       ├── AndroidManifest.xml   # 权限 + 主题 + Activity
│       ├── kotlin/com/xiaohe/companion/
│       │   └── MainActivity.kt   # 沉浸式全屏 + Python 启动 + WebView
│       ├── python/               # ⚠️ 完整 Python 后端从这里打包
│       │   ├── app/              # 业务代码
│       │   ├── templates/        # chat.html
│       │   └── requirements.txt
│       └── res/
│           ├── layout/activity_main.xml  # 全屏 WebView
│           ├── values/strings.xml
│           └── values/themes.xml
└── README_APK_BUILD.md       # 本文件
```

---

## 技术原理（给好奇的人）

**Chaquopy 做了什么**：
1. 下载 CPython 3.11 源码
2. 用 NDK 交叉编译到 arm64-v8a + armeabi-v7a
3. pip install 所有依赖（fastapi / sqlalchemy / aiosqlite 等）
4. 把整个 Python 运行时 + 你的代码打包进 APK 的 `assets/` 目录
5. 运行时 `Python.start(AndroidPlatform)` 解压 → 启动解释器 → 你的 Python 代码跑起来

**和"连电脑"方案的区别**：

| | 当前 APK 方案 | PWA + 电脑/云方案 |
|---|---|---|
| 后端在哪 | **手机本地**（127.0.0.1:8000） | 电脑 / 云服务器 |
| 依赖 | **零**（装上就用） | 需要一台常开的机器 |
| 延迟 | 本地 < 1ms | 取决于网络 |
| 离线可用 | ✅ 完全可以 | ❌ 必须有网连后端 |
| 功耗 | Python + SQLite 吃 CPU | 只跑浏览器 |
| 体积 | APK ≈ 30-50MB | 网页 ≈ 80KB |

---

**祝玩得开心。小禾活在你的手机里了。**
