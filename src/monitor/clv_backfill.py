"""CLV 回填分析 — 用 OddsPortal 收盘价(≈Pin收盘价) 对已结算注算 CLV。

随 OddsPortal 数据下载进度累积匹配样本, 最终判断 BB 赔率是否长期 > 收盘价。

用法: python3 -m src.monitor.clv_backfill [--detail]
"""
import json, csv, glob, os, re, statistics, argparse
from collections import defaultdict

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

RESULT_FILE = DATA_DIR / "clv_backfill.json"


def _norm(name):
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def _load_op_matches():
    """加载所有 OddsPortal 数据 → {(norm_home, norm_away): row}"""
    op = {}
    # OddsPortal 数据在 data/oddsportal/ (DATA_DIR 是 data/storage)
    for f in glob.glob(str(DATA_DIR.parent / 'oddsportal' / '*' / '*.csv')):
        try:
            for row in csv.DictReader(open(f)):
                h, a = _norm(row.get('home_team')), _norm(row.get('away_team'))
                if h and a:
                    op.setdefault((h, a), row)
        except Exception:
            continue
    return op


def _side_from_des(des):
    des = des or ''
    if '和' in des or '平' in des:
        return 'draw'
    if '客' in des:
        return 'away'
    return 'home'


def backfill_clv() -> dict:
    """对已结算注回填 CLV。返回统计。"""
    cn2en = {}
    tm_file = DATA_DIR / 'team_name_map.json'
    if tm_file.exists():
        tm = json.loads(tm_file.read_text())
        cn2en = {k: v for k, v in tm.items() if k != '_meta' and isinstance(v, str)}

    op = _load_op_matches()

    bets = json.loads((DATA_DIR / 'tracked_bets.json').read_text())['bets']
    settled = [b for b in bets if b.get('status') == 'settled']

    matched = []
    for b in settled:
        sport = b.get('sport', '')
        sub = b.get('sub_market', '')
        # 只算 moneyline 类盘口 (OddsPortal 覆盖的): football 1x2, 其他 2-way
        if sport == 'football' and sub != '1x2':
            continue
        if sport != 'football' and sub not in ('ml', '1x2', 'moneyline', ''):
            continue

        hp = b.get('home_pin', '') or cn2en.get(b.get('home', ''), b.get('home', ''))
        ap = b.get('away_pin', '') or cn2en.get(b.get('away', ''), b.get('away', ''))
        nh, na = _norm(hp), _norm(ap)
        if not nh or not na:
            continue
        row = op.get((nh, na)) or op.get((na, nh))
        if not row:
            for (oh, oa), r in op.items():
                if (nh and (nh in oh or oh in nh)) and (na and (na in oa or oa in na)):
                    row = r
                    break
        if not row:
            continue

        if sport == 'football':
            side = _side_from_des(b.get('designation', ''))
            close = float(row.get({'home': 'home_odds', 'draw': 'draw_odds', 'away': 'away_odds'}[side]) or 0)
        else:
            # 2-way: 按 designation 判断主/客
            des = b.get('designation', '') or ''
            side = 'away' if ('客' in des or 'away' in des.lower()) else 'home'
            close = float(row.get({'home': 'home_odds', 'away': 'away_odds'}[side]) or 0)

        bb = float(b.get('bb_odds') or 0)
        if close <= 1.0 or bb <= 1.0:
            continue
        # 匹配错误过滤: BB/收盘 差 >50% 几乎肯定是错配(不同比赛/主客颠倒)
        if bb / close > 1.5 or bb / close < 0.67:
            continue
        clv = (bb - close) / close * 100
        matched.append({
            'sport': sport, 'league': b.get('league', ''), 'sub': sub,
            'bb_odds': bb, 'close_odds': close, 'clv': round(clv, 2),
            'result': b.get('result', ''),
        })

    # 每次重算全量 (OddsPortal 数据增长 → 匹配数自然增长, 不手动累加避免重复)
    if matched:
        RESULT_FILE.write_text(json.dumps(
            {'samples': matched, 'updated': __import__('time').time()},
            ensure_ascii=False, indent=2))

    all_clvs = [m['clv'] for m in matched]
    stats = {
        'matched_now': len(matched),
    }
    if all_clvs:
        stats['median_clv'] = round(statistics.median(all_clvs), 2)
        stats['mean_clv'] = round(statistics.mean(all_clvs), 2)
        stats['positive_pct'] = round(sum(1 for c in all_clvs if c > 0) / len(all_clvs) * 100, 1)
    return stats


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--detail', action='store_true')
    args = ap.parse_args()
    s = backfill_clv()
    print(json.dumps(s, ensure_ascii=False, indent=2))
    if args.detail and RESULT_FILE.exists():
        samples = json.loads(RESULT_FILE.read_text()).get('samples', [])
        for m in samples[-20:]:
            print(f"  {m['sport']}/{m['league'][:14]} {m['sub']} bb={m['bb_odds']} close={m['close_odds']} clv={m['clv']}% {m['result']}")
