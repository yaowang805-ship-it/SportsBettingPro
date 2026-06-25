"""Live Line Shopping 投注执行器 — 将 +EV 机会转为虚拟投注。

流程:
  line_shopping_results.json
    → 过滤 edge >= 3% 的机会
    → 按 EV 排序，Kelly 计算比例
    → 归一化到每日 ¥10,000 预算
    → auto_place_bets → virtual_portfolio.json
"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

from config.logging_config import get_logger
from config.settings import DATA_DIR, DEFAULT_BUDGET

logger = get_logger(__name__)

LS_FILE = DATA_DIR / "line_shopping_results.json"
DAILY_BUDGET = float(DEFAULT_BUDGET)  # 10000
MAX_PER_BET_PCT = 0.20   # 单注上限 20%
MAX_PER_MATCH_PCT = 0.35  # 单场比赛总暴露上限 35%
MIN_EDGE = 0.03
KELLY_FRACTION = 0.25    # 1/4 Kelly 保守策略
MAX_ODDS = 10.0          # 高赔率过滤（>10的赔率模型概率不可靠）
MAX_PER_MATCH_BETS = 2   # 同一比赛最多下注方向数
SCAN_BUDGET_PCT = 0.30   # 每次扫描最多花剩余预算的 30%

# 两段式投注：扫描发现机会后，只在临近比赛时再投注
# 72h 扫到 → 等 → 进入 MAX_HOURS_AHEAD 窗口 → 重新验证 → 投注
MAX_HOURS_AHEAD = 30     # 超过此小时数的比赛不投注（留给后续验证）

# 备份配置
BACKUP_DIR = DATA_DIR / "backups" / "virtual_portfolio"
BACKUP_KEEP = 30


def _backup_vp(vp_file: Path):
    """备份 virtual_portfolio.json，保留最近 BACKUP_KEEP 份。"""
    if not vp_file.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(vp_file, BACKUP_DIR / f"virtual_portfolio_{ts}.json")
    # 清理旧备份
    backups = sorted(BACKUP_DIR.glob("virtual_portfolio_*.json"), reverse=True)
    for old in backups[BACKUP_KEEP:]:
        old.unlink()


def place_line_shops(daily_budget: Optional[float] = None) -> int:
    """读取 Line Shopping 结果，将符合条件的 +EV 机会写入虚拟投注。

    分配策略:
      1. 按 EV 从高到低排序
      2. 计算每笔的 Kelly 比例
      3. 归一化到 daily_budget，确保 total = daily_budget
      4. 单注上限 20% of daily_budget

    Args:
        daily_budget: 当日总预算，默认 DAILY_BUDGET (¥10,000)

    Returns:
        新增的投注数量
    """
    budget = daily_budget or DAILY_BUDGET

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

    # 校准：获取 edge 折扣系数（偏差大的联赛/市场打折处理）
    try:
        from src.risk.calibration import BetCalibrator
        calibrator = BetCalibrator()
        cal_report = calibrator.analyze()
        if cal_report.get("status") == "ok" and cal_report.get("flagged"):
            discounted = 0
            for opp in opportunities:
                league = opp.get("league", "")
                market = opp.get("market", "1x2")
                adj = calibrator.get_edge_adjustment(league, market)
                if adj < 1.0:
                    opp["_edge_pct"] = opp.get("edge_pct", 0) * adj
                    opp["_ev"] = opp.get("_ev", 0) * adj
                    discounted += 1
            if discounted:
                logger.info("  校准打折: %d 条机会 edge 已折扣（偏差联赛/市场）", discounted)
    except Exception:
        pass

    # 读取已存在的投注 ID，避免重复
    vp_file = DATA_DIR / "virtual_portfolio.json"
    existing_ids = set()
    already_allocated_today = 0.0
    today_str = datetime.now().strftime("%Y-%m-%d")
    if vp_file.exists():
        try:
            vp = json.loads(vp_file.read_text())
            for h in vp.get("history", []):
                existing_ids.add(h.get("id", ""))
            for b in vp.get("pending_bets", []):
                existing_ids.add(b.get("id", ""))
                # 统计今日已分配金额
                ct = b.get("created_at", "")
                if today_str in ct:
                    already_allocated_today += b.get("stake", 0)
            for k in vp.get("settled", {}).keys():
                existing_ids.add(k)
        except Exception:
            pass

    # 今日剩余预算
    remaining_budget = max(0, budget - already_allocated_today)
    if remaining_budget < 100:
        logger.info("  ⏭️ 今日预算已用完（¥%.0f / ¥%.0f）", already_allocated_today, budget)
        return 0

    # 每次扫描最多花剩余预算的 SCAN_BUDGET_PCT，留子弹给后面的机会
    scan_budget = round(remaining_budget * SCAN_BUDGET_PCT, 0)
    scan_budget = max(scan_budget, 100)  # 至少留¥100（给小额机会）
    scan_budget = min(scan_budget, remaining_budget)
    logger.info("  今日已分配 ¥%.0f / ¥%.0f，本次扫描预算 ¥%.0f（剩余 ¥%.0f 留后续）",
                already_allocated_today, budget, scan_budget, remaining_budget - scan_budget)
    budget = scan_budget

    # ── 按 EV 降序排列 ──
    opportunities.sort(key=lambda x: x.get("_ev", 0), reverse=True)

    # ── 第一遍：计算 Kelly，筛选有效机会 ──
    candidates = []
    skipped_far = 0  # 统计因太远跳过
    for opp in opportunities:
        ev = opp.get("_ev", 0)
        if ev < MIN_EDGE:
            continue

        odds = opp.get("odds", 0)
        model_prob = opp.get("model_prob", 0)
        if odds <= 1 or model_prob <= 0:
            continue

        # 两段式过滤：超过 MAX_HOURS_AHEAD 的比赛不投注，等下次扫描
        ct = opp.get("commence_time", "")
        if ct:
            try:
                dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                hours_until = (dt - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_until > MAX_HOURS_AHEAD:
                    skipped_far += 1
                    continue
            except Exception:
                pass

        b = odds - 1.0
        kelly = (model_prob * b - (1.0 - model_prob)) / b if b > 0 else 0
        if kelly <= 0:
            continue

        home = opp.get("home_team", "")
        away = opp.get("away_team", "")
        outcome = opp.get("outcome", "")
        bid = f"line_shop_{home}_{away}_{outcome}".replace(" ", "_")[:80]
        if bid in existing_ids:
            continue

        candidates.append({
            "opp": opp,
            "bid": bid,
            "kelly": kelly,
            "odds": odds,
            "model_prob": model_prob,
            "home": home,
            "away": away,
            "outcome": outcome,
        })

    if skipped_far:
        logger.info("  ⏳ %d 个机会距离开赛 > %d 小时，两段式等待中（暂不投注）",
                    skipped_far, MAX_HOURS_AHEAD)

    if not candidates:
        if skipped_far:
            logger.info("  ⏭️ 全部 %d 个机会因距离开赛超过 %d 小时跳过（两段式等待中）",
                        skipped_far, MAX_HOURS_AHEAD)
        else:
            logger.info("  ⏭️ 所有机会已存在或无满足条件的机会")
        return 0

    # ── 优化过滤 ──
    before = len(candidates)

    # 1) 过滤高赔率（>10倍模型概率不可靠）
    candidates = [c for c in candidates if c["odds"] <= MAX_ODDS]
    filtered_odds = before - len(candidates)

    # 2) 同一比赛最多 MAX_PER_MATCH_BETS 个方向（按 EV 取top）
    match_groups = {}
    for c in candidates:
        key = f"{c['home']}_{c['away']}"
        match_groups.setdefault(key, []).append(c)
    candidates = []
    for key, group in match_groups.items():
        # 组内已按 EV 降序（外层已排序）
        candidates.extend(group[:MAX_PER_MATCH_BETS])
    filtered_match = sum(len(g) - MAX_PER_MATCH_BETS for g in match_groups.values()
                          if len(g) > MAX_PER_MATCH_BETS)

    if filtered_odds or filtered_match:
        logger.info("  优化过滤: 高赔率 %d 条, 同比赛超额 %d 条", filtered_odds, filtered_match)

    if not candidates:
        logger.info("  ⏭️ 过滤后无候选")
        return 0

    # ── 第二遍：归一化到 daily_budget ──
    raw_stakes = [min(c["kelly"] * KELLY_FRACTION, MAX_PER_BET_PCT) for c in candidates]
    total_raw = sum(raw_stakes)

    bet_list = []
    total_allocated = 0.0
    for i, c in enumerate(candidates):
        if total_raw > 0:
            stake = round(budget * raw_stakes[i] / total_raw, 2)
        else:
            stake = round(budget / len(candidates), 2)

        # 单注硬上限
        max_stake = budget * MAX_PER_BET_PCT
        stake = min(stake, max_stake)
        if stake <= 0:
            continue

        total_allocated += stake
        bet_list.append({
            "id": c["bid"],
            "sport": c["opp"].get("sport", "football"),
            "league": c["opp"].get("league", ""),
            "home_team": c["home"],
            "away_team": c["away"],
            "home_cn": c["home"],
            "away_cn": c["away"],
            "market_type": c["outcome"],
            "odds": c["odds"],
            "stake": stake,
            "model_prob": c["model_prob"],
            "commence_time": c["opp"].get("commence_time", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

    if not bet_list:
        return 0

    # ── 单场比赛总暴露上限 ──
    match_exposure = {}
    for b in bet_list:
        key = f"{b['home_team']}_{b['away_team']}"
        match_exposure.setdefault(key, 0)
        match_exposure[key] += b["stake"]
    max_per_match = DAILY_BUDGET * MAX_PER_MATCH_PCT
    for b in bet_list:
        key = f"{b['home_team']}_{b['away_team']}"
        if match_exposure[key] > max_per_match:
            ratio = max_per_match / match_exposure[key]
            b["stake"] = round(b["stake"] * ratio, 2)
    # 修正后重新汇总
    total_allocated = sum(b["stake"] for b in bet_list)

    # ── 写入虚拟组合（直接写入，绕过 auto_place_bets 的 30% 上限） ──
    vp_file = DATA_DIR / "virtual_portfolio.json"
    _backup_vp(vp_file)
    added_count = _direct_write(bet_list, vp_file)

    logger.info("  ✅ 虚拟投注: %d 条新增 / %d 条候选 | 总 ¥%.0f / 日预算 ¥%.0f",
                added_count, len(bet_list), total_allocated, budget)
    for b in bet_list:
        ev_pct = (b["model_prob"] - 1.0 / b["odds"]) / (1.0 / b["odds"]) * 100
        logger.info("    %s vs %s [%s] odds=%.2f stake=¥%.0f edge=%.1f%%",
                    b["home_team"], b["away_team"], b["market_type"],
                    b["odds"], b["stake"], ev_pct)

    return added_count


def _make_store_id(rec: dict) -> str:
    """生成与 virtual_portfolio._make_bet_id 一致的 ID。"""
    sport = rec.get("sport", "unknown")
    league = rec.get("league", "unknown")
    home = rec.get("home_cn", rec.get("home_team", ""))
    away = rec.get("away_cn", rec.get("away_team", ""))
    market = rec.get("market_type", "")
    return f"{sport}_{league}_{home}_{away}_{market}" \
        .replace(" ", "_").replace(".", "")[:80]


def _direct_write(bet_list: List[Dict], vp_file: Path):
    """直接追加到 virtual_portfolio.json（绕过 auto_place_bets 的 30% 上限）。"""
    state = {"settled": {}, "pending_bets": [], "balance": DAILY_BUDGET, "history": []}
    if vp_file.exists():
        try:
            state = json.loads(vp_file.read_text())
        except Exception:
            state = {"settled": {}, "pending_bets": [], "balance": DAILY_BUDGET, "history": []}

    pending = state.get("pending_bets", [])
    history = state.get("history", [])
    settled = state.get("settled", {})

    # 收集已存在的 ID（用 _make_store_id 格式匹配）
    existing_ids = set()
    for b in pending:
        existing_ids.add(_make_store_id(b))
    for h in history:
        existing_ids.add(_make_store_id(h))
    existing_ids.update(settled.keys())

    added = 0
    for b in bet_list:
        bid = _make_store_id(b)
        if bid in existing_ids:
            continue
        pending.append(b)
        existing_ids.add(bid)
        added += 1

    state["pending_bets"] = pending
    vp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    if added:
        logger.info("  实际新增 %d 条投注（%d 条已存在跳过）", added, len(bet_list) - added)
    return added
