#!/usr/bin/env python3
"""盘口释放清单计算 — 真实结算赢率 vs 隐含 + 早盘收盘线 CLV 闸门。

核心思想:
    比价套利的"哪些盘口能投"看两层: 真实结算赢率是否 > 隐含, 且早盘格子收盘线 CLV 必须为正。

    2026-09-15 用户纠正(重要): 早盘「收盘线 CLV」是职业团队唯一最看重的 edge 指标, 不能
    用「赢率 vs Pin下注时价隐含」去推翻它 —— 那个隐含是 Pin 下注时价, 自带 favorite-longshot
    bias(冷门虚高/热门虚低), 差值会被偏差污染。负 CLV = 逆向选择(线朝你反向走), 即使
    「赢率>下注时隐含」为正也可能是 Pin 自身偏差造的假象。故加 CLV 闸门: 早盘格子中位
    true_clv_pct < 0 一律不释放(见 live-clv-capture-rate-fix-20260915)。
    注: 旧头「CLV 假正」结论只针对滚球「稍后价 CLV」(软书滞后), 不适用早盘「收盘线 CLV」。

    2026-10-18 再纠正(实证): 早盘收盘价本身仍有 +2.2pp 残留偏差(和局档收盘后仍高估),
    正 CLV 的注实际亏钱(Whelan: 3/5 正CLV档仍亏)。三路对比: 原始 bb vs fair 全留 ROI -2.7%
    (没分开) / 真实CLV +2.7% / 结果锚校准 +14.65%。故早盘释放「决策尺」从 CLV 换成「结果锚
    校准」(校准到实际胜率, 去除热门/和局假溢价), CLV 降级为「事后验证尺」(见 observe_clv_validation)。
    这不是推翻 CLV——CLV 仍是正确的事后验证(下注价是否赢过收盘), 只是下注那一刻没有收盘价,
    决策只能用「预测真实概率」的结果锚校准。

释放规则(混合粒度, 三通道):
    1. 主开关(运动×盘口):      实盘 ROI > 4% 且 n≥30 → 释放。
    2. 联赛细化(运动×联赛×盘口): 某联赛三维格子 n≥30 时, 用该联赛自己的 ROI 覆盖主开关
                                (ROI>4% 释放, 否则封杀)。
    3. 观察库释放(运动×盘口×方向×概率桶×来源): 早盘用「结果锚校准」(校准后 ev_cal>0 的注
                                ROI>0 且 n≥20) → 释放; CLV 降级为验证/审计字段(2026-10-18)。
                                滚球(scope=live)仍用 n≥20 + 纸面ROI>0(校准过不了时间切分)。
    其余盘口一律封杀(只观察积累, 不投注)。

数据源:
    实盘 ROI   = data/storage/tracked_bets.json (status=settled 的 profit/stake)
    观察库 CLV = data/storage/clv_results.csv   (source=validate 的 true_clv_pct, dc/btts 剔除改版前)
    观察库 ROI = data/storage/paper_bets.json   (纸面投注结算结果, 由 paper_settle 生成, 含 league)

输出:
    data/storage/market_release.json
    {
      "generated_at": "...",
      "market_released":  [["football","1x2"], ...],        # 主开关(运动×盘口)
      "league_released":  [["football","MLS","hc"],...],    # 联赛细化(释放)
      "league_blocked":   [["football","X","hc"],...],      # 联赛细化(封杀, 覆盖主开关)
      "observe_released": [["football","X","ht"], ...],     # 观察库三维释放
    }

用法: .venv312/bin/python scripts/compute_market_release.py
"""
import json
import math
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"
TRACKED = DATA / "tracked_bets.json"
PAPER = DATA / "paper_bets.json"
OUT = DATA / "market_release.json"
PENDING = DATA / "pending_release_approval.json"  # 待确认的释放/封锁变更(2026-09-22 用户要求确认后执行)

REAL_ROI_MIN = 4.0     # 实盘 ROI 释放阈值(%)
N_REAL_MIN = 50        # 实盘赢率采信最小样本量(2026-09-12 30→100→50: 实盘真金白银质量高, 50够; 观察库才要100)

# ── 2026-09-12 观察库释放改造(用户要求): 样本>100 且 赢率>隐含 才释放 ──
# 判据从「CLV中位>0 + 正率>55% + 纸面ROI>0」改为「赢率 > 隐含」(CURRENT_STATUS 核心判据),
# 样本门槛从 n≥30 提到 n>100。滚球+早盘观察库合并统计, 滚球 league 归 "滚球"。
# 2026-09-13: n>100 → n>200, 对齐职业 CLV 有效标准(CLV 从 n≥200 才统计有效, <100 是噪声)。
OBS_N_MIN = 200            # 早盘观察库释放采信最小结算样本数(>200, 对齐职业CLV标准)。单位=笔
OBS_N_MIN_LIVE = 50        # 滚球释放样本门槛(2026-11-01 从 30 提到 50, 口径=已结算样本数):
                           # 原 10-11 从 200 降到 20(单 grid 攒 200 笔几个月达不到 → 数据驱动形同虚设),
                           # 但 20 太少(SE≈11%)且当时 n 口径是「价格变动样本」(依赖 clv 字段, 已断)。
                           # 10-31 改口径=已结算样本数(不依赖 clv) + 提到 30; 11-01 再提到 50(SE≈7.1%),
                           # 因足球小球释放后实盘 -44.8% 印证纸面正仍可能是假正, 提高样本门槛挡噪声。单位=笔。
OBS_LIVE_ROI_MIN = 2.0     # 滚球释放纸面ROI门槛(2026-10-31 用户要求): 纸面ROI>2% 才释放(挡 0~2% 的噪声正格)

# 滚球负段黑名单(2026-11-01 实盘 ROI 矩阵推导, 治本): 用「平均隐含概率」判断, 绕过概率桶标签运动定制的复杂性。
# 核心规律 = 中间概率段(50-60%)系统性出血: 独赢(1x2) 30-70% 负(50-60%最负 -41.2%/60注),
# 让球(hc) 50-70% 负(-11.4%/368注); 大小球(ou)/网球让局(hc_games) 全线负 → None 表示该盘口全拦。
LIVE_NEGATIVE_IMPLIED = {
    '1x2': (30.0, 70.0),      # 独赢中间段 - 保留(Shin假溢价, tier3)
    'hc': (0.0, 0.0),         # 2026-11-05 释放: line bug已修, 让球中间段不再拦(tier1)
    'dc': None,               # 2026-11-05 dc(双重机会, 从1x2推导公平价)彻底停投+停入库, 全拦
    'ou': (0.0, 0.0),         # 2026-11-05 释放: line bug已修, 大小球全线负不再拦(tier1)
    'hc_games': (0.0, 0.0),   # 2026-11-05 释放: 网球让局同 line bug(tier1)
}
# 滚球观察库 14 天时间窗(2026-10-11): 保险, 非调参。防未来换锚点/换公平价源时旧样本混进新 grid 的 n。
# 纯过滤, 不动阈值不动判据。env 开关默认开(回退用), 与 ENFORCE_REAL_ROI_GATE_ON_MANUAL 同待遇。
LIVE_CLV_WINDOW_ENABLED = os.getenv("LIVE_CLV_WINDOW_ENABLED", "true").lower() in ("1", "true", "yes")
LIVE_CLV_WINDOW_DAYS = 14.0  # 窗口天数

# 2026-11-05 干净数据起点: line bug修复(11-04)+滚球platt校准(11-05)之前的历史观察是「错线+Shin bias+未校准」
# 时期采的, fair/ev/ROI 全是脏的。释放判据只看此之后的干净样本(宁缺毋滥), 防止 cron 用脏数据把已释放的格重新拦回。
CLEAN_DATA_CUTOFF = 1794009600  # 2026-11-07 00:00 UTC
OBS_CLV_MIN = 2.0          # 早盘释放中位CLV阈值(>2% no-vig CLV): 职业sharp选手平均+2~5%, 单位=%edge(收盘线CLV)
                            # >0太松(+0.5%以下是devig/匹配噪声), >2 既严格又留容错(2026-09-15 用户定)
OBS_LEV_MIN = 0.4          # [审计字段, 非释放闸门] 滚球 LEV(价格变化方向 clv-ev)阈值, 单位=%edge(3s复验)
                            # 2026-10-11 降级: LEV 是 3s 复验, 信噪比接近 0(实测中位 -0.36pp), 只落审计/诊断, 不再当释放判据。
                            # 释放判据改为 n≥OBS_N_MIN_LIVE + obsROI>0 + REAL_ROI_GATE(见 live 释放循环)。
OBS_WINRATE_EDGE_MIN = 3.0 # 赢率>隐含(差>3pp 才释放)。2026-09-15 0→3pp: 观察库整体负edge(逆向选择),
                            # 差0pp会把打平的噪声格子释放; 对齐实盘 REAL_WINRATE_EDGE_MIN=3pp 去噪声
# 2026-10-18 早盘释放判据改「结果锚校准」: 下注时 Pin 公平价系统性高估热门/和局(隐含胜率比实际
# 高 ~7pp), 导致 CLV(bb vs 收盘价)也继承收盘价 +2.2pp 残留偏差(Whelan: 3/5 正CLV档仍亏)。
# 实测三路对比: 原始 bb vs fair 全留 ROI -2.7%(没分开) / 真实CLV +2.7% / 结果锚校准 +14.65%。
# 结论: 释放决策用「结果锚校准」(校准到实际胜率), CLV 降级为事后验证尺(验证有没有赢过收盘)。
OBS_N_CAL_MIN = 20          # 结果锚校准释放最小样本(校准后 ev_cal>0 的注数, 2026-10-18)
REAL_WINRATE_EDGE_MIN = 3.0  # 实盘主开关/方向释放赢率vs隐含差值阈值(差>3pp 去噪声)
LIQUIDITY_MAX_SPREAD = 6.0   # Betfair 流动性门槛(2026-09-19): 格子中位 back-lay 价差 ≥6% 判薄盘,
                             # 公平价/CLV 不可信(低流动性联赛 Betfair 高估冷门方向 → 假溢价), 不采信不释放。
                             # 6% 是初值(基于「让球真edge价差<3% vs 大小球假edge价差6~15%」反推), 待攒 spread 数据后标定
REAL_GAP_N_MIN = 20          # 实盘-纸单 gap 指标的最小实盘样本数(只展示, <20 的 gap 是噪声, 2026-09-19)
REAL_ROI_GATE_N_MIN = 20     # 实盘ROI交叉验证最小样本(2026-11-01 从 5 提到 20): 有≥20笔实盘且ROI<0 才拦,
                             # 5 太少(5笔负可能是噪声, 误撤真edge格), 20 才够统计置信(SE≈11%)。用户要求。
LIVE_CIRCUIT_BREAKER_N_MIN = 30  # 2026-10-28 滚球熔断: 已释放滚球格 新投注>30单 且 ROI<0 → 自动撤回
EARLY_CIRCUIT_BREAKER_N_MIN = 10  # 2026-10-30 早盘熔断: 已释放早盘格 新投注>10单 且 ROI<0 → 自动撤回
DROP_N_MIN = 10  # 2026-11-01 drop 源数据驱动释放最小样本: drop 实盘稀疏(245笔/全时段), 10 笔(SE≈32%)才敢放,
                 # 5 笔 SE≈45% 纯噪声(6笔4赢2输就+77%假edge)。只作「已确认正/负」的硬闸, 不足 10 笔走 DROP_BLOCK 兜底。

# 放弃的特殊盘口(2026-09-15 用户决定): 这些盘口 margin 15%+ (vs 主盘口 3-8%), 收盘线不 sharp,
# 且 clv_collector 里 close_fair 用的是 proportional devig(非 Shin), CLV 虚高无意义。
# 套利团队放弃: 正确比分/半全场/先进球/精确进球/净胜球/总进球区间 + 角球(前缀 corner)。
SPECIAL_MARKETS = {'correct_score', 'correct_score_ht', 'htft', 'first_to_score',
                   'exact_goals_ht', 'winning_margin', 'total_goals_range'}
SPECIAL_MARKET_PREFIX = ('corner',)
# 实盘数据切分点(2026-09-12 用户要求): 实盘释放只用「重收后」的干净数据, 不用之前被结算bug
# (ms=7漏判/league_cn对不上)污染的旧数据。切分点=观察库重收日 9-10。
REAL_DATA_CUTOFF_TS = datetime.fromisoformat("2026-09-10T00:00:00+00:00").timestamp()
# 滚球实盘 ROI 交叉验证时间窗起点(2026-10-31): 只认「统一入库鲜价生效后」的实盘。
# 旧快照价(10-31前)实盘是假负(足球小球 10-25/10-26 -7.5%/-21.1%), 不该拦新口径(鲜价+模拟拒单)的正格。
LIVE_REAL_ROI_CUTOFF_TS = CLEAN_DATA_CUTOFF  # 2026-11-07 起: line bug+Power+platt校准全生效, 之前实盘ROI是脏的
LIVE_PAPER = DATA / "live_paper_bets.json"
LIVE_REAL = DATA / "real_bets.json"  # 滚球实盘(second_level_monitor 写), 实盘ROI交叉验证用(2026-10-05)
OBS_STATE = DATA / "observe_release_state.json"  # 释放状态(首次释放时间 + cap)

# 滚球观察库口径映射: BB 运动 id → 英文运动名; 滚球 sub → sub_market
BB_SPORT_MAP = {1: "football", 3: "basketball", 5: "tennis", 7: "baseball", 6: "american_football",
                 2: "ice_hockey", 13: "volleyball", 15: "pingpong", 18: "mma", 19: "boxing",
                 47: "badminton",
                 "1": "football", "3": "basketball", "5": "tennis", "7": "baseball", "6": "american_football",
                 "2": "ice_hockey", "13": "volleyball", "15": "pingpong", "18": "mma", "19": "boxing", "47": "badminton",
                 "football": "football", "basketball": "basketball", "tennis": "tennis", "baseball": "baseball",
                 "american_football": "american_football", "ice_hockey": "ice_hockey", "volleyball": "volleyball",
                 "pingpong": "pingpong", "mma": "mma", "boxing": "boxing", "badminton": "badminton"}  # 2026-10-26 补字符串运动码(之前只 int, 漏统计157条football字符串)
