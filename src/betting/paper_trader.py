"""模拟交易引擎 — 评估线上交易就绪度。

工作流程：
  1. 只读读取 virtual_portfolio.json + prediction_log.csv
  2. 计算指标（胜率/ROI/夏普/回撤/CLV）
  3. 运行就绪检查（7 项全部通过才 go）
  4. 生成控制台报告
  5. 持久化快照到 paper_trading.json

用法:
    from src.betting.paper_trader import PaperTrader
    PaperTrader().print_report()
"""
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from config.logging_config import get_logger
from config.settings import BASE_DIR, DATA_DIR

logger = get_logger(__name__)

_INITIAL_BALANCE = 10000.0
_PORTFOLIO_FILE = DATA_DIR / "virtual_portfolio.json"
_PRED_LOG_FILE = DATA_DIR / "prediction_log.csv"
_STATE_FILE = DATA_DIR / "paper_trading.json"
_MIN_SETTLED_FOR_READINESS = 50
_MAX_SNAPSHOTS = 90


class PaperTrader:
    """模拟交易引擎 — 只读聚合器，不修改现有数据文件。"""

    def __init__(self, data_dir: Optional[Path] = None,
                 initial_balance: float = _INITIAL_BALANCE):
        self.data_dir = data_dir or DATA_DIR
        self.initial_balance = initial_balance
        self.portfolio_file = self.data_dir / "virtual_portfolio.json"
        self.pred_log_file = self.data_dir / "prediction_log.csv"
        self.state_file = self.data_dir / "paper_trading.json"

    # ── 数据读取 ───────────────────────────────────────

    def _load_portfolio(self) -> dict:
        """读取虚拟投注组合状态。"""
        if not self.portfolio_file.exists():
            return {"settled": {}, "pending_bets": [], "balance": self.initial_balance, "history": []}
        try:
            return json.loads(self.portfolio_file.read_text())
        except Exception as e:
            logger.warning("读取虚拟投注组合失败: %s", e)
            return {"settled": {}, "pending_bets": [], "balance": self.initial_balance, "history": []}

    def _load_prediction_log(self) -> list:
        """读取预测日志 CSV。"""
        if not self.pred_log_file.exists():
            return []
        try:
            import csv
            with open(self.pred_log_file) as f:
                reader = csv.DictReader(f)
                return [r for r in reader if r.get("status") in ("won", "lost", "pending")]
        except Exception as e:
            logger.warning("读取预测日志失败: %s", e)
            return []

    def _load_own_state(self) -> dict:
        """读取自己的持久化状态。"""
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except Exception:
                pass
        return {"snapshot_history": []}

    def _save_state(self, state: dict):
        """持久化自己的状态。"""
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    # ── 指标计算 ───────────────────────────────────────

    @staticmethod
    def _compute_sharpe(equity_curve: list) -> Optional[float]:
        """年化夏普比率。"""
        if len(equity_curve) < 3:
            return None
        balances = [p.get("balance", 0) for p in equity_curve]
        returns = []
        for i in range(1, len(balances)):
            prev = balances[i - 1]
            if prev > 0:
                returns.append((balances[i] - prev) / prev)
        if len(returns) < 3:
            return None
        mean_r = sum(returns) / len(returns)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / len(returns))
        if std_r < 1e-8:
            return None
        return (mean_r / std_r) * math.sqrt(252)

    @staticmethod
    def _compute_sortino(equity_curve: list) -> Optional[float]:
        """年化 Sortino 比率（仅下行波动）。"""
        if len(equity_curve) < 3:
            return None
        balances = [p.get("balance", 0) for p in equity_curve]
        returns = []
        for i in range(1, len(balances)):
            prev = balances[i - 1]
            if prev > 0:
                returns.append((balances[i] - prev) / prev)
        if len(returns) < 3:
            return None
        mean_r = sum(returns) / len(returns)
        downside = [r for r in returns if r < 0]
        if not downside:
            return None
        dd_std = math.sqrt(sum(r * r for r in downside) / len(downside))
        if dd_std < 1e-8:
            return None
        return (mean_r / dd_std) * math.sqrt(252)

    @staticmethod
    def _compute_max_drawdown(equity_curve: list) -> float:
        """最大回撤。"""
        if len(equity_curve) < 2:
            return 0.0
        balances = [p.get("balance", 0) for p in equity_curve]
        peak = balances[0]
        mdd = 0.0
        for b in balances[1:]:
            if b > peak:
                peak = b
            dd = (peak - b) / peak if peak > 0 else 0
            if dd > mdd:
                mdd = dd
        return mdd

    @staticmethod
    def _compute_var(history: list, ci: float = 0.95) -> Optional[float]:
        """Value at Risk — 历史模拟法。"""
        profits = [h.get("profit", 0) for h in history if h.get("profit") is not None]
        if len(profits) < 10:
            return None
        profits.sort()
        idx = int((1 - ci) * len(profits))
        return round(profits[idx], 2)

    @staticmethod
    def _compute_cvar(history: list, ci: float = 0.95) -> Optional[float]:
        """Conditional VaR — VaR 尾部均值。"""
        profits = [h.get("profit", 0) for h in history if h.get("profit") is not None]
        if len(profits) < 10:
            return None
        profits.sort()
        idx = int((1 - ci) * len(profits))
        tail = profits[:idx]
        if not tail:
            return None
        return round(sum(tail) / len(tail), 2)

    @staticmethod
    def _z_score_test(win_count: int, total: int, p0: float = 0.52) -> float:
        """单样本比例 z-test 统计量。"""
        if total < 1:
            return 0.0
        p_hat = win_count / total
        se = math.sqrt(p0 * (1 - p0) / total)
        if se < 1e-8:
            return 0.0
        return (p_hat - p0) / se

    @staticmethod
    def _win_rate_p_value(win_count: int, total: int, p0: float = 0.52) -> float:
        """单侧 p-value（H0: p <= p0）。"""
        if total < 1:
            return 1.0
        from math import erf
        z = PaperTrader._z_score_test(win_count, total, p0)
        return 1 - 0.5 * (1 + erf(z / math.sqrt(2))) if z < 5 else 0.0

    # ── 核心计算 ───────────────────────────────────────

    def refresh(self) -> dict:
        """读取所有数据源，重新计算指标，持久化快照。

        Returns:
            完整的 paper_trading 状态字典。
        """
        portfolio = self._load_portfolio()
        pred_rows = self._load_prediction_log()
        own_state = self._load_own_state()

        # 从 virtual_portfolio 提取数据
        history = portfolio.get("history", [])
        pending = portfolio.get("pending_bets", [])
        settled_map = portfolio.get("settled", {})
        current_balance = portfolio.get("balance", self.initial_balance)

        # ── 基础统计 ──
        win_count = sum(1 for h in history if h.get("status") == "won")
        loss_count = sum(1 for h in history if h.get("status") == "lost")
        void_count = sum(1 for h in history if h.get("status") in ("void", "push", "refund"))
        total_settled = win_count + loss_count + void_count
        total_bets = total_settled + len(pending)
        win_rate = win_count / (win_count + loss_count) if (win_count + loss_count) > 0 else 0.0
        total_profit = sum(h.get("profit", 0) for h in history if h.get("profit") is not None)
        total_stake = sum(h.get("stake", 0) for h in history if h.get("stake") is not None)
        roi = total_profit / max(self.initial_balance, 1)
        avg_odds = sum(h.get("odds", 0) for h in history if h.get("odds"))
        avg_odds = avg_odds / max(len([h for h in history if h.get("odds")]), 1)

        # 从 prediction_log 补充 avg EV
        pred_evs = [float(r.get("ev", 0)) for r in pred_rows
                     if r.get("status") in ("won", "lost") and r.get("ev")]
        avg_ev = sum(pred_evs) / len(pred_evs) if pred_evs else None

        # 构建权益曲线（初始 -> 每次结算后）
        equity_curve = [{"date": "start", "balance": self.initial_balance}]
        running = self.initial_balance
        for h in history:
            profit = h.get("profit", 0)
            running += profit
            equity_curve.append({
                "date": h.get("date", datetime.now(timezone.utc).isoformat()),
                "balance": round(running, 2),
            })
        # 当前余额补到最后
        if equity_curve and equity_curve[-1]["balance"] != round(current_balance, 2):
            equity_curve.append({
                "date": datetime.now(timezone.utc).isoformat(),
                "balance": round(current_balance, 2),
            })

        # ── 风险指标 ──
        sharpe = self._compute_sharpe(equity_curve)
        sortino = self._compute_sortino(equity_curve)
        max_dd = self._compute_max_drawdown(equity_curve)
        var_95 = self._compute_var(history)
        cvar_95 = self._compute_cvar(history)

        # 最大连续亏损
        max_consecutive_losses = 0
        current_streak = 0
        for h in history:
            if h.get("status") == "lost":
                current_streak += 1
                max_consecutive_losses = max(max_consecutive_losses, current_streak)
            else:
                current_streak = 0
        # 当前连败/连胜
        current_result_streak = 0
        for h in reversed(history):
            if h.get("status") == "won":
                current_result_streak = current_result_streak + 1 if current_result_streak >= 0 else 1
            elif h.get("status") == "lost":
                current_result_streak = current_result_streak - 1 if current_result_streak <= 0 else -1

        # ── CLV 统计 ──
        clv_values = []
        for h in history:
            clv = h.get("clv")
            if clv is not None:
                clv_values.append(float(clv))
        for b in pending:
            clv = b.get("clv")
            if clv is not None:
                clv_values.append(float(clv))
        avg_clv = sum(clv_values) / len(clv_values) if clv_values else None
        positive_clv_count = sum(1 for c in clv_values if c > 0)
        positive_clv_rate = positive_clv_count / len(clv_values) if clv_values else None

        # ── 按运动拆分 ──
        by_sport = {}
        for h in history:
            bid = h.get("id", "")
            # 从 id 推断 sport: "nba_NBA_队名_队名_市场"
            parts = bid.split("_")
            sport_key = parts[0] if parts else "unknown"
            if sport_key not in by_sport:
                by_sport[sport_key] = {"bets": 0, "settled": 0, "win_count": 0,
                                       "loss_count": 0, "total_stake": 0.0,
                                       "total_profit": 0.0, "clv_values": []}
            s = by_sport[sport_key]
            s["settled"] += 1
            s["bets"] += 1
            if h.get("status") == "won":
                s["win_count"] += 1
            elif h.get("status") == "lost":
                s["loss_count"] += 1
            s["total_stake"] += h.get("stake", 0)
            s["total_profit"] += h.get("profit", 0)
            clv = h.get("clv")
            if clv is not None:
                s["clv_values"].append(float(clv))
        # pending 也算在总 bet 数内
        for b in pending:
            sid = b.get("sport", "unknown")
            if sid not in by_sport:
                by_sport[sid] = {"bets": 0, "settled": 0, "win_count": 0,
                                 "loss_count": 0, "total_stake": 0.0,
                                 "total_profit": 0.0, "clv_values": []}
            by_sport[sid]["bets"] += 1

        for sk, sv in by_sport.items():
            sv["win_rate"] = (sv["win_count"] / max(sv["settled"], 1)
                              if sv["settled"] > 0 else None)
            sv["roi"] = (sv["total_profit"] / max(self.initial_balance, 1)
                         if sv["settled"] > 0 else None)
            sv["avg_clv"] = (sum(sv["clv_values"]) / len(sv["clv_values"])
                             if sv["clv_values"] else None)

        # ── 时间窗口统计 ──
        now = datetime.now(timezone.utc)
        def _in_window(h, days):
            try:
                dt = datetime.fromisoformat(h["date"]) if isinstance(h["date"], str) else now
                return (now - dt) <= timedelta(days=days)
            except Exception:
                return False

        def _window_metrics(days):
            wh = [h for h in history if _in_window(h, days)]
            ww = sum(1 for h in wh if h.get("status") == "won")
            wl = sum(1 for h in wh if h.get("status") == "lost")
            wp = sum(h.get("profit", 0) for h in wh)
            ws = sum(h.get("stake", 0) for h in wh)
            return {
                "bets": len(wh),
                "win_rate": ww / (ww + wl) if (ww + wl) > 0 else None,
                "roi": wp / max(self.initial_balance, 1) if len(wh) > 0 else None,
                "profit": round(wp, 2),
            }

        metrics_by_tier = {
            "last_7_days": _window_metrics(7),
            "last_30_days": _window_metrics(30),
            "all_time": {
                "bets": total_settled,
                "win_rate": win_rate,
                "roi": roi,
                "profit": round(total_profit, 2),
            },
        }

        # ── 日期范围 ──
        dates = []
        for h in history:
            try:
                dates.append(datetime.fromisoformat(h["date"]))
            except Exception:
                pass
        first_bet = min(dates).strftime("%Y-%m-%d") if dates else None
        last_bet = max(dates).strftime("%Y-%m-%d") if dates else None
        days_active = (max(dates) - min(dates)).days + 1 if len(dates) >= 2 else 0

        # ── 就绪检查 ──
        z_stat = self._z_score_test(win_count, win_count + loss_count)
        p_value = self._win_rate_p_value(win_count, win_count + loss_count)

        checks = {
            "min_bets": {
                "passed": total_settled >= _MIN_SETTLED_FOR_READINESS,
                "actual": total_settled,
                "required": _MIN_SETTLED_FOR_READINESS,
                "detail": f"还需 {max(0, _MIN_SETTLED_FOR_READINESS - total_settled)} 笔",
            },
            "win_rate": {
                "passed": win_count + loss_count >= 20 and win_rate > 0.52 and p_value < 0.10,
                "actual": round(win_rate, 4) if win_count + loss_count > 0 else 0,
                "required": "> 0.52 (z-test p<0.10)",
                "detail": f"z={z_stat:.2f}, p={p_value:.3f}" if total_settled > 0 else "无数据",
            },
            "positive_roi": {
                "passed": total_settled >= 5 and roi > 0,
                "actual": round(roi, 4),
                "required": "> 0",
                "detail": "",
            },
            "positive_clv": {
                "passed": avg_clv is not None and avg_clv > 0,
                "actual": round(avg_clv, 4) if avg_clv is not None else None,
                "required": "> 0",
                "detail": "",
            },
            "max_drawdown": {
                "passed": max_dd < 0.15,
                "actual": round(max_dd, 4),
                "required": "< 0.15",
                "detail": "",
            },
            "sharpe_ratio": {
                "passed": sharpe is not None and sharpe > 0.5,
                "actual": round(sharpe, 2) if sharpe is not None else None,
                "required": "> 0.5",
                "detail": "",
            },
        }

        # 稳定性检查 —— 需要 3+ 连续快照全部通过
        snapshot_history = own_state.get("snapshot_history", [])
        stability_passed = False
        consecutive_ready = 0
        for snap in reversed(snapshot_history):
            if snap.get("all_checks_passed"):
                consecutive_ready += 1
            else:
                break
        # 加上当前（如果所有非稳定性检查通过）
        current_non_stability = all(
            v["passed"] for k, v in checks.items() if k != "stability"
        )
        if current_non_stability:
            consecutive_ready += 1
        stability_passed = consecutive_ready >= 3

        checks["stability"] = {
            "passed": stability_passed,
            "actual": consecutive_ready,
            "required": ">= 3",
            "detail": f"连续 {consecutive_ready}/3 天通过",
        }

        ready = all(v["passed"] for v in checks.values())

        # 就绪时间
        ready_since = own_state.get("ready_since")
        if ready and not ready_since:
            ready_since = datetime.now(timezone.utc).isoformat()
        elif not ready:
            ready_since = None

        # 生成中文建议
        recommendation_cn = self._build_recommendation(checks, total_settled,
                                                       win_rate, roi, avg_clv,
                                                       max_dd, sharpe,
                                                       consecutive_ready)
        recommendation_en = recommendation_cn  # 保持中文

        # ── 构建完整状态 ──
        state = {
            "version": 1,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "initial_bankroll": self.initial_balance,
            "current_bankroll": round(current_balance, 2),
            "total_bets": total_bets,
            "settled_bets": total_settled,
            "pending_bets": len(pending),
            "win_count": win_count,
            "loss_count": loss_count,
            "void_count": void_count,
            "win_rate": round(win_rate, 4),
            "total_stake": round(total_stake, 2),
            "total_profit": round(total_profit, 2),
            "roi": round(roi, 4),
            "max_drawdown": round(max_dd, 4),
            "sharpe_ratio": round(sharpe, 2) if sharpe is not None else None,
            "sortino_ratio": round(sortino, 2) if sortino is not None else None,
            "var_95": var_95,
            "cvar_95": cvar_95,
            "avg_clv": round(avg_clv, 4) if avg_clv is not None else None,
            "positive_clv_rate": round(positive_clv_rate, 4) if positive_clv_rate is not None else None,
            "max_consecutive_losses": max_consecutive_losses,
            "current_streak": current_result_streak,
            "avg_odds": round(avg_odds, 2),
            "avg_ev": round(avg_ev, 4) if avg_ev is not None else None,
            "first_bet_date": first_bet,
            "last_bet_date": last_bet,
            "total_days_active": days_active,
            "by_sport": by_sport,
            "equity_curve": equity_curve,
            "metrics_by_tier": metrics_by_tier,
            "readiness": {
                "ready": ready,
                "ready_since": ready_since,
                "checks": checks,
                "recommendation_cn": recommendation_cn,
                "recommendation_en": recommendation_en,
            },
        }

        # ── 更新快照历史 ──
        snapshot = {
            "date": state["last_updated"],
            "settled": total_settled,
            "win_rate": round(win_rate, 4),
            "roi": round(roi, 4),
            "max_drawdown": round(max_dd, 4),
            "sharpe": round(sharpe, 2) if sharpe is not None else None,
            "ready": ready,
            "all_checks_passed": current_non_stability,
        }
        snapshot_history.append(snapshot)
        # 修剪
        if len(snapshot_history) > _MAX_SNAPSHOTS:
            snapshot_history = snapshot_history[-_MAX_SNAPSHOTS:]
        own_state["snapshot_history"] = snapshot_history
        own_state["ready_since"] = ready_since
        state["snapshot_history"] = snapshot_history
        self._save_state(own_state)
        # 也保存完整状态
        self._save_full_state(state)

        return state

    def _save_full_state(self, state: dict):
        """保存完整状态到 state_file。"""
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    @staticmethod
    def _build_recommendation(checks: dict, total_settled: int,
                               win_rate: float, roi: float, avg_clv: Optional[float],
                               max_dd: float, sharpe: Optional[float],
                               consecutive_ready: int) -> str:
        """生成可读的中文建议。"""
        failed = [k for k, v in checks.items() if not v["passed"]]
        if not failed:
            return ("所有就绪检查通过！系统在 {} 笔已结算投注中实现 "
                    "胜率 {:.1%}，ROI {:.1%}，夏普 {:.2f}，回撤 {:.1%}。"
                    "可以启用自动线上交易。").format(total_settled, win_rate,
                                                       roi, sharpe or 0, max_dd)

        parts = []
        if "min_bets" in failed:
            parts.append(f"累计仅 {total_settled} 笔已结算投注，需 50 笔")
        if "win_rate" in failed:
            parts.append(f"当前胜率 {win_rate:.1%}（需显著 > 52%）")
        if "positive_roi" in failed:
            parts.append(f"ROI {roi:.1%} 尚未转正")
        if "positive_clv" in failed:
            parts.append(f"平均 CLV {avg_clv or 0:.4f} 需为正")
        if "max_drawdown" in failed:
            parts.append(f"最大回撤 {max_dd:.1%} 超过 15%")
        if "sharpe_ratio" in failed:
            parts.append(f"夏普 {sharpe} 需 > 0.5")
        if "stability" in failed:
            parts.append(f"稳定期 {consecutive_ready}/3 天")

        return "就绪检查未通过。" + "；".join(parts) + "。继续模拟交易。如需更多信息，请运行 PaperTrader().print_report() 查看完整报告。"

    # ── 报告生成 ───────────────────────────────────────

    def generate_report(self, state: dict = None) -> str:
        """生成可读的控制台报告。"""
        if state is None:
            state = self.refresh()
        s = state
        rd = s.get("readiness", {})

        lines = []
        sep = "=" * 80
        sub = "-" * 80

        # ═══════════ HEADER ═══════════
        lines.append(sep)
        lines.append("  Paper Trading Report  |  模拟交易评估报告")
        lines.append(sep)
        lines.append(f"  Generated: {s.get('last_updated', 'N/A')[:19]}")
        if s.get("first_bet_date") and s.get("last_bet_date"):
            lines.append(f"  Period:    {s['first_bet_date']} ~ {s['last_bet_date']} "
                         f"({s['total_days_active']} days active)")
        lines.append("")

        # ═══════════ 1. OVERVIEW ═══════════
        lines.append(sub)
        lines.append("  [1] Overview")
        lines.append(sub)
        pending_pct = (s["pending_bets"] / max(s["total_bets"], 1) * 100)
        settled_pct = (s["settled_bets"] / max(s["total_bets"], 1) * 100)
        lines.append(f"  Initial Bankroll:         ¥{self.initial_balance:>10,.2f}")
        lines.append(f"  Current Bankroll:         ¥{s['current_bankroll']:>10,.2f}")
        lines.append(f"  Total Bets:               {s['total_bets']:>10}")
        lines.append(f"  Settled:                  {s['settled_bets']:>10}  ({settled_pct:.1f}%)")
        lines.append(f"  Pending:                  {s['pending_bets']:>10}  ({pending_pct:.1f}%)")
        lines.append(f"  Void:                     {s['void_count']:>10}")
        lines.append("")

        # ═══════════ 2. PERFORMANCE ═══════════
        lines.append(sub)
        lines.append("  [2] Performance")
        lines.append(sub)
        wr = s.get("win_rate", 0)
        lines.append(f"  Win Rate:                 {wr:>7.1%}  "
                     f"({s['win_count']}W / {s['loss_count']}L)")
        lines.append(f"  Total Profit:            ¥{s['total_profit']:>10,.2f}")
        lines.append(f"  ROI:                      {s['roi']:>+7.1%}" if s.get('roi') is not None
                     else "  ROI:                      N/A")
        lines.append(f"  Total Stake:             ¥{s['total_stake']:>10,.2f}")
        avg_stake = s["total_stake"] / max(s["settled_bets"], 1)
        lines.append(f"  Avg Stake per Bet:       ¥{avg_stake:>10,.2f}")
        lines.append(f"  Avg Odds:                 {s['avg_odds']:>7.2f}")
        if s.get("avg_ev") is not None:
            lines.append(f"  Avg EV:                   {s['avg_ev']:>+7.2%}")
        lines.append("")

        # ═══════════ 3. RISK ═══════════
        lines.append(sub)
        lines.append("  [3] Risk Metrics")
        lines.append(sub)
        lines.append(f"  Max Drawdown:             {s['max_drawdown']:>7.1%}")
        lines.append(f"  Sharpe Ratio (ann.):      {s['sharpe_ratio']:>7.2f}"
                     if s.get("sharpe_ratio") is not None else "  Sharpe Ratio (ann.):      N/A")
        lines.append(f"  Sortino Ratio (ann.):     {s['sortino_ratio']:>7.2f}"
                     if s.get("sortino_ratio") is not None else "  Sortino Ratio (ann.):     N/A")
        lines.append(f"  VaR (95%):               ¥{s['var_95']:>10,.2f}"
                     if s.get("var_95") is not None else "  VaR (95%):               N/A")
        lines.append(f"  CVaR (95%):              ¥{s['cvar_95']:>10,.2f}"
                     if s.get("cvar_95") is not None else "  CVaR (95%):              N/A")
        lines.append(f"  Max Consecutive Losses:   {s['max_consecutive_losses']:>10}")
        streak = s.get("current_streak", 0)
        streak_str = f"{streak}W" if streak > 0 else f"{abs(streak)}L" if streak < 0 else "-"
        lines.append(f"  Current Streak:           {streak_str:>10}")
        lines.append("")

        # ═══════════ 4. CLV ═══════════
        lines.append(sub)
        lines.append("  [4] CLV (Closing Line Value)")
        lines.append(sub)
        if s.get("avg_clv") is not None:
            lines.append(f"  Avg CLV:                 {s['avg_clv']:>+7.1%}")
            lines.append(f"  Positive CLV Rate:        {s['positive_clv_rate']:>7.1%}")
            clv_pos = int(s.get("positive_clv_rate", 0) * 100) if s.get("positive_clv_rate") else 0
            lines.append(f"  Interpretation:          "
                         f"{'Positive CLV -- system has real edge' if s['avg_clv'] > 0 else 'Negative CLV -- system may be lucky, not skilled'}")
        else:
            lines.append("  Avg CLV:                  N/A  (no CLV data available)")
        lines.append("")

        # ═══════════ 5. BY SPORT ═══════════
        lines.append(sub)
        lines.append("  [5] By Sport Breakdown")
        lines.append(sub)
        by_sport = s.get("by_sport", {})
        if by_sport:
            header = f"  {'Sport':<14} {'Bets':>6} {'Settled':>8} {'Won/Loss':>10} {'WinRate':>8} {'Profit':>10} {'ROI':>8} {'CLV':>8}"
            lines.append(header)
            lines.append("  " + "-" * (len(header) - 2))
            for sk in sorted(by_sport.keys()):
                sv = by_sport[sk]
                if sv["settled"] > 0:
                    wl = f"{sv['win_count']}W/{sv['loss_count']}L"
                    wr_str = f"{sv['win_rate']:.1%}" if sv["win_rate"] is not None else "N/A"
                    profit_str = f"¥{sv['total_profit']:+.0f}" if abs(sv["total_profit"]) >= 0.5 else "¥0"
                    roi_str = f"{sv['roi']:.1%}" if sv["roi"] is not None else "N/A"
                    clv_str = f"{sv['avg_clv']:.1%}" if sv["avg_clv"] is not None else "N/A"
                else:
                    wl = "0/0"
                    wr_str = "N/A"
                    profit_str = "N/A"
                    roi_str = "N/A"
                    clv_str = "N/A"
                lines.append(f"  {sk:<14} {sv['bets']:>6} {sv['settled']:>8} {wl:>10} {wr_str:>8} {profit_str:>10} {roi_str:>8} {clv_str:>8}")
            lines.append("")
            line = "  * Sports with <20 settled bets: insufficient data for reliable assessment"
            lines.append(line)
        lines.append("")

        # ═══════════ 6. TIERED ═══════════
        lines.append(sub)
        lines.append("  [6] Performance by Time Window")
        lines.append(sub)
        tiers = s.get("metrics_by_tier", {})
        header2 = f"  {'Period':<16} {'Bets':>6} {'WinRate':>9} {'ROI':>9} {'Profit':>12}"
        lines.append(header2)
        lines.append("  " + "-" * (len(header2) - 2))
        for tier_name, tier_data in [("Last 7 days", "last_7_days"),
                                      ("Last 30 days", "last_30_days"),
                                      ("All time", "all_time")]:
            td = tiers.get(tier_data, {})
            tb = td.get("bets", 0)
            twr = f"{td['win_rate']:.1%}" if td.get("win_rate") is not None else "N/A"
            troi = f"{td['roi']:.1%}" if td.get("roi") is not None else "N/A"
            tprofit = f"¥{td['profit']:>+,.0f}" if td.get("profit") is not None else "N/A"
            lines.append(f"  {tier_name:<16} {tb:>6} {twr:>9} {troi:>9} {tprofit:>12}")
        lines.append("")

        # ═══════════ 7. READINESS ═══════════
        lines.append(sep)
        lines.append("  [7] Readiness Assessment")
        lines.append(sep)
        ready = rd.get("ready", False)
        verdict = "✅  GO" if ready else "❌  NO-GO"
        lines.append(f"")
        lines.append(f"  GO / NO-GO:  {verdict}")
        lines.append("")
        lines.append("  Checks:")
        lines.append(f"  {'Check':<25} {'Status':<10} {'Actual':<12} {'Required':<12} {'Detail':<20}")
        lines.append(f"  {'-'*25} {'-'*10} {'-'*12} {'-'*12} {'-'*20}")
        for ck, cv in rd.get("checks", {}).items():
            status = "PASS" if cv["passed"] else "FAIL"
            status = status if cv["passed"] else ("FAIL" if cv.get("actual") is not None and (
                cv["passed"] is False) else "N/A")
            actual_str = str(cv.get("actual", "")) if cv.get("actual") is not None else "N/A"
            if isinstance(cv.get("actual"), float):
                actual_str = f"{cv['actual']:.4f}" if cv["actual"] < 1 else f"{cv['actual']:.0f}"
            required_str = str(cv.get("required", "")) if cv.get("required") is not None else "N/A"
            detail = cv.get("detail", "")[:20]
            lines.append(f"  {ck:<25} {status:<10} {actual_str:<12} {required_str:<12} {detail:<20}")
        lines.append("")

        # 中文结论
        rec_cn = rd.get("recommendation_cn", "")
        if rec_cn:
            lines.append("  Recommendation:")
            # wrap at ~60 chars
            for i in range(0, len(rec_cn), 60):
                lines.append(f"    {rec_cn[i:i+60]}")
        lines.append("")
        lines.append(sep)

        return "\n".join(lines)

    # ── 公开接口 ───────────────────────────────────────

    def print_report(self):
        """刷新数据并打印报告。"""
        state = self.refresh()
        print(self.generate_report(state))

    def run(self) -> dict:
        """刷新数据、打印报告、返回状态。

        Returns:
            paper_trading 状态字典。
        """
        state = self.refresh()
        print(self.generate_report(state))
        return state

    def readiness_summary(self) -> dict:
        """仅返回就绪评估摘要（不打印）。"""
        state = self.refresh()
        return state.get("readiness", {})
