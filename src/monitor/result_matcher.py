#!/usr/bin/env python3
"""投注结果自动匹配引擎 — 绩效闭环核心

数据源优先级:
  1. virtual_portfolio.json（已手工/自动结算的投注）
  2. basketball_history.csv / football_history.csv（含比分的完赛场次）
  3. 手动标注（fallback）

用法:
    # 自动结算所有 pending 投注
    python3 src/monitor/result_matcher.py auto

    # 查看待结算
    python3 src/monitor/result_matcher.py validate

    # 生成报告
    python3 src/monitor/result_matcher.py report

    # 手动标注
    python3 src/monitor/result_matcher.py update <date> <game> <bet_type> <result> <profit>
"""
import json, re, unicodedata, sys
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR, DEFAULT_BUDGET
from config.logging_config import get_logger
logger = get_logger(__name__)

PERF_FILE = DATA_DIR / "performance_history.csv"
PORTFOLIO_FILE = DATA_DIR / "virtual_portfolio.json"
BB_HISTORY = DATA_DIR / "basketball_history.csv"
FB_HISTORY = DATA_DIR / "football_history.csv"

# CLI 模式直接打印
_IS_CLI = len(sys.argv) > 1
if _IS_CLI:
    import logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', stream=sys.stdout, force=True)

# ── 队名别名缓存 ──
_TEAM_ALIASES = {}      # 通用别名
_CN2EN = {}             # 中文队名 → 英文队名
_alias_path = DATA_DIR / "team_aliases.json"
if _alias_path.exists():
    try:
        _TEAM_ALIASES.update(json.loads(_alias_path.read_text(encoding='utf-8')))
    except Exception:
        pass

_cn_path = ROOT / "data" / "team_mapping.json"
if _cn_path.exists():
    try:
        tmap = json.loads(_cn_path.read_text(encoding='utf-8'))
        # team_mapping.json 是 {英文: 中文}，反转成 {中文: 英文}
        _CN2EN = {_norm(v): k for k, v in tmap.items() if isinstance(v, str)}
        # 同时加入别名映射 中文→中文（保持 _norm 一致性）
        for k, v in tmap.items():
            _TEAM_ALIASES[_norm(k)] = _norm(k)  # 英文保留
            _TEAM_ALIASES[_norm(v)] = _norm(v)  # 中文保留
    except Exception:
        pass


def _norm(name: str) -> str:
    if not name:
        return ""
    n = re.sub(r"\(.*?\)", "", name)
    n = unicodedata.normalize('NFKD', n)
    n = ''.join(ch for ch in n if not unicodedata.combining(ch))
    n = re.sub(r"[^0-9A-Za-z一-鿿]+", " ", n).strip().lower()
    return _TEAM_ALIASES.get(n, n)


def _cn_to_en(name: str) -> str:
    """中文队名 → 英文队名"""
    n = _norm(name)
    return _CN2EN.get(n, name)


def _parse_game(s: str) -> tuple:
    if not s:
        return "", ""
    for sep in [' vs ', ' v ', ' - ']:
        if sep in s:
            parts = s.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return "", ""


def _recalc(df: pd.DataFrame) -> pd.DataFrame:
    cum = float(DEFAULT_BUDGET)
    for i in range(len(df)):
        r = df.iloc[i]['result']
        if r in ('won', 'lost'):
            cum += float(df.iloc[i].get('profit', 0) or 0)
        df.iloc[i, df.columns.get_loc('cumulative_balance')] = cum
    return df


def _read_perf() -> pd.DataFrame:
    if not PERF_FILE.exists():
        return pd.DataFrame(columns=[
            'date', 'game', 'bet', 'prob', 'market_prob',
            'stake', 'result', 'profit', 'odds', 'cumulative_balance'
        ])
    return pd.read_csv(PERF_FILE)


def _save_perf(df: pd.DataFrame):
    PERF_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(PERF_FILE, index=False)


# ═══════════════════════════════════════════════════════════
#  虚拟组合同步
# ═══════════════════════════════════════════════════════════