BB_SUB_MAP = {"over_under": "ou", "handicap": "hc", "opportunities": "1x2",
              "double_chance": "dc", "btts": "btts",
              "ht": "ht", "ht_ou": "ht_ou",
              "ou": "ou", "hc": "hc", "1x2": "1x2", "dc": "dc", "ml": "1x2",
              "hc_games": "hc_games", "handicap_games": "hc_games",
              "ht_hc": "ht_hc", "ht_dc": "ht_dc", "dnb": "dnb"}  # 2026-10-26 补子盘缩写(之前只全称, 漏统计 hc/ou/dc/1x2/handicap_games 缩写)
LIVE_LEAGUE = "滚球"

# 运动中文名(2026-09-12 用户要求: 释放盘口必须标注具体运动)。释放清单 sport 字段是英文,
# 展示/推送时用此表转中文, 避免 ht/ou/hc 等盘口在足球/篮球/网球间混淆。
SPORT_CN = {
    "football": "⚽足球", "basketball": "🏀篮球", "tennis": "🎾网球",
    "baseball": "⚾棒球", "american_football": "🏈美足", "ice_hockey": "🏒冰球",
    "volleyball": "🏐排球", "pingpong": "🏓乒乓球", "mma": "🥊MMA",
    "boxing": "👊拳击", "badminton": "🏸羽毛球",
}

# 观察库释放粒度(2026-09-12 用户要求): 从「运动×联赛×盘口」改为「运动×盘口×方向」+
# 来源(早盘/滚球分开, 因滚球小球真溢价 vs 早盘小球假溢价方向相反不能混)。
# 早盘聚合所有联赛(样本易凑100), 方向归一化(ou大/小、hc主/客分开)。
SCOPE_EARLY = "early"   # 早盘观察库(paper_bets, 聚合所有联赛)
SCOPE_LIVE = "live"     # 滚球观察库(live_paper_bets)

# 投注额分阶段上限(用户要求): 新释放 150, 实盘满一周 ROI>4% 提 300
OBS_CAP_NEW = 150
OBS_CAP_MATURE = 300
OBS_MATURE_DAYS = 7        # 实盘投一周(7天)后评估提额
# 新释放盘口当日累计投注额上限(2026-09-12 用户要求): 当天总投注额≤2000(2026-09-15 1000→2000), 次日实盘ROI>4% 解除
DAILY_STAKE_LIMIT = 1000  # 2026-09-18 新数据源风控: 早盘每天投注≤1000, 原2000

# 已验证方向(硬编码真edge, 2026-09-13 统一进观察库释放机制): grandfathered 进 observe_released,
# 固定 cap(不走 150→300 分阶段), 无当日累计上限(limit_removed 恒 True)。之前散落在
# second_level_monitor._try_live_auto_bet 的硬编码 _bettable, 统一到这里单一事实来源。
# 赔率区间用 "*" 表示"所有区间"(已验证方向不分区间的历史口径)。
MANUAL_OBSERVE_RELEASE = {
    # 2026-09-19 撤销小球2格释放: 之前「edge +4.8pp/+3.5pp 真edge」是低流动性联赛 Betfair 薄盘
    # 漂出来的假溢价(今日实盘小球[2.0-2.3) 45注-31%ROI坐实)。撤销后交还数据驱动判据
    # (CLV>2%+n>200+赢率>隐含3pp), 待深挖补「Betfair流动性门槛」后再由判据自动决定是否释放。
    # 大球 1.0-2.0 的"正盈亏"是注额加权假象(edge 实为 -2.3pp/-2.7pp), 放 BLOCK 拦截。
    # 2026-09-24 撤让球主/客: 观察库数据负(主-3.1%/客-5.3%)+CLV逆向(客-17%~-31%), 交还数据驱动判据。
    # 2026-09-27 撤篮球独赢主/客 + 棒球独赢客: 原"CLV+ROI双正(篮主+54.9%/篮客+83.3%/棒客+51.4%)"
    # 是 mty 对调 bug 的假 edge(滚球盘口提取把篮球/棒球独赢与让分码写反, 让分赔率1.87配独赢结果),
    # 污染样本已清(105条), 交还数据驱动判据, 等 mty 修好后真独赢/真让分重新积累到 n 足够再判。
    # 棒球主胜 ROI-37.9% 负不释放。
    # 2026-09-27 撤 ht 主/ht_dc 客早盘释放: 原"CLV+2.73%/+3.55% 正率92%"是 Pin 口径(Pin 已暂停),
    # 注释误标"Betfair口径"。用户定「只用 Betfair 锚点统计 CLV」→ 两格子 CLV 依据作废, 交还数据驱动
    # 判据, 等 Betfair 收盘线 CLV(close_source=betfair)重新积累到 n>200 再自动释放。
    # 2026-10-03 撤足球独赢主胜1.5-2.0: 09-27释放(ROI+10.5%+CLV+0.4%双正), 但实盘连续两周负edge
    # (09-28~30 -6.2pp → 10-01~03 -43.4pp, 8笔7连败-953元), 纸单微正实盘巨亏=逆向选择铁证, 交还数据驱动判据。
    # 2026-09-27 足球让球主胜1.5-2.0: Betfair口径 edge+3.2pp/ROI+8.8% 双正 + 该区间真LEV+10.61pp
    # (价格朝你走), 三信号一致且 n=226 够。2.0-3.0 是毒区(真LEV-3.43pp, 已在 MANUAL_OBSERVE_BLOCK 拦),
    # 只放 1.5-2.0。cap270
    # 2026-10-03 概率桶迁移: 赔率1.5-2.0(概率50-66.7%) → 50-60% + 60-70%
    # 2026-10-06 撤 50-55%/55-60%: 实盘近10天 ROI -16.9%/-15.5%(n=19/20, 出血合计-908元)。
    # 2026-10-08 撤 60-70%(最后一个让球主胜释放桶): 实盘三天 ROI -8.0%(n=31, 出血-459),
    # 观察库 real_winrate 36.4% vs paper 64.5% gap -28.2pp 逆向选择铁证。交还数据驱动判据。
    # ── 2026-11-05 试探释放(第一二档): line bug(WS/REST线值结构不一致)已修 + Power去水 + platt校准,
    # hc/ou/ht_ou/ht_hc(让球/大小球)与深冷门1x2 的历史负ROI是错线+Shin bias 冤枉的。clean cutoff 之后
    # 干净样本不足(0笔), 数据驱动判据会「不足→拦」, 故手动 grandfather 放行, 走 REAL_ROI_GATE 自动撤假正。
    "basketball|1x2|客|<45%|live", "basketball|hc|主|*|early", "basketball|hc|主|45-55%|live",
    "basketball|hc|主|55-65%|live", "basketball|hc|主|65-75%|live", "basketball|hc|客|45-55%|live",
    "basketball|hc|客|55-65%|early", "basketball|hc|客|55-65%|live", "basketball|ou|大|*|live",
    "basketball|ou|大|45-55%|live", "basketball|ou|大|55-65%|early", "basketball|ou|大|55-65%|live",
    "basketball|ou|小|55-65%|live", "football|1x2|主|10-20%|early", "football|1x2|主|10-20%|live",
    "football|1x2|主|<10%|early", "football|1x2|主|<10%|live", "football|hc|主|40-50%|live",
    "football|hc|主|50-55%|live", "football|hc|主|55-60%|early", "football|hc|主|55-60%|live",
    "football|hc|主|60-70%|live", "football|hc|主|>70%|live", "football|hc|客|*|early",
    "football|hc|客|30-40%|live", "football|hc|客|40-50%|live", "football|hc|客|50-55%|live",
    "football|hc|客|55-60%|early", "football|hc|客|55-60%|live", "football|hc|客|60-70%|early",
    "football|hc|客|60-70%|live", "football|hc|客|>70%|live", "football|ht_hc|主|*|early",
    "football|ht_ou|大|20-30%|live", "football|ht_ou|大|30-40%|live", "football|ht_ou|大|40-50%|live",
    "football|ht_ou|大|50-55%|live", "football|ht_ou|大|55-60%|live", "football|ht_ou|大|60-70%|live",
    "football|ht_ou|大|>70%|live", "football|ht_ou|小|60-70%|live", "football|ht_ou|小|>70%|live",
    "football|ou|大|*|live", "football|ou|大|10-20%|live", "football|ou|大|20-30%|live",
    "football|ou|大|30-40%|live", "football|ou|大|40-50%|live", "football|ou|大|50-55%|early",
    "football|ou|大|50-55%|live", "football|ou|大|55-60%|early", "football|ou|大|55-60%|live",
    "football|ou|大|60-70%|early", "football|ou|大|60-70%|live", "football|ou|大|>70%|live",
    "football|ou|小|*|early", "football|ou|小|40-50%|live", "football|ou|小|50-55%|live",
    "football|ou|小|55-60%|live", "football|ou|小|60-70%|live", "football|ou|小|>70%|live",
    "ice_hockey|hc|客|55-65%|live", "ice_hockey|ou|大|*|early", "ice_hockey|ou|大|45-55%|live",
    "ice_hockey|ou|大|55-65%|live", "ice_hockey|ou|小|45-55%|live", "tennis|hc|主|*|early",
    "tennis|hc|客|*|early", "tennis|ou|大|40-60%|live", "tennis|ou|大|60-80%|live",
    "tennis|ou|小|40-60%|live", "tennis|ou|小|60-80%|live", "volleyball|hc|客|40-60%|live",
    "volleyball|hc|客|60-80%|live", "volleyball|ou|小|60-80%|live",
}

# 2026-11-07 用户决定: 早盘三个主力盘口(独赢1x2/大小球ou/让球hc)×三运动(足球/篮球/网球) 全量放开。
# 2026-11-09 用户决定: 放开所有运动×所有主力盘口(1x2/hc/ou), 只留「大热排除」(隐含>60%)+EV门槛+REAL_ROI_GATE。
# 依据: 实时赔率比价 + 匹配正确的真溢价已就位, 不再按数据驱动/手动拦截过滤, 只要符合EV门槛就投。
# 实现: 全量放开格直接放行(绕过 observe_blocked/observe_released/数据驱动拦截), 由 REAL_ROI_GATE 兜底撤假正。
FULL_RELEASE_EARLY = {
    f"{sport}|{sm}"
    for sport in ("football", "basketball", "tennis", "baseball",
                  "ice_hockey", "american_football", "volleyball")
    for sm in ("1x2", "hc", "ou")
} | {
    # 2026-11-13 用户要求"打开所有运动及对应盘口": 拳击/MMA 只有独赢(2-way 无线, 匹配天然安全)。
    # 羽毛球 Pin 无覆盖+BB无主盘口、乒乓球无数据 → 不补。
    "boxing|1x2", "mma|1x2",
}
# 2026-11-10 滚球与早盘保持一致: 同样放开 7 运动 × 1x2/hc/ou(两端排除 20-75% 在 second_level_monitor 里)。
FULL_RELEASE_LIVE = set(FULL_RELEASE_EARLY)

