"""real_bets.json 跨线程写锁(2026-10-24)。

早盘触发(Drop/WS)拆到独立循环 _early_trigger_loop 后, 与滚球下单(_append_real_bet)、
结算(_settle_real_bet)并发写 real_bets.json(read-modify-write), 无锁会丢单(后写覆盖先写)。
同一个进程内用 threading.Lock 串行化三处写盘。
"""
import threading

REAL_BETS_LOCK = threading.Lock()