def settle_from_portfolio() -> int:
    """从 virtual_portfolio.json 同步已结算记录。"""
    if not PORTFOLIO_FILE.exists():
        return 0
    try:
        state = json.loads(PORTFOLIO_FILE.read_text())
    except Exception:
        return 0

    perf = _read_perf()
    history = state.get('history', [])
    updated = 0

    for h in history:
        bid = h.get('id', '')
        status = h.get('status', '')
        if status not in ('won', 'lost'):
            continue

        match_mask = perf['game'].astype(str) == bid
        if match_mask.any():
            idx = match_mask.values.argmax()
            if perf.iloc[idx]['result'] == 'pending':
                perf.iloc[idx, perf.columns.get_loc('result')] = status
                perf.iloc[idx, perf.columns.get_loc('profit')] = h.get('profit', 0)
                updated += 1
        else:
            stake = float(h.get('stake', 0) or 0)
            odds = float(h.get('odds', 1) or 1)
            profit = float(h.get('profit', 0) or 0)
            if stake == 0 and profit != 0:
                stake = abs(profit) / (odds - 1) if odds > 1 and status == 'won' else abs(profit)
            new_row = {
                'date': h.get('date', '')[:10],
                'game': bid,
                'bet': bid.split('_')[-1] if '_' in bid else '',
                'prob': 0.0, 'market_prob': 0.0,
                'stake': stake,
                'result': status,
                'profit': profit,
                'odds': odds,
                'cumulative_balance': 0,
            }
            perf = pd.concat([perf, pd.DataFrame([new_row])], ignore_index=True)
            updated += 1

    if updated > 0:
        perf = _recalc(perf)
        _save_perf(perf)
        logger.info("✅ 从组合同步 %d 条", updated)
    return updated


# ═══════════════════════════════════════════════════════════
#  历史文件匹配
# ═══════════════════════════════════════════════════════════

def settle_from_history(sport: str = 'all') -> int:
    """从比赛历史 CSV 文件匹配 pending 记录。"""
    perf = _read_perf()
    pending = perf[perf['result'] == 'pending'].copy()
    if pending.empty:
        return 0

    sources = {}
    if sport in ('bb', 'all') and BB_HISTORY.exists():
        sources['bb'] = pd.read_csv(BB_HISTORY)
    if sport in ('fb', 'all') and FB_HISTORY.exists():
        sources['fb'] = pd.read_csv(FB_HISTORY)
    if not sources:
        return 0

    updated = 0
    for idx, row in pending.iterrows():
        game = str(row.get('game', '')).strip()
        bet = str(row.get('bet', '')).strip()
        if not game or not bet:
            continue

        home, away = _parse_game(game)
        if not home or not away:
            continue

        # 尝试中文 → 英文转换（因为 history csv 里是英文队名）
        home_en = _cn_to_en(home)
        away_en = _cn_to_en(away)
        hn, an = _norm(home_en), _norm(away_en)
        # 如果翻译后没变（不是中文名），保留原 norm
        if hn == _norm(home_en):
            hn = hn or _norm(home)
        if an == _norm(away_en):
            an = an or _norm(away)

        st = 'fb' if any(w in game for w in ['FC', 'United', 'City', 'Real', 'Atlético',
                                                'Bournemouth', 'Wolverhampton', 'Villarreal',
                                                '英超', '西甲', '德甲', '意甲', '法甲']) else 'bb'
        hist = sources.get(st)
        if hist is None:
            continue

        for _, g in hist.iterrows():
            gh, ga = _norm(str(g.get('home', ''))), _norm(str(g.get('away', '')))
            if not (hn in gh or gh in hn) or not (an in ga or ga in an):
                continue

            hs = float(g.get('home_score' if st == 'bb' else 'home_goals', 0) or 0)
            as_ = float(g.get('away_score' if st == 'bb' else 'away_goals', 0) or 0)
            bl = bet.lower()

            side = None
            for pre, s in [('主', 'home'), ('客', 'away'), ('home', 'home'), ('away', 'away')]:
                if bl.startswith(pre):
                    side = s
                    break
            if side is None:
                if hn in _norm(bet):
                    side = 'home'
                elif an in _norm(bet):
                    side = 'away'
                elif any(k in bl for k in ['大', 'over']):
                    side = 'over'
                elif any(k in bl for k in ['小', 'under']):
                    side = 'under'
            if side is None:
                continue

            if side == 'home':
                result = 'won' if hs > as_ else ('lost' if hs < as_ else 'push')
            elif side == 'away':
                result = 'won' if as_ > hs else ('lost' if as_ < hs else 'push')
            else:
                continue

            stake = float(row.get('stake', 0) or 0)
            odds = float(row.get('odds', 1) or 1)
            profit = stake * (odds - 1) if result == 'won' else (-stake if result == 'lost' else 0)

            perf.iloc[idx, perf.columns.get_loc('result')] = result
            perf.iloc[idx, perf.columns.get_loc('profit')] = profit
            logger.info("  ✅ %s vs %s → %s", home, away, result)
            updated += 1
            break

    if updated > 0:
        perf = _recalc(perf)
        _save_perf(perf)
        logger.info("✅ 历史匹配 %d 条", updated)
    return updated