# 手动拦截的赔率区间(用户明确要求): 数据驱动「方向×赔率区间」按 edge(赢率vs隐含) 硬编码拦截(2026-09-19)。
# 优先于 MANUAL_OBSERVE_RELEASE 的区间。数据驱动拦截(observe_blocked)照常追加。
MANUAL_OBSERVE_BLOCK = {
    # 2026-10-15 撤网球独赢: 交叉验证实盘负(客-11.3%胜率36%/主+0.9%胜率41%), 逆向选择毒性流。
    # 2026-10-26 网球独赢主只留 40-60%(实盘+5.8%), 其余区间仍拦。
    "tennis|1x2|主|20-40%|live",
    "tennis|1x2|主|60-80%|live",
    "tennis|1x2|客|*|live",
    "tennis|hc_games|主|*|live",
    # 2026-10-16 撤篮球让球主滚球(纸面-28%+实盘-13.9%双负) + 足球独赢主早盘(纸面-2.2%→实盘-14.4%逆向选择)。
    # 2026-10-18 篮球让球主改桶级: 45-55%释放(纸面+55.5%/49注), 其余桶不释放。
    # 2026-10-27 改桶级: 只放 主40-50%/50-55%(CLV正), 其余桶拦。
    # 2026-11-01 恢复主胜两端(>70%/<10%/10-20%/20-30%)试探释放(加进LIMITED cap50); 中间段30-40%/55-60%/60-70%仍拦。
    "football|1x2|主|30-40%|early",
    "football|1x2|主|55-60%|early",
    "football|1x2|主|60-70%|early",
    # 2026-10-17 撤篮球大小球大(观察库45-55% n=952 -7.2%负)。
    "basketball|ou|大|45-55%|live",
    # 2026-10-18 结果锚校准撤假溢价(下注时公平价高估, 校准ROI负): 和局 20-30%(-56%)/40-50%(-6.4%)。
    "football|1x2|平|20-30%|early",
    "football|1x2|平|40-50%|early",
    # 2026-10-18 撤滚球大小球大 50-55%: 实盘 -5.3%(n=59) 双负(纸面 -25.3%)。
    # 2026-10-26 恢复(实盘已转正 +0.3%/79注)。
    # 2026-10-20 撤 ht 上半场独赢全部(实盘 ROI -59%/胜率7.7%, 半场公平价假 edge)。
    "football|ht|主|20-30%|early",
    "football|ht|主|40-50%|early",
    "football|ht|主|60-70%|early",
    "football|ht|主|>70%|early",
    "football|ht|客|10-20%|early",
    "football|ht|客|40-50%|early",
    "football|ht|客|50-55%|early",
    # 2026-10-22 撤滚球实盘负(REAL_ROI_GATE n≥5 且 ROI<0), 交还数据驱动判据。
    "basketball|hc|客|55-65%|live",
    "ice_hockey|hc|客|55-65%|live",
    "football|1x2|客|10-20%|live",
    "basketball|hc|主|45-55%|live",
    "basketball|hc|客|45-55%|live",
    "football|hc|主|50-55%|live",
    "ice_hockey|ou|大|55-65%|live",
    "football|hc|客|50-55%|live",
    "tennis|hc_games|客|60-80%|live",
    "football|dc|1X|>70%|live",
    # 2026-10-25 解除 football|ou|小|55-60%|live 拦截(用户要求足球小球全量放开)。
    # 2026-10-22 撤早盘 CLV+纸面ROI 双负(假溢价)。
    "football|dc|X2|60-70%|early",
    # 2026-10-27 撤 12>70% 拦截(实盘+2.5%正, 改释放)。
    # 2026-10-22 撤早盘: 实盘负/校准负/CLV负(逆向选择或假溢价)。
    "football|ou|大|55-60%|early",
    "football|ou|大|50-55%|early",
    "football|hc|客|55-60%|early",
    "tennis|hc|客|*|early",
    # 2026-10-25 撤实盘负格(最近一周 REAL_ROI n≥5 且 ROI<0):
    # 篮球让球客胜55-65%早盘(-33.2%/13注)、足球让球客胜55-60%(-7.0%/20注)、60-70%(-12.0%/41注)、>70%(-6.3%/6注)。
    "basketball|hc|客|55-65%|early",
    "football|hc|客|55-60%|live",
    "football|hc|客|60-70%|live",
    # 2026-10-26 撤实盘负格(全量 n>10 且 ROI<0):
    # 篮球让球主胜55-65%(-8.6%/51注,昨日按load_real_roi_cell漏统计假正误释)、足球1x2客40-50%(-8.6%/34注)、
    # 足球小球60-70%(-10.1%/52注)、足球大球40-50%(-22.9%/20注)、dc1X60-70%(-5.2%/32注)、小球55-60%(-16.1%/15注)、>70%(-4.7%/23注)。
    "basketball|hc|主|55-65%|live",
    "football|1x2|客|40-50%|early",
    # 2026-10-31 解除 football|ou|小 的 60-70%/>70%/通配* 拦截: 模拟拒单后新数据
    # 60-70% ROI+8.3%、>70% ROI+5.8% 转正(旧实盘负是快照价假负), 交数据驱动释放(n≥30+ROI>2%)。
    # 55-60% 仍负(-16.1% 旧数据)继续拦; 其余区间(<40%/40-50%/50-55%)由数据驱动判据拦。
    "football|ou|大|40-50%|live",
    # 2026-10-27 撤 1X 60-70% 拦截(实盘+22.3%正, 改释放)。
    "football|ou|小|55-60%|live",
    # 2026-10-26 撤双负盘口(实盘负+观察库纸面负): 足球大球/篮球大球/足球小球(滚球)+足球和局(早盘)。
    "basketball|ou|大|*|live",
    "football|ou|大|*|live",
    # 2026-10-27 改桶级: 只放 平20-30%(CLV正), 其余桶拦。
    "football|1x2|平|<10%|early",
    # 2026-11-01 恢复和局10-20%/30-40%(早盘ws和局+5.5%正, 之前被drop方向错配bug连累误杀; CLV负但实盘正, 同dc超热门格绕CLV用LIMITED释放)
    "football|1x2|平|40-50%|early",
    "football|1x2|平|50-55%|early",
    "football|1x2|平|55-60%|early",
    "football|1x2|平|60-70%|early",
    "football|1x2|平|>70%|early",
    # 2026-10-26 撤早盘 CLV 负方向(CLV 主判据):
    # 2026-10-27 改桶级: 只放 客30-40%/40-50%(CLV正), 其余桶拦。
    # 2026-11-01 恢复客胜两端(>70%/<10%/10-20%/20-30%)试探释放(加进LIMITED cap50); 中间段50-55%/55-60%/60-70%仍拦。
    "football|1x2|客|50-55%|early",
    "football|1x2|客|55-60%|early",
    "football|1x2|客|60-70%|early",
    # 2026-10-27 改桶级(dc 超热门): 只放 1X 60-70%/>70%、X2 >70%、12 >70%(实盘ROI正), 其余桶拦。
    "football|dc|1X|<10%|early",
    "football|dc|1X|10-20%|early",
    "football|dc|1X|20-30%|early",
    "football|dc|1X|30-40%|early",
    "football|dc|1X|40-50%|early",
    "football|dc|1X|50-55%|early",
    "football|dc|1X|55-60%|early",
    "football|dc|X2|10-20%|early",
    "football|dc|X2|20-30%|early",
    "football|dc|X2|30-40%|early",
    "football|dc|X2|40-50%|early",
    "football|dc|X2|50-55%|early",
    "football|dc|X2|60-70%|early",
    "football|dc|12|10-20%|early",
    "football|dc|12|20-30%|early",
    "football|dc|12|30-40%|early",
    "football|dc|12|40-50%|early",
    "football|dc|12|50-55%|early",
    "football|dc|12|55-60%|early",
    "football|dc|12|60-70%|early",
    "football|ht|主|*|early",
    "tennis|hc|主|*|early",
    "football|hc|客|*|early",
    # 2026-10-27 撤滚球实盘负格(近3天 REAL_ROI n>=5 且 ROI<0):
    "tennis|1x2|主|40-60%|live",   # -17.9%/19注
    "football|hc|客|40-50%|live",  # -10.5%/17注
    # 2026-11-01 撤足球早盘出血盘口(历史累计实盘持续负):
    "football|hc|主|55-60%|early",   # 让球主胜 -12.1%/21注
    "football|ht_hc|主|*|early",     # 上半场让球主 -68.8%/7注(胜1负6)
    # 2026-11-01 撤网球大小球小(实盘 ou 全线负, 纸面+7.8% 假正):
    "tennis|ou|小|40-60%|live",
    # 2026-11-01 撤早盘实盘负盘口:
    "football|ou|小|*|early",         # 足球小球 -27.5%/16注
    "ice_hockey|ou|大|*|early",       # 冰球大分 -19.4%/5注
    "basketball|1x2|客|*|early",      # 篮球独赢客 -11.8%/38注
    # 2026-11-01 全量交叉验证撤回(鲜价后10-31起, 实盘n>=10且ROI<0, 纸面假正):
    "football|ou|小|60-70%|live",      # -51.3%/14注
    "football|dc|X2|>70%|early",       # -1.7%/37注
    # 2026-11-01 撤早盘历史实盘负格(和局假溢价 + 篮球大球全输):
    "football|1x2|平|10-20%|early",     # 5笔全输 -100%
    "football|1x2|平|30-40%|early",     # -3.2%/70注
    "basketball|ou|大|55-65%|early",    # 6笔全输 -100%
}


# 手动释放 + 当日累计上限(2026-09-15 用户要求): 释放的是"有希望的格子"试探, 单注≤150, 当日累计≤2000。
# 2026-09-15 清空: 原 dc 早盘 1.0-3.0 是旧判据(赢率vs隐含)放的, 但 dc 在 CLV 口径下中位为负
# (-0.52%), 与「CLV唯一标准」矛盾, 撤掉交还数据驱动(负 CLV 不会释放)。
# 2026-09-28 用户要求试探释放篮球/棒球独赢主胜: CLV 全正(2.1~17.3pp, 价格朝你走是真实苗头),
# 但样本极薄(每区间 n=2~6, 远低于 n>200), 走日限额通道攒实盘数据(单注≤150, 当日累计≤1000)。
# 棒球 1.0-2.0 = 1.0-1.5(n5)+1.5-2.0(n3); 篮球 1.0-3.0 = 1.0-1.5(n6)+2.0-3.0(n2)。样本够后转数据驱动。
MANUAL_OBSERVE_RELEASE_LIMITED = {
    # 2026-10-30 用户要求: 释放 3 个 dc 超热门格(实盘 ROI 正, 但 CLV 负被 CLV 判据误杀), 单笔 cap 50。
    # 2026-11-13 早盘单笔上限+20: cap 50→70。
    # 依据 = 实盘 ROI 正(唯一可信尺): X2 55-60% +19.3%/26、X2>70% +8.0%/28、1X>70% +3.2%/47。
    # dc 双选 + 超热门的 CLV 合成不准(Pin 收盘价对超热门偏低), 所以绕过 CLV 判据, 按实盘放。
    "football|dc|X2|55-60%|early": 70,
    "football|dc|1X|>70%|early": 70,
    # 2026-10-27 撤: 之前按 CLV 放的 6 格, 校准后落在中热门亏损黑洞, 全撤。改按「校准后EV正+实盘ROI正」重选。
    # 2026-10-10 用户要求撤回全部滚球释放: 棒球独赢主(4桶)+篮球独赢主(5桶)撤回观察库,
    # 滚球释放一律改走数据驱动 LEV 判据(不再手动试探)。
    # 2026-10-10 撤回足球独赢客 30-70% 全区间: 实盘逆向选择铁证(全量14笔 -29.5% ROI, 赢率29% vs 隐含52% -23pp;
    # 近2天7笔 -60% 出血-483元)。观察库 edge 是纸面假正(BB滞后Betfair sharp move), 交还数据驱动判据。
    # 2026-10-10 撤回足球btts 50-70%: 无LEV背书(clv字段从未采集, LEV判据盲区)+纸面ROI假正(近2天7笔-52.7%), 交还数据驱动。
    # 2026-10-10 撤回棒球大小(4桶): 滚球释放一律改走数据驱动 LEV 判据。
    # 2026-10-10 早盘释放按「CLV>0 且 干净ROI>0」重定(注额口径已修): 足球独赢主 40-55% 是真溢价格子
    # (主40-50% CLV+4.72%/赢率54.5%vs41% +13.6pp, 主50-55% CLV+5.40%/赢率58.3%vs50% +8.3pp)。
    # 撤独赢客30-40%(CLV -0.66% 负, 赢率正只是方差); btts暂不释放(正ROI靠n=3~4小桶全中堆的方差)。
    # 2026-10-11 撤回足球独赢主 40-55%: 切 Pin 后这两个格 Pin 口径 CLV 变负(40-50% -4.68%/50-55% -2.41%),
    # 10-10 按「CLV>0」放的依据是 Betfair 口径, 换 Pin 后符号翻转=锚点偏差。交还数据驱动判据(负 CLV 不会释放)。
    # 2026-09-30 撤回: 双机会主/客 2.0-3.0 实盘首日 -15.9% ROI(19笔胜率33%, 观察库 edge+3.5pp 苗头被打脸),
    # 早盘上半场独赢客 >5.0 实盘 -20.7% ROI(8笔6负1胜胜率14%, 高赔率彩票型), 用户要求放回观察库交还数据驱动判据。
    # 2026-10-12 用户要求: 释放足球独赢平局(和局)四桶, 单笔 cap 100(试探)。
    # 依据 = 去水CLV(Shin) 全正: 10-20% +15.09%(n34)/20-30% +3.79%(n214)/30-40% +2.97%(n198)/40-50% +8.05%(n16),
    # 方向级纸面 ROI +52%(n13) 双正。n 仍远小于判据 n≥200, 走手动试探(cap 100 控风险)。
    # 2026-11-01 撤平 10-20%/30-40%: 历史实盘负(平30-40% -3.2%/70注, 平10-20% 5笔全输), 和局假溢价, 已移 MANUAL_OBSERVE_BLOCK。
    # 2026-10-14 用户要求释放独赢主/客(cap 30 试探)。足球/篮球 1x2 主/客早盘全桶。
    # 2026-10-16 撤足球独赢主早盘(纸面-2.2%→实盘-14.4%, 逆向选择)。
    # 2026-10-17 撤 football 独赢客胜早盘(CLV+4.0% 但纸面ROI-15% 假正)。
    # 2026-10-22 撤篮球1x2客全桶(实盘-72.4%/12注)。
    "basketball|1x2|主|*|early": 300,
    "tennis|hc|主|*|early": 300,
    # 2026-10-14 用户要求释放网球测试格(cap 30): 早盘=让盘(hc)主/客。
    # 2026-10-15 网球滚球改测让局主(实盘独赢负, 交叉验证毒性流), 撤独赢主客。
    # 2026-10-22 撤网球让盘客全桶(60-80%桶 CLV-58.4%)。
    # 2026-10-20 撤 ht 上半场独赢全部(实盘 ROI -59%/胜率7.7%, 半场公平价假 edge), 交还数据驱动判据。
    # 2026-10-22 撤ou 50-55%(小实盘-56.2%/大纸面-5.2%+实盘-87.5%)。
    # 2026-10-18 释放 55-60%(近3天双正: 小球CLV+3.2%ROI+33.1% / 大球CLV+3.9%ROI+15.0%)
    # 2026-10-22 撤ou 55-60%(大校准-26.8%假溢价/小实盘-58.4%)。
    # 2026-10-28 用户要求: 滚球只留 4 个稳定盈利格(10-11以来 ROI>1% 且 n>30)。
    "football|hc|主|>70%|live": 60,        # +16.8%/51
    "football|hc|客|40-50%|live": 60,      # +17.4%/49
    "football|hc|客|>70%|live": 80,        # +1.7%/31
    # 2026-11-01 撤 tennis|ou|小|40-60%(纸面+7.8% 但实盘 ou 全线负, 假正), 已移 MANUAL_OBSERVE_BLOCK。
    # 2026-10-17 撤 football dc X2(客/和)(近3天观察库 -1.9% ROI/12笔, 微负)。
    # 2026-10-18 结果锚校准释放真 edge(校准ROI正, 用户要求直接放不等 n≥200): dc/ht/1x2客/hc/ou。
    # 依据 = 结果锚校准 ROI(下注时公平价高估被修正后仍为正), cap 100 试探控风险(实盘冻结期检验)。
    # 2026-10-27 撤 dc 手动释放: 改由数据驱动 CLV>2% 判据统一决定(避免和 CLV 判据冲突)。
    # 2026-11-01 撤独赢客胜40-50%/60-70%+让球主胜55-60%(历史累计实盘负: 独赢客-10.2%/51注, 让球主-12.1%/21注), 交还数据驱动+MANUAL_OBSERVE_BLOCK。

    # 2026-10-22 撤 football|hc|客|55-60%(CLV-33.2%+实盘-100%)。
    "football|hc|客|60-70%|early": 100,
    "football|ou|大|60-70%|early": 100,
    # 2026-11-01 撤 basketball|ou|大|55-65%|early(历史实盘 6笔全输 -100%, 纸面假正), 已移 MANUAL_OBSERVE_BLOCK。
    # 2026-10-22 撤 basketball|1x2|客|<45%(实盘-100%/6注+CLV-9.0%)。
    # 2026-10-26 释放 CLV 正 + 纸面ROI 正 方向(早盘, CLV 主判据):
    # 2026-11-01 撤上半场让球主(历史累计实盘 -68.8%/7注, 胜1负6), 已移 MANUAL_OBSERVE_BLOCK。

    # 2026-11-01 撤早盘实盘负盘口: 足球小球(-27.5%/16注)、冰球大分(-19.4%/5注)、篮球独赢客(-11.8%/38注), 已移 MANUAL_OBSERVE_BLOCK。
    "basketball|hc|主|*|early": 100,
    # 2026-11-01 试探释放独赢两端(drop骤降方向Home/Away的正段, cap50试探攒数据裁决)。
    # 2026-11-13 早盘单笔上限+20: cap 50→70。
    # 两端=热门>70% + 冷门<10%/10-20%/20-30%(足球概率桶, <30%拆3桶)。
    "football|1x2|主|>70%|early": 70,
    "football|1x2|主|<10%|early": 70,
    "football|1x2|主|10-20%|early": 70,
    "football|1x2|主|20-30%|early": 70,
    "football|1x2|客|>70%|early": 70,
    "football|1x2|客|<10%|early": 70,
    "football|1x2|客|10-20%|early": 70,
    "football|1x2|客|20-30%|early": 70,
    # 2026-11-01 全量试探释放: 滚球观察库纸面ROI为正的47格, 每注固定30(用户要求)。
    # 注意: 纸面正≠实盘正(历史已证明虚高), 小注试探攒实盘数据裁决。
    "ice_hockey|1x2|平|<45%|live": 30,
    "ice_hockey|1x2|客|<45%|live": 30,
    "volleyball|hc|客|60-80%|live": 30,
    "football|hc|客|30-40%|live": 30,
    "volleyball|hc|客|40-60%|live": 30,
    "football|1x2|主|55-60%|live": 30,
    "ice_hockey|ou|小|45-55%|live": 30,
    "football|ht|主|30-40%|live": 30,
    "volleyball|ou|小|60-80%|live": 30,
    "football|ht|客|20-30%|live": 30,
    "tennis|hc_games|主|60-80%|live": 30,
    "football|ht_ou|大|40-50%|live": 30,
    "basketball|1x2|主|45-55%|live": 30,
    "football|1x2|客|50-55%|live": 30,
    "football|ou|大|20-30%|live": 30,
    "football|1x2|主|>70%|live": 30,
    "basketball|1x2|客|>75%|live": 30,
    "football|1x2|客|20-30%|live": 30,
    "football|1x2|客|40-50%|live": 30,
    "football|hc|主|40-50%|live": 30,
    "basketball|1x2|主|>75%|live": 30,
    "football|ht_ou|大|60-70%|live": 30,
    "football|dc|12|>70%|live": 30,
    "football|ou|大|>70%|live": 30,
    "football|dc|X2|>70%|live": 30,
    "basketball|1x2|主|<45%|live": 30,
    "football|ht_ou|大|>70%|live": 30,
    "ice_hockey|hc|客|55-65%|live": 30,
    "football|hc|客|50-55%|live": 30,
    "football|ou|小|55-60%|live": 30,
    "football|hc|客|60-70%|live": 30,
    "football|hc|客|>70%|live": 30,
    "football|hc|客|55-60%|live": 30,
    "basketball|hc|主|55-65%|live": 30,
    "football|hc|主|50-55%|live": 30,
    "tennis|hc_games|主|40-60%|live": 30,
    "football|dc|X2|60-70%|live": 30,
    "football|ou|小|>70%|live": 30,
    "tennis|hc_games|客|40-60%|live": 30,
    "basketball|ou|大|45-55%|live": 30,
    "football|1x2|客|10-20%|live": 30,
    "ice_hockey|ou|大|55-65%|live": 30,
    "football|hc|主|>70%|live": 30,
    "football|hc|客|40-50%|live": 30,
    "tennis|ou|小|40-60%|live": 30,
    "basketball|1x2|客|55-65%|live": 30,
}




