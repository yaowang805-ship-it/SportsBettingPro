"""CLV 归档库回溯 — 用 Pinnacle 归档库(pinnacle_odds_archive.db)算历史 CLV。

与 OddsPortal 回溯(clv_backfill.py)互补: 归档库存的是系统扫描时逐次抓的 Pinnacle
赔率快照(含低级别联赛), 能覆盖 OddsPortal 只覆盖主流联赛的盲区。

口径: CLV = (BB开盘价 - Pinnacle去抽水公平收盘价) / 公平收盘价 × 100
      与 clv_collector 的 true_clv 一致(用公平价, 不用含抽水的原始价)。
      1X2 三向去抽水; hc/ou 二向去抽水。

用法:
    .venv312/bin/python -m src.monitor.clv_archive_backfill [--detail] [--write]
"""
import sqlite3, json, re, unicodedata, statistics, argparse
from collections import defaultdict

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

RESULT_FILE = DATA_DIR / "clv_archive_results.json"


def _norm(name):
    """队名归一化: 小写 + 去重音 + 去非字母数字。"""
    s = (name or '').strip().replace('\xa0', ' ')
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _devig_2way(p1, p2):
    """2-way 去抽水(乘法比例法) → (fair1, fair2)。"""
    i1, i2 = 1.0 / p1, 1.0 / p2
    s = i1 + i2
    return 1.0 / (i1 / s), 1.0 / (i2 / s)


def _devig_3way(home_raw, draw_raw, away_raw):
    """3-way 去抽水 → (fair_home, fair_draw, fair_away)。"""
    ih, idr, ia = 1.0 / home_raw, 1.0 / draw_raw, 1.0 / away_raw
    s = ih + idr + ia
    return 1.0 / (ih / s), 1.0 / (idr / s), 1.0 / (ia / s)


def _load_archive():
    """归档库 → {(norm_home, norm_away): {market: fair_price}}。

    market 键: '1x2|home' / '1x2|draw' / '1x2|away' / 'hc|<line>|home' / 'hc|<line>|away'
              / 'ou|<line>|over' / 'ou|<line>|under'
    取每个 matchup 最后一次抓取的快照, 去抽水得公平收盘价。
    """
    db = sqlite3.connect(DATA_DIR / 'pinnacle_odds_archive.db')
    db.execute("PRAGMA busy_timeout=30000")  # 归档库可能被扫描写入, 30s等待避免读时报locked
    cur = db.cursor()
    rows = cur.execute('''
        SELECT matchup_id, home, away, designation, points, price, fetched_at
        FROM odds_archive
        WHERE period = 0 AND designation IN ('home', 'draw', 'away', 'over', 'under')
    ''').fetchall()
    db.close()

    # 按 matchup 取最后快照
    latest = {}
    for mid, h, a, des, pts, price, fa in rows:
        key = (mid, h, a)
        if key not in latest or fa > latest[key]['fa']:
            latest[key] = {'fa': fa, 'p': {}}
        if fa == latest[key]['fa']:
            latest[key]['p'][(des, pts)] = price

    out = {}
    for (mid, h, a), d in latest.items():
        nh, na = _norm(h), _norm(a)
        if not nh or not na:
            continue
        p = d['p']
        mkt = {}
        # 1X2 (points NULL)
        ml = {des: pr for (des, pts), pr in p.items() if pts is None and des in ('home', 'draw', 'away')}
        try:
            if 'draw' in ml and ml.get('home') and ml.get('away'):
                fh, fd, fa = _devig_3way(ml['home'], ml['draw'], ml['away'])
                mkt['1x2|home'], mkt['1x2|draw'], mkt['1x2|away'] = fh, fd, fa
            elif ml.get('home') and ml.get('away'):
                fh, fa = _devig_2way(ml['home'], ml['away'])
                mkt['1x2|home'], mkt['1x2|away'] = fh, fa
        except (ZeroDivisionError, ValueError, TypeError):
            pass
        # spread (home/away, points 非空)
        sp = [(des, pts, pr) for (des, pts), pr in p.items()
              if pts is not None and des in ('home', 'away')]
        for line in {pts for _, pts, _ in sp}:
            hm = next((pr for des, pts, pr in sp if des == 'home' and pts == line), None)
            aw = next((pr for des, pts, pr in sp if des == 'away' and pts == line), None)
            if hm and aw:
                try:
                    fh, fa = _devig_2way(hm, aw)
                    mkt[f'hc|{line}|home'], mkt[f'hc|{line}|away'] = fh, fa
                except (ZeroDivisionError, ValueError, TypeError):
                    pass
        # total (over/under, points 非空)
        tt = [(des, pts, pr) for (des, pts), pr in p.items()
              if pts is not None and des in ('over', 'under')]
        for line in {pts for _, pts, _ in tt}:
            ov = next((pr for des, pts, pr in tt if des == 'over' and pts == line), None)
            un = next((pr for des, pts, pr in tt if des == 'under' and pts == line), None)
            if ov and un:
                try:
                    fo, fu = _devig_2way(ov, un)
                    mkt[f'ou|{line}|over'], mkt[f'ou|{line}|under'] = fo, fu
                except (ZeroDivisionError, ValueError, TypeError):
                    pass
        if mkt:
            out[(nh, na)] = mkt
    return out


