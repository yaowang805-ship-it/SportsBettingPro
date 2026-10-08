#!/usr/bin/env python3
"""BB token 失效独立提醒(2026-09-25 从 second_level_monitor 独立出来)。

token 失效时独立推钉钉(标题「⚠️ BB token 失效」), 不依赖滚球监控是否假死。
launchd 每 30min 跑一次, 30min 冷却(失效期间最多每30min推一次, 避免刷屏)。
2026-10-06: token_valid 加重试(3次×2s), 且连续3次失败(约60min)才判失效, 防 VPN 网络抖动误判。
"""
import sys, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data" / "storage"
TOK_FILE = DATA_DIR / ".bb_token"
COOLDOWN_FILE = DATA_DIR / "token_push_cooldown.json"
COOLDOWN = 30 * 60  # 30min 冷却
STREAK_FILE = DATA_DIR / "token_failure_streak.json"
STREAK_THRESHOLD = 3  # 连续3次(约60min)都失败才判失效(2026-10-06 防网络抖动误判)


def _bb_domain():
    """动态读 .bb_domain(2026-09-30: BB 切域名 invf1→nsvip9, 硬编码会静默失效)。"""
    try:
        dom = (DATA_DIR / ".bb_domain").read_text().strip().rstrip("/")
        if dom.startswith("http"):
            return dom
    except Exception:
        pass
    return "https://api.infv1.com"


def token_valid(tok, retries=3, retry_delay=2):
    """测 token 是否有效, 带重试。三态: True=有效, False=真失效(code!=0), None=网络异常(连不上)。

    2026-10-08 区分网络异常 vs 真失效: 之前把 SSLError/DNS/超时 都归为「失效」, 导致 VPN 切节点
    网络抖动时误判 token 失效、误开浏览器续期。现在网络异常返回 None, 不触发续期。
    """
    import requests, urllib3, time as _time
    urllib3.disable_warnings()
    for attempt in range(retries):
        try:
            r = requests.post(f'{_bb_domain()}/v1/order/new/bet/list',
                              json={'languageType': 'CMN', 'isSettled': False, 'current': 1, 'size': 1},
                              headers={'Content-Type': 'application/json', 'Authorization': tok,
                                       'User-Agent': 'Mozilla/5.0'},
                              timeout=10, verify=False)
            return r.json().get('code') == 0
        except Exception:
            if attempt < retries - 1:
                _time.sleep(retry_delay)
    return None


def _load_streak():
    try:
        return int(json.loads(STREAK_FILE.read_text()).get("streak", 0) or 0)
    except Exception:
        return 0


def _save_streak(n):
    try:
        STREAK_FILE.write_text(json.dumps({"streak": n}))
    except Exception:
        pass


def _renew_on_demand():
    """按需续期(2026-10-01 替代 bb_renew 每6h定时): 启动 Chrome 9222 → renew_bb_login → 关 9222 省CPU。

    BB token 实测极长寿(3.5天+), 失效真因是域名切换, 无需定时续期。
    平时关浏览器省 CPU, token 失效时再临时开 9222 续期, 续完即关。返回 True=续期成功。
    """
    import subprocess
    print("🚀 检测到 token 失效, 启动按需续期...")
    # 1. 启动 Chrome 9222(会等端口就绪, 幂等)
    try:
        subprocess.run(["bash", str(ROOT / "scripts" / "launch_chrome_9222.sh")],
                       capture_output=True, text=True, timeout=90)
    except Exception as e:
        print(f"启动 9222 异常: {e}")
    # 2. 跑 renew_bb_login(CDP 连 9222 读 st-auth, 失效则清+reload 触发自动登录)
    try:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "renew_bb_login.py")],
                           capture_output=True, text=True, timeout=180)
        print("renew 输出:", (r.stdout or "").strip()[-300:])
    except Exception as e:
        print(f"renew 异常: {e}")
    # 3. 关 9222(省 CPU) —— 双保险: 杀 9222 主进程 + 杀 .chrome-bb 独立配置的所有 Chrome 进程
    try:
        subprocess.run(["pkill", "-f", "remote-debugging-port=9222"], capture_output=True, timeout=10)
        subprocess.run(["pkill", "-f", ".chrome-bb"], capture_output=True, timeout=10)
    except Exception:
        pass
    # 4. 验证续期结果
    try:
        new_tok = TOK_FILE.read_text().strip()
        if new_tok and len(new_tok) > 30 and token_valid(new_tok):
            return True
    except Exception:
        pass
    return False


def main():
    if not TOK_FILE.exists():
        return
    tok = TOK_FILE.read_text().strip()
    if not tok or len(tok) < 30:
        return
    _res = token_valid(tok)
    if _res is True:
        _save_streak(0)  # 有效 → 清连续失败计数
        return
    if _res is None:
        # 网络异常(连不上域名): 无法判断 token 是否失效, 不判失效不续期(防网络抖动误开浏览器)
        print("token 检测网络异常(连不上域名, 非 token 失效), 跳过续期")
        return

    # 真失效(code!=0) → 累计连续失败次数, 达阈值才判失效(2026-10-06 防网络抖动误判)
    streak = _load_streak() + 1
    _save_streak(streak)
    if streak < STREAK_THRESHOLD:
        print(f"token 检测失败(第 {streak}/{STREAK_THRESHOLD} 次, 可能是网络抖动), 暂不续期")
        return

    # 连续 N 次失败 → 判失效, 走 30min 冷却
    now = time.time()
    try:
        last = float(json.loads(COOLDOWN_FILE.read_text()).get("ts", 0) or 0)
    except Exception:
        last = 0.0
    if now - last < COOLDOWN:
        print(f"冷却中(距上次 {(now - last) / 60:.0f}min), 跳过")
        return

    # 2026-10-01: 按需续期(替代 bb_renew 每6h定时)——token 失效时自动开 9222 续期再关
    ok = _renew_on_demand()
    _save_streak(0)  # 续期后清计数(无论成败, 冷却会防刷)

    from config.settings import send_dingtalk
    if ok:
        title = "✅ BB token 已自动续期"
        body = "token 失效后已自动打开浏览器续期成功，下单恢复。"
    else:
        title = "⚠️ BB token 需手动登录"
        body = "token 失效且自动续期失败。请打开 Chrome 的 BB 页面(vv899.bbty0vip7.com)手动登录。"
    try:
        send_dingtalk(title, body)
        COOLDOWN_FILE.write_text(json.dumps({"ts": now}))
        print("已推送:", title)
    except Exception as e:
        print(f"推送失败: {e}")


if __name__ == "__main__":
    main()
