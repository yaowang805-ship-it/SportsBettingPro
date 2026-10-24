"""网络/代理熔断器 (2026-10-24)。

背景: 代理(Shadowrocket)挂掉时, BB/Pin 域名全连不上, 投注进程(second_level_monitor /
pin_ws_trigger)会无限重试烧 CPU(实测双进程 127%+87%) + 日志爆炸(949MB)。

设计: 连续 THRESHOLD 次「连接级失败」(ProxyError/SSL/ConnectionError/HTTP503) → 开熔断
+ 推钉钉(一次)。熔断期内 is_open()=True, 调用方应停止重试。冷却 COOLDOWN_SECONDS 后
自动关闭(允许重试, 若仍失败会再次计数→再次通知)。

连接成功 report_success() → 立即关闭熔断 + 清计数(代理恢复)。

进程内单例状态: second_level_monitor 和 pin_ws_trigger 是两个独立进程, 各自独立熔断。
"""
import time

THRESHOLD = 3                 # 连续连接失败 3 次触发(用户定)
COOLDOWN_SECONDS = 15 * 60    # 熔断冷却 15 分钟, 之后自动重试

_fail_count = 0
_circuit_open_until = 0.0
_notified = False


def _now():
    return time.time()


def is_open():
    """熔断是否打开(调用方应停止重试)。冷却到期自动关闭。"""
    global _circuit_open_until, _fail_count, _notified
    if _circuit_open_until and _now() >= _circuit_open_until:
        _circuit_open_until = 0.0
        _fail_count = 0
        _notified = False
    return _circuit_open_until > 0


def report_failure():
    """上报一次连接级失败。达 THRESHOLD 次 → 开熔断 + 推钉钉(一次)。"""
    global _fail_count, _circuit_open_until, _notified
    if _circuit_open_until:
        return  # 已在熔断中, 不重复计数
    _fail_count += 1
    if _fail_count >= THRESHOLD and not _notified:
        _notified = True
        _circuit_open_until = _now() + COOLDOWN_SECONDS
        _fail_count = 0
        _alert()


def report_success():
    """连接成功 → 清计数 + 关熔断(代理已恢复)。"""
    global _fail_count, _circuit_open_until, _notified
    _fail_count = 0
    _circuit_open_until = 0.0
    _notified = False


def _alert():
    try:
        from config.settings import send_dingtalk
        send_dingtalk(
            "⚠️ 网络/代理故障",
            "连续连接失败≥3次, 已熔断停止重试(15分钟后自动恢复)。\n"
            "请检查 Shadowrocket 代理节点 / VPN 是否正常。",
            urgent=True,
        )
    except Exception:
        pass
