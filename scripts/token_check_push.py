#!/usr/bin/env python3
"""BB token 失效独立提醒(2026-09-25 从 second_level_monitor 独立出来)。

token 失效时独立推钉钉(标题「⚠️ BB token 失效」), 不依赖滚球监控是否假死。
launchd 每 30min 跑一次, 30min 冷却(失效期间最多每30min推一次, 避免刷屏)。
"""
import sys, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data" / "storage"
TOK_FILE = DATA_DIR / ".bb_token"
COOLDOWN_FILE = DATA_DIR / "token_push_cooldown.json"
COOLDOWN = 30 * 60  # 30min 冷却


def token_valid(tok):
    import requests, urllib3
    urllib3.disable_warnings()
    try:
        r = requests.post('https://api.infv1.com/v1/order/new/bet/list',
                          json={'languageType': 'CMN', 'isSettled': False, 'current': 1, 'size': 1},
                          headers={'Content-Type': 'application/json', 'Authorization': tok,
                                   'User-Agent': 'Mozilla/5.0'},
                          timeout=10, verify=False)
        return r.json().get('code') == 0
    except Exception:
        return False


def main():
    if not TOK_FILE.exists():
        return
    tok = TOK_FILE.read_text().strip()
    if not tok or len(tok) < 30:
        return
    if token_valid(tok):
        return  # 有效, 不推

    # 30min 冷却
    now = time.time()
    try:
        last = float(json.loads(COOLDOWN_FILE.read_text()).get("ts", 0) or 0)
    except Exception:
        last = 0.0
    if now - last < COOLDOWN:
        print(f"冷却中(距上次 {(now - last) / 60:.0f}min), 跳过")
        return

    from config.settings import send_dingtalk
    body = "⚠️ BB token 失效\n\n滚球/早盘自动下单已暂停。请打开 Chrome 的 BB 页面(vv899.bbty0vip7.com)让它自动登录, 系统才能续期。"
    try:
        send_dingtalk("⚠️ BB token 失效", body)
        COOLDOWN_FILE.write_text(json.dumps({"ts": now}))
        print("已推送 token 失效提醒")
    except Exception as e:
        print(f"推送失败: {e}")


if __name__ == "__main__":
    main()
