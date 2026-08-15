"""OddsPortal 平均收盘价 → V5 权重矩阵校准。

读取 data/oddsportal/<sport>/<league>__<season>.csv (op_scraper 下载的 212 个联赛),
按赔率区间计算各联赛胜率, 补 football-data.co.uk 没覆盖的联赛。

足球: 3-way (home/draw/away)。篮球/棒球/冰球: 2-way (home/away)。
输出: config/oddsportal_calibrated.py
  ODDSPORTAL_1X2_DATA  = {中文联赛名: {bin: (wr, avg_odds, n)}}  (足球)
  ODDSPORTAL_ML_DATA   = {中文联赛名: {bin: (wr, avg_odds, n)}}  (2-way 运动)

用法: python3 scripts/calibrate_oddsportal.py
"""
import csv, sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "oddsportal"
OUTPUT = ROOT / "config" / "oddsportal_calibrated.py"

ODDS_BINS = [
    1.30, 1.50, 1.70, 1.90, 2.10, 2.30, 2.50, 2.70, 2.90,
    3.10, 3.30, 3.50, 3.70, 3.90, 4.20, 4.50, 4.80,
    5.20, 5.60, 6.00, 6.50, 7.00, 7.50, 8.00, 9.00,
    10.00, 12.00, 15.00, 20.00, float('inf'),
]

# OddsPortal slug → 中文联赛名 (与 BB/pinnacle_league_map 口径一致)
SLUG_TO_CN = {
    # ── 足球 ──
    "afc-champions-league": "亚冠联赛", "algeria-ligue-1": "阿尔及利亚甲级联赛",
    "argentina-primera-nacional": "阿根廷全国联赛", "australia-a-league": "澳大利亚甲级联赛",
    "austria-2-liga": "奥地利乙级联赛", "austria-bundesliga": "奥地利甲级联赛",
    "belarus-vysshaya-liga": "白俄罗斯超级联赛", "belgium-challenger-pro-league": "比利时挑战者联赛",
    "champions-league": "欧洲冠军联赛", "chile-primera-division": "智利甲级联赛",
    "concacaf-champions-cup": "中北美冠军杯", "conference-league": "欧会联赛",
    "copa-sudamericana": "南美杯", "costa-rica-primera-division": "哥斯达黎加甲级联赛",
    "croatia-hnl": "克罗地亚甲级联赛", "czech-republic-chance-liga": "捷克甲级联赛",
    "denmark-1st-division": "丹麦乙级联赛", "denmark-superliga": "丹麦超级联赛",
    "ecuador-liga-pro": "厄瓜多尔甲级联赛", "egypt-premier-league": "埃及甲级联赛",
    "england-championship": "英冠", "england-fa-cup": "英格兰足总杯",
    "england-league-one": "英甲", "england-league-two": "英乙",
    "eredivisie": "荷甲", "estonia-meistriliiga": "爱沙尼亚甲级联赛",
    "europa-league": "欧罗巴联赛", "finland-veikkausliiga": "芬兰超级联赛",
    "france-coupe-de-france": "法国杯", "france-ligue-1": "法甲", "france-ligue-2": "法乙",
    "germany-bundesliga": "德甲", "germany-bundesliga-2": "德乙", "germany-dfb-pokal": "德国杯",
    "greece-super-league": "希超", "india-isl": "印度超级联赛",
    "ireland-premier-division": "爱尔兰超级联赛", "italy-coppa-italia": "意大利杯",
    "italy-serie-a": "意甲", "japan-j1-league": "日本J1联赛",
    "jupiler-pro-league": "比甲", "liga-portugal": "葡超",
    "mexico-liga-de-expansion": "墨西哥扩展联赛", "morocco-botola-pro": "摩洛哥甲级联赛",
    "netherlands-eerste-divisie": "荷乙", "northern-ireland-nifl-premiership": "北爱尔兰超级联赛",
    "peru-liga-1": "秘鲁甲级联赛", "poland-ekstraklasa": "波兰超级联赛",
    "qatar-qsl": "卡塔尔星级联赛", "romania-liga-2": "罗马尼亚乙级联赛",
    "romania-superliga": "罗马尼亚甲级联赛", "russia-premier-league": "俄罗斯超级联赛",
    "saudi-professional-league": "沙特阿拉伯职业联赛", "scotland-championship": "苏格兰冠军联赛",
    "scotland-premiership": "苏超", "south-africa-premiership": "南非超级联赛",
    "spain-copa-del-rey": "西班牙国王杯", "spain-laliga": "西甲", "spain-laliga2": "西乙",
    "switzerland-challenge-league": "瑞士挑战者联赛", "switzerland-super-league": "瑞士超级联赛",
    "turkey-1-lig": "土耳其甲级联赛", "turkey-super-lig": "土超",
    "uae-league": "阿联酋联赛", "uefa-nations-league": "欧国联",
    "ukraine-premier-league": "乌克兰超级联赛", "usa-usl-championship": "美国USL冠军联赛",
    "uzbekistan-super-league": "乌兹别克斯坦超级联赛", "wales-cymru-premier": "威尔士超级联赛",
    # ── 篮球 ──
    "a-division-slovakia": "斯洛伐克甲级联赛", "bbl-germany": "德国BBL联赛",
    "bnxt-league": "BNXT联赛", "cba-china": "中国CBA联赛", "euroleague": "欧洲篮球联赛",
    "greek-basket-league": "希腊篮球联赛", "korisliiga-finland": "芬兰篮球联赛",
    "lbl-latvia": "拉脱维亚篮球联赛", "liga-aba": "亚得里亚海联赛",
    "liga-leumit-israel": "以色列篮球联赛", "liga-uruguay": "乌拉圭篮球联赛",
    "lkl-lithuania": "立陶宛篮球联赛", "lnb-france": "法国篮球联赛", "nba": "NBA",
    "nbl-australia": "澳大利亚篮球联赛", "nemzeti-hungary": "匈牙利篮球联赛",
    "prva-crna-gora": "黑山篮球联赛", "super-lig-turkey": "土耳其篮球联赛",
    "vtb-united-league": "VTB联合联赛",
    # ── 棒球 / 冰球 ──
    "japan-baseball-league": "日本棒球联赛", "mlb": "MLB 美国职业棒球大联盟",
    "del": "德国冰球联赛", "khl": "大陆冰球联赛", "nhl": "NHL 美国职业冰球联赛",
    "shl": "瑞典冰球联赛",
}


