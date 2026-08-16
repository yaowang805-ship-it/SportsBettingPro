"""CLV 回填分析 — 用 OddsPortal 收盘价(≈Pin收盘价) 对已结算注算 CLV。

V5.7 重写:
  1. 覆盖更多盘口(1X2 + HC/OU/DC/DNB/BTTS/OE), 不再只算 1X2
  2. 队名归一化加 unicode 变体折叠(重音/Björk→bjork, 全角)
  3. 市场数据从 data/oddsportal_markets/(盘口收盘价) 读

随 OddsPortal 数据下载进度累积匹配样本, 最终判断 BB 赔率是否长期 > 收盘价。
"""
import json, csv, glob, re, statistics, argparse, unicodedata
from collections import defaultdict

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

RESULT_FILE = DATA_DIR / "clv_backfill.json"


def _norm(name):
    """队名归一化: 小写 + 去重音 + 去非字母数字。"""
    s = (name or '').strip().replace('\xa0', ' ')
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))  # 去重音
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _load_op_1x2():
    """OddsPortal 1X2 数据 → {(norm_home, norm_away): row}"""
    op = {}
    for f in glob.glob(str(DATA_DIR.parent / 'oddsportal' / '*' / '*.csv')):
        try:
            for row in csv.DictReader(open(f)):
                h, a = _norm(row.get('home_team')), _norm(row.get('away_team'))
                if h and a:
                    op.setdefault((h, a), row)
        except Exception:
            continue
    return op


def _load_op_markets():
    """OddsPortal 盘口数据 → {(norm_home, norm_away): {market_key: [rows]}}
    market_key 如 'hc|-0.5|home' / 'ou|3.0|under' / 'btts||yes'。"""
    op = defaultdict(lambda: defaultdict(list))
    for f in glob.glob(str(DATA_DIR.parent / 'oddsportal_markets' / '*' / '*.csv')):
        try:
            for row in csv.DictReader(open(f)):
                h, a = _norm(row.get('home')), _norm(row.get('away'))
                if not h or not a:
                    continue
                market = (row.get('market') or '').strip()
                side = (row.get('side') or '').strip()
                line = (row.get('line') or '').strip()
                try:
                    avg = float(row.get('avg_odds') or 0)
                except (ValueError, TypeError):
                    continue
                if avg <= 1.0:
                    continue
                key = f"{market}|{line}|{side}"
                op[(h, a)][key].append(avg)
        except Exception:
            continue
    # 平均多个 season 的收盘价
    out = {}
    for k, d in op.items():
        out[k] = {mk: sum(v) / len(v) for mk, v in d.items()}
    return out


def _parse_designation(sub, des):
    """从 sub_market + designation 解析 (market, line, side)。返回 None 表示不支持。"""
    des = (des or '').replace(' ', '')
    line = None
    m = re.search(r'\(([+-]?\d+(?:\.\d+)?)\)', des)
    if m:
        line = m.group(1)

    if sub == '1x2':
        if '和' in des or '平' in des:
            return ('1x2', None, 'draw')
        if '客' in des:
            return ('1x2', None, 'away')
        return ('1x2', None, 'home')
    if sub == 'hc':
        side = 'away' if '客' in des else 'home'
        return ('hc', line, side)
    if sub == 'ou':
        side = 'under' if '小' in des else 'over'
        return ('ou', line, side)
    if sub == 'dc':
        if '和' in des and '客' in des:
            return ('dc', None, 'draw/away')
        if '主' in des and '客' in des:
            return ('dc', None, 'home/away')
        return ('dc', None, 'home/draw')
    if sub == 'dnb':
        side = 'away' if '客' in des else 'home'
        return ('dnb', None, side)
    if sub == 'btts':
        side = 'no' if ('否' in des or 'no' in des.lower()) else 'yes'
        return ('btts', None, side)
    if sub == 'oe':
        side = 'even' if ('双' in des or 'even' in des.lower()) else 'odd'
        return ('oe', None, side)
    return None


def _match_teams(nh, na, op_dict):
    """匹配 (nh, na) 到 op_dict 的键, 返回键或 None。"""
    if (nh, na) in op_dict:
        return (nh, na)
    if (na, nh) in op_dict:
        return (na, nh)
    # 子串模糊(允许 IK Sirius vs Sirius)
    for (oh, oa) in op_dict:
        if (nh and (nh in oh or oh in nh)) and (na and (na in oa or oa in na)):
            return (oh, oa)
    return None


def backfill_clv() -> dict:
    cn2en = {}
    tm_file = DATA_DIR / 'team_name_map.json'
    if tm_file.exists():
        tm = json.loads(tm_file.read_text())
        cn2en = {k: v for k, v in tm.items() if k != '_meta' and isinstance(v, str)}

    op_1x2 = _load_op_1x2()
    op_mk = _load_op_markets()

    bets = json.loads((DATA_DIR / 'tracked_bets.json').read_text())['bets']
    settled = [b for b in bets if b.get('status') == 'settled']

    matched = []
    for b in settled:
        sub = b.get('sub_market', '')
        parsed = _parse_designation(sub, b.get('designation', ''))
        if not parsed:
            continue  # ht/ht_dc 等半场盘口无收盘价数据, 跳过
        market, line, side = parsed

        hp = b.get('home_pin', '') or cn2en.get(b.get('home', ''), b.get('home', ''))
        ap = b.get('away_pin', '') or cn2en.get(b.get('away', ''), b.get('away', ''))
        nh, na = _norm(hp), _norm(ap)
        if not nh or not na:
            continue

        close = 0
        if market == '1x2':
            key = _match_teams(nh, na, op_1x2)
            if key:
                row = op_1x2[key]
                col = {'home': 'home_odds', 'draw': 'draw_odds', 'away': 'away_odds'}[side]
                close = float(row.get(col) or 0)
        else:
            key = _match_teams(nh, na, op_mk)
            if key:
                mk = op_mk[key]
                # 让球/大小要匹配线; 其它不匹配线
                if market in ('hc', 'ou'):
                    close = mk.get(f"{market}|{line}|{side}", 0)
                else:
                    close = mk.get(f"{market}||{side}", 0)

        bb = float(b.get('bb_odds') or 0)
        if close <= 1.0 or bb <= 1.0:
            continue
        if bb / close > 1.5 or bb / close < 0.67:
            continue  # 错配过滤
        clv = (bb - close) / close * 100
        matched.append({
            'sport': b.get('sport', ''), 'league': b.get('league', ''), 'sub': sub,
            'bb_odds': bb, 'close_odds': round(close, 3), 'clv': round(clv, 2),
            'result': b.get('result', ''),
        })

    if matched:
        RESULT_FILE.write_text(json.dumps(
            {'samples': matched, 'updated': __import__('time').time()},
            ensure_ascii=False, indent=2))

    all_clvs = [m['clv'] for m in matched]
    stats = {'matched_now': len(matched)}
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
