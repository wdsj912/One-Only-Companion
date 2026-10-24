"""小禾桌面版启动器 — PyInstaller 入口。

与 android_fullstack 的 MainActivity.kt 同一思路：
  1. 在导入 app 包之前把数据目录环境变量指向可写的用户目录
     （复用 ANDROID_DATA_DIR 机制，config.py / security.py 零改动），
     数据库、.env、.auth_token 全部落在 %LOCALAPPDATA%\\XiaoHe。
  2. 后台线程启动 uvicorn（Windows 下 app.main 已设置 SelectorEventLoop 策略）。
  3. 主线程轮询 HTTP 就绪（最多 60s），读取 .auth_token 拼 URL 实现自动登录。
  4. pywebview 开原生窗口（WebView2）；不可用时降级 Edge --app 模式，再降级默认浏览器。
  5. QQ 式托盘：点 X 或最小化隐藏到系统托盘，双击托盘图标恢复；
     JS 端经 window.pywebview.api.notify(title, body) 触发气泡通知。

端口策略：优先 8000，被占用则向后扫描到 8010 —— 不干扰可能正在运行的
开发服务器，也不与其他实例冲突。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path


def _data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(base) / "XiaoHe"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _setup_logging(data_dir: Path) -> Path:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "xiaohe.log"
    # --windowed 模式没有 stderr，只落文件。密钥/令牌由 app 层保证不入日志。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
    )
    return log_path


def _find_free_port(start: int = 8000, tries: int = 11) -> int:
    import socket
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"所有可用端口 ({start}-{start + tries - 1}) 均被占用，无法启动服务")


def _wait_ready(url: str, timeout: float = 60.0) -> bool:
    import time
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _icon_path() -> Path:
    """定位应用图标 — PyInstaller onedir 下在 _internal，源码模式在项目根/app/static。"""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        for rel in ("XiaoHe.ico", "app/static/icon-512.png"):
            p = base / rel
            if p.exists():
                return p
    root = Path(__file__).resolve().parent
    for rel in ("XiaoHe.ico", "app/static/icon-512.png"):
        p = root / rel
        if p.exists():
            return p
    raise FileNotFoundError("未找到 XiaoHe.ico 或 app/static/icon-512.png")


class _TrayController:
    """QQ 式系统托盘 — pystray 实现，daemon 线程跑 GUI 循环。"""

    def __init__(self, icon_path: Path, on_open, on_exit) -> None:
        self._icon_path = icon_path
        self._on_open = on_open
        self._on_exit = on_exit
        self._icon = None
        self._log = logging.getLogger(__name__ + ".tray")

    def start(self) -> None:
        import pystray
        from PIL import Image

        image = Image.open(self._icon_path)
        menu = pystray.Menu(
            pystray.MenuItem("打开主界面", lambda: self._on_open(), default=True),
            pystray.MenuItem("退出", lambda: self._on_exit()),
        )
        self._icon = pystray.Icon("XiaoHe", image, "小禾", menu)
        threading.Thread(target=self._run, daemon=True, name="tray").start()

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception:
            self._log.exception("tray loop crashed")

    def notify(self, title: str, body: str) -> None:
        """系统气泡通知 — Windows 10+ 上由 Shell_NotifyIcon 转为 toast。"""
        if self._icon is None:
            return
        try:
            # pystray.Icon.notify(message, title)
            self._icon.notify(body, title)
        except Exception:
            self._log.exception("tray notify failed")

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None


class _JsBridge:
    """暴露给 JS 的原生能力 — window.pywebview.api.* 自动映射到这些方法。

    注意：pywebview.util.get_functions 会递归遍历公开属性生成 JS API，
    因此 window/tray/allow_close 必须用下划线开头避免把 .NET 对象图拖进来。
    """

    def __init__(self) -> None:
        self._window = None        # pywebview Window，create_window 后注入
        self._tray: _TrayController | None = None
        self._allow_close = False  # 托盘"退出"置 True 后，closing 事件才真正放行

    # ---- JS 可调方法 ----
    def notify(self, title: str, body: str) -> bool:
        if self._tray is None:
            return False
        self._tray.notify(title, body)
        return True

    def hide_window(self) -> None:
        if self._window is not None:
            self._window.hide()

    def show_window(self) -> None:
        if self._window is None:
            return
        try:
            self._window.show()
            self._window.restore()
            # 闪一下标题栏抢回焦点 — on_top 短暂置顶再取消
            self._window.on_top = True
            self._window.on_top = False
        except Exception:
            logging.getLogger(__name__ + ".bridge").exception("show_window failed")


def _open_window(url: str) -> None:
    """原生窗口，失败时逐级降级到 Edge --app / 默认浏览器。"""
    try:
        import webview  # pywebview
        # 注：pywebview winforms.py 的 on_close 已在源码层修复：
        # `del BrowserView.instances[self.uid]` → `pop(self.uid, None)`，
        # 避免 FormClosed 双触发时 KeyError('master') 经 Python.NET 传播
        # 弹出 .NET Framework 未处理异常对话框。
        # 同时 edgechromium.py 的 clear_user_data 已增加 PermissionError 重试，
        # 避免 WinError 5 导致的浏览器进程清理失败。
        # （由于 .NET delegate 在 BrowserForm.__init__ 时即已绑定，
        #  运行时替换 Python 类方法对已有实例无效，
        #  因此真正的修复必须在 PyInstaller 打包前写入源码。）

        bridge = _JsBridge()
        window = webview.create_window(
            "小禾", url,
            width=1200, height=820, min_size=(920, 640),
            js_api=bridge,
        )
        bridge._window = window

        # 托盘"退出"：置 allow_close → 销毁窗口 → webview.start() 返回 → os._exit
        def _do_exit() -> None:
            bridge._allow_close = True
            if bridge._tray is not None:
                bridge._tray.stop()
            try:
                window.destroy()
            except Exception:
                pass

        # 启动托盘
        tray = _TrayController(
            icon_path=_icon_path(),
            on_open=bridge.show_window,
            on_exit=_do_exit,
        )
        try:
            tray.start()
            bridge._tray = tray
        except Exception:
            logging.getLogger(__name__).exception("tray unavailable, continuing without it")

        # 点 X：阻止销毁，仅隐藏到托盘
        def _on_closing() -> bool:
            if bridge._allow_close:
                return True
            window.hide()
            return False
        window.events.closing += _on_closing

        # 最小化：也隐藏到托盘（QQ 行为）
        def _on_minimized() -> None:
            window.hide()
        window.events.minimized += _on_minimized

        webview.start()
        return
    except Exception:
        logging.getLogger(__name__).exception("pywebview unavailable, falling back to browser")

    # Edge app 模式：无地址栏、独立窗口，观感接近原生应用。
    import subprocess
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    for base in (pf86, pf):
        edge = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if edge.exists():
            subprocess.Popen([str(edge), f"--app={url}", "--window-size=1200,820"])
            break
    else:
        import webbrowser
        webbrowser.open(url)

    # 浏览器无法回传关闭事件——后端保持常驻，直到用户结束进程。
    import time
    while True:
        time.sleep(3600)


def main() -> None:
    data_dir = _data_dir()
    log_path = _setup_logging(data_dir)
    try:
        # --windowed 模式下 stdout/stderr 是 None，任何库碰它们都会崩
        # （uvicorn 的日志格式化器就会调 sys.stderr.isatty()）。
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")

        # 必须在导入 app.* 之前设置：config.py 在模块导入时读取该变量。
        os.environ["ANDROID_DATA_DIR"] = str(data_dir)

        port = _find_free_port()
        from app.main import app  # noqa: E402  (import 顺序有讲究，见上)

        import threading
        import uvicorn

        config = uvicorn.Config(
            app, host="127.0.0.1", port=port,
            log_level="info", access_log=False,
            log_config=None,  # 跳过 uvicorn 自带日志配置，统一走根 logger → 文件
        )
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True, name="uvicorn").start()

        base_url = f"http://127.0.0.1:{port}"
        if not _wait_ready(base_url):
            raise RuntimeError(f"后端 {base_url} 60 秒内未就绪，详见 {log_path}")

        # 自动登录：与 Android 相同，读取 .auth_token 经 URL 注入，
        # 前端会立即换取 HttpOnly Cookie 并清除地址栏参数。
        from app.security import TOKEN_FILE  # noqa: E402
        url = base_url
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if token:
                url = f"{base_url}/?token={token}"
        except OSError:
            pass

        _open_window(url)
        os._exit(0)  # 窗口关闭 → 结束常驻的 uvicorn 守护线程
    except Exception as exc:
        logging.getLogger(__name__).exception("fatal startup error")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, f"启动失败：{exc}\n\n日志文件：{log_path}", "小禾", 0x10,
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
