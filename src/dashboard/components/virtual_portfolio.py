"""虚拟投注组合 — 自动跟踪推荐并模拟投注。

工作流程：
  1. auto_place_bets(rec_list) — 从推荐自动创建虚拟投注（幂等）
  2. settle_bet(bet_id, result) — 手动标记赢/输
  3. compute_portfolio() — 计算余额/ROI/胜率/权益曲线
"""
import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from src.dashboard.config import PRED_LOG_FILE

_PORTFOLIO_STATE_FILE = Path(PRED_LOG_FILE).parent / "virtual_portfolio.json"
_INITIAL_BALANCE = 10000.0


# ── 跨进程文件锁 ──

_LOCK_FD = None


@contextmanager
def _portfolio_lock():
    """独占锁：与 bb_virtual_bet.py 共享同一锁文件。"""
    global _LOCK_FD
    lock_path = _PORTFOLIO_STATE_FILE.with_suffix(_PORTFOLIO_STATE_FILE.suffix + ".lck")
    fd = open(lock_path, "w")
    _LOCK_FD = fd
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()
        _LOCK_FD = None


# ── 内部持久化 ──

def _load_state() -> dict:
    """委托给虚拟投注引擎的规范加载器。"""
    from src.betting.bb_virtual_bet import _load_portfolio as _real_load
    return _real_load()


def _save_state(state: dict):
    _PORTFOLIO_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _make_bet_id(rec: dict) -> str:
    sport = rec.get("sport", "unknown")
    league = rec.get("league", "unknown")
    home = rec.get("home_cn", rec.get("home_team", ""))
    away = rec.get("away_cn", rec.get("away_team", ""))
    market = rec.get("market", rec.get("market_type", ""))
    return f"{sport}_{league}_{home}_{away}_{market}" \
        .replace(" ", "_").replace(".", "")[:80]


# ── 公开 API ──

def update_clv_for_pending():
    """V5 placeholder: CLV tracking moved to src/monitor/clv_collector."""
    return 0

