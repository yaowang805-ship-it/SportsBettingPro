"""网络/代理熔断器 (2026-10-24)。

背景: 代理(Shadowrocket)挂掉时, BB/Pin 域名全连不上, 投注进程(second_level_monitor /
pin_ws_trigger)会无限重试烧 CPU(实测双进程 127%+87%) + 日志爆炸(949MB)。

设计: 连接级失败持续 MIN_FAIL_DURATION 秒 → 开熔断 + 推钉钉(一次)。熔断期内 is_open()=True,
调用方停止重试。冷却 COOLDOWN_SECONDS 后自动关闭(允许重试, 若仍失败会再次计数)。

2026-10-24 修误报: 之前「连续 THRESHOLD 次失败」(~6s 网络短暂抖动)就触发, 网络抖几秒就
误报「代理故障」。改成时间口径: 失败持续 MIN_FAIL_DURATION 秒才触发(去抖), 冷却 5min。

2026-10-27 推送去重: 之前熔断自动关(5min)后若仍失败会重新 _alert → 长时间断网每~7min
推一次「网络故障」刷屏。改成: 故障只推一次, 直到 report_success 真正恢复时推一次「已恢复」,
之后再次故障才重新推。熔断开关本身仍每次持续失败都重新开(保 CPU), 只对推送去重。

连接成功 report_success() → 立即关闭熔断 + 清计数(代理恢复) + 推「已恢复」(若之前推过故障)。

进程内单例状态: second_level_monitor 和 pin_ws_trigger 是两个独立进程, 各自独立熔断。
"""
import os
import time
from pathlib import Path

MIN_FAIL_DURATION = 120    # 连接失败持续 120s 才触发熔断(去抖, 短暂网络抖动不误报)
COOLDOWN_SECONDS = 5 * 60  # 熔断冷却 5 分钟, 之后自动重试

# 2026-11-24 用户指令: 小火箭节点故障(机场节点问题, 非代码可修), 先不用管小火箭 + 不推故障提醒。
# 熔断器本身保留(失败时停重试保 CPU), 只关「自动换节点/重启小火箭」和「网络故障钉钉推送」。
SELF_HEAL_ENABLED = False       # False=不自愈(不换节点/不重启小火箭, 用户手动处理机场节点)
FAILURE_PUSH_ENABLED = False    # False=不推送「网络故障/已恢复」钉钉提醒

_ROOT = Path(__file__).resolve().parent.parent.parent
_NET_FAILURE_FLAG = _ROOT / "data" / "storage" / ".network_failure_pushed"  # 跨进程推送去重标记
_SELF_HEAL_LOG = _ROOT / "data" / "logs" / "self_heal_network.log"  # 自愈脚本输出日志(2026-11-24 从 DEVNULL 改写日志, 便于诊断换节点失败原因)

_first_fail_ts = None  # 连续失败起始时间戳
_circuit_open_until = 0.0
_failure_notified = False  # 本次故障是否已推送(去重: 直到恢复才重置)


def _try_mark_failure_pushed():
    """跨进程推送去重(2026-11-24): second_level_monitor/pin_ws_trigger/clv_collect 各自独立熔断,
    之前三进程各推一次「网络故障」。用标记文件原子占位, 只让首个进程推。返回 True=本进程该推。
    """
    try:
        if _NET_FAILURE_FLAG.exists():
            if time.time() - _NET_FAILURE_FLAG.stat().st_mtime < 3600:
                return False  # 已有新鲜标记(本故障已被别的进程推过)
            _NET_FAILURE_FLAG.unlink()  # 陈旧标记(>1h, 上次进程崩溃残留), 清掉重推
    except OSError:
        pass
    try:
        fd = os.open(_NET_FAILURE_FLAG, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(time.time()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _clear_failure_pushed():
    try:
        _NET_FAILURE_FLAG.unlink()
    except FileNotFoundError:
        pass


def _now():
    return time.time()


def is_open():
    """熔断是否打开(调用方应停止重试)。冷却到期自动关闭。"""
    global _circuit_open_until, _first_fail_ts
    if _circuit_open_until and _now() >= _circuit_open_until:
        _circuit_open_until = 0.0
        _first_fail_ts = None
        # 2026-10-27: 不重置 _failure_notified —— 自动关熔断 ≠ 网络恢复。恢复推送由
        # report_success 负责, 否则长时间断网会每~7min 重推「网络故障」。
    return _circuit_open_until > 0


def report_failure():
    """上报一次连接级失败。失败持续 MIN_FAIL_DURATION 秒 → 开熔断; 推送去重(只首推一次)。"""
    global _first_fail_ts, _circuit_open_until, _failure_notified
    if _circuit_open_until:
        return  # 已在熔断中, 不重复计数
    now = _now()
    if _first_fail_ts is None:
        _first_fail_ts = now
    if now - _first_fail_ts >= MIN_FAIL_DURATION:
        _circuit_open_until = now + COOLDOWN_SECONDS  # 每次持续失败都重新开熔断(保 CPU)
        if not _failure_notified:
            _failure_notified = True
            if FAILURE_PUSH_ENABLED and _try_mark_failure_pushed():  # 跨进程去重: 只首个进程推「网络故障」
                _alert("故障")
        if SELF_HEAL_ENABLED:  # 2026-11-24 用户暂停自愈(机场节点问题非代码可修); 重开后这里会每次熔断重开都再试
            _self_heal()


def report_success():
    """连接成功 → 清计数 + 关熔断(代理已恢复) + 推「已恢复」(若之前推过故障)。"""
    global _first_fail_ts, _circuit_open_until, _failure_notified
    was_failing = _failure_notified
    _first_fail_ts = None
    _circuit_open_until = 0.0
    _failure_notified = False
    if was_failing:
        _clear_failure_pushed()  # 清跨进程去重标记, 下次故障可再推
        if FAILURE_PUSH_ENABLED:
            _alert("恢复")


def _alert(kind):
    try:
        from config.settings import send_dingtalk
        if kind == "故障":
            send_dingtalk(
                "⚠️ 网络/代理故障",
                "连接失败持续 120s, 已熔断停止重试(5分钟后自动恢复)。\n"
                "请检查 Shadowrocket 代理节点 / VPN 是否正常。",
                urgent=True,
            )
        else:
            send_dingtalk(
                "✅ 网络/代理已恢复",
                "连接恢复成功, 已关闭熔断, 恢复正常请求。",
                urgent=True,
            )
    except Exception:
        pass


def _self_heal():
    """2026-11-11 用户要求: 网络故障时自动重启 Shadowrocket 隧道自愈。

    后台跑 scripts/self_heal_network.py(自带 5min 文件锁冷却, 多进程只触发一次实际自愈)。
    不阻塞调用方(网络已熔断, 这里不能同步等)。
    2026-11-24 输出从 DEVNULL 改写日志, 换节点失败原因可查。
    """
    try:
        import subprocess
        import sys
        _log = open(_SELF_HEAL_LOG, "a", encoding="utf-8")
        try:
            subprocess.Popen(
                [sys.executable, str(_ROOT / "scripts" / "self_heal_network.py")],
                stdout=_log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            _log.close()  # 父进程关自己的句柄(子进程已继承 fd), 防长进程每 7min 漏一个 fd
    except Exception:
        pass

