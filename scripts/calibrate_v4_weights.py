#!/usr/bin/env python3
"""
V4.5 权重矩阵校准 — 从 Pinnacle 历史 CSV 提取真实胜率
数据源: data/pinnacle_historical/ (275+ 联赛, football-data.co.uk 格式)
输出: 可直接粘贴到 config/weight_matrix_v4.py 的 Python dict
"""
import csv, sys, json
from collections import defaultdict
from pathlib import Path
from datetime import datetime

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
DATA = SRC / "data" / "pinnacle_historical"

ODDS_BINS = [1.25, 1.39, 1.60, 1.80, 2.00, 2.19, 2.40, 2.60, 2.80, 3.00,
             3.20, 3.40, 3.60, 3.80, 4.05, 4.35, 4.65, 5.00, 5.40, 5.80,
             6.25, 6.75, 7.25, 7.75, 8.50, 9.50, 11.00, 13.50, 17.50, 25.00]

OU_BINS = [1.42, 1.62, 1.80, 1.99, 2.18, 2.38, 2.58, 2.78, 3.00, 3.20, 3.38]

def bin_idx(odds, bins):
    for i, t in enumerate(bins):
        if odds <= t: return i
    return len(bins)

def calibrate_football():
    """从 Pinnacle 历史 CSV 提取 1X2/OU/AH 权重."""
    LEAGUE_DIRS = [d for d in DATA.iterdir() if d.is_dir() and len(d.name) <= 4 and d.name.isalnum()]

    # 联赛名映射
    LEAGUE_NAMES = {"E0":"英超","E1":"英冠","E2":"英甲","E3":"英乙","EC":"英议联",
                    "SP1":"西甲","SP2":"西乙","D1":"德甲","D2":"德乙",
                    "I1":"意甲","I2":"意乙","F1":"法甲","F2":"法乙",
                    "P1":"葡超","P2":"葡甲","N1":"荷甲","B1":"比甲",
                    "T1":"土超","G1":"希超","SC0":"苏超","SC1":"苏甲",
                    "SC2":"苏乙","SC3":"苏丙","ECU":"厄瓜多尔甲"}

    ml_data = {}   # {league: {bin: (wr, avg_o, n)}}
    ou_data = {}   # {league: {bin: (wr, avg_o, n)}}
    ah_data = defaultdict(lambda: [0, 0, 0.0])  # aggregate AH only for now

    total_rows = 0
    for d in sorted(LEAGUE_DIRS):
        league_cn = LEAGUE_NAMES.get(d.name, d.name)
        if league_cn not in LEAGUE_NAMES.values() and d.name not in LEAGUE_NAMES:
            if len(d.name) <= 3:  # Skip non-league dirs
                continue

        ml_bins = defaultdict(lambda: [0, 0, 0.0])
        ou_bins = defaultdict(lambda: [0, 0, 0.0])

        for csv_file in sorted(d.glob("*.csv")):
            try:
                with open(csv_file, encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        # 1X2
                        try:
                            psh = float(row.get("PSH", 0) or 0)
                            psd = float(row.get("PSD", 0) or 0)
                            psa = float(row.get("PSA", 0) or 0)
                        except: continue
                        if psh <= 1 or psd <= 1 or psa <= 1: continue

                        ftr = (row.get("FTR", "") or "").strip()
                        if ftr not in ("H", "D", "A"): continue

                        total_rows += 1

                        # ML
                        for odds, outcome in [(psh, ftr=="H"), (psd, ftr=="D"), (psa, ftr=="A")]:
                            bi = bin_idx(odds, ODDS_BINS)
                            if bi >= len(ODDS_BINS): continue
                            w, t, s = ml_bins[bi]
                            ml_bins[bi] = [w + (1 if outcome else 0), t + 1, s + odds]

                        # OU (P>2.5, P<2.5)
                        try:
                            po = float(row.get("P>2.5", 0) or 0)
                            pu = float(row.get("P<2.5", 0) or 0)
                        except: po = pu = 0
                        if po > 1 and pu > 1:
                            total_goals = int(row.get("FTHG",0) or 0) + int(row.get("FTAG",0) or 0)
                            # OU 2.5: over wins if total > 2.5
                            for odds, is_win in [(po, total_goals > 2.5), (pu, total_goals < 2.5)]:
                                bi = bin_idx(odds, OU_BINS)
                                if bi >= len(OU_BINS): continue
                                w, t, s = ou_bins[bi]
                                ou_bins[bi] = [w + (1 if is_win else 0), t + 1, s + odds]

                        # AH
                        try:
                            pahh = float(row.get("PAHH", 0) or 0)
                            paha = float(row.get("PAHA", 0) or 0)
                            ah_line = float(row.get("AHh", 0) or 0)
                        except: pahh = paha = 0; ah_line = 0
                        if pahh > 1 and paha > 1:
                            home_goals = int(row.get("FTHG",0) or 0)
                            away_goals = int(row.get("FTAG",0) or 0)
                            adjusted = home_goals + ah_line  # AH line is negative for home favorite
                            if adjusted > away_goals:
                                ah_win = "H"
                            elif adjusted == away_goals:
                                ah_win = "push"
                            else:
                                ah_win = "A"
                            for odds, is_win in [(pahh, ah_win=="H"), (paha, ah_win=="A")]:
                                bi = bin_idx(odds, ODDS_BINS)
                                if bi >= len(ODDS_BINS): continue
                                w, t, s = ah_data[bi]
                                ah_data[bi] = [w + (1 if is_win else 0), t + 1, s + odds]

            except Exception as e:
                continue

        # Store per-league results
        if ml_bins:
            ml_out = {}
            for bi in sorted(ml_bins):
                w, t, s = ml_bins[bi]
                if t >= 30:
                    ml_out[bi] = (round(w/t,3), round(s/t,2), t)
            if ml_out:
                ml_data[league_cn] = ml_out

        if ou_bins:
            ou_out = {}
            for bi in sorted(ou_bins):
                w, t, s = ou_bins[bi]
                if t >= 20:
                    ou_out[bi] = (round(w/t,3), round(s/t,2), t)
            if ou_out:
                ou_data[league_cn] = ou_out

    # ── 聚合 ──
    ml_agg = defaultdict(lambda: [0, 0, 0.0])
    ou_agg = defaultdict(lambda: [0, 0, 0.0])
    for lg_data in ml_data.values():
        for bi, (wr, avg_o, n) in lg_data.items():
            ml_agg[bi][0] += int(wr * n); ml_agg[bi][1] += n; ml_agg[bi][2] += avg_o * n
    for lg_data in ou_data.values():
        for bi, (wr, avg_o, n) in lg_data.items():
            ou_agg[bi][0] += int(wr * n); ou_agg[bi][1] += n; ou_agg[bi][2] += avg_o * n

    ml_agg_out = {}
    for bi in sorted(ml_agg):
        w, t, s = ml_agg[bi]
        if t >= 50:
            ml_agg_out[bi] = (round(w/t,3), round(s/t,2), t)

    ou_agg_out = {}
    for bi in sorted(ou_agg):
        w, t, s = ou_agg[bi]
        if t >= 50:
            ou_agg_out[bi] = (round(w/t,3), round(s/t,2), t)

    ah_agg_out = {}
    for bi in sorted(ah_data):
        w, t, s = ah_data[bi]
        if t >= 30:
            ah_agg_out[bi] = (round(w/t,3), round(s/t,2), t)

    # ── 输出 ──
    print(f"# 扫描: {total_rows} 场比赛, {len(LEAGUE_DIRS)} 个联赛目录")

    print(f"\n# PIN_1X2_DATA ({len(ml_data)} 联赛 + _AGGREGATE):")
    print("PIN_1X2_DATA = {")
    for lg in sorted(ml_data):
        print(f'    "{lg}": {json.dumps(ml_data[lg], ensure_ascii=False)},')
    print(f'    "_AGGREGATE": {json.dumps(ml_agg_out, ensure_ascii=False)},')
    print("}")

    print(f"\n# PIN_OU_DATA ({len(ou_data)} 联赛 + _AGGREGATE):")
    print("PIN_OU_DATA = {")
    for lg in sorted(ou_data):
        print(f'    "{lg}": {json.dumps(ou_data[lg], ensure_ascii=False)},')
    print(f'    "_AGGREGATE": {json.dumps(ou_agg_out, ensure_ascii=False)},')
    print("}")

    print(f"\n# PIN_AH_DATA ({len(ah_agg_out)} bins aggregate):")
    print(f"PIN_AH_DATA = {json.dumps(ah_agg_out, ensure_ascii=False)}")

    return ml_data, ou_data, ah_agg_out

if __name__ == "__main__":
    ml, ou, ah = calibrate_football()
    # 保存为 JSON 供 weight_matrix_v4.py 加载
    result = {"PIN_1X2_DATA": ml, "PIN_OU_DATA": ou, "PIN_AH_DATA": ah}
    out_path = SRC / "data" / "storage" / "v4_calibrated_weights.json"
    with open(out_path, "w") as f:
        json.dump(result, f, ensure_ascii=False)
    print(f"\n✅ 已保存到 {out_path} ({len(ml)} 1X2联赛, {len(ou)} OU联赛, {len(ah)} AH bins)")