def auto_place_bets(rec_list: list, reset_pending: bool = False):
    with _portfolio_lock():
        state = _load_state()
        if reset_pending:
            state["pending_bets"] = []
        pending = state.get("pending_bets", [])
        balance = state.get("balance", _INITIAL_BALANCE)
        pending_ids = {b.get("id", "") for b in pending}
        settled_ids = set(state.get("settled", {}).keys())
        history_ids = {h.get("id", "") for h in state.get("history", [])}
        existing_ids = pending_ids | settled_ids | history_ids

        total_pending_stake = sum(b.get("stake", 0) for b in pending)
        max_total_exposure = balance * 0.30

        added = 0
        for rec in rec_list:
            bid = _make_bet_id(rec)
            if bid in existing_ids:
                continue
            stake = float(rec.get("stake", 0))
            if total_pending_stake + stake > max_total_exposure:
                continue
            odds = float(rec.get("odds", 0))
            league = rec.get("league", "")
            home_team_en = rec.get("home_team", "")
            away_team_en = rec.get("away_team", "")
            # market_type 必须存具体结果（如 "客胜"、"让球主胜(-0.5/1)"），
            # 而非通用市场类别（"1x2"、"handicap"）。
            # 优先级：designation > market > market_type
            specific_outcome = (
                rec.get("designation")
                or rec.get("market")
                or rec.get("market_type", "")
            )
            pending.append({
                "id": bid,
                "sport": rec.get("sport", ""),
                "league": league,
                "home_cn": rec.get("home_cn", home_team_en),
                "away_cn": rec.get("away_cn", away_team_en),
                "home_team": home_team_en,
                "away_team": away_team_en,
                "market_type": specific_outcome,
                "market_detail": specific_outcome,
                "odds": odds,
                "stake": stake,
                "model_prob": float(rec.get("model_prob", 0)),
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            existing_ids.add(bid)
            total_pending_stake += stake
            added += 1

        if added > 0:
            state["pending_bets"] = pending
            _save_state(state)


def load_portfolio_state() -> dict:
    return _load_state()


def reset_portfolio():
    with _portfolio_lock():
        _save_state({"settled": {}, "pending_bets": [], "balance": _INITIAL_BALANCE, "history": []})


def settle_bet(bet_id: str, result: str, stake: float, odds: float) -> dict:
    with _portfolio_lock():
        state = _load_state()
        state["settled"][bet_id] = result

        # 从待结算中找到这场比赛信息
        pending_bet = None
        remaining = []
        for b in state.get("pending_bets", []):
            if b.get("id") == bet_id:
                pending_bet = b
            else:
                remaining.append(b)
        state["pending_bets"] = remaining

        if result == "won":
            profit = stake * (odds - 1)
            # V4.4: 区分 track_only (本金未扣) vs 正常投注 (本金已扣)
            if pending_bet and pending_bet.get("track_only"):
                state["balance"] += profit  # 只加利润，本金从未扣除
            else:
                state["balance"] += stake + profit  # 本金已在投注时扣除，结算归还本金+利润
        elif result == "push":
            profit = 0.0
            # V4.4: track_only 本金从未扣除，无需归还
            if not (pending_bet and pending_bet.get("track_only")):
                state["balance"] += stake  # 走水：本金已在投注时扣除，结算归还本金
        else:
            profit = -stake
            # V4.4: track_only 本金从未扣除，亏损需从余额扣除
            if pending_bet and pending_bet.get("track_only"):
                state["balance"] -= stake
            # 正常投注：本金已在投注时扣除且无法收回，余额不做调整

        entry = {
            "id": bet_id,
            "match": f"{stake:.0f}¥ @ {odds:.2f}",
            "date": datetime.now(timezone.utc).isoformat(),
            "stake": stake,
            "odds": odds,
            "profit": round(profit, 2),
            "result": result,
            "status": result,
            "source": "bb_vs_pinnacle",
        }
        # 从待结算记录提取详细信息
        if pending_bet:
            entry["home_cn"] = pending_bet.get("home_cn", "")
            entry["away_cn"] = pending_bet.get("away_cn", "")
            entry["market_type"] = pending_bet.get("market", pending_bet.get("market_type", ""))
            entry["league"] = pending_bet.get("league", "")
            entry["sport"] = pending_bet.get("sport", "")

        state["history"].append(entry)

        # 余额校验：每次结算后检查是否有漂移，自动修正
        _validate_balance(state)

        _save_state(state)
        return state


def _validate_balance(state: dict):
    """结算后验证余额一致性，漂移超过 ¥1 时自动修正。"""
    from src.core.balance_recalc import recalculate_balance
    stored = state.get("balance", 0)
    calc = recalculate_balance(state)
    drift = stored - calc
    if abs(drift) > 1.0:
        import logging
        logger = logging.getLogger(__name__)
        logger.warning("⚠️ 余额漂移 ¥%+.2f，自动修正: stored=%.0f → calc=%.0f", drift, stored, calc)
        state["balance"] = calc


def compute_portfolio(pred_df: Optional[pd.DataFrame] = None) -> dict:
    state = _load_state()
    balance = state.get("balance", _INITIAL_BALANCE)
    settled_overrides = state.get("settled", {})
    pending_bets = state.get("pending_bets", [])
    history = state.get("history", [])

    total_profit = 0.0
    win_count = 0
    loss_count = 0
    equity_points = []
    running_balance = _INITIAL_BALANCE

    for h in history:
        profit = h.get("profit", 0)
        running_balance += profit
        total_profit += profit
        if h.get("status") == "won":
            win_count += 1
        elif h.get("status") in ("lost", "loss"):
            loss_count += 1
        # "push" 不记输赢
        equity_points.append({
            "date": h.get("date", datetime.now().isoformat()),
            "balance": round(running_balance, 2),
        })

    total_bets_from_pred = 0
    if pred_df is not None and not pred_df.empty:
        total_bets_from_pred = len(pred_df)
        for _, row in pred_df.iterrows():
            bid = str(row.get("id", ""))
            stake = row.get("stake", 0)
            odds = row.get("odds", 1.0)
            status = str(row.get("status", "pending")).strip().lower()
            timestamp = row.get("timestamp", row.get("date", ""))

            if bid in settled_overrides:
                effective_status = settled_overrides[bid]
            else:
                effective_status = status

            if pd.isna(stake) or stake == 0:
                continue

            if effective_status == "won" and bid not in {h.get("id") for h in history}:
                profit = stake * (odds - 1)
                total_profit += profit
                running_balance += profit
                win_count += 1
                equity_points.append({
                    "date": str(timestamp) if timestamp else datetime.now().isoformat(),
                    "balance": round(running_balance, 2),
                })
            elif effective_status == "lost" and bid not in {h.get("id") for h in history}:
                total_profit -= stake
                running_balance -= stake
                loss_count += 1
                equity_points.append({
                    "date": str(timestamp) if timestamp else datetime.now().isoformat(),
                    "balance": round(running_balance, 2),
                })

    total_settled = win_count + loss_count
    win_rate = win_count / total_settled if total_settled > 0 else 0.0
    total_roi = total_profit / _INITIAL_BALANCE if total_settled > 0 else 0.0

    return {
        "balance": round(balance, 2),
        "total_bets": total_bets_from_pred + len(pending_bets) + len(history),
        "pending_count": len(pending_bets),
        "pending_bets": pending_bets,
        "total_settled": total_settled,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": win_rate,
        "total_roi": total_roi,
        "total_profit": round(total_profit, 2),
        "history": history,
        "equity_curve": equity_points,
    }
