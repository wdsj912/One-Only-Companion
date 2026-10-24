# 小禾桌面版打包脚本 — 产出 dist\XiaoHe\XiaoHe.exe（onedir，双击即用）
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 1. 依赖
python -m pip install --quiet pywebview pystray pillow
if ($LASTEXITCODE -ne 0) { throw "pip install pywebview/pystray/pillow failed" }

# 2. 修补 pywebview 已知 bug（pip install 会覆盖之前的 patch，每次重建都要重新 patch）
#    bug 1: winforms.py FormClosed 双触发时 `del BrowserView.instances[self.uid]` 导致
#           KeyError('master')，经 Python.NET 传播弹出 .NET Framework 未处理异常对话框
#           修: del → pop(self.uid, None)
#    bug 2: edgechromium.py clear_user_data 在浏览器进程未完全退出时执行
#           shutil.rmtree 导致 WinError 5 Access Denied
#           修: 增加进程等待 + PermissionError 重试
python -c @"
import os, site, glob
site_paths = site.getusersitepackages() or site.getsitepackages()
winforms = glob.glob(os.path.join(site_paths, 'webview', 'platforms', 'winforms.py'))[0]
edgechromium = glob.glob(os.path.join(site_paths, 'webview', 'platforms', 'edgechromium.py'))[0]

# patch winforms.py
with open(winforms, 'r', encoding='utf-8') as f:
    src = f.read()
old = '            del BrowserView.instances[self.uid]'
new = '            BrowserView.instances.pop(self.uid, None)'
if old in src:
    src = src.replace(old, new)
    with open(winforms, 'w', encoding='utf-8') as f:
        f.write(src)
    print(f'  patched: {winforms}')
else:
    print(f'  already patched: {winforms}')

# patch edgechromium.py
with open(edgechromium, 'r', encoding='utf-8') as f:
    src = f.read()
old_block = '''    def clear_user_data(self):
        if not _state['private_mode']:
            return

        try:
            process_id = Convert.ToInt32(self.webview.CoreWebView2.BrowserProcessId)
            process = Process.GetProcessById(process_id)
            self.webview.Dispose()
            process.WaitForExit(3000)

            shutil.rmtree(self.user_data_folder)
        except Exception as e:
            logger.warning(f'Failed to delete user data folder: {e}')'''
new_block = '''    def clear_user_data(self):
        if not _state['private_mode']:
            return

        try:
            process_id = Convert.ToInt32(self.webview.CoreWebView2.BrowserProcessId)
            process = Process.GetProcessById(process_id)
            self.webview.Dispose()
            process.WaitForExit(5000)

            import time
            for attempt in range(3):
                try:
                    shutil.rmtree(self.user_data_folder)
                    break
                except PermissionError:
                    time.sleep(0.5)
            else:
                logger.warning(f'Failed to delete user data folder after retries: {self.user_data_folder}')
        except Exception as e:
            logger.warning(f'Failed to delete user data folder: {e}')'''
if old_block in src:
    src = src.replace(old_block, new_block)
    with open(edgechromium, 'w', encoding='utf-8') as f:
        f.write(src)
    print(f'  patched: {edgechromium}')
else:
    print(f'  already patched or changed: {edgechromium}')
"@
if ($LASTEXITCODE -ne 0) { Write-Output "WARNING: pywebview patch failed, build may still work but bug may recur" }

# 3. 从 PWA 图标生成应用图标（.ico 多尺寸）
python -c "from PIL import Image; im = Image.open('app/static/icon-512.png'); im.save('XiaoHe.ico', sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)])"
if ($LASTEXITCODE -ne 0) { throw "ico generation failed" }

# 4. PyInstaller
#    - onedir：启动快、_internal 只读没问题（数据写在 %LOCALAPPDATA%\XiaoHe）
#    - 模板与静态资源打进包里；.env 故意不打（密钥由数据目录 .env 或界面配置提供）
$pyiArgs = @(
  "--noconfirm", "--clean", "--windowed", "--name", "XiaoHe", "--icon", "XiaoHe.ico",
  "--add-data", "templates;templates",
  "--add-data", "app/static;app/static",
  "--add-data", "XiaoHe.ico;.",
  "--hidden-import", "greenlet",
  "--hidden-import", "clr",
  "--hidden-import", "aiosqlite",
  "--hidden-import", "pystray._win32",
  "--collect-submodules", "uvicorn",
  "--collect-all", "webview",
  "--collect-all", "pythonnet",
  "--collect-all", "clr_loader",
  "--collect-all", "pystray",
  "desktop_app.py"
)
python -m PyInstaller @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$exe = Join-Path $PSScriptRoot "dist\XiaoHe\XiaoHe.exe"
if (Test-Path $exe) {
  $size = [math]::Round((Get-ChildItem (Join-Path $PSScriptRoot "dist\XiaoHe") -Recurse | Measure-Object Length -Sum).Sum / 1MB, 1)
  "BUILD OK: $exe (${size}MB)"
} else {
  throw "exe not found after build"
}