def _match_teams(nh, na, archive):
    if (nh, na) in archive:
        return (nh, na)
    if (na, nh) in archive:
        return (na, nh)
    for k in archive:
        if (nh and (nh in k[0] or k[0] in nh)) and (na and (na in k[1] or k[1] in na)):
            return k
    return None


def _parse_market(sub, des):
    """从 sub_market + designation 解析 (market, line, side)。返回 None 表示不支持。"""
    des = (des or '').replace(' ', '')
    m = re.search(r'\(([+-]?\d+(?:\.\d+)?)\)', des)
    line = m.group(1) if m else None
    if sub == '1x2':
        if '客' in des:
            return ('1x2', None, 'away')
        if '和' in des or '平' in des:
            return ('1x2', None, 'draw')
        return ('1x2', None, 'home')
    if sub == 'hc' and line is not None:
        side = 'away' if '客' in des else 'home'
        return ('hc', line, side)
    if sub == 'ou' and line is not None:
        side = 'under' if '小' in des else 'over'
        return ('ou', line, side)
    return None


def backfill_clv(write: bool = False) -> dict:
    archive = _load_archive()
    bets = json.loads((DATA_DIR / 'tracked_bets.json').read_text())['bets']

    results = []
    for b in bets:
        if not (b.get('home_pin') and b.get('away_pin')):
            continue
        parsed = _parse_market(b.get('sub_market', ''), b.get('designation', ''))
        if parsed is None:
            continue
        market, line, side = parsed
        k = _match_teams(_norm(b.get('home_pin')), _norm(b.get('away_pin')), archive)
        if not k:
            continue
        key = f"{market}|{line}|{side}" if line else f"{market}|{side}"
        close_fair = archive[k].get(key, 0)
        if not close_fair or close_fair <= 1.0:
            continue
        bb = float(b.get('bb_odds') or 0)
        if bb <= 1.0:
            continue
        if bb / close_fair > 1.6 or bb / close_fair < 0.6:
            continue  # 错配过滤
        clv = (bb - close_fair) / close_fair * 100
        results.append({
            'sport': b.get('sport', ''), 'league': b.get('league', ''), 'sub': b.get('sub_market', ''),
            'home': b.get('home_pin', ''), 'away': b.get('away_pin', ''),
            'bb_odds': bb, 'close_fair': round(close_fair, 3), 'clv': round(clv, 2),
            'result': b.get('result', ''),
        })

    stats = {'matched': len(results)}
    clvs = [r['clv'] for r in results]
    if clvs:
        stats['mean_clv'] = round(statistics.mean(clvs), 2)
        stats['median_clv'] = round(statistics.median(clvs), 2)
        stats['positive_pct'] = round(sum(1 for c in clvs if c > 0) / len(clvs) * 100, 1)
        by_sport = defaultdict(list)
        by_sub = defaultdict(list)
        for r in results:
            by_sport[r['sport']].append(r['clv'])
            by_sub[r['sub']].append(r['clv'])
        stats['by_sport'] = {s: round(statistics.mean(v), 2) for s, v in by_sport.items()}
        stats['by_sub'] = {s: round(statistics.mean(v), 2) for s, v in by_sub.items()}

    if write and results:
        RESULT_FILE.write_text(json.dumps(
            {'samples': results, 'stats': stats,
             'updated': __import__('time').time()},
            ensure_ascii=False, indent=2))

    return stats


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--detail', action='store_true')
    ap.add_argument('--write', action='store_true')
    args = ap.parse_args()
    s = backfill_clv(write=args.write)
    print(json.dumps(s, ensure_ascii=False, indent=2))
    if args.detail and RESULT_FILE.exists():
        d = json.loads(RESULT_FILE.read_text())
        for smp in d['samples']:
            print(f"  {smp['sport']:>18s} {smp['league'][:24]:24s} {smp['home']} vs {smp['away']} "
                  f"BB={smp['bb_odds']:.2f} close_fair={smp['close_fair']:.2f} CLV={smp['clv']:+.1f}%")