# ═══════════════════════════════════════════════════════════
#  手动标注（保留原接口）
# ═══════════════════════════════════════════════════════════

def update_result_manual(date: str, game: str, bet_type: str,
                         result: str, profit: float = 0.0, notes: str = ""):
    """手动标注投注结果。"""
    perf = _read_perf()
    mask = (perf['date'] == date) & (perf['game'] == game) & (perf['bet'] == bet_type)
    if not mask.any():
        logger.warning("❌ 未找到匹配记录: %s %s %s", date, game, bet_type)
        return False

    idx = mask.idxmax()
    perf.at[idx, 'result'] = result
    perf.at[idx, 'profit'] = profit
    perf = _recalc(perf)
    _save_perf(perf)
    logger.info("✅ 已更新: %s %s → %s (利润 %.0f)", date, game, result, profit)
    return True


def batch_update_results(updates: list):
    for upd in updates:
        update_result_manual(**upd)


def validate_pending_records():
    """列出所有待结算记录。"""
    perf = _read_perf()
    pending = perf[perf['result'] == 'pending']
    settled = perf[perf['result'].isin(['won', 'lost'])]
    print(f"\n📊 绩效摘要")
    print(f"   {'已结算:' :12} {len(settled):>3} 笔")
    print(f"   {'待结算:' :12} {len(pending):>3} 笔")
    print(f"   {'总计:' :12} {len(perf):>3} 笔")
    if not pending.empty:
        print(f"\n📋 待结算列表:")
        for _, r in pending.iterrows():
            print(f"   [{r['date']}] {r['game']} | {r['bet']} | ¥{float(r.get('stake',0) or 0):.0f} @ {float(r.get('odds',1) or 1):.2f}")
    print()


def generate_summary_report():
    """生成详细的绩效报告。"""
    perf = _read_perf()
    settled = perf[perf['result'].isin(['won', 'lost'])]
    pending = perf[perf['result'] == 'pending']

    print("\n" + "=" * 60)
    print("📈 投注绩效报告")
    print("=" * 60)
    if not settled.empty:
        total = len(settled)
        won = len(settled[settled['result'] == 'won'])
        wr = won / total
        tp = float(settled['profit'].sum())
        fb = float(DEFAULT_BUDGET)
        roi = tp / fb if fb > 0 else 0
        cum = fb + tp
        peak = max(perf['cumulative_balance'].max() if 'cumulative_balance' in perf.columns else fb, fb)
        dd = (cum - peak) / peak if peak > 0 else 0
        print(f"   已结算: {total} 笔 | 胜 {won} / 负 {total - won}")
        print(f"   胜率:   {wr:.1%}")
        print(f"   利润:   ¥{tp:+.0f}")
        print(f"   ROI:    {roi:+.1%}")
        print(f"   资金:   ¥{cum:.0f} / ¥{fb:.0f}")
        print(f"   回撤:   {dd:.1%}")
    else:
        print(f"\n   ⏳ 尚无已结算投注")
    if not pending.empty:
        print(f"\n   ⏳ 待结算: {len(pending)} 笔 (注额 ¥{float(pending['stake'].sum()):.0f})")
    print("=" * 60 + "\n")


def auto_settle(sport: str = 'all') -> dict:
    """一键结算：先同步组合，再匹配历史。"""
    r = {}
    r['portfolio'] = settle_from_portfolio()
    r['history'] = settle_from_history(sport)
    r['total'] = r['portfolio'] + r['history']
    if r['total'] > 0:
        logger.info("🎯 自动结算完成: %d 条", r['total'])
    else:
        logger.info("📭 无待结算记录")
    return r


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "auto":
            auto_settle(sys.argv[2] if len(sys.argv) > 2 else 'all')
        elif cmd == "validate":
            validate_pending_records()
        elif cmd == "report":
            generate_summary_report()
        elif cmd == "update" and len(sys.argv) >= 7:
            update_result_manual(
                date=sys.argv[2], game=sys.argv[3], bet_type=sys.argv[4],
                result=sys.argv[5], profit=float(sys.argv[6]),
                notes=sys.argv[7] if len(sys.argv) > 7 else "",
            )
    else:
        validate_pending_records()
        generate_summary_report()