DIR_N_MIN = 30              # 方向级赢率采信最小样本量(2026-09-12 15→50→30: 实盘方向样本, 30够)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _direction(desig, sub_market):
    """从 designation 提取方向(主/平/客/大/小/1X/12/X2/其他), 供方向级释放/封杀。

    与 bb_ev_push._release_direction 保持同一归一化口径。
    1x2: 主胜/平/客胜 → 主/平/客; dc 三向(2026-10-04): 主/和局→1X, 主/客→12, 和局/客→X2。
    htft 半全场方向复杂, 由整盘护栏(观察库交叉验证)封杀, 这里归一化不拆分。
    """
    d = (desig or "")
    if sub_market == "dc":
        # dc 三向归一化(2026-10-04): 之前把 12/X2 混成「客」, 掩盖各自负 edge
        if "主" in d and "和" in d:
            return "1X"
        if "主" in d and "客" in d:
            return "12"
        if "和" in d and "客" in d:
            return "X2"
        return "其他"
    if sub_market == "htft":
        return "其他"
    dl = d.lower()
    if "大" in d or "over" in dl:
        return "大"
    if "小" in d or "under" in dl:
        return "小"
    if ("和" in d or "平" in d or "draw" in dl) and "客" not in d and "主" not in d:
        return "平"
    if "客" in d or "away" in dl:
        return "客"
    if "主" in d or "home" in dl:
        return "主"
    return "其他"


def _odds_interval(fair_or_odds, sport=None):
    """公平价(优先)或BB赔率 → 概率桶(内部算隐含概率=1/price)。见 src.scrapers.odds_interval.prob_bucket。

    2026-10-03 从赔率区间改概率桶(按运动定制), favorite-longshot bias 本质是概率偏差。
    """
    import sys as _sys
    _root = str(ROOT)
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from src.scrapers.odds_interval import prob_bucket
    if fair_or_odds is None:
        return "?"
    try:
        p = float(fair_or_odds)
    except (TypeError, ValueError):
        return "?"
    if p <= 1.0:
        return "?"
    return prob_bucket(1.0 / p, sport)


def _window(match_epoch, push_ts):
    """时间窗分档: 临场<6h / 近场6-24h / 早盘24-72h / None(已开赛或超72h)。"""
    if not match_epoch or not push_ts:
        return None
    lead = match_epoch - push_ts
    if lead < 0:
        return None
    if lead < 6 * 3600:
        return "临场"
    if lead < 24 * 3600:
        return "近场"
    if lead < 72 * 3600:
        return "早盘"
    return None


def _dir_threshold(edge, n):
    """方向赢率vs隐含差值 → EV 门槛(2026-09-12 改赢率: 数据驱动, 替代 ROI 版)。

    下限 3.0 = 职业底线(历史教训: <3% 会放行大量临场漂移假机会)。
    赢率差越大门槛越低(充分信任), 样本越少越保守。
    """
    if edge >= 20.0:
        thr = 3.0
    elif edge >= 10.0:
        thr = 4.0
    elif edge >= 5.0:
        thr = 5.0
    else:
        thr = 6.0
    # 样本置信度折扣: n<30 太不可信不设方向门槛(回退基础), n<50 加保守折扣
    if n < 30:
        return None
    if n < 50:
        thr += 1.0
    return round(thr, 1)


def _agg_bets(bets, key_fn):
    agg = defaultdict(lambda: {"n": 0, "stake": 0.0, "profit": 0.0})
    for b in bets:
        stake = _f(b.get("stake")) or 0.0
        profit = _f(b.get("profit")) or 0.0
        k = key_fn(b)
        if k is None:
            continue
        agg[k]["n"] += 1
        agg[k]["stake"] += stake
        agg[k]["profit"] += profit
    for d in agg.values():
        d["roi"] = (d["profit"] / d["stake"] * 100.0) if d["stake"] > 0 else 0.0
    return agg


def load_real_roi():
    """实盘 ROI: tracked_bets.json settled 记录 → {(sport,sub_market): agg}。

    仅用于「满7天提额」的实盘 ROI 判定。league/方向/时间窗 ROI 已随判据统一
    「赢率 vs 隐含」废弃(2026-09-12)。
    """
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled" and b.get("anchor") == "pin"]
    return _agg_bets(bets, lambda b: (b.get("sport") or "?", b.get("sub_market") or "?"))


def load_real_roi_direction():
    """实盘 ROI(方向级): tracked_bets.json settled → {(sport,sub_market,direction): {n,roi}}。

    2026-09-18 补: 「满7天提额」原来用 (sport,sub_market) 两级 ROI, 会把同盘口下
    方向相反的方向混在一起(如 ht 主胜 +31.7% 被 ht 客胜 -38.9% 拉低), 导致真赚的
    方向永远提不了额。改按 (sport,sub_market,direction) 三级, 让提额看方向级 ROI。
    """
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled" and b.get("anchor") == "pin"]
    return _agg_bets(bets, lambda b: (
        b.get("sport") or "?", b.get("sub_market") or "?",
        _direction(b.get("designation"), b.get("sub_market"))))


def _winrate_threshold(n):
    """赢率vs隐含差值阈值(2026-09-12 按统计显著): 样本越少要求差越大。

    标准误 SE≈0.5/sqrt(n)(p≈0.5 二项分布), 差要 ≥1 SE 才不算纯噪声。
    n=100→5pp, n=50→7pp, 下限 REAL_WINRATE_EDGE_MIN=3pp。
    """
    se = 50.0 / math.sqrt(n) if n > 0 else 100.0
    return max(REAL_WINRATE_EDGE_MIN, se)


def _winrate_agg(bets, key_fn):
    """按 key 聚合实盘赢率 vs 隐含: 赢率=won/(won+lost) 去 void/push; 隐含=平均(1/Pin公平价)。

    2026-09-12 修正隐含基准: 之前用 bb_odds(软书价)算隐含, 判的是「BB价是否+EV」;
    但「真溢价」的定义是「真实赢率 > Pin公平价隐含」(Pin是sharp基准, Pin低估才是市场错误定价)。
    用 BB 赔率算隐含会高估 edge 1~4pp, 把「BB自己定价粗」误判成真溢价(假溢价源头)。改用 fair_price。
    2026-09-13 修口径: 隐含必须是 mean(1/fair), 不是 1/mean(fair)。1/x 是凸函数,
    1/mean(fair) < mean(1/fair), 旧口径系统性低估隐含、高估 edge(赔率方差越大虚高越多,
    1x2 独赢冷门多实测虚高 +13.5pp), 会误放行「BB定价粗」假溢价。
    """
    by = defaultdict(lambda: {"won": 0, "lost": 0, "inv_sum": 0.0})
    for b in bets:
        r = b.get("result")
        if r not in ("won", "lost"):
            continue
        # 时间切分(2026-09-12): 只用重收后的干净实盘数据, 切掉之前被结算bug污染的旧样本
        _pt = _ts(b.get("push_time"))
        if _pt is not None and _pt < REAL_DATA_CUTOFF_TS:
            continue
        o = _f(b.get("fair_price")) or _f(b.get("bb_odds")) or 0
        if o <= 1.0:
            continue
        k = key_fn(b)
        if k is None:
            continue
        if r == "won":
            by[k]["won"] += 1
        else:
            by[k]["lost"] += 1
        by[k]["inv_sum"] += 1.0 / o
    out = {}
    for k, d in by.items():
        n = d["won"] + d["lost"]
        if n == 0:
            continue
        out[k] = {
            "n": n,
            "winrate": d["won"] / n * 100.0,
            "implied": d["inv_sum"] / n * 100.0,
        }
    return out


def load_real_winrate_market():
    """实盘赢率 vs 隐含(两维): tracked_bets.json settled → {(sport,sub_market): {n,winrate,implied}}。"""
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled" and b.get("anchor") == "pin"]
    return _winrate_agg(bets, lambda b: (b.get("sport") or "?", b.get("sub_market") or "?"))


def load_real_winrate_direction():
    """实盘赢率 vs 隐含(方向级): tracked_bets.json settled → {(sport,sub_market,direction): {n,winrate,implied}}。"""
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled" and b.get("anchor") == "pin"]
    return _winrate_agg(bets, lambda b: (
        b.get("sport") or "?", b.get("sub_market") or "?",
        _direction(b.get("designation"), b.get("sub_market"))))


def load_real_winrate_league():
    """实盘赢率 vs 隐含(联赛三维): tracked_bets settled → {(sport,league,sub_market): {n,winrate,implied}}。"""
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled" and b.get("anchor") == "pin"]
    return _winrate_agg(bets, lambda b: (
        b.get("sport") or "?",
        (b.get("league") or "").strip() or "?",
        b.get("sub_market") or "?"))


def load_real_winrate_direction_window():
    """实盘赢率 vs 隐含(方向×时间窗): tracked_bets settled → {(sport,sub_market,direction,window): {n,winrate,implied}}。"""
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled" and b.get("anchor") == "pin"]

    def _key(b):
        w = _window(_f(b.get("match_epoch")), _ts(b.get("push_time")))
        if w is None:
            return None
        return (b.get("sport") or "?", b.get("sub_market") or "?",
                _direction(b.get("designation"), b.get("sub_market")), w)

    return _winrate_agg(bets, _key)


def load_real_winrate_cell():
    """实盘赢率(方向×赔率区间): tracked_bets settled → {(sport,sub_market,direction,interval): {n,winrate,implied}}。

    2026-09-19 供「实盘-纸单 gap」指标用: 纸单是快单口径系统性乐观(不含慢单劣化),
    实盘才含时序衰减/逆向选择成本, 两者差 = 时序劣化重灾区。只展示不设闸门(等实盘 n 攒够再当判据)。
    """
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled" and b.get("anchor") == "pin"]
    return _winrate_agg(bets, lambda b: (
        b.get("sport") or "?", b.get("sub_market") or "?",
        _direction(b.get("designation"), b.get("sub_market")),
        _odds_interval(_f(b.get("fair_price")) or _f(b.get("bb_odds")), b.get("sport"))))


