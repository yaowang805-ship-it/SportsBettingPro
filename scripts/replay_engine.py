#!/usr/bin/env python3
"""历史回放引擎 — 在历史数据上跑完整下注→结算流水线，加速样本积累。

空窗期 PaperTrader 样本量不足（28 笔），回放引擎在 fb_odds_raw.csv
（79K 行，2013~2025 历史足球赔率+赛果）上逐日模拟真实投注流程。

策略: Line Shopping — Pinnacle (sharp) 去 vig 概率 vs 零售最佳赔率。
"""
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from config.logging_config import setup_logging, get_logger
from config.settings import DATA_DIR, DEFAULT_BUDGET

setup_logging()
logger = get_logger("replay_engine")

# ── 零售博彩公司赔率列（football-data.co.uk 格式） ──
RETAIL_BOOKS = {
    'B365': ('B365H', 'B365D', 'B365A'),
    'BW':   ('BWH', 'BWD', 'BWA'),
    'IW':   ('IWH', 'IWD', 'IWA'),
    'LB':   ('LBH', 'LBD', 'LBA'),
    'WH':   ('WHH', 'WHD', 'WHA'),
    'SJ':   ('SJH', 'SJD', 'SJA'),
    'VC':   ('VCH', 'VCD', 'VCA'),
}

HISTORY_FILE = ROOT / "data" / "raw" / "fb_odds_raw.csv"
BET_LOG_FILE = DATA_DIR / "bet_history.csv"
BANKROLL = float(DEFAULT_BUDGET)  # 10,000


def _remove_vig(h_odds: float, d_odds: float, a_odds: float):
    imp_h, imp_d, imp_a = 1.0 / h_odds, 1.0 / d_odds, 1.0 / a_odds
    vig = imp_h + imp_d + imp_a - 1.0
    if vig <= 0:
        return imp_h, imp_d, imp_a, 0.0
    return imp_h / (1 + vig), imp_d / (1 + vig), imp_a / (1 + vig), vig


def _best_retail_odds(row: pd.Series) -> Dict[str, float]:
    """返回所有零售博彩公司中每个结果的最佳赔率。"""
    best = {}
    for outcome, cols in [('H', 0), ('D', 1), ('A', 2)]:
        best_odds = 0.0
        for book, (hc, dc, ac) in RETAIL_BOOKS.items():
            col = {'H': hc, 'D': dc, 'A': ac}[outcome]
            odds = row.get(col, 0)
            if pd.isna(odds) or odds <= 1:
                continue
            if odds > best_odds:
                best_odds = odds
        best[outcome] = best_odds if best_odds > 0 else None
    return best


def load_data() -> pd.DataFrame:
    """加载并清洗 fb_odds_raw.csv。"""
    df = pd.read_csv(HISTORY_FILE, low_memory=False)
    logger.info("原始行数: %d", len(df))

    # 过滤有效 Pinnacle 赔率
    for c in ['PSH', 'PSD', 'PSA']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df[df['PSH'].notna() & df['PSD'].notna() & df['PSA'].notna()].copy()

    # 数值化零售赔率
    for cols in RETAIL_BOOKS.values():
        for c in cols:
            df[c] = pd.to_numeric(df[c], errors='coerce')

    # 过滤有结果的比赛
    df['FTHG'] = pd.to_numeric(df['FTHG'], errors='coerce')
    df['FTAG'] = pd.to_numeric(df['FTAG'], errors='coerce')
    df = df.dropna(subset=['FTHG', 'FTAG']).copy()

    df['date'] = pd.to_datetime(df['Date'], dayfirst=True, errors='coerce')
    df = df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)

    # 结果标签
    df['home_win'] = (df['FTHG'] > df['FTAG']).astype(int)
    df['draw'] = (df['FTHG'] == df['FTAG']).astype(int)
    df['away_win'] = (df['FTHG'] < df['FTAG']).astype(int)

    logger.info("清洗后: %d 行 (%s ~ %s)", len(df),
                df['date'].min().date(), df['date'].max().date())
    return df


