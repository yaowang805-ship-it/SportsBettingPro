"""虚拟投注组合 — 自动跟踪推荐并模拟投注。

工作流程：
  1. auto_place_bets(rec_list) — 从推荐自动创建虚拟投注（幂等）
  2. settle_bet(bet_id, result) — 手动标记赢/输
  3. compute_portfolio() — 计算余额/ROI/胜率/权益曲线/CLV
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from src.dashboard.config import PRED_LOG_FILE

_PORTFOLIO_STATE_FILE = Path(PRED_LOG_FILE).parent / "virtual_portfolio.json"
_INITIAL_BALANCE = 10000.0


# ── CLV 辅助 ──

def _fetch_closing_odds(home_team: str, away_team: str, league: str) -> Optional[float]:
    """获取指定比赛当前的主胜赔率作为'收盘参考价'。

    Args:
        home_team: 主队名（英文）
        away_team: 客队名（英文）
        league: 联赛名（如 NBA, 英超）

    Returns:
        当前最优主胜赔率，或 None
    """
    try:
        from fetchers.odds_api import fetch_odds_api
    except Exception:
        return None

    sport_map = {
        "NBA": "basketball_nba",
        "英超": "soccer_epl",
        "西甲": "soccer_spain_la_liga",
        "德甲": "soccer_germany_bundesliga",
        "意甲": "soccer_italy_serie_a",
        "法甲": "soccer_france_ligue_one",
        "巴甲": "soccer_brazil_serie_a",
        "解放者杯": "soccer_copa_libertadores",
        "美职联": "soccer_usa_mls",
        "墨超": "soccer_mexico_liga_mx",
        "阿甲": "soccer_argentina_primera_division",
        "葡超": "soccer_portugal_primeira_liga",
        "荷甲": "soccer_netherlands_eredivisie",
        "比甲": "soccer_belgium_first_div",
        "土超": "soccer_turkey_super_league",
        "苏超": "soccer_scotland_premiership",
        "J联赛": "soccer_japan_j_league",
        "澳超": "soccer_australia_aleague",
        "德乙": "soccer_germany_bundesliga2",
        "法乙": "soccer_france_ligue_two",
        "英冠": "soccer_england_championship",
        "欧冠": "soccer_uefa_champions_league",
        "欧联": "soccer_uefa_europa_league",
        "NFL": "americanfootball_nfl",
    }
    sport_key = sport_map.get(league)
    if not sport_key:
        return None

    try:
        data = fetch_odds_api(sport_key, force=True)
    except Exception:
        return None

    if not data:
        return None

    ht = home_team.strip().lower()
    at = away_team.strip().lower()
    for match in data:
        api_home = match.get("home_team", "").strip().lower()
        api_away = match.get("away_team", "").strip().lower()
        if ht == api_home and at == api_away:
            best = None
            for bm in match.get("bookmakers", []):
                for market in bm.get("markets", []):
                    if market.get("key") != "h2h":
                        continue
                    for out in market.get("outcomes", []):
                        if out.get("name", "").strip().lower() == ht:
                            p = out.get("price")
                            if p and (best is None or p > best):
                                best = p
            return best
    return None


# ── 内部持久化 ──

def _load_state() -> dict:
    """加载已持久化的虚拟投注状态。"""
    if _PORTFOLIO_STATE_FILE.exists():
        try:
            return json.loads(_PORTFOLIO_STATE_FILE.read_text())
        except Exception:
            pass
    return {"settled": {}, "pending_bets": [], "balance": _INITIAL_BALANCE, "history": []}


def _save_state(state: dict):
    """保存虚拟投注状态。"""
    _PORTFOLIO_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _make_bet_id(rec: dict) -> str:
    """为推荐生成唯一投注 ID。"""
    sport = rec.get("sport", "unknown")
    league = rec.get("league", "unknown")
    home = rec.get("home_cn", rec.get("home_team", ""))
    away = rec.get("away_cn", rec.get("away_team", ""))
    market = rec.get("market", rec.get("market_type", ""))
    return f"{sport}_{league}_{home}_{away}_{market}" \
        .replace(" ", "_").replace(".", "")[:80]


# ── 公开 API ──

def auto_place_bets(rec_list: list):
    """自动将推荐同步为虚拟投注（幂等：已存在的不会重复添加）。

    Args:
        rec_list: daily_recommendations.json 中的推荐列表
    """
    state = _load_state()
    pending = state.get("pending_bets", [])
    pending_ids = {b.get("id", "") for b in pending}
    settled_ids = set(state.get("settled", {}).keys())
    history_ids = {h.get("id", "") for h in state.get("history", [])}
    existing_ids = pending_ids | settled_ids | history_ids

    added = 0
    for rec in rec_list:
        bid = _make_bet_id(rec)
        if bid in existing_ids:
            continue
        odds = float(rec.get("odds", 0))
        league = rec.get("league", "")
        home_team_en = rec.get("home_team", "")
        away_team_en = rec.get("away_team", "")
        # 获取当前赔率作为"开盘参考价"
        opening_odds = _fetch_closing_odds(home_team_en, away_team_en, league)
        pending.append({
            "id": bid,
            "sport": rec.get("sport", ""),
            "league": league,
            "home_cn": rec.get("home_cn", home_team_en),
            "away_cn": rec.get("away_cn", away_team_en),
            "home_team": home_team_en,
            "away_team": away_team_en,
            "market_type": rec.get("market", rec.get("market_type", "")),
            "market_detail": rec.get("market", rec.get("market_type", "")),
            "odds": odds,
            "opening_odds": opening_odds or odds,
            "closing_odds": None,
            "stake": float(rec.get("stake", 0)),
            "model_prob": float(rec.get("model_prob", 0)),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        existing_ids.add(bid)
        added += 1

    if added > 0:
        state["pending_bets"] = pending
        _save_state(state)


def load_portfolio_state() -> dict:
    """加载虚拟投注状态（公开接口）。"""
    return _load_state()


def reset_portfolio():
    """重置虚拟投注组合到初始状态。"""
    _save_state({"settled": {}, "pending_bets": [], "balance": _INITIAL_BALANCE, "history": []})


def settle_bet(bet_id: str, result: str, stake: float, odds: float) -> dict:
    """手动结算一笔虚拟投注（保留 CLV 数据）。

    Args:
        bet_id: 投注ID
        result: "won" 或 "lost"
        stake: 投注金额
        odds: 赔率

    Returns:
        更新后的 portfolio 状态
    """
    state = _load_state()
    state["settled"][bet_id] = result

    # 从 pending 中移除，保留 CLV 数据
    pending_bet = None
    remaining = []
    for b in state.get("pending_bets", []):
        if b.get("id") == bet_id:
            pending_bet = b
        else:
            remaining.append(b)
    state["pending_bets"] = remaining

    profit = stake * (odds - 1) if result == "won" else -stake
    state["balance"] += profit

    clv_val = None
    if pending_bet and pending_bet.get("clv") is not None:
        clv_val = pending_bet["clv"]

    entry = {
        "id": bet_id,
        "match": f"{stake:.0f}¥ @ {odds:.2f}",
        "date": datetime.now(timezone.utc).isoformat(),
        "stake": stake,
        "odds": odds,
        "profit": round(profit, 2),
        "status": result,
    }
    if clv_val is not None:
        entry["clv"] = clv_val

    state["history"].append(entry)
    _save_state(state)
    return state


def update_clv_for_pending():
    """遍历待结算投注，尝试获取当前赔率作为收盘价并计算 CLV。"""
    state = _load_state()
    pending = state.get("pending_bets", [])
    if not pending:
        return 0

    updated = 0
    for bet in pending:
        if bet.get("closing_odds") is not None:
            continue  # 已计算过
        league = bet.get("league", "")
        home_en = bet.get("home_team", "")
        away_en = bet.get("away_team", "")
        placed_odds = bet.get("odds", 0)
        if not home_en or not away_en or not league or placed_odds <= 0:
            continue
        current_odds = _fetch_closing_odds(home_en, away_en, league)
        if current_odds and current_odds > 0:
            bet["closing_odds"] = round(current_odds, 4)
            bet["clv"] = round((placed_odds - current_odds) / current_odds, 6)
            updated += 1

    if updated > 0:
        state["pending_bets"] = pending
        _save_state(state)
    return updated


def compute_portfolio(pred_df: Optional[pd.DataFrame] = None) -> dict:
    """计算虚拟投注组合的全部指标。

    包含 CLV（收盘价价值）统计 — 职业博彩第一指标。

    Args:
        pred_df: （可选）prediction_log.csv 的 DataFrame，用于补充数据

    Returns:
        {balance, total_bets, pending_count, total_settled,
         win_rate, total_roi, total_profit, history,
         equity_curve, pending_bets, clv_metrics}
    """
    state = _load_state()
    balance = state.get("balance", _INITIAL_BALANCE)
    settled_overrides = state.get("settled", {})
    pending_bets = state.get("pending_bets", [])
    history = state.get("history", [])

    total_stake = 0.0
    total_profit = 0.0
    win_count = 0
    loss_count = 0
    equity_points = []
    running_balance = _INITIAL_BALANCE

    # 1. 从 history 重建权益曲线
    for h in history:
        profit = h.get("profit", 0)
        running_balance += profit
        total_profit += profit
        if h.get("status") == "won":
            win_count += 1
        else:
            loss_count += 1
        equity_points.append({
            "date": h.get("date", datetime.now().isoformat()),
            "balance": round(running_balance, 2),
        })

    # 2. pred_df 中补充统计（未在 history 中的 pending 记录）
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

    # CLV 统计
    clv_values = []
    for bet in pending_bets:
        clv = bet.get("clv")
        if clv is not None:
            clv_values.append(clv)
    for h in history:
        clv = h.get("clv")
        if clv is not None:
            clv_values.append(clv)
    clv_series = pd.Series(clv_values) if clv_values else pd.Series()
    clv_metrics = {
        "avg_clv": round(float(clv_series.mean()), 4) if len(clv_values) > 0 else None,
        "positive_clv": int((clv_series > 0).sum()) if len(clv_values) > 0 else 0,
        "negative_clv": int((clv_series <= 0).sum()) if len(clv_values) > 0 else 0,
        "total_with_clv": len(clv_values),
        "best_clv": round(float(clv_series.max()), 4) if len(clv_values) > 0 else None,
        "worst_clv": round(float(clv_series.min()), 4) if len(clv_values) > 0 else None,
    }

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
        "clv_metrics": clv_metrics,
    }
