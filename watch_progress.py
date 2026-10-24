import json
import time
import os
import sys

CHECKPOINT_FILE = "_stress_test_checkpoint.json"
TOTAL_TURNS = 1000

def find_records(obj):
    if isinstance(obj, list) and len(obj) > 0 and isinstance(obj[0], dict) and 'turn' in obj[0]:
        return obj
    if isinstance(obj, dict):
        for val in obj.values():
            res = find_records(val)
            if res: return res
    return None

while True:
    try:
        with open(CHECKPOINT_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            records = find_records(data)
            if records:
                current = records[-1]['turn']
                percent = (current / TOTAL_TURNS) * 100

                # 计算进度条长度（20格）
                filled = int(percent / 5)
                bar = "█" * filled + "░" * (20 - filled)

                # 获取最新一条对话
                latest_user = records[-1]['user_input'][:20]
                latest_ai = records[-1]['ai_reply'][:20]

                # 清屏并打印（Windows 用 cls，Mac/Linux 用 clear）
                os.system('cls' if os.name == 'nt' else 'clear')

                print("=========================================")
                print("       小禾 拟人度压力测试监控台        ")
                print("=========================================")
                print(f"\n[{bar}] {current} / {TOTAL_TURNS} ({percent:.1f}%)\n")
                print(f"👉 用户刚刚说: {latest_user}...")
                print(f"👈 小禾刚刚回: {latest_ai}...\n")
                print("=========================================")
                print("按 Ctrl+C 退出监控（不会停止后台测试）")
            else:
                print("等待第一轮数据生成中...")
    except Exception as e:
        print(f"读取数据中，等待下一次刷新... ({e})")

    time.sleep(3)  # 每 3 秒刷新一次