class ReplayEngine:
    """历史回放引擎 — 逐场比赛模拟下注 + 结算。"""

    def __init__(self, min_edge: float = 0.02, kelly_fraction: float = 0.25,
                 max_single_pct: float = 0.05, max_total_pct: float = 0.30,
                 start_date: Optional[str] = None, end_date: Optional[str] = None):
        self.min_edge = min_edge
        self.kelly_fraction = kelly_fraction
        self.max_single_pct = max_single_pct
        self.max_total_pct = max_total_pct
        self.start_date = pd.Timestamp(start_date) if start_date else None
        self.end_date = pd.Timestamp(end_date) if end_date else None

        self.balance = BANKROLL
        self.total_bets = 0
        self.total_matches = 0
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.bet_records: List[Dict] = []
        self.equity_curve = [{"date": "start", "balance": BANKROLL}]

    def _running_exposure(self) -> float:
        """当前待结算投注的总仓位比例（简化版，不同日比赛视为不相关）。"""
        return 0.0  # 简化：历史回放中不同日比赛依次结算，无并行仓位

    def evaluate_match(self, row: pd.Series) -> Optional[Dict]:
        """评估单场比赛 — line shopping 策略。

        Returns:
            bet_dict 或 None（不满足条件）
        """
        pinny_h, pinny_d, pinny_a, vig = _remove_vig(
            row['PSH'], row['PSD'], row['PSA'])
        retail = _best_retail_odds(row)

        # 检查每个结果
        for outcome, pinny_prob, result_col, retail_key in [
            ('H', pinny_h, 'home_win', 'H'),
            ('D', pinny_d, 'draw', 'D'),
            ('A', pinny_a, 'away_win', 'A'),
        ]:
            retail_odds = retail[retail_key]
            if retail_odds is None:
                continue

            retail_implied = 1.0 / retail_odds
            ev = (pinny_prob - retail_implied) / retail_implied

            if ev <= self.min_edge:
                continue

            kelly = (pinny_prob * retail_odds - 1) / (retail_odds - 1)
            stake_pct = min(kelly * self.kelly_fraction, self.max_single_pct)
            if stake_pct <= 0:
                continue

            stake = self.balance * stake_pct
            actual_win = int(row[result_col])

            return {
                "date": row['date'],
                "home": row['HomeTeam'],
                "away": row['AwayTeam'],
                "league": row.get('_league', ''),
                "outcome": outcome,
                "odds": retail_odds,
                "model_prob": round(pinny_prob, 4),
                "mkt_prob": round(retail_implied, 4),
                "edge_pct": round(ev * 100, 2),
                "stake": round(stake, 2),
                "won": actual_win,
                "profit": round(stake * (retail_odds - 1) if actual_win else -stake, 2),
            }
        return None

    def run(self) -> List[Dict]:
        """执行全历史回放。"""
        df = load_data()
        if self.start_date:
            df = df[df['date'] >= self.start_date]
        if self.end_date:
            df = df[df['date'] <= self.end_date]
        df = df.reset_index(drop=True)

        logger.info("=" * 60)
        logger.info("🏁 历史回放开始")
        logger.info("  策略: Line Shopping (Pinnacle vs 零售最佳)")
        logger.info("  Min Edge: %.0f%% | Kelly: %.0f%% | Max单注: %.0f%%",
                    self.min_edge * 100, self.kelly_fraction * 100,
                    self.max_single_pct * 100)
        logger.info("  比赛: %d 场 (%s ~ %s)", len(df),
                    df['date'].min().date(), df['date'].max().date())
        logger.info("=" * 60)

        batch_size = max(1, len(df) // 20)
        last_progress = 0
        for idx, (_, row) in enumerate(df.iterrows()):
            self.total_matches = len(df)
        for idx, (_, row) in enumerate(df.iterrows()):
            pct = (idx + 1) / len(df) * 100
            if pct // 5 > last_progress:
                last_progress = pct // 5
                logger.info("  进度: %d%% (%d/%d, %d bets)",
                            int(pct), idx + 1, len(df), self.total_bets)

            bet = self.evaluate_match(row)
            if bet is None:
                continue

            self.bet_records.append(bet)
            self.total_bets += 1

            if bet["won"]:
                self.wins += 1
                self.balance += bet["profit"]
                self.consecutive_losses = 0
            else:
                self.losses += 1
                self.balance += bet["profit"]  # profit is negative
                self.consecutive_losses += 1

            self.equity_curve.append({
                "date": bet["date"].isoformat()[:10],
                "balance": round(self.balance, 2),
            })

        self._print_report()
        self._persist_bets()
        return self.bet_records

    def _print_report(self):
        """打印回放报告。"""
        logger.info("=" * 60)
        logger.info("📊 历史回放报告")
        logger.info("=" * 60)
        logger.info("  总比赛: %d, 下注: %d (%.1f%%)",
                    self.total_matches, self.total_bets,
                    self.total_bets / max(1, self.total_matches) * 100)
        logger.info("  胜: %d / %d (%.1f%%)",
                    self.wins, self.total_bets,
                    self.wins / self.total_bets * 100 if self.total_bets > 0 else 0)
        logger.info("  最终资金: ¥%.2f (初始 ¥%.0f)",
                    self.balance, BANKROLL)
        logger.info("  总利润: ¥%+.2f (ROI: %+.2f%%)",
                    self.balance - BANKROLL,
                    (self.balance - BANKROLL) / BANKROLL * 100)

        if len(self.bet_records) >= 5:
            profits = [b["profit"] for b in self.bet_records]
            avg_profit = np.mean(profits)
            std_profit = np.std(profits)
            sharpe = np.sqrt(365) * avg_profit / std_profit if std_profit > 0 else 0
            equity = BANKROLL + np.cumsum(profits)
            peak = np.maximum.accumulate(equity)
            dd = (equity - peak) / peak
            max_dd = dd.min()
            logger.info("  夏普(年化): %.2f", sharpe)
            logger.info("  最大回撤: %.2f%%", max_dd * 100)
            logger.info("  平均利润: ¥%.2f | 标准差: ¥%.2f", avg_profit, std_profit)

        # 按 outcome 拆分
        for oc in ['H', 'D', 'A']:
            sub = [b for b in self.bet_records if b["outcome"] == oc]
            if len(sub) >= 5:
                w = sum(b["won"] for b in sub)
                p = sum(b["profit"] for b in sub)
                s = sum(b["stake"] for b in sub)
                logger.info("  %s: %d 注, 胜率 %.1f%%, ROI %+.2f%%",
                           {"H": "主胜", "D": "平局", "A": "客胜"}[oc],
                           len(sub), w / len(sub) * 100,
                           p / s * 100 if s > 0 else 0)

    def _persist_bets(self):
        """将投注记录持久化到 bet_history.csv 供 PaperTrader 消费。"""
        if not self.bet_records:
            logger.info("  无投注记录，跳过持久化")
            return

        # 生成 CSV（追加以防重复运行）
        needs_header = not BET_LOG_FILE.exists() or BET_LOG_FILE.stat().st_size == 0
        with open(BET_LOG_FILE, 'a') as f:
            if needs_header:
                f.write("date,stake,win,odds,model_prob,balance_after\n")
            running = BANKROLL
            for b in self.bet_records:
                running += b["profit"]
                odds = b["odds"]
                b_odds = odds - 1.0
                model_prob = b["model_prob"]
                f.write(f"{b['date'].isoformat()[:10]},{b['stake']},{b['won']},{odds},{model_prob},{running:.2f}\n")

        logger.info("  已写入 %s: %d 行", BET_LOG_FILE.name, len(self.bet_records))

        # 也同步到 virtual_portfolio.json（供 PaperTrader 读取）
        self._sync_to_virtual_portfolio()

    def _sync_to_virtual_portfolio(self):
        """将回放结果写入 virtual_portfolio.json 的 settled + history 字段。"""
        vp_file = DATA_DIR / "virtual_portfolio.json"
        STATE = {"settled": {}, "pending_bets": [],
                 "balance": float(self.balance), "history": []}

        if vp_file.exists():
            try:
                existing = json.loads(vp_file.read_text())
                # 保留已有的非 replay 投注
                STATE["pending_bets"] = [b for b in existing.get("pending_bets", [])
                                         if "replay_" not in b.get("id", "")]
                STATE["balance"] = max(float(existing.get("balance", BANKROLL)),
                                       float(self.balance))
                # 合并历史记录
                old_history = [h for h in existing.get("history", [])
                               if "replay_" not in h.get("id", "")]
                STATE["history"] = old_history
                STATE["settled"] = {k: v for k, v in existing.get("settled", {}).items()
                                    if "replay_" not in k}
            except Exception:
                pass

        import json
        for b in self.bet_records:
            bid = f"replay_{b['date'].isoformat()[:10]}_{b['home']}_{b['away']}_{b['outcome']}"
            bid = bid.replace(" ", "_").replace(".", "")[:80]
            STATE["settled"][bid] = "won" if b["won"] else "lost"
            STATE["history"].append({
                "id": bid,
                "match": f"{b['home']} vs {b['away']} [{b['outcome']}]",
                "date": b['date'].isoformat()[:10],
                "stake": b["stake"],
                "odds": b["odds"],
                "profit": b["profit"],
                "status": "won" if b["won"] else "lost",
            })

        vp_file.write_text(json.dumps(STATE, ensure_ascii=False, indent=2))
        logger.info("  已同步至 %s: %d 笔已结算", vp_file.name, len(self.bet_records))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="历史回放引擎")
    parser.add_argument("--min-edge", type=float, default=0.02,
                        help="最小 Edge 阈值 (默认 0.02 = 2%%)")
    parser.add_argument("--kelly", type=float, default=0.25,
                        help="凯利分数 (默认 0.25)")
    parser.add_argument("--start", type=str, default=None,
                        help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None,
                        help="结束日期 YYYY-MM-DD")
    parser.add_argument("--quick", action="store_true",
                        help="快速模式：只跑最近 30 天")

    args = parser.parse_args()

    if args.quick:
        # 按日期排序后取最后一批（约等同于最近30天）
        df = load_data()
        last_date = df['date'].max()
        args.start = (last_date - pd.Timedelta(days=30)).isoformat()[:10]
        args.end = last_date.isoformat()[:10]
        logger.info("快速模式: %s ~ %s", args.start, args.end)

    engine = ReplayEngine(
        min_edge=args.min_edge,
        kelly_fraction=args.kelly,
        start_date=args.start,
        end_date=args.end,
    )
    engine.run()


if __name__ == "__main__":
    import json  # for _sync_to_virtual_portfolio
    main()