def load_live_real_winrate():
    """滚球实盘赢率: live_settled_log.json → {(sport,sub_market,direction,interval): {n,winrate}}。

    live_settled_log 结构是 mgn/on/mn(非 sub_market/designation), 需映射:
    mgn 大/小→ou、让球→hc、独赢→1x2; on 大/小/主/客/和直接取, 让球的 on 是队名(用 mn 判主客)。
    赔率区间用 _odds_interval(od) 重算(旧日志的 interval 是4档, 与5档口径不一致)。
    """
    p = DATA / "live_settled_log.json"
    if not p.exists():
        return {}
    try:
        rows = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    _MGN_TO_SM = {"大/小": "ou", "让球": "hc", "独赢": "1x2"}
    by = defaultdict(lambda: {"won": 0, "lost": 0})
    for x in rows:
        mgn = x.get("mgn", "")
        on = (x.get("on") or "").strip()
        sm = _MGN_TO_SM.get(mgn)
        sport = BB_SPORT_MAP.get(x.get("sid", 0))
        if not sm or not sport:
            continue
        if on in ("大", "小", "主", "客", "和", "平"):
            dr = "平" if on == "和" else on
        elif mgn == "让球":
            dr = None
            mn = x.get("mn", "")
            for sep in (" vs ", " VS "):
                if sep in mn:
                    h, a = mn.split(sep, 1)
                    if on == h.strip():
                        dr = "主"
                    elif on == a.strip():
                        dr = "客"
                    break
            if dr is None:
                continue
        else:
            continue
        iv = _odds_interval(_f(x.get("od")), sport)
        if iv == "?":
            continue
        k = (sport, sm, dr, iv)
        if x.get("won"):
            by[k]["won"] += 1
        else:
            by[k]["lost"] += 1
    out = {}
    for k, d in by.items():
        n = d["won"] + d["lost"]
        if n == 0:
            continue
        out[k] = {"n": n, "winrate": d["won"] / n * 100.0}
    return out


def load_observe_brier():
    """观察库 Betfair 公平价校准检验(Brier, 2026-09-19): {(sport,sm,direction,interval,scope): {n,brier,brier_base,calib_err}}。

    brier = mean((p-y)^2), p=1/fair(Betfair 隐含概率), y=1赢/0输。
    brier_base = mean(p(1-p)) = 若完全校准的期望 Brier(不可约误差)。
    calib_err = brier - brier_base: >0 表示该格子公平价系统性 miscalibrated(高估/低估真实概率)。
    只展示不设闸门, 供检测「Betfair 锚在哪些格子有 bias」(如已发现的 [2.0-2.3] 冷门方向高估)。
    """
    by = defaultdict(lambda: {"n": 0, "brier": 0.0, "base": 0.0})

    def _feed(k, result, fair):
        if result not in ("won", "lost") or not fair or fair <= 1.0:
            return
        p = 1.0 / fair
        y = 1.0 if result == "won" else 0.0
        d = by[k]
        d["n"] += 1
        d["brier"] += (p - y) ** 2
        d["base"] += p * (1.0 - p)

    for b in _read_paper_bets():
        sm = b.get("sub_market") or "?"
        _feed((b.get("sport") or "?", sm, _direction(b.get("designation"), sm),
               _odds_interval(_f(b.get("fair_price")) or _f(b.get("bb_odds")), b.get("sport")), SCOPE_EARLY),
              b.get("result"), _f(b.get("fair_price")) or _f(b.get("bb_odds")))

    for b in _read_live_paper_bets():
        sport = BB_SPORT_MAP.get(b.get("sport"))
        sm = BB_SUB_MAP.get(b.get("sub"))
        if not sport or not sm:
            continue
        _feed((sport, sm, _direction(b.get("designation"), sm),
               _odds_interval(_f(b.get("fair")) or _f(b.get("bb_odds")), sport), SCOPE_LIVE),
              b.get("result"), _f(b.get("fair")) or _f(b.get("bb_odds")))

    out = {}
    for k, d in by.items():
        if d["n"] == 0:
            continue
        out[k] = {
            "n": d["n"],
            "brier": round(d["brier"] / d["n"], 4),
            "brier_base": round(d["base"] / d["n"], 4),
            "calib_err": round((d["brier"] - d["base"]) / d["n"], 4),
        }
    return out