def bin_index(odds):
    for i, t in enumerate(ODDS_BINS):
        if odds < t:
            return i
    return len(ODDS_BINS) - 1


def _f(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _result(flag):
    return 1 if flag == "H" else (-1 if flag == "A" else 0)


def process_file(path, cn_name, three_way, data):
    """累加胜率。three_way=True 表示 3-way(有平局), 否则 2-way。"""
    try:
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
    except Exception:
        return 0
    n = 0
    for r in rows:
        hs = _f(r.get("home_score"))
        as_ = _f(r.get("away_score"))
        if hs is None or as_ is None:
            continue
        res = _result("H" if hs > as_ else ("A" if as_ > hs else "D"))
        # 主场
        ho = _f(r.get("home_odds"))
        if ho and 1.01 <= ho <= 51.0:
            bi = bin_index(ho)
            data[cn_name][bi][1] += 1
            data[cn_name][bi][2] += ho
            if res == 1:
                data[cn_name][bi][0] += 1
        # 平局 (3-way only)
        if three_way:
            do = _f(r.get("draw_odds"))
            if do and 1.01 <= do <= 51.0:
                bi = bin_index(do)
                data[cn_name][bi][1] += 1
                data[cn_name][bi][2] += do
                if res == 0:
                    data[cn_name][bi][0] += 1
        # 客场
        ao = _f(r.get("away_odds"))
        if ao and 1.01 <= ao <= 51.0:
            bi = bin_index(ao)
            data[cn_name][bi][1] += 1
            data[cn_name][bi][2] += ao
            if res == -1:
                data[cn_name][bi][0] += 1
        n += 1
    return n


def main():
    football = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))
    ml = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))
    total = {"football": 0, "ml": 0}

    for sport in ["football", "basketball", "baseball", "ice-hockey"]:
        d = DATA_DIR / sport
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.csv")):
            slug = p.stem.split("__")[0]
            cn = SLUG_TO_CN.get(slug)
            if not cn:
                continue
            if sport == "football":
                total["football"] += process_file(p, cn, three_way=True, data=football)
            else:
                total["ml"] += process_file(p, cn, three_way=False, data=ml)

    # _AGGREGATE
    def _agg(data):
        agg = defaultdict(lambda: [0, 0, 0.0])
        for lg, bins in data.items():
            for bi, (w, b, s) in bins.items():
                agg[bi][0] += w; agg[bi][1] += b; agg[bi][2] += s
        return agg

    def _fmt(data, min_n):
        out = {}
        for lg, bins in sorted(data.items()):
            out[lg] = {}
            for bi in sorted(bins):
                w, b, s = bins[bi]
                if b >= min_n:
                    out[lg][bi] = (round(w / b, 4), round(s / b, 4), b)
        return out

    fb_agg = _agg(football)
    ml_agg = _agg(ml)
    fb_out = _fmt(football, 30)
    ml_out = _fmt(ml, 20)
    fb_out["_AGGREGATE"] = {bi: (round(w / b, 4), round(s / b, 4), b)
                            for bi, (w, b, s) in sorted(fb_agg.items()) if b >= 30}
    ml_out["_AGGREGATE"] = {bi: (round(w / b, 4), round(s / b, 4), b)
                            for bi, (w, b, s) in sorted(ml_agg.items()) if b >= 20}

    def _write(name, data):
        lines = [f"{name} = {{"]
        for lg in sorted(data):
            if not data[lg]:
                continue
            lines.append(f'    "{lg}": {{')
            for bi in sorted(data[lg]):
                wr, avg_o, n = data[lg][bi]
                lines.append(f"        {bi}: ({wr}, {avg_o}, {n}),")
            lines.append("    },")
        lines.append("}")
        return lines

    out_lines = [
        '"""OddsPortal 平均收盘价校准数据 (op_scraper 下载的 212 联赛)。',
        "格式: {中文联赛名: {bin: (win_rate, avg_odds, n)}}。",
        f"生成: 足球 {total['football']:,} 场 | 篮球/棒球/冰球 2-way {total['ml']:,} 场",
        "顶级联赛折扣 0.9, 小联赛 0.75 (get_oddsportal_discount)。",
        '"""',
        "",
    ]
    out_lines += _write("ODDSPORTAL_1X2_DATA", fb_out)
    out_lines.append("")
    out_lines += _write("ODDSPORTAL_ML_DATA", ml_out)
    out_lines.append("")
    OUTPUT.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"✅ OddsPortal 校准完成: 足球 {total['football']:,} 场 ({len(fb_out)-1} 联赛), "
          f"2-way {total['ml']:,} 场 ({len(ml_out)-1} 联赛) → {OUTPUT.name}")


if __name__ == "__main__":
    main()
