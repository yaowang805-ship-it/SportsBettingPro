"""Live Line Shopping 投注执行器 — 将 +EV 机会转为虚拟投注。

流程:
  line_shopping_results.json
    → 过滤 edge >= 3% 的机会
    → 计算 Kelly 仓位 (fraction=0.07)
    → auto_place_bets → virtual_portfolio.json
"""
import json
from pathlib import Path
from typing import List, Dict

from config.logging_config import get_logger
from config.settings import DATA_DIR, DEFAULT_BUDGET

logger = get_logger(__name__)

LS_FILE = DATA_DIR / "line_shopping_results.json"
BANKROLL = float(DEFAULT_BUDGET)
KELLY_FRACTION = 0.07  # 与回放引擎一致
MIN_EDGE = 0.03


def place_line_shops() -> int:
    """读取 Line Shopping 结果，将符合条件的 +EV 机会写入虚拟投注。

    Returns:
        新增的投注数量
    """
    if not LS_FILE.exists():
        logger.info("  ⏭️ 无 Line Shopping 结果文件")
        return 0

    try:
        data = json.loads(LS_FILE.read_text())
    except Exception as e:
        logger.warning("  ⚠️ 读取 Line Shopping 结果失败: %s", e)
        return 0

    opportunities = data.get("opportunities", [])
    if not opportunities:
        logger.info("  ⏭️ 无 Line Shopping 机会")
        return 0

    # 从虚拟组合获取当前余额
    vp_file = DATA_DIR / "virtual_portfolio.json"
    current_balance = BANKROLL
    if vp_file.exists():
        try:
            vp = json.loads(vp_file.read_text())
            current_balance = float(vp.get("balance", BANKROLL))
        except Exception:
            pass

    # 读取已存在的投注 ID，避免重复
    existing_ids = set()
    if vp_file.exists():
        try:
            vp = json.loads(vp_file.read_text())
            for h in vp.get("history", []):
                existing_ids.add(h.get("id", ""))
            for b in vp.get("pending_bets", []):
                existing_ids.add(b.get("id", ""))
            for k in vp.get("settled", {}).keys():
                existing_ids.add(k)
        except Exception:
            pass

    bet_list = []
    for opp in opportunities:
        ev = opp.get("_ev", 0)
        if ev < MIN_EDGE:
            continue

        home = opp.get("home_team", "")
        away = opp.get("away_team", "")
        outcome = opp.get("outcome", "")
        odds = opp.get("odds", 0)
        model_prob = opp.get("model_prob", 0)
        league = opp.get("league", "")
        commence_time = opp.get("commence_time", "")

        # 计算 Kelly 仓位
        b = odds - 1.0
        kelly = (model_prob * b - (1.0 - model_prob)) / b if b > 0 else 0
        stake_pct = min(kelly * KELLY_FRACTION, 0.05)
        if stake_pct <= 0:
            continue
        stake = round(current_balance * stake_pct, 2)
        if stake <= 0:
            continue

        # 生成唯一 ID（与 virtual_portfolio._make_bet_id 一致）
        bid = f"line_shop_{home}_{away}_{outcome}".replace(" ", "_")[:80]
        if bid in existing_ids:
            continue

        bet_list.append({
            "id": bid,
            "sport": opp.get("sport", "football"),
            "league": league,
            "home_team": home,
            "away_team": away,
            "home_cn": home,
            "away_cn": away,
            "market": "line_shopping",
            "market_type": "line_shopping",
            "odds": odds,
            "stake": stake,
            "model_prob": model_prob,
            "commence_time": commence_time,
            "created_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(),
        })

    if not bet_list:
        logger.info("  ⏭️ 所有机会已存在或无满足条件的机会")
        return 0

    # 写入虚拟组合
    try:
        from src.dashboard.components.virtual_portfolio import auto_place_bets
        auto_place_bets(bet_list, reset_pending=False)
    except Exception as e:
        logger.warning("  ⚠️ auto_place_bets 失败: %s，直接写入", e)
        # 降级：直接写入 virtual_portfolio.json
        _direct_write(bet_list, vp_file)

    logger.info("  ✅ Line Shopping 投注已执行: %d 条", len(bet_list))
    for b in bet_list:
        logger.info("    %s vs %s [%s] odds=%.2f stake=¥%.0f edge=%.1f%%",
                    b["home_team"], b["away_team"], b["market"],
                    b["odds"], b["stake"],
                    (b.get("model_prob", 0) - 1.0 / b["odds"]) / (1.0 / b["odds"]) * 100
                    if b["odds"] > 0 else 0)

    return len(bet_list)


def _direct_write(bet_list: List[Dict], vp_file: Path):
    """降级方案：直接追加到 virtual_portfolio.json。"""
    import datetime
    state = {"settled": {}, "pending_bets": [], "balance": BANKROLL, "history": []}
    if vp_file.exists():
        try:
            state = json.loads(vp_file.read_text())
        except Exception:
            state = {"settled": {}, "pending_bets": [], "balance": BANKROLL, "history": []}

    pending = state.get("pending_bets", [])
    existing_ids = set()
    for h in state.get("history", []):
        existing_ids.add(h.get("id", ""))
    for b in pending:
        existing_ids.add(b.get("id", ""))

    for b in bet_list:
        if b["id"] in existing_ids:
            continue
        pending.append(b)

    state["pending_bets"] = pending
    vp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