def _read_paper_bets():
    """读早盘观察库(paper_bets.json)记录(只 Betfair 口径, 2026-09-19 锚点分区)。

    干净口径: 优先 ts(采集时刻, 2026-11-09 起 paper_settle 落盘), 老记录无 ts 回退
    settled_at(结算时刻)。与滚球(live_paper 有 ts)口径对齐。之前只认 ts 致早盘无 ts 全丢(已修)。
    """
    if not PAPER.exists():
        return []
    try:
        raw = json.loads(PAPER.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    bets = raw.get("bets", []) if isinstance(raw, dict) else raw
    out = []
    for b in bets:
        if b.get("anchor") != "pin":
            continue
        # 2026-11-14 校准样本(source=calibration, ev>0 全量)不进释放判定, 只供 refit 拟合去水 bias
        if b.get("source") == "calibration":
            continue
        _t = b.get("ts") or _ts(b.get("settled_at")) or 0
        if _t >= CLEAN_DATA_CUTOFF:
            out.append(b)
    return out


def _read_live_paper_bets():
    """读滚球观察库(live_paper_bets.json)记录(只 Betfair 口径, 2026-09-19 锚点分区)。

    2026-10-31 补 sim_rejected 过滤: 纸面模拟拒单标记的样本(下单一刻 oddsChange=0 会被拒的)
    不参与任何观察库统计(ROI/赢率/CLV/释放判据), 消除「纸面全样本 vs 实盘只成交」的 selection bias。
    """
    if not LIVE_PAPER.exists():
        return []
    try:
        raw = json.loads(LIVE_PAPER.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    bets = raw.get("bets", []) if isinstance(raw, dict) else raw
    return [b for b in bets if b.get("anchor") == "pin" and not b.get("sim_rejected")
            and b.get("source") != "calibration"  # 2026-11-14 校准样本不进释放判定
            and (b.get("ts") or 0) >= CLEAN_DATA_CUTOFF]


def load_observe_winrate():
    """观察库赢率 vs 隐含(合并滚球+早盘纸面结算): {(sport,sub_market,direction,odds_interval,scope): {n,winrate,implied,roi}}。

    判据(2026-09-12): 赢率 = won/(won+lost) 去 void/push; 隐含 = 平均(1/赔率)。
    粒度「运动×盘口×方向×赔率区间×来源」: 早盘聚合所有联赛(scope=early), 滚球(scope=live);
    方向用 _direction 归一化; 赔率区间用 _odds_interval(BB赔率分4档)。
    """
    by = defaultdict(lambda: {"won": 0, "lost": 0, "inv_sum": 0.0, "stake": 0.0, "profit": 0.0, "spreads": []})

    def _feed(k, result, odds, stake, profit, spread=None):
        if result not in ("won", "lost") or not odds or odds <= 1.0:
            return
        d = by[k]
        if result == "won":
            d["won"] += 1
        else:
            d["lost"] += 1
        d["inv_sum"] += 1.0 / odds
        d["stake"] += stake
        d["profit"] += profit
        if spread is not None:
            d["spreads"].append(float(spread))

    for b in _read_paper_bets():
        sm = b.get("sub_market") or "?"
        _iv = _odds_interval(_f(b.get("fair_price")) or _f(b.get("bb_odds")), b.get("sport"))
        _feed((b.get("sport") or "?", sm, _direction(b.get("designation"), sm), _iv, SCOPE_EARLY),
              b.get("result"), _f(b.get("fair_price")) or _f(b.get("bb_odds")),
              _f(b.get("stake")) or 0, _f(b.get("profit")) or 0, b.get("spread"))

    for b in _read_live_paper_bets():
        sport = BB_SPORT_MAP.get(b.get("sport"))
        sm = BB_SUB_MAP.get(b.get("sub"))
        if not sport or not sm:
            continue
        _iv = _odds_interval(_f(b.get("fair")) or _f(b.get("bb_odds")), sport)
        _feed((sport, sm, _direction(b.get("designation"), sm), _iv, SCOPE_LIVE),
              b.get("result"), _f(b.get("fair")) or _f(b.get("bb_odds")),
              _f(b.get("stake")) or 0, _f(b.get("profit")) or 0, b.get("spread"))

    out = {}
    for k, d in by.items():
        n = d["won"] + d["lost"]
        if n == 0:
            continue
        _spreads = sorted(d["spreads"])
        out[k] = {
            "n": n,
            "winrate": d["won"] / n * 100.0,
            "implied": d["inv_sum"] / n * 100.0,
            "roi": d["profit"] / d["stake"] * 100.0 if d["stake"] > 0 else 0.0,
            "median_spread": _spreads[len(_spreads) // 2] if _spreads else None,
        }
    return out


def load_observe_calibrated():
    """观察库「结果锚校准」ROI(2026-10-18): {(sport,sm,direction,interval,scope): {n_cal, roi_cal}}。

    决策尺(结果锚): 用历史已结算注拟合 isotonic 曲线(下注时隐含胜率=1/fair → 实际胜率),
    把公平价校准到真实胜率, 再算校准后 EV。本函数返回每格子「校准后 ev_cal>0 的注」的实际 ROI
    与笔数。roi_cal>0 才是真 edge(公平价低估了它); 否则是假溢价(公平价高估, 尤其和局/热门)。

    为什么不用 CLV 当闸门(2026-10-18): 早盘收盘价本身仍有 +2.2pp 残留偏差(和局档收盘后仍高估),
    CLV 继承此偏差 → 正 CLV 的注实际亏钱(实测 +2.7% ROI vs 结果锚 +14.65%)。CLV 降级为事后
    验证尺(开赛才有, 验证「有没有赢过收盘」), 不再是释放决策。见 load_clv_median 保留用于审计。

    只对 scope=early 生效(滚球校准过不了时间切分样本外, 不用于 live 释放)。
    """
    from sklearn.isotonic import IsotonicRegression
    # 拟合校准(早盘): 只收集早盘已结算注
    X, Y = [], []
    for b in _read_paper_bets():
        if b.get("result") in ("won", "lost") and _f(b.get("fair_price")):
            X.append(1.0 / _f(b.get("fair_price")))
            Y.append(1.0 if b.get("result") == "won" else 0.0)
    if len(X) < 50:
        return {}
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(X, Y)
    # 逐格统计 ev_cal>0 的注
    by = defaultdict(lambda: {"n_cal": 0, "stake_cal": 0.0, "profit_cal": 0.0})
    for b in _read_paper_bets():
        if b.get("result") not in ("won", "lost") or not _f(b.get("fair_price")) or not _f(b.get("bb_odds")):
            continue
        fair = _f(b.get("fair_price"))
        bb = _f(b.get("bb_odds"))
        p_cal = max(float(iso.predict([1.0 / fair])[0]), 0.01)
        fair_cal = 1.0 / p_cal
        if (bb - fair_cal) / fair_cal <= 0:
            continue
        sm = b.get("sub_market") or "?"
        k = (b.get("sport") or "?", sm, _direction(b.get("designation"), sm),
             _odds_interval(fair, b.get("sport")), SCOPE_EARLY)
        d = by[k]
        d["n_cal"] += 1
        d["stake_cal"] += _f(b.get("stake")) or 0
        d["profit_cal"] += _f(b.get("profit")) or 0
    out = {}
    for k, d in by.items():
        if d["n_cal"] == 0:
            continue
        out[k] = {
            "n_cal": d["n_cal"],
            "roi_cal": round(d["profit_cal"] / d["stake_cal"] * 100.0, 2) if d["stake_cal"] > 0 else 0.0,
        }
    return out


def load_clv_median():
    """早盘收盘线 CLV 中位+n(运动×盘口×方向×赔率区间): clv_results.csv 的 true_clv_pct。

    2026-09-15 用户最终决定: CLV(打不打得过 Pin 收盘价)是职业团队唯一最看重的 edge 指标,
    释放判据改为「只看正 CLV + n>200」, 替换掉「赢率 vs Pin下注时价隐含 + ROI」——
    那个隐含带 favorite-longshot bias, 差值被偏差污染, 会把「热门假edge」当真。

    true_clv_pct = (BB下注价 - Pin收盘公平价) / Pin收盘公平价, 隐含基准就是「收盘价」
    (不是 Pin 下注时价), 所以这套判据同时落地「隐含用收盘价」这条铁律(正 CLV = 下注价打过收盘价)。
    滚球(scope=live)无收盘线 CLV, 此判据只对 scope=early 生效。

    Returns: {(sport, sub_market, direction, odds_interval): (median_clv, n)}
    """
    import csv as _csv, statistics as _st
    f = DATA / "clv_results.csv"
    if not f.exists():
        return {}
    by = defaultdict(list)
    with open(f, encoding="utf-8-sig") as fh:
        for r in _csv.DictReader(fh):
            # 口径过滤(2026-10-11 改): Pin 已恢复(付费实时 key), 锚点切回 Pin 收盘价——
            # 1) close_source 只留 live(Pin 收盘价=唯一锚点), 排除 betfair(Betfair 收盘价, 长腿
            #    bias 6-12% 致 CLV 假负, 见 pin-paid-subscription-plan 记忆) 和 archive(旧归档)。
            # 2) source 只留 validate(观察库口径), 排除 push(实盘下注口径 CLV 系统性偏高)。
            if r.get("close_source") != "live":
                continue
            if r.get("source") != "validate":
                continue
            try:
                clv = float(r.get("true_clv_pct") or 0)
                odds = float(r.get("bb_odds") or 0)
            except (ValueError, TypeError):
                continue
            key = (r.get("sport") or "?", r.get("sub_market") or "?",
                   _direction(r.get("designation"), r.get("sub_market")), _odds_interval(_f(r.get("push_fair_price")) or odds, r.get("sport")))
            by[key].append(clv)
    return {k: (_st.median(v), len(v)) for k, v in by.items() if v}


def _obs_included(b, cutoff):
    """观察是否纳入统计(时间口径, 2026-10-11 统一 total/windowed 两路, 杜绝口径 drift)。

    cutoff=None(累计口径): 只判 ts 存在性——缺 ts / ts 不可解析的记录无法参与任何时间判据, 一律排除。
    cutoff 有值(窗口口径): 额外判 ts >= cutoff(窗口内)。
    两路共用同一谓词, 未来任何口径变更不可能再漂移(同 10-03 教训: 两份逻辑必然长歪)。
    """
    _ts = b.get("ts")
    if _ts is None:
        return False
    try:
        t = float(_ts)
    except (TypeError, ValueError):
        return False
    return cutoff is None or t >= cutoff


def _count_missing_ts():
    """数据质量探针(2026-10-11): 观察库里无法纳入统计的记录数(=缺 ts / ts 不可解析)。

    >0 = 写入链路丢了时间戳, 是事故(不是窗口效应), 须查写入侧。复用 _obs_included(b, None)
    保证与统计口径零漂移。
    """
    return sum(1 for b in _read_live_paper_bets() if not _obs_included(b, None))


def load_live_clv(window_days=None):
    """滚球真 LEV(价格变化方向)按(运动×盘口×方向×赔率区间)聚合。

    2026-09-27 改口径: 原 clv 字段是「下注后3s复验 Betfair 价的 edge」= (bb_odds-fair@T+3)/fair@T+3,
    但实测 54% 的样本下注后3s价格根本没变(clv==ev), 复验价=下注价 → clv 字段混入大量「静态 edge」
    (BB 给高赔的度量), 不是「下注后市场往你走」的逆向选择信号, 导致 LEV 中位被静态 edge 主导、区分度低。

    真 LEV 应测「价格变化方向」= clv - ev = (fair@T - fair@T+3)/fair@T(一阶), 正=Betfair 价跌
    (结果变更可能, 市场朝你走=真edge), 负=Betfair 价涨(逆向选择)。故:
    1) 过滤 clv≈ev(价格3s没变, 静态样本) 和 clv=None(复验失败);
    2) 只统计 clv!=ev 样本的 clv-ev(价格变化)。
    注意: 价格变化量级远小于「复验时edge」(通常<1pp), 判据阈值 OBS_CLV_MIN=2% 对它偏严, 需另定。

    window_days(2026-10-11): 若非 None, 只统计 ts >= now - window_days 的观察(时间窗, 纯过滤),
    防未来换锚点/换公平价源时旧样本混进新 grid 的 n。None = 累计口径。缺 ts 的记录无论哪种口径都排除
    (统一走 _obs_included, 缺 ts 由 _count_missing_ts 独立计数告警)。

    Returns: {(sport, sub_market, direction, odds_interval): (median_clv, n)}
    """
    import statistics as _st
    _cutoff = (time.time() - window_days * 86400) if window_days else None
    by = defaultdict(list)
    for b in _read_live_paper_bets():
        if not _obs_included(b, _cutoff):
            continue
        clv = b.get("clv")
        ev = b.get("ev")
        if clv is None or ev is None:
            continue
        try:
            clv = float(clv)
            ev = float(ev)
        except (ValueError, TypeError):
            continue
        # 过滤静态样本(价格3s没变: clv==ev, 因为 clv/ev 都 round 到 2 位, 精确相等即价格没动)
        if abs(clv - ev) < 0.01:
            continue
        sport = BB_SPORT_MAP.get(b.get("sport"))
        sm = BB_SUB_MAP.get(b.get("sub"))
        if not sport or not sm:
            continue
        _iv = _odds_interval(_f(b.get("fair")) or _f(b.get("bb_odds")), sport)
        by[(sport, sm, _direction(b.get("designation"), sm), _iv)].append(clv - ev)
    return {k: (_st.median(v), len(v)) for k, v in by.items() if v}


def load_live_real_roi():
    """滚球实盘 ROI: BB 官方已结算订单(isSettled=true, uwl) 滚球部分(mt>bt), 按(运动,盘口)聚合。

    mgn 映射: "大/小"→ou, "让球"→hc, "独赢"→1x2; sid 用 BB_SPORT_MAP。
    调 BB API 可能失败(token 过期/网络), 失败返回空(提额判定保守回退 cap=150)。
    """
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from scripts.daily_review import _fetch_settled_orders
        orders = _fetch_settled_orders() or []
    except Exception:
        return {}
    _mgn_map = {"大/小": "ou", "让球": "hc", "独赢": "1x2"}
    agg = defaultdict(lambda: {"n": 0, "stake": 0.0, "profit": 0.0})
    for o in orders:
        op = (o.get("ops") or [{}])[0]
        if o.get("mt", 0) <= op.get("bt", 0):  # 只要滚球(下单晚于开赛)
            continue
        sport = BB_SPORT_MAP.get(op.get("sid"))
        sm = _mgn_map.get(op.get("mgn"))
        if not sport or not sm:
            continue
        try:
            stake = float(o.get("sat", 0))
            profit = float(o.get("uwl", 0))
        except (TypeError, ValueError):
            continue
        d = agg[(sport, sm)]
        d["n"] += 1
        d["stake"] += stake
        d["profit"] += profit
    return {k: {"n": d["n"], "roi": (d["profit"] / d["stake"] * 100.0) if d["stake"] > 0 else 0.0}
            for k, d in agg.items()}


def load_real_roi_cell():
    """实盘 ROI 交叉验证(2026-10-05): 按释放格粒度(运动×盘口×方向×概率桶×来源)聚合实盘 ROI。

    观察库释放判据里的 _roi 是「纸面投注」(虚拟)ROI, CLV 正 + 纸面 ROI 正仍可能是假正——
    真金白银的实盘 ROI(tracked_bets 早盘 / real_bets 滚球)才是最终验证。有实盘样本且 ROI<0
    的格子, 即便 CLV 正也拦(早盘收盘线 CLV 假正: 线朝你走但实盘亏, 见 bb-domain-failover 记忆)。

    返回 {(sport,sub_market,direction,interval,scope): {n, roi}}。key 口径与 load_observe_winrate
    完全一致(同一 _direction/_odds_interval), 供 main() 释放循环直接 get 交叉验证。
    """
    out = {}

    def _agg(bets, scope, fmt="early"):
        # fmt: "live" = real_bets.json 格式(sub/desig/sport数字/fair), "early" = tracked_bets 格式(sub_market/designation/sport字符串/fair_price)
        for b in bets:
            if fmt == "live":
                sp = BB_SPORT_MAP.get(b.get("sport"))
                sm = BB_SUB_MAP.get(b.get("sub"))
                if not sp or not sm:
                    continue
                fair = _f(b.get("fair")) or _f(b.get("bb_odds"))
                dr = _direction(b.get("desig") or b.get("designation"), sm)
            else:
                sp = b.get("sport") or "?"
                sm = b.get("sub_market") or "?"
                fair = _f(b.get("fair_price")) or _f(b.get("bb_odds"))
                dr = _direction(b.get("designation"), sm)
            iv = _odds_interval(fair, sp)
            if iv == "?":
                continue
            d = out.setdefault((sp, sm, dr, iv, scope), {"n": 0, "stake": 0.0, "profit": 0.0})
            d["n"] += 1
            d["stake"] += _f(b.get("stake")) or 0.0
            d["profit"] += _f(b.get("profit")) or 0.0

    # 早盘实盘(老口径 tracked_bets.json)
    if TRACKED.exists():
        try:
            raw = json.loads(TRACKED.read_text())
            bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
                    if isinstance(b, dict) and b.get("status") == "settled"
                    and b.get("anchor") == "pin"
                    and (_ts(b.get("push_time")) or 0) >= CLEAN_DATA_CUTOFF]
            _agg(bets, SCOPE_EARLY, "early")
        except (json.JSONDecodeError, OSError):
            pass
    # 滚球+早盘实盘(real_bets.json, 按 trigger_source 拆来源, 2026-11-01 修正)
    # 之前把 real_bets.json 全算成滚球 live, 早盘 707 笔(pin_drop/ws_scan)误计入滚球污染 ROI,
    # 且早盘实盘只读老 tracked_bets 漏了这批新早盘样本。滚球=pin/poll/None, 早盘=pin_drop/ws_scan。
    if LIVE_REAL.exists():
        try:
            raw = json.loads(LIVE_REAL.read_text())
            d = raw.get("bets", raw) if isinstance(raw, dict) else {}
            vals = list(d.values()) if isinstance(d, dict) else d
            live_bets, early_bets = [], []
            for b in vals:
                if not isinstance(b, dict) or b.get("result") not in ("won", "lost"):
                    continue
                (early_bets if b.get("trigger_source") in ("pin_drop", "ws_scan") else live_bets).append(b)
            # 2026-10-31 时间窗: 只认鲜价生效后的实盘(滚球+早盘同口径, 旧快照价是假负)
            live_bets = [b for b in live_bets if (b.get("ts") or 0) >= LIVE_REAL_ROI_CUTOFF_TS]
            early_bets = [b for b in early_bets if (b.get("ts") or 0) >= LIVE_REAL_ROI_CUTOFF_TS]
            _agg(live_bets, SCOPE_LIVE, "live")
            _agg(early_bets, SCOPE_EARLY, "live")
        except (json.JSONDecodeError, OSError):
            pass

    return {k: {"n": d["n"], "roi": (d["profit"] / d["stake"] * 100.0) if d["stake"] > 0 else 0.0}
            for k, d in out.items()}


def load_drop_obs_cells():
    """drop 源观察库(CLV + 纸面ROI) → {(sport,sub_market,direction,interval): {n_clv, clv, n_paper, paper_roi}}。

    2026-11-01 修正: drop 释放标准不能用实盘 ROI(不释放就没实盘=死循环), 改用观察库:
      - CLV = clv_results.csv source=pin_drop 的 true_clv_pct 中位(收盘线 CLV, 下注价 vs 收盘价)。
      - 纸面ROI = paper_bets.json source=pin_drop/drop 的 profit/stake。
    观察库对每个信号都记(不管放不放), 永远在攒数据, 没有死循环。
    """
    import csv as _csv
    out = {}
    _clv = defaultdict(list)
    _paper = defaultdict(lambda: {"n": 0, "stake": 0.0, "profit": 0.0})
    # 1) CLV (clv_results.csv source=pin_drop)
    _clv_f = DATA / "clv_results.csv"
    if _clv_f.exists():
        try:
            with open(_clv_f, encoding="utf-8-sig") as f:
                for r in _csv.DictReader(f):
                    if r.get("source") != "pin_drop":
                        continue
                    sp = r.get("sport"); sm = r.get("sub_market")
                    if not sp or not sm:
                        continue
                    fair = _f(r.get("push_fair_price")) or _f(r.get("bb_odds"))
                    if not fair or fair <= 1.0:
                        continue
                    iv = _odds_interval(fair, sp)
                    dr = _direction(r.get("designation"), sm)
                    v = _f(r.get("true_clv_pct"))
                    if v is None or iv == "?":
                        continue
                    _clv[(sp, sm, dr, iv)].append(v)
        except Exception:
            pass
    # 2) 纸面 ROI (paper_bets.json source=pin_drop/drop)
    if PAPER.exists():
        try:
            raw = json.loads(PAPER.read_text())
            bets = raw.get("bets", []) if isinstance(raw, dict) else raw
            for b in bets:
                if b.get("source") not in ("pin_drop", "drop"):
                    continue
                if b.get("result") not in ("won", "lost"):
                    continue
                sp = b.get("sport"); sm = b.get("sub_market")
                fair = _f(b.get("fair_price")) or _f(b.get("bb_odds"))
                if not fair or fair <= 1.0:
                    continue
                iv = _odds_interval(fair, sp)
                dr = _direction(b.get("designation"), sm)
                if iv == "?":
                    continue
                x = _paper[(sp, sm, dr, iv)]
                x["n"] += 1; x["stake"] += _f(b.get("stake")) or 0.0; x["profit"] += _f(b.get("profit")) or 0.0
        except Exception:
            pass
    for k in set(_clv) | set(_paper):
        c = _clv.get(k, [])
        p = _paper.get(k, {"n": 0, "stake": 0.0, "profit": 0.0})
        out[k] = {
            "n_clv": len(c),
            "clv": (sorted(c)[len(c) // 2] if c else None),
            "n_paper": p["n"],
            "paper_roi": (p["profit"] / p["stake"] * 100.0 if p["stake"] > 0 else None),
        }
    return out


def _load_obs_state():
    """读释放状态(首次释放时间 + cap)。文件不存在返回空。"""
    if not OBS_STATE.exists():
        return {}
    try:
        return json.loads(OBS_STATE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_obs_state(state):
    tmp = OBS_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(OBS_STATE)


def _notify_release(newly_released, obs_winrate):
    """新释放盘口推钉钉通知(2026-09-12 用户要求)。"""
    try:
        from config.settings import send_dingtalk
    except Exception:
        return
    lines = []
    for (sport, sm, dr, interval, scope) in newly_released:
        sp_cn = SPORT_CN.get(sport, sport)
        scope_cn = "滚球" if scope == SCOPE_LIVE else "早盘"
        lines.append(
            f"{sp_cn} {sm}-{dr}({interval},{scope_cn}) | 单注≤{OBS_CAP_NEW} | 当日累计≤{DAILY_STAKE_LIMIT}")
    body = "🆕 新释放盘口\n\n" + "\n".join(lines)
    try:
        send_dingtalk("🆕 新释放盘口", body)
    except Exception:
        pass


def _notify_pending_release(new_releases, new_blocks):
    """释放/封锁变更推钉钉待确认(2026-09-22 用户要求: 确认后才执行)。"""
    try:
        from config.settings import send_dingtalk
    except Exception:
        return
    lines = []
    for (sport, sm, dr, interval, scope) in new_releases:
        sp_cn = SPORT_CN.get(sport, sport)
        scope_cn = "滚球" if scope == SCOPE_LIVE else "早盘"
        lines.append(f"🟢 拟释放: {sp_cn} {sm}-{dr}({interval},{scope_cn})")
    for (sport, sm, dr, interval, scope) in new_blocks:
        sp_cn = SPORT_CN.get(sport, sport)
        scope_cn = "滚球" if scope == SCOPE_LIVE else "早盘"
        lines.append(f"🔴 拟封锁(已释放): {sp_cn} {sm}-{dr}({interval},{scope_cn})")
    if not lines:
        return
    body = ("⚠️ 释放/封锁变更待确认\n\n" + "\n".join(lines)
            + "\n\n回复『确认』后执行(compute_market_release.py --confirm)")
    try:
        send_dingtalk("⚠️ 释放变更待确认", body)
    except Exception:
        pass


def _real_roi_gate_blocks(real_roi_cell, sport, sm, dr, interval, scope):
    """REAL_ROI_GATE(2026-10-11 单一出口): 有实盘样本 n≥5 且 ROI<0 → 拦(CLV假正/逆向选择)。

    三态语义: n≥5&ROI<0 拦 / n≥5&ROI≥0 放 / n<5(无数据) 放(手动格靠熔断兜底)。
    所有释放路径(数据驱动+手动)都必须过这道门——10-03 事故 = 手动格 continue 绕过此门,
    放出了逆向选择格(足球独赢客 30-70%) 连亏 7 天。返回 (blocked, n, roi)。
    """
    _real = real_roi_cell.get((sport, sm, dr, interval, scope), {})
    _n = _real.get("n", 0)
    _roi = _real.get("roi", 0.0)
    # 2026-10-30 熔断: 滚球(live)用 >30 单, 早盘(early)用 >10 单(用户要求, 比原来5更宽观察期)。
    _n_min = LIVE_CIRCUIT_BREAKER_N_MIN if scope == "live" else EARLY_CIRCUIT_BREAKER_N_MIN
    return (_n > _n_min and _roi < 0), _n, _roi


# 回退开关(2026-10-11): 手动格是否强制过 REAL_ROI_GATE。默认 true(根治); false 回退旧行为(跳过)。
# 留着不是因为它可能错, 而是闸门行为变更属于不可逆影响成交集的操作, 逃生口是纪律。
ENFORCE_REAL_ROI_GATE_ON_MANUAL = os.getenv("ENFORCE_REAL_ROI_GATE_ON_MANUAL", "true").lower() in ("1", "true", "yes")


def _manual_gate_blocks(real_roi_cell, sport, sm, dr, interval, scope):
    """手动格的 REAL_ROI_GATE(带 ENFORCE 开关)。"""
    if not ENFORCE_REAL_ROI_GATE_ON_MANUAL:
        return False, 0, 0.0
    return _real_roi_gate_blocks(real_roi_cell, sport, sm, dr, interval, scope)


def main():
    market_roi = load_real_roi()
    market_roi_dir = load_real_roi_direction()  # 方向级 ROI(满7天提额用, 2026-09-18 修混方向bug)
    obs_winrate = load_observe_winrate()
    clv_med = load_clv_median()  # 早盘收盘线 CLV 中位(2026-10-18 降级为验证/审计, 不再是释放闸门)
    obs_cal = load_observe_calibrated()  # 结果锚校准 ROI(2026-10-18 起 = 早盘释放决策尺)
    live_clv = load_live_clv(window_days=(LIVE_CLV_WINDOW_DAYS if LIVE_CLV_WINDOW_ENABLED else None))  # 14天窗口过滤(2026-10-11)
    # 自洽检查(2026-10-11): 按 grid 逐格比对 n_total vs n_window(不是集合差——窗口会造成 n 变少但格不消失)。
    # 今天静默(全部相等); 哪天响, 说明窗口开始咬人, 且能立刻看到是哪个格、掉了多少笔(排查「grid 突然不释放」的唯一线索)。
    if LIVE_CLV_WINDOW_ENABLED:
        _live_clv_total = load_live_clv(window_days=None)
        for _k, (_med_t, _n_t) in _live_clv_total.items():
            _n_w = live_clv.get(_k, (0, 0))[1]
            if _n_t != _n_w:
                print(f"[release] ⚠️ 14天窗口已生效: {_k} n_total={_n_t} n_window={_n_w}", flush=True)
    # 数据质量探针(2026-10-11): 缺 ts 记录独立计数告警(与窗口哨兵分离, 单一职责)。
    _n_no_ts = _count_missing_ts()
    if _n_no_ts:
        print(f"[release] ⚠️ {_n_no_ts} 条观察记录缺 ts，已排除(数据质量事故, 查写入链路)", flush=True)
    real_roi_cell = load_real_roi_cell()  # 实盘 ROI 交叉验证(2026-10-05): CLV假正拦截

    # 2026-11-01 drop 数据驱动释放: 用观察库 CLV(收盘线)+纸面ROI 分格(不是实盘 ROI——不释放就没实盘=死循环),
    # 替换 bb_ev_push.py 的硬编码 DROP_BLOCK。已确认正/负的格进清单, 不足样本走 DROP_BLOCK 兜底。
    drop_obs = load_drop_obs_cells()
    drop_released = []
    drop_blocked = []
    # 试探释放(2026-11-01): 这 4 格 drop 实盘已转正(n=5~9, +19.7%~+77%), 先放出攒数据;
    # 否则按「实盘ROI≥10笔才放」会死循环(不放出就没实盘)。观察库 CLV/纸面样本不足的走这里。
    _drop_trial = [
        ["football", "1x2", "主", "30-40%"],   # 实盘 +77.2%/6注
        ["basketball", "1x2", "客", "<45%"],   # 实盘 +19.7%/8注
        ["basketball", "1x2", "主", ">75%"],   # 实盘 +20.8%/5注
        ["football", "hc", "客", "55-60%"],    # 实盘 +29.8%/9注
    ]
    for (_sp, _sm, _dr, _iv), _d in sorted(drop_obs.items()):
        _entry = [_sp, _sm, _dr, _iv]
        # CLV 优先(收盘线 CLV 更 sharp, 正向=下注价优于收盘价)
        if _d["n_clv"] >= DROP_N_MIN and _d["clv"] is not None:
            if _d["clv"] > 0:
                drop_released.append(_entry)
            else:
                drop_blocked.append(_entry)
        # CLV 不足时用纸面 ROI 兜底
        elif _d["n_paper"] >= DROP_N_MIN and _d["paper_roi"] is not None:
            if _d["paper_roi"] > 0:
                drop_released.append(_entry)
            else:
                drop_blocked.append(_entry)
    # 试探格并入释放(去重)
    for _e in _drop_trial:
        if _e not in drop_released and _e not in drop_blocked:
            drop_released.append(_e)
    if drop_released or drop_blocked:
        print(f"[release] drop数据驱动(观察库CLV+纸面ROI): 释放 {len(drop_released)} 格 / 拦截 {len(drop_blocked)} 格 (n≥{DROP_N_MIN})", flush=True)

    # 2026-09-19 用户要求: 释放盘口不用实盘投注数据(不释放就没实盘=死循环), 只用观察库。
    # 实盘释放通道(主开关/方向/时间窗/联赛)废弃, 全设空; 实盘 ROI 仅保留「满7天提额」。
    market_released = []
    direction_released = []
    direction_blocked = []
    direction_min_ev = []
    direction_window_blocked = []
    direction_window_released = []
    league_released = []
    league_blocked = []

    # 观察库释放(2026-09-12 用户要求): 结算样本 n>100 且 赢率>隐含 → 释放。
    # 粒度「运动×盘口×方向×赔率区间×来源」(2026-09-13 加赔率区间): 早盘聚合所有联赛(scope=early),
    # 滚球(scope=live); 方向用 _direction 归一化, 赔率区间用 _odds_interval 分4档。
    # 每个赔率区间独立判据: n>200 + 赢率>隐含 + ROI>0 → 释放; 否则 → 拦截(表现差不投)。
    observe_released = []
    observe_blocked = []
    # 早盘(scope=early): CLV>2% + n>200 是唯一标准(2026-09-15 用户最终决定), 替换掉赢率vs隐含+ROI。
    # 特殊盘口(margin 15%+, 收盘线不 sharp, CLV 无意义)直接放弃, 不进清单。
    for (sport, sm, dr, interval), (med, n) in sorted(clv_med.items()):
        if sm in SPECIAL_MARKETS or sm.startswith(SPECIAL_MARKET_PREFIX):
            continue
        # 2026-10-10 修: 手动释放格(试探)skip 数据驱动拦截。否则 released+blocked 同时出现,
        # _is_market_released 先查 observe_blocked → 释放被拦(早盘一场投不出)。与滚球循环同逻辑。
        if (f"{sport}|{sm}|{dr}|{interval}|early" in MANUAL_OBSERVE_RELEASE
                or f"{sport}|{sm}|{dr}|{interval}|early" in MANUAL_OBSERVE_RELEASE_LIMITED):
            continue
        # 足球冷门封顶(2026-09-15): favorite-longshot bias 使足球冷门系统性高估, 真实赢率全负
        # (CLV 正是 devig 残差, 非真 edge)。足球 >5.0 不用 CLV 释放。冰球/棒球有 reverse bias,
        # 不套用此封顶(等那俩运动攒够样本单独判)。
        if sport == "football" and interval == ">5.0":
            observe_blocked.append([sport, sm, dr, interval, "early"])
            continue
        _cell = obs_winrate.get((sport, sm, dr, interval, "early"), {})
        _roi = _cell.get("roi", 0.0)
        _mspread = _cell.get("median_spread")
        if _mspread is not None and _mspread >= LIQUIDITY_MAX_SPREAD:
            observe_blocked.append([sport, sm, dr, interval, "early"])  # 2026-09-19 薄盘拦(公平价/CLV不可信)
            continue
        # 实盘 ROI 交叉验证(2026-10-05): 有实盘样本且 ROI<0 → 拦(CLV假正: 线朝你走但真金白银亏)
        _real = real_roi_cell.get((sport, sm, dr, interval, "early"), {})
        if _real.get("n", 0) >= REAL_ROI_GATE_N_MIN and _real.get("roi", 0) < 0:
            observe_blocked.append([sport, sm, dr, interval, "early"])
            continue
        # 2026-11-01 负段过滤(实盘 ROI 矩阵): 中间概率段系统性出血, 大小球/网球让局全线负(早盘同滚球)。
        _neg = LIVE_NEGATIVE_IMPLIED.get(sm)
        if _neg is None:
            observe_blocked.append([sport, sm, dr, interval, "early"])
            continue
        _implied = _cell.get("implied")
        if _implied is not None and _neg[0] <= _implied <= _neg[1]:
            observe_blocked.append([sport, sm, dr, interval, "early"])
            continue
        # 2026-10-27 决策尺改回 CLV>2%(用户要求 CLV 主判据): 早盘观察库 CLV 中位 >2% 就释放。
        # 之前(10-18)用「结果锚校准」, 用户要求放弃, 回到 CLV。REAL_ROI_GATE(上面)仍拦实盘负格。
        if med > 2.0:
            observe_released.append([sport, sm, dr, interval, "early"])
        else:
            observe_blocked.append([sport, sm, dr, interval, "early"])
    # 滚球(scope=live): 2026-09-15 用户定, 和早盘一样用 CLV(LEV)判释放, 不用赢率vs隐含(带bias且向后看)。
    # LEV = 下注后复验 Pin 价算的 CLV, 正=真edge, 负=逆向选择。clv 字段今天才修好采集率(60s→3s),
    # 样本极薄, 短期 n<200 不会释放, 等积累(释放清单暂时由 MANUAL_OBSERVE_RELEASE 的滚球方向撑)。
    # MANUAL_OBSERVE_RELEASE 的「已验证方向」是用户显式开放的(如小球全区间), 数据驱动的 LEV 拦截
    # 对它们不适用(LEV 薄样本 n<200 会误拦已验证方向, 与用户「全量打开」冲突)。
    _manual_dirs = set()
    for _mkey in MANUAL_OBSERVE_RELEASE:
        _p = _mkey.split("|")
        # 2026-09-24 修bug: 只有全区间(*)才方向级跳过; 具体区间(如1.0-1.5)不该让同方向其他区间被跳过
        if _p[3] == "*":
            _manual_dirs.add((_p[0], _p[1], _p[2]))  # (sport, sub_market, direction)
    for (sport, sm, dr, interval, scope), _cell in sorted(obs_winrate.items()):
        if scope != "live":
            continue  # 只处理滚球(live)格; 早盘(early)已在上面的 clv_med 循环处理
        # 2026-09-28 修bug: MANUAL_OBSERVE_RELEASE 的「具体区间」格子(如 football|hc|主|1.5-2.0)也被
        # 数据驱动判据加到 observe_blocked, 导致 released+blocked 同时出现 → 释放被拦截, 一场都投不出。
        # 之前 _manual_dirs 只跳全区间(*), 漏了具体区间。这里补: MANUAL 释放的完整 key(含区间)也跳过。
        if (f"{sport}|{sm}|{dr}|{interval}|live" in MANUAL_OBSERVE_RELEASE
                or f"{sport}|{sm}|{dr}|{interval}|live" in MANUAL_OBSERVE_RELEASE_LIMITED):
            continue
        if (sport, sm, dr) in _manual_dirs:
            continue  # 已验证方向(全区间*), 数据驱动不拦(用户显式开放)
        # 2026-09-27 手动拦截优先: 被 MANUAL_OBSERVE_BLOCK 拦的格子, 数据驱动不释放
        # (避免 released+blocked 同时出现的脏数据, 如大球1.5-2.0 假正但实盘劣化被手动撤)
        if f"{sport}|{sm}|{dr}|{interval}|live" in MANUAL_OBSERVE_BLOCK:
            observe_blocked.append([sport, sm, dr, interval, "live"])
            continue
        _roi = _cell.get("roi", 0.0)
        _n = _cell.get("n", 0)
        _mspread = _cell.get("median_spread")
        if _mspread is not None and _mspread >= LIQUIDITY_MAX_SPREAD:
            observe_blocked.append([sport, sm, dr, interval, "live"])  # 2026-09-19 薄盘拦(公平价/CLV不可信)
            continue
        # 实盘 ROI 交叉验证(2026-10-05): 有实盘样本且 ROI<0 → 拦(纸面假正)
        _real = real_roi_cell.get((sport, sm, dr, interval, "live"), {})
        if _real.get("n", 0) >= REAL_ROI_GATE_N_MIN and _real.get("roi", 0) < 0:
            observe_blocked.append([sport, sm, dr, interval, "live"])
            continue
        # 2026-11-01 负段过滤(实盘 ROI 矩阵治本): 中间概率段系统性出血, 大小球/网球让局全线负。
        # 用「平均隐含概率」(obs_winrate.implied) 判断, 绕过概率桶标签运动定制的复杂性。
        _neg = LIVE_NEGATIVE_IMPLIED.get(sm)
        if _neg is None:
            observe_blocked.append([sport, sm, dr, interval, "live"])
            continue
        _implied = _cell.get("implied")
        if _implied is not None and _neg[0] <= _implied <= _neg[1]:
            observe_blocked.append([sport, sm, dr, interval, "live"])
            continue
        # 2026-10-31 滚球释放判据改口径: n = obs_winrate 的「已结算样本数」(非 live_clv 的价格变动样本)。
        # 原 10-11 用 live_clv 的 n 依赖 clv 字段, 而滚球 live 在 Pin 缓存覆盖极差 → pin_matchup_id 空
        # → clv 全 None → n 永远 0, 数据驱动释放形同虚设。改回已结算样本数, 不依赖 clv。
        # 纸面ROI 门槛 >2%(OBS_LIVE_ROI_MIN, 2026-10-31 用户要求), 挡 0~2% 的噪声正格(篮球小球+0.3% 这类)。
        if _n >= OBS_N_MIN_LIVE and _roi > OBS_LIVE_ROI_MIN:
            observe_released.append([sport, sm, dr, interval, "live"])
        else:
            observe_blocked.append([sport, sm, dr, interval, "live"])

    # 已验证方向(硬编码真edge)统一进释放机制(2026-09-13): grandfathered, 不走 n>200 判据。
    # 2026-10-11 修 10-03 漏洞: 手动格也过 REAL_ROI_GATE(实盘ROI交叉验证)。之前 continue 直接跳过此门,
    # 放出逆向选择格(足球独赢客 30-70%)连亏 7 天。三态: n≥5&ROI<0 拦(撤回) / n≥5&ROI≥0 放 / n<5 放但挂熔断。
    for _mkey in MANUAL_OBSERVE_RELEASE:
        _p = _mkey.split("|")
        _sport, _sm, _dr, _interval, _scope = _p[0], _p[1], _p[2], _p[3], _p[4]
        _blk, _n, _roi = _manual_gate_blocks(real_roi_cell, _sport, _sm, _dr, _interval, _scope)
        if _blk:
            observe_blocked.append([_sport, _sm, _dr, _interval, _scope])  # 撤回
            print(f"  ⚠️ 手动格被 REAL_ROI_GATE 撤回: {_mkey} (n={_n} ROI={_roi:+.1f}%)", flush=True)
            continue
        if _n < REAL_ROI_GATE_N_MIN:  # 无数据手动格: 放行但挂熔断(骨架)
            print(f"  🔶 手动格无数据放行(挂熔断): {_mkey} (n={_n})", flush=True)
        _entry = [_sport, _sm, _dr, _interval, _scope]
        if _entry not in observe_released:
            observe_released.append(_entry)

    # 手动释放 + 当日累计上限(2026-09-15 用户要求): dc 早盘 1.0-3.0 试探, 走日限额。
    for _mkey in MANUAL_OBSERVE_RELEASE_LIMITED:
        _p = _mkey.split("|")
        _sport, _sm, _dr, _interval, _scope = _p[0], _p[1], _p[2], _p[3], _p[4]
        _blk, _n, _roi = _manual_gate_blocks(real_roi_cell, _sport, _sm, _dr, _interval, _scope)
        if _blk:
            observe_blocked.append([_sport, _sm, _dr, _interval, _scope])
            print(f"  ⚠️ 手动格被 REAL_ROI_GATE 撤回: {_mkey} (n={_n} ROI={_roi:+.1f}%)", flush=True)
            continue
        if _n < REAL_ROI_GATE_N_MIN:
            print(f"  🔶 手动格无数据放行(挂熔断): {_mkey} (n={_n})", flush=True)
        _entry = [_sport, _sm, _dr, _interval, _scope]
        if _entry not in observe_released:
            observe_released.append(_entry)

    # 手动拦截的赔率区间(用户明确要求, 2026-09-13): 已验证方向里表现巨差的区间, 优先于"*"释放。
    for _mkey in MANUAL_OBSERVE_BLOCK:
        _p = _mkey.split("|")
        _entry = [_p[0], _p[1], _p[2], _p[3], _p[4]]
        # 2026-11-05: 修复后手动释放(MANUAL_OBSERVE_RELEASE)优先于旧手动拦截——后者是 line bug/Shin bias
        # 脏数据时期的结论, 已被本次释放覆盖。二者冲突时以释放为准(REAL_ROI_GATE 仍兜底撤假正)。
        if _mkey in MANUAL_OBSERVE_RELEASE:
            continue
        if _entry not in observe_blocked:
            observe_blocked.append(_entry)

    # 监控(2026-10-11): released live grid 数量随时间的曲线。只画图不设阈值不告警。
    # LEV 降级后滚球释放是「单向阀」(进靠 n≥20 纸面, 出靠实盘 n≥5 且 ROI<0 熔断), released 数量单调增长,
    # 需观察趋势; 等涨到两位数再决定要不要加背压(定期重验 / 最大释放格数)。今天别加, 加了是凭信仰设计。
    _n_live_released = sum(1 for e in observe_released if len(e) >= 5 and e[4] == "live")
    _n_early_released = sum(1 for e in observe_released if len(e) >= 5 and e[4] == "early")
    print(f"[release] 释放格数量: live={_n_live_released} early={_n_early_released} (监控, 不设阈值)", flush=True)

    # 2026-09-22 用户要求: 释放盘口/封锁已释放盘口, 必须先推钉钉待确认, 确认后才执行。
    # 无 --confirm 时, 若检测到释放/封锁变更, 推钉钉+写 pending 文件, 不写 market_release.json(保持旧清单)。
    if not CONFIRM:
        _old_released = set()
        _old_blocked = set()
        if OUT.exists():
            try:
                _old = json.loads(OUT.read_text())
                _old_released = {tuple(x) for x in _old.get("observe_released", [])}
                _old_blocked = {tuple(x) for x in _old.get("observe_blocked", [])}
            except Exception:
                pass
        _new_releases = [list(x) for x in observe_released if tuple(x) not in _old_released]
        _new_blocks = [list(x) for x in observe_blocked
                       if tuple(x) not in _old_blocked and tuple(x) in _old_released]
        if _new_releases or _new_blocks:
            # 2026-11-11 去重: pending 内容相同(变更未确认), 不重复推钉钉(网络故障期间每轮 cron 都推的刷屏根因)
            try:
                _old_pending = json.loads(PENDING.read_text()) if PENDING.exists() else {}
                _same = (_old_pending.get("released") == _new_releases
                         and _old_pending.get("block_released") == _new_blocks)
            except Exception:
                _same = False
            if _same:
                print(f"[release] 变更已推过待确认(pending未变), 不重复推(释放{len(_new_releases)} 封锁{len(_new_blocks)})")
                return
            try:
                PENDING.write_text(json.dumps(
                    {"released": _new_releases, "block_released": _new_blocks,
                     "ts": datetime.now().isoformat()}, ensure_ascii=False, indent=2))
            except Exception:
                pass
            _notify_pending_release(_new_releases, _new_blocks)
            print(f"[release] 检测到释放/封锁变更, 已推钉钉待确认(释放{len(_new_releases)} 封锁{len(_new_blocks)}), 本次不写清单")
            return

    # 释放状态维护 + 投注额 cap 分阶段 + 当日累计上限 + 释放通知(2026-09-12 用户要求)。
    # 状态持久化到 observe_release_state.json: first_released_at/cap/daily_stake/limit_removed。
    obs_state = _load_obs_state()
    now_ts = datetime.now().timestamp()
    today = datetime.now().strftime("%Y-%m-%d")
    live_real_roi = load_live_real_roi()
    observe_release_caps = {}
    new_state = {}
    newly_released = []  # 新释放的盘口(推钉钉通知)
    for (sport, sm, dr, interval, scope) in observe_released:
        key = f"{sport}|{sm}|{dr}|{interval}|{scope}"
        _manual = key in MANUAL_OBSERVE_RELEASE  # 已验证方向(固定cap+无日限额) — 2026-11-07 修 set 用 .get 崩溃
        _manual_limited = MANUAL_OBSERVE_RELEASE_LIMITED.get(key)  # 手动释放+有日限额(固定cap, 不走150→300提额)
        prev = obs_state.get(key)
        first = (prev or {}).get("first_released_at")
        cap = _manual_limited if _manual_limited else (prev or {}).get("cap", OBS_CAP_NEW)
        daily_stake = (prev or {}).get("daily_stake", 0)
        daily_date = (prev or {}).get("daily_stake_date", "")
        limit_removed = True if _manual else (prev or {}).get("limit_removed", False)
        if not first:
            first = datetime.now().isoformat()
            if not _manual and not _manual_limited:
                cap = OBS_CAP_NEW
                newly_released.append([sport, sm, dr, interval, scope])
        elif not _manual and not _manual_limited:
            try:
                first_ts = datetime.fromisoformat(str(first)).timestamp()
            except (ValueError, TypeError):
                first_ts = now_ts
            if now_ts - first_ts >= OBS_MATURE_DAYS * 86400:
                # 满一周: 看实盘 ROI(滚球用 BB 官方订单两维, 早盘用 tracked_bets 两维)
                r = (live_real_roi.get((sport, sm)) if scope == SCOPE_LIVE
                     else market_roi_dir.get((sport, sm, dr)))
                if r and r.get("n", 0) > 0 and r.get("roi", 0) > REAL_ROI_MIN:
                    cap = OBS_CAP_MATURE
            # 次日解除 1000 限制(2026-09-12 用户要求): 实盘 ROI>4% → 解除当日累计上限
            # 不用加实盘样本门槛: 观察库释放已用 n>100 测过真溢价, 释放即已验证, 实盘 ROI>4% 就解除。
            if not limit_removed:
                r = (live_real_roi.get((sport, sm)) if scope == SCOPE_LIVE
                     else market_roi_dir.get((sport, sm, dr)))
                if r and r.get("n", 0) > 0 and r.get("roi", 0) > REAL_ROI_MIN:
                    limit_removed = True
        # 当日累计跨天重置
        if daily_date != today:
            daily_stake = 0
            daily_date = today
        new_state[key] = {"first_released_at": first, "cap": cap,
                          "daily_stake": daily_stake, "daily_stake_date": daily_date,
                          "limit_removed": limit_removed}
        observe_release_caps[key] = cap
    _save_obs_state(new_state)
    # 释放通知: 新释放盘口推钉钉
    if newly_released:
        _notify_release(newly_released, obs_winrate)

    # 实盘-纸单 gap(只展示, 2026-09-19): 纸单快单口径乐观, gap<0=时序劣化重灾区。等实盘 n 攒够再当判据。
    real_cell = load_real_winrate_cell()   # 早盘实盘(tracked_bets)
    live_cell = load_live_real_winrate()   # 滚球实盘(live_settled_log)
    observe_real_paper_gap = []
    for (sport, sm, dr, interval, scope), pw in obs_winrate.items():
        rw = (live_cell if scope == SCOPE_LIVE else real_cell).get((sport, sm, dr, interval))
        if not rw or rw.get("n", 0) < REAL_GAP_N_MIN:
            continue
        observe_real_paper_gap.append({
            "cell": f"{sport}|{sm}|{dr}|{interval}|{scope}",
            "real_n": rw["n"], "real_winrate": round(rw["winrate"], 1),
            "paper_winrate": round(pw["winrate"], 1),
            "gap_pp": round(rw["winrate"] - pw["winrate"], 1),
        })
    observe_real_paper_gap.sort(key=lambda x: x["gap_pp"])
    if observe_real_paper_gap:
        print(f"[gap] 实盘-纸单赢率差(负=纸单高估/时序劣化): {observe_real_paper_gap}", flush=True)

    # Brier 校准检验(只展示, 2026-09-19): calib_err>0 = 该格子 Betfair 公平价系统性 miscalibrated
    calib = load_observe_brier()
    observe_calibration = []
    for (sport, sm, dr, interval, scope), c in calib.items():
        if c["n"] < OBS_N_MIN:  # 样本够才展示(与释放判据同门槛)
            continue
        observe_calibration.append({
            "cell": f"{sport}|{sm}|{dr}|{interval}|{scope}",
            "n": c["n"], "brier": c["brier"], "brier_base": c["brier_base"],
            "calib_err": c["calib_err"],
        })
    observe_calibration.sort(key=lambda x: -x["calib_err"])
    if observe_calibration:
        _worst = observe_calibration[:5]
        print(f"[calib] Betfair公平价校准误差(>0=miscalibrated): {_worst}", flush=True)

    # 2026-11-09 修 full_release 无 REAL_ROI_GATE 兜底(agent2 S1): 全量放开格也过实盘负 ROI 熔断,
    # 实盘 n>=REAL_ROI_GATE_N_MIN 且 ROI<0 的方向×区间加进 full_release_blocked, 投注端 full_release 命中先查。
    full_release_blocked = []
    for (_sp, _sm, _dr, _iv, _scope), _real in real_roi_cell.items():
        if _scope != SCOPE_EARLY:
            continue
        if f"{_sp}|{_sm}" not in FULL_RELEASE_EARLY:
            continue
        if _real.get("n", 0) >= REAL_ROI_GATE_N_MIN and _real.get("roi", 0) < 0:
            full_release_blocked.append([_sp, _sm, _dr, _iv, SCOPE_EARLY])
    if full_release_blocked:
        print(f"[full-release] 实盘负ROI熔断撤回 {len(full_release_blocked)} 格: {full_release_blocked}", flush=True)

    out = {
        "generated_at": datetime.now().isoformat(),
        "full_release_early": sorted(FULL_RELEASE_EARLY),  # 2026-11-07 早盘三运动三主力盘口全量放开
        "full_release_live": sorted(FULL_RELEASE_LIVE),  # 2026-11-10 滚球与早盘一致(7运动×1x2/hc/ou)
        "full_release_blocked": full_release_blocked,  # 2026-11-09 全量放开格的实盘负ROI熔断清单(投注端先查)
        "market_released": market_released,
        "league_released": league_released,
        "league_blocked": league_blocked,
        "observe_released": observe_released,
        "observe_blocked": observe_blocked,
        "drop_released": drop_released,   # 2026-11-01 drop 源数据驱动释放(盘口×方向×概率桶)
        "drop_blocked": drop_blocked,     # 2026-11-01 drop 源数据驱动拦截(实盘ROI负)
        "observe_release_caps": observe_release_caps,
        "direction_released": direction_released,
        "direction_blocked": direction_blocked,
        "direction_min_ev": direction_min_ev,
        "direction_window_blocked": direction_window_blocked,
        "direction_window_released": direction_window_released,
        "observe_real_paper_gap": observe_real_paper_gap,  # 实盘-纸单赢率差(只展示, 2026-09-19)
        "observe_calibration": observe_calibration,  # Brier 校准检验(只展示, 2026-09-19)
        "observe_clv_validation": {  # 早盘 CLV 中位(验证/审计, 2026-10-18 起不再是释放闸门)
            f"{s}|{sm}|{dr}|{iv}|early": round(med, 2)
            for (s, sm, dr, iv), (med, _n) in clv_med.items()
        },
        "sport_cn": SPORT_CN,
    }
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    tmp.replace(OUT)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


CONFIRM = False  # --confirm 时跳过"待确认"门(2026-09-22 用户确认后执行)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="盘口释放清单计算")
    ap.add_argument("--confirm", action="store_true", help="确认待执行的释放/封锁变更(用户确认后运行)")
    _args = ap.parse_args()
    CONFIRM = _args.confirm
    main()
