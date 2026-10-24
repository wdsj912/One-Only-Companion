"""Open the default browser once the local server is actually listening.

Launched in the background by 启动.bat. Polls http://127.0.0.1:8000/ every
0.5s; on the first HTTP 200 response opens the default browser and exits.
Gives up silently after 120 tries (~60s).
"""
import time
import urllib.request
import webbrowser

URL = "http://127.0.0.1:8000/"

for _ in range(120):
    try:
        with urllib.request.urlopen(URL, timeout=1) as resp:
            if resp.status == 200:
                webbrowser.open(URL)
                break
    except Exception:
        pass
    time.sleep(0.5)
