"""统一预测引擎 — 整合特征流水线 + 集成模型 + 市场赔率。

用法:
    from src.predict.ensemble_predictor import EnsemblePredictor
    predictor = EnsemblePredictor('bb')
    predictions = predictor.predict(odds_data)
"""
import json
import sys
from pathlib import Path
from typing import Dict, List
from math import radians, sin, cos, sqrt, asin

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

import joblib
import numpy as np
import pandas as pd

from config.settings import MODEL_DIR, DATA_DIR
from src.core.calibration import dynamic_shrinkage
from src.features.bb_pipeline import (
    _process_team_stats as _bb_process_team_stats,
    _NBA_CITY_COORDS as _BB_CITY_COORDS,
    _haversine as _bb_haversine,
)
from src.features.football_pipeline import (
    _process_team_stats as _fb_process_team_stats,
)

# 兼容已 pickle 的 Stage2Stacking / WeightedEnsemble（旧模型存为 __main__.*）
import src.models.stacking as _stacking_mod
sys.modules['__main__'].Stage2Stacking = _stacking_mod.Stage2Stacking
sys.modules['__main__'].WeightedEnsemble = _stacking_mod.WeightedEnsemble

MODEL_DIR_PATH = Path(MODEL_DIR) if isinstance(MODEL_DIR, str) else MODEL_DIR
DATA_DIR_PATH = Path(DATA_DIR) if isinstance(DATA_DIR, str) else DATA_DIR

# ── 博彩公司锋利度评级（sync with line_movement.py） ────────────
_BOOK_SHARPNESS = {
    "pinnacle": 1.0, "bet365": 0.6, "william hill": 0.5,
    "ladbrokes": 0.3, "betfair": 0.9, "betmgm": 0.3,
    "fanduel": 0.2, "draftkings": 0.2, "unibet": 0.4,
    "888sport": 0.3, "sportsbet": 0.3, "neds": 0.3,
    "pointsbet": 0.3, "betway": 0.4, "betstars": 0.2,
    "bovada": 0.4, "mybookie": 0.1, "betonline": 0.4,
    "bwin": 0.5, "smarkets": 0.8, "matchbook": 0.7,
}
_SHARP_THRESHOLD = 0.7


def _is_sharp_book(bookmaker_title: str) -> bool:
    """判断博彩公司是否属于 sharp 类。"""
    name = bookmaker_title.lower().replace(" ", "")
    for known_name, sharpness in _BOOK_SHARPNESS.items():
        if known_name in name and sharpness >= _SHARP_THRESHOLD:
            return True
    return False


# ── 欧洲球队坐标（用于行程距离特征）──
_FB_COORDS = {
    "Arsenal": (51.555, -0.108), "Aston Villa": (52.509, -1.885), "Bournemouth": (50.735, -1.838),
    "Brentford": (51.488, -0.289), "Brighton": (50.861, -0.084), "Chelsea": (51.482, -0.191),
    "Crystal Palace": (51.398, -0.086), "Everton": (53.439, -2.966), "Fulham": (51.475, -0.221),
    "Liverpool": (53.431, -2.961), "Leeds United": (53.778, -1.572), "Leicester City": (52.620, -1.142),
    "Manchester City": (53.483, -2.200), "Manchester United": (53.463, -2.291),
    "Newcastle United": (54.975, -1.622), "Nottingham Forest": (52.942, -1.133),
    "Tottenham Hotspur": (51.603, -0.066), "West Ham United": (51.539, 0.017),
    "Wolverhampton Wanderers": (52.590, -2.130),
    "Atletico Madrid": (40.437, -3.599), "Barcelona": (41.381, 2.123),
    "Real Madrid": (40.453, -3.688), "Sevilla": (37.384, -5.970),
    "Real Betis": (37.356, -5.981), "Valencia": (39.475, -0.358),
    "Villarreal": (39.944, -0.103), "Athletic Bilbao": (43.263, -2.948),
    "Real Sociedad": (43.301, -1.974), "Celta Vigo": (42.212, -8.739),
    "AC Milan": (45.478, 9.124), "Inter Milan": (45.478, 9.124),
    "Juventus": (45.110, 7.641), "Roma": (41.935, 12.455), "Lazio": (41.935, 12.455),
    "Napoli": (40.828, 14.193), "Atalanta": (45.699, 9.744), "Fiorentina": (43.771, 11.282),
    "FC Bayern Munich": (48.219, 11.625), "Borussia Dortmund": (51.492, 7.415),
    "RB Leipzig": (51.345, 12.348), "Bayer Leverkusen": (51.038, 7.002),
    "Paris Saint-Germain": (48.841, 2.253), "Olympique Marseille": (43.270, 5.396),
    "Olympique Lyon": (45.724, 4.832), "AS Monaco": (43.727, 7.415),
    "Lille": (50.612, 3.130), "Nice": (43.704, 7.194),
}
_FB_CENTER_EUROPE = (50.0, 10.0)


def _haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    R = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * asin(sqrt(a))


def _team_distance(home: str, away: str) -> float:
    c1 = _FB_COORDS.get(home, _FB_CENTER_EUROPE)
    c2 = _FB_COORDS.get(away, _FB_CENTER_EUROPE)
    return _haversine(c1[0], c1[1], c2[0], c2[1])


def extract_sharp_market_probs(odds_data: List[Dict]) -> Dict[str, Dict]:
    """仅使用 sharp 博彩公司提取市场隐含概率。

    用 Pinnacle/Betfair 等 sharp 公司的赔率来估算"真实"市场概率，
    避免软公司虚高赔率导致的虚假正EV信号。

    Returns:
        {match_key: {sharp_home_prob, sharp_away_prob, sharp_draw_prob, sharp_unavailable}}
    """
    results = {}
    for game in odds_data:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        bookmakers = game.get("bookmakers", [])
        match_key = f"{home} @ {away}"

        sharp_home_prices, sharp_away_prices, sharp_draw_prices = [], [], []

        for bm in bookmakers:
            bm_title = bm.get("title", "")
            if not _is_sharp_book(bm_title):
                continue
            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                outcomes = market.get("outcomes", [])
                h = next((o["price"] for o in outcomes
                          if o.get("name", "").strip().lower() == home.strip().lower()), None)
                a = next((o["price"] for o in outcomes
                          if o.get("name", "").strip().lower() == away.strip().lower()), None)
                d = None
                if len(outcomes) == 3:
                    for o in outcomes:
                        on = o.get("name", "").strip().lower()
                        if on not in (home.strip().lower(), away.strip().lower()):
                            d = o["price"]; break
                if h and a:
                    sharp_home_prices.append(h)
                    sharp_away_prices.append(a)
                    sharp_draw_prices.append(d)

        if len(sharp_home_prices) < 2:
            results[match_key] = {"sharp_unavailable": True}
            continue

        n = len(sharp_home_prices)
        home_probs, away_probs, draw_probs = [], [], []
        for i in range(n):
            imp_h, imp_a = 1.0 / sharp_home_prices[i], 1.0 / sharp_away_prices[i]
            total = imp_h + imp_a
            if i < len(sharp_draw_prices) and sharp_draw_prices[i]:
                total += 1.0 / sharp_draw_prices[i]
            home_probs.append(imp_h / total)
            away_probs.append(imp_a / total)
            if i < len(sharp_draw_prices) and sharp_draw_prices[i]:
                draw_probs.append((1.0 / sharp_draw_prices[i]) / total)

        results[match_key] = {
            "sharp_home_prob": float(np.mean(home_probs)),
            "sharp_away_prob": float(np.mean(away_probs)),
            "sharp_draw_prob": float(np.mean(draw_probs)) if draw_probs else 0.0,
            "sharp_home_odds": max(sharp_home_prices),
            "sharp_n_bookmakers": n,
            "sharp_unavailable": False,
        }
    return results


def _extract_spread_total_market_probs(game: Dict) -> dict:
    """从单场比赛提取 spread/total 市场的隐含概率（去水）。

    Returns:
        {spread_home_prob, total_over_prob} 或空字典
    """
    home = game.get("home_team", "").strip().lower()
    away = game.get("away_team", "").strip().lower()
    bookmakers = game.get("bookmakers", [])

    spread_home_prices, spread_away_prices = [], []
    total_over_prices, total_under_prices = [], []

    for bm in bookmakers:
        for market in bm.get("markets", []):
            if market.get("key") == "spreads":
                outcomes = market.get("outcomes", [])
                h = next((o["price"] for o in outcomes if o.get("name", "").strip().lower() == home), None)
                a = next((o["price"] for o in outcomes if o.get("name", "").strip().lower() == away), None)
                if h and a:
                    spread_home_prices.append(h)
                    spread_away_prices.append(a)
            elif market.get("key") == "totals":
                outcomes = market.get("outcomes", [])
                over = next((o["price"] for o in outcomes if o.get("name") == "Over"), None)
                under = next((o["price"] for o in outcomes if o.get("name") == "Under"), None)
                if over and under:
                    total_over_prices.append(over)
                    total_under_prices.append(under)

    result = {}
    if spread_home_prices:
        avg_h = np.mean(spread_home_prices)
        avg_a = np.mean(spread_away_prices)
        result["spread_home_prob"] = (1.0 / avg_h) / (1.0 / avg_h + 1.0 / avg_a)
    if total_over_prices:
        avg_o = np.mean(total_over_prices)
        avg_u = np.mean(total_under_prices)
        result["total_over_prob"] = (1.0 / avg_o) / (1.0 / avg_o + 1.0 / avg_u)
    return result


# ── 市场概率提取 ─────────────────────────────────────────────────

def extract_market_probs(odds_data: List[Dict]) -> Dict[str, Dict]:
    """从 Odds API 响应中提取市场隐含概率。

    跨所有博彩公司计算平均市场概率（扣除水钱），以及市场分歧度。

    Returns:
        {match_key: {home_prob, away_prob, n_bookmakers, home_odds, ...}}
    """
    results = {}
    for game in odds_data:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        bookmakers = game.get("bookmakers", [])
        match_key = f"{home} @ {away}"

        home_prices, away_prices, draw_prices = [], [], []

        for bm in bookmakers:
            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                outcomes = market.get("outcomes", [])
                h = next((o["price"] for o in outcomes
                          if o.get("name", "").strip().lower() == home.strip().lower()), None)
                a = next((o["price"] for o in outcomes
                          if o.get("name", "").strip().lower() == away.strip().lower()), None)
                d = None
                if len(outcomes) == 3:
                    for o in outcomes:
                        on = o.get("name", "").strip().lower()
                        if on not in (home.strip().lower(), away.strip().lower()):
                            d = o["price"]; break
                if h and a:
                    home_prices.append(h); away_prices.append(a); draw_prices.append(d)

        if not home_prices:
            continue

        n = len(home_prices)
        home_probs, away_probs, draw_probs = [], [], []
        for i in range(n):
            imp_h, imp_a = 1.0 / home_prices[i], 1.0 / away_prices[i]
            total = imp_h + imp_a
            if i < len(draw_prices) and draw_prices[i]:
                total += 1.0 / draw_prices[i]
            home_probs.append(imp_h / total)
            away_probs.append(imp_a / total)
            if i < len(draw_prices) and draw_prices[i]:
                draw_probs.append((1.0 / draw_prices[i]) / total)

        results[match_key] = {
            "home_team": home,
            "away_team": away,
            "commence_time": game.get("commence_time", ""),
            "market_home_prob": float(np.mean(home_probs)),
            "market_away_prob": float(np.mean(away_probs)),
            "market_draw_prob": float(np.mean(draw_probs)) if draw_probs else 0.0,
            "home_odds": max(home_prices),
            "away_odds": max(away_prices),
            "n_bookmakers": n,
            "home_prob_std": float(np.std(home_probs)) if n > 1 else 0.0,
            "away_prob_std": float(np.std(away_probs)) if n > 1 else 0.0,
        }
    return results


# ── 球队级滚动统计（复用 pipeline 内部函数） ────────────────────

def _slope(y):
    if len(y) < 2:
        return 0.0
    return np.polyfit(np.arange(len(y)), y, 1)[0]


# ── 历史数据加载 ──────────────────────────────────────────────

def _load_bb_history():
    """加载篮球历史原始比赛数据（含主客队、比分）。"""
    # 直接从原始CSV加载，避免build_bb_features返回的合并特征DataFrame
    base = Path(__file__).resolve().parent.parent.parent
    legacy = base / "data" / "storage" / "nba_scores.csv"
    modern = base / "data" / "storage" / "basketball_history.csv"
    parts = []

    if legacy.exists():
        old = pd.read_csv(legacy)
        old.columns = [c.strip().lower() for c in old.columns]
        old["date"] = pd.to_datetime(old["dateslash"])
        old["home"] = old["team"].str.strip()
        old["away"] = old["oppteam"].str.strip()
        old["home_goals"] = pd.to_numeric(old["teampts"], errors="coerce")
        old["away_goals"] = pd.to_numeric(old["opppts"], errors="coerce")
        parts.append(old[["date", "home", "away", "home_goals", "away_goals"]])

    if modern.exists():
        new = pd.read_csv(modern)
        new.columns = [c.strip().lower() for c in new.columns]
        new["date"] = pd.to_datetime(new["date"], utc=True, format='mixed').dt.tz_localize(None)
        new["home"] = new["home"].str.strip()
        new["away"] = new["away"].str.strip()
        new["home_goals"] = pd.to_numeric(new["home_score"], errors="coerce")
        new["away_goals"] = pd.to_numeric(new["away_score"], errors="coerce")
        parts.append(new[["date", "home", "away", "home_goals", "away_goals"]])

    if not parts:
        raise FileNotFoundError("未找到篮球历史数据")
    df = pd.concat(parts, ignore_index=True).sort_values("date").reset_index(drop=True)
    return df


def _load_fb_history():
    """加载足球历史原始比赛数据。"""
    base = Path(__file__).resolve().parent.parent.parent
    csv_path = base / "data" / "storage" / "football_history.csv"
    if not csv_path.exists():
        raise FileNotFoundError("未找到足球历史数据")
    df = pd.read_csv(csv_path)
    df.columns = [c.strip().lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"], format="mixed", utc=True).dt.tz_localize(None)
    df = df.rename(columns={"home_score": "home_goals", "away_score": "away_goals"})
    cols = ["date", "home", "away", "home_goals", "away_goals"]
    if "competition" in df.columns:
        cols.append("competition")
    return df[cols]


def _load_nfl_history():
    """加载 NFL 历史原始比赛数据。"""
    base = Path(__file__).resolve().parent.parent.parent
    csv_path = base / "data" / "storage" / "nfl_history.csv"
    if not csv_path.exists():
        raise FileNotFoundError("未找到 NFL 历史数据")
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df[["date", "home", "away", "home_score", "away_score",
               "spread_line", "total_line", "roof", "temp", "wind",
               "home_rest", "away_rest"]]


# ── 市场价值 / 伤病特征注入（足球） ──────────────────────────

def _add_fb_market_value_features(match_df):
    """向比赛 DataFrame 注入球队市值特征。"""
    try:
        from src.features.transfermarkt_client import get_team_market_value
        from src.features.football_pipeline import _TM_NAME_MAP, _load_tm_cache, _save_tm_cache
    except ImportError:
        match_df["home_market_value"] = 0.0
        match_df["away_market_value"] = 0.0
        match_df["market_value_diff"] = 0.0
        return match_df

    mv_cache = _load_tm_cache()
    for team in set(match_df["home"].unique()) | set(match_df["away"].unique()):
        if team not in mv_cache:
            search = _TM_NAME_MAP.get(team, team)
            val = get_team_market_value(search) or 0.0
            mv_cache[team] = val
    _save_tm_cache(mv_cache)

    match_df["home_market_value"] = match_df["home"].map(mv_cache).fillna(0.0)
    match_df["away_market_value"] = match_df["away"].map(mv_cache).fillna(0.0)
    match_df["market_value_diff"] = match_df["home_market_value"] - match_df["away_market_value"]
    return match_df


# ── 主预测器 ─────────────────────────────────────────────────

class EnsemblePredictor:
    """统一预测引擎：加载集成模型，构建特征，输出预测。"""

    def __init__(self, sport: str):
        self.sport = sport
        if sport == "bb":
            self.prefix = "model_bb"
        elif sport == "nfl":
            self.prefix = "model_nfl"
        else:
            self.prefix = "model_fb"

        feat_file = MODEL_DIR_PATH / f"{self.prefix}_features.json"
        with open(feat_file) as f:
            self.feat_cols = json.load(f)
        logger.info("  加载特征列: %s 个", len(self.feat_cols))

        self.models = {}
        self._target_feat_cols = {}  # per-target feature override
        for target in ["win", "spread_result", "total_result"]:
            self._try_load_weighted(target)
            if target not in self.models:
                path = MODEL_DIR_PATH / f"{self.prefix}_{target}_ensemble.pkl"
                if path.exists():
                    self.models[target] = joblib.load(path)
                    logger.info("  加载模型: %s", path.name)

        # ── 模型版本（用于投注审计日志） ──
        self.model_version = "unknown"
        meta_path = MODEL_DIR_PATH / "model_metadata.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                key = f"last_train_{sport}"
                if key in meta:
                    ts = meta[key]
                    date_part = ts[:10].replace("-", "")
                    self.model_version = f"{sport}_{date_part}"
                    logger.info("  模型版本: %s", self.model_version)
            except Exception:
                pass

    def _get_feature_cols(self, target: str) -> list:
        """返回指定目标使用的特征列列表。

        BB total_result 排除 ovrundr（市场大小分线），其他模型使用完整特征集。
        """
        if self.sport == 'bb' and target == 'total_result':
            return [c for c in self.feat_cols if c != 'ovrundr']
        return self.feat_cols

    def _try_load_weighted(self, target: str):
        """记录集成权重信息（预测统一使用校准后的集成模型）。"""
        meta_path = MODEL_DIR_PATH / f"{self.prefix}_{target}_ensemble_meta.json"
        if not meta_path.exists():
            return
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            weights = meta.get("ensemble_weights", {})
            if weights:
                names_str = ", ".join(f"{n}({w:.3f})" for n, w in weights.items())
                logger.info("  集成权重 [%s]: %s", target, names_str)
        except Exception as e:
            logger.warning("  集成权重读取失败 (%s): %s", target, e)

    def _build_features(self, odds_data: List[Dict]) -> np.ndarray:
        """构建预测特征矩阵。"""
        if self.sport == "bb":
            return self._build_bb_features(odds_data)
        if self.sport == "nfl":
            return self._build_nfl_features(odds_data)
        return self._build_fb_features(odds_data)

    def _build_bb_features(self, odds_data: List[Dict]) -> np.ndarray:
        """构建篮球预测特征。"""
        from src.core.normalizer import find_best_odds

        hist = _load_bb_history()

        today_rows = []
        for g in odds_data:
            # 提取总盘口线作为 ovrundr 特征（用于 total 模型）
            _, _, total_pt = find_best_odds(g, market_type='totals')
            today_rows.append({
                "date": pd.Timestamp.now(tz="UTC").tz_localize(None),
                "home": g["home_team"], "away": g["away_team"],
                "home_score": np.nan, "away_score": np.nan,
                "home_goals": np.nan, "away_goals": np.nan,
                "ovrundr": total_pt if total_pt else np.nan,
            })
        today_df = pd.DataFrame(today_rows)

        combined = pd.concat([hist, today_df], ignore_index=True).sort_values("date").reset_index(drop=True)
        # 清理空值日期和队名（历史上的脏数据，今日数据是干净的所以不会丢）
        combined = combined.dropna(subset=["date", "home", "away"]).copy()
        n_hist = len(combined) - len(today_df)  # dropna 可能减少 hist 行数

        # ELO 评级
        from src.features.elo import compute_elo
        combined = compute_elo(combined, K=20)

        # ── 使用训练流水线的特征工程（_process_team_stats 生成全部球队特征）──
        combined_input = combined.copy()
        combined_input['home_score'] = combined_input['home_goals']
        combined_input['away_score'] = combined_input['away_goals']
        team_feats = _bb_process_team_stats(combined_input)

        match_df = combined[["date", "home", "away", "home_goals", "away_goals",
                             "home_elo", "away_elo", "elo_diff", "ovrundr"]].copy()
        for side, tc in [("home", "home"), ("away", "away")]:
            sf = team_feats.copy()
            sf.columns = [f"{side}_{c}" if c not in ("date", "team") else c for c in sf.columns]
            sf.rename(columns={"date": "date", "team": tc}, inplace=True)
            sf = sf.dropna(subset=["date", tc]).copy()
            match_df = pd.merge_asof(
                match_df.sort_values("date"), sf.sort_values("date"),
                by=tc, on="date", direction="backward")

        # ── 基础交叉特征（与 bp_pipeline.build_bb_features 一致）──
        match_df["off_vs_def"] = match_df["home_gf_ewm5"] - match_df["away_ga_ewm5"]
        match_df["b2b_diff"] = match_df["home_b2b"] - match_df["away_b2b"]
        match_df["rest_diff"] = match_df["home_rest_days"] - match_df["away_rest_days"]
        match_df["travel_diff"] = match_df["home_travel_distance"] - match_df["away_travel_distance"]
        match_df["travel_3_diff"] = match_df["home_travel_distance_3"] - match_df["away_travel_distance_3"]

        # ── NBA 伤病特征注入（加权严重度） ──
        try:
            from src.features.nba_injuries import get_nba_injuries, TEAM_ABBR
            _INJURY_WEIGHTS = {
                'out': 1.0, 'doubtful': 0.7,
                'questionable': 0.4, 'day-to-day': 0.2,
            }
            injuries = get_nba_injuries()
            if injuries:
                injury_df = pd.DataFrame(injuries)
                if 'status' in injury_df.columns:
                    injury_df['weight'] = injury_df['status'].str.lower().map(
                        lambda s: next((w for kw, w in _INJURY_WEIGHTS.items()
                                        if kw in s.lower()), 0.0))
                else:
                    injury_df['weight'] = 1.0
                team_scores = injury_df.groupby('team')['weight'].sum().to_dict()
                def _injured_score(team_name):
                    team_clean = team_name.strip()
                    abbr = TEAM_ABBR.get(team_clean, '')
                    if not abbr:
                        rev = {v.lower(): k for k, v in TEAM_ABBR.items()}
                        full = rev.get(team_clean.lower(), '')
                        abbr = TEAM_ABBR.get(full, '')
                    return team_scores.get(abbr, 0.0)
                match_df['home_injured'] = match_df['home'].map(_injured_score).fillna(0.0)
                match_df['away_injured'] = match_df['away'].map(_injured_score).fillna(0.0)
                match_df['injured_diff'] = match_df['home_injured'] - match_df['away_injured']
                match_df['home_key_injury'] = (match_df['home_injured'] >= 1.0).astype(int)
                match_df['away_key_injury'] = (match_df['away_injured'] >= 1.0).astype(int)
            else:
                match_df['home_injured'] = 0.0
                match_df['away_injured'] = 0.0
                match_df['injured_diff'] = 0.0
                match_df['home_key_injury'] = 0
                match_df['away_key_injury'] = 0
        except Exception:
            match_df['home_injured'] = 0.0
            match_df['away_injured'] = 0.0
            match_df['injured_diff'] = 0.0
            match_df['home_key_injury'] = 0
            match_df['away_key_injury'] = 0

        # ── 总分预测交互特征 ──
        match_df["combined_total_avg_5"] = match_df["home_total_pts_avg_5"] + match_df["away_total_pts_avg_5"]
        match_df["combined_pts_scored_avg_5"] = match_df["home_pts_scored_avg_5"] + match_df["away_pts_scored_avg_5"]
        match_df["combined_pts_allowed_avg_5"] = match_df["home_pts_allowed_avg_5"] + match_df["away_pts_allowed_avg_5"]
        match_df["total_volatility_interaction"] = match_df["home_total_pts_volatility_5"] * match_df["away_total_pts_volatility_5"]
        match_df["high_score_rate_sum"] = (match_df["home_high_score_rate_10"] + match_df["away_high_score_rate_10"]).clip(0, 2)
        match_df["total_rest_sum"] = match_df["home_rest_days"] + match_df["away_rest_days"]
        match_df["pace_proxy"] = (match_df["home_gf_ewm5"] + match_df["away_gf_ewm5"] +
                                  match_df["home_ga_ewm5"] + match_df["away_ga_ewm5"]) / 2

        # ── 新增交叉特征（与 build_bb_features 同步）──
        # 净胜分 / SoS / 主客场 / 波动率
        if 'home_avg_margin_5' in match_df.columns and 'away_avg_margin_5' in match_df.columns:
            match_df['margin_diff'] = match_df['home_avg_margin_5'] - match_df['away_avg_margin_5']
        if 'home_sos_5' in match_df.columns and 'away_sos_5' in match_df.columns:
            match_df['sos_diff'] = match_df['home_sos_5'] - match_df['away_sos_5']
        if 'home_home_win_rate_5' in match_df.columns and 'away_away_win_rate_5' in match_df.columns:
            match_df['home_away_win_diff'] = match_df['home_home_win_rate_5'] - match_df['away_away_win_rate_5']
        if 'home_margin_volatility_5' in match_df.columns and 'away_margin_volatility_5' in match_df.columns:
            match_df['margin_volatility_interaction'] = match_df['home_margin_volatility_5'] * match_df['away_margin_volatility_5']

        # 动量 + 形态回归
        if 'home_streak' in match_df.columns and 'away_streak' in match_df.columns:
            match_df['streak_diff'] = match_df['home_streak'] - match_df['away_streak']
        if 'home_form_regression' in match_df.columns and 'away_form_regression' in match_df.columns:
            match_df['form_regression_diff'] = match_df['home_form_regression'] - match_df['away_form_regression']

        # 赛季阶段
        _month = pd.to_datetime(match_df['date']).dt.month
        match_df['season_stage'] = _month.map({
            10: 0, 11: 0, 12: 1, 1: 1, 2: 2, 3: 2, 4: 3, 5: 3, 6: 3,
        }).fillna(-1)

        # H2H 历史交锋（过去 5 次对阵）
        h2h_cache = {}
        for _, r in combined.sort_values('date').iterrows():
            h, a = r['home'], r['away']
            key = tuple(sorted([h, a]))
            if key not in h2h_cache:
                h2h_cache[key] = []
            h2h_cache[key].append({
                'home': h, 'away': a,
                'home_goals': r['home_goals'], 'away_goals': r['away_goals'],
                'date': r['date'],
            })
        h2h_rows = []
        for _, row in match_df.iterrows():
            h, a = row['home'], row['away']
            key = tuple(sorted([h, a]))
            meetings = h2h_cache.get(key, [])
            prior = [m for m in meetings if m['date'] < row['date']]
            last_5 = prior[-5:]
            if len(last_5) >= 2:
                h_wins = a_wins = 0
                for m in last_5:
                    if m['home'] == h:
                        if m['home_goals'] > m['away_goals']:
                            h_wins += 1
                        else:
                            a_wins += 1
                    else:
                        if m['away_goals'] > m['home_goals']:
                            h_wins += 1
                        else:
                            a_wins += 1
                avg_total = sum(m['home_goals'] + m['away_goals'] for m in last_5) / len(last_5)
            else:
                h_wins = a_wins = 0
                avg_total = 0.0
            h2h_rows.append({
                'h2h_home_wins': h_wins, 'h2h_away_wins': a_wins,
                'h2h_avg_total_pts': avg_total, 'h2h_net_wins': h_wins - a_wins,
            })
        if h2h_rows:
            h2h_df = pd.DataFrame(h2h_rows, index=match_df.index)
            match_df = pd.concat([match_df, h2h_df], axis=1)

        # 联赛宏观趋势（expanding window）
        league_df = combined.sort_values('date').copy()
        league_df['_total_pts'] = league_df['home_goals'] + league_df['away_goals']
        league_df['_home_win'] = (league_df['home_goals'] > league_df['away_goals']).astype(int)
        league_df['_home_adv'] = league_df['home_goals'] - league_df['away_goals']
        league_df['_home_win'] = league_df['_home_win'].shift(1).expanding(min_periods=10).mean()
        league_df['_avg_total_pts'] = league_df['_total_pts'].shift(1).expanding(min_periods=10).mean()
        league_df['_home_adv'] = league_df['_home_adv'].shift(1).expanding(min_periods=10).mean()
        match_df['league_home_win_rate'] = league_df['_home_win'].values
        match_df['league_avg_total_pts'] = league_df['_avg_total_pts'].values
        match_df['league_home_adv'] = league_df['_home_adv'].values

        # B2B 密度
        if 'home_b2b' in match_df.columns and 'away_b2b' in match_df.columns:
            match_df['home_b2b_density'] = match_df['home_b2b'].rolling(5, min_periods=1).sum()
            match_df['away_b2b_density'] = match_df['away_b2b'].rolling(5, min_periods=1).sum()
            match_df['b2b_density_diff'] = match_df['home_b2b_density'] - match_df['away_b2b_density']

        # 休息综合优势
        r_diff = match_df['rest_diff'].clip(-3, 3) / 3
        a_b2b = match_df['away_b2b'] * 0.5
        h_b2b = match_df['home_b2b'] * 0.5
        t_diff = match_df['travel_3_diff'].clip(-3000, 3000) / 3000
        match_df['rest_advantage'] = r_diff + (a_b2b - h_b2b) + t_diff

        # 动量质量
        if 'home_streak' in match_df.columns and 'home_avg_margin_5' in match_df.columns:
            match_df['home_momentum_quality'] = match_df['home_streak'] * match_df['home_avg_margin_5'].clip(-15, 15) / 15
            match_df['away_momentum_quality'] = match_df['away_streak'] * match_df['away_avg_margin_5'].clip(-15, 15) / 15
            match_df['momentum_quality_diff'] = match_df['home_momentum_quality'] - match_df['away_momentum_quality']

        # H2H 主导率
        if 'h2h_home_wins' in match_df.columns and 'h2h_away_wins' in match_df.columns:
            match_df['h2h_total'] = match_df['h2h_home_wins'] + match_df['h2h_away_wins']
            match_df['h2h_dominance'] = match_df['h2h_net_wins'] / match_df['h2h_total'].clip(lower=1)
        if 'h2h_dominance' in match_df.columns and 'home_home_win_rate_5' in match_df.columns:
            match_df['h2h_form_x'] = match_df['h2h_dominance'] * match_df['home_home_win_rate_5']

        # ELO 残差差
        if 'home_elo_residual_5' in match_df.columns and 'away_elo_residual_5' in match_df.columns:
            match_df['elo_residual_diff'] = match_df['home_elo_residual_5'] - match_df['away_elo_residual_5']

        match_df = match_df.ffill().fillna(0)

        today_start = min(n_hist, len(match_df))
        X = match_df.iloc[today_start:].copy()
        # 确保所有训练时使用的特征都存在（缺失特征补0，保持列顺序一致）
        for col in self.feat_cols:
            if col not in X.columns:
                X[col] = 0.0
        return X[self.feat_cols].fillna(0)

    def _build_nfl_features(self, odds_data: List[Dict]) -> np.ndarray:
        """构建 NFL 预测特征（轻量版，直接基于 nfl_features.csv 特征骨架计算）。"""
        hist = _load_nfl_history()

        today_rows = []
        for g in odds_data:
            today_rows.append({
                "date": pd.Timestamp.now(tz="UTC").tz_localize(None),
                "home": g["home_team"], "away": g["away_team"],
                "home_score": np.nan, "away_score": np.nan,
                "spread_line": np.nan, "total_line": np.nan,
                "roof": np.nan, "temp": np.nan, "wind": np.nan,
                "home_rest": 7, "away_rest": 7,
            })
        today_df = pd.DataFrame(today_rows)

        combined = pd.concat([hist, today_df], ignore_index=True).sort_values("date").reset_index(drop=True)

        # ELO
        from src.features.elo import compute_elo
        combined = compute_elo(combined, K=40,
                               score_home_col="home_score", score_away_col="away_score")

        # 球队滚动统计（使用 points for/against）
        team_records = []
        for _, row in combined.iterrows():
            for side, team in [("home", row["home"]), ("away", row["away"])]:
                pf = row["home_score"] if side == "home" else row["away_score"]
                pa = row["away_score"] if side == "home" else row["home_score"]
                win = 1.0 if (side == "home" and row["home_score"] > row["away_score"]) or \
                             (side == "away" and row["away_score"] > row["home_score"]) else 0.0
                team_records.append({
                    "date": row["date"], "team": team,
                    "pf": pf, "pa": pa, "win": win, "net": pf - pa,
                })
        tr = pd.DataFrame(team_records).sort_values(["team", "date"])

        for w in [3, 5]:
            tr[f"pf_avg_{w}"] = tr.groupby("team")["pf"].transform(
                lambda x: x.shift(1).rolling(w, min_periods=1).mean())
            tr[f"pa_avg_{w}"] = tr.groupby("team")["pa"].transform(
                lambda x: x.shift(1).rolling(w, min_periods=1).mean())
            tr[f"net_rating_{w}"] = tr[f"pf_avg_{w}"] - tr[f"pa_avg_{w}"]
        tr["pf_ewm5"] = tr.groupby("team")["pf"].transform(
            lambda x: x.shift(1).ewm(span=5, adjust=False).mean())
        tr["pa_ewm5"] = tr.groupby("team")["pa"].transform(
            lambda x: x.shift(1).ewm(span=5, adjust=False).mean())
        tr["win_rate_5"] = tr.groupby("team")["win"].transform(
            lambda x: x.shift(1).rolling(5, min_periods=1).mean())
        # 净胜分斜率（最近5场）
        tr["net_slope_5"] = tr.groupby("team")["net"].transform(
            lambda x: x.shift(1).rolling(5, min_periods=2).apply(lambda y: np.polyfit(np.arange(len(y)), y, 1)[0], raw=False))

        feat_cols = ["date", "team", "pf_avg_3", "pf_avg_5", "pa_avg_3", "pa_avg_5",
                     "net_rating_3", "net_rating_5", "pf_ewm5", "pa_ewm5",
                     "win_rate_5", "net_slope_5"]
        team_feats = tr[feat_cols]

        match_df = combined[["date", "home", "away", "home_elo", "away_elo", "elo_diff"]].copy()
        for side, tc in [("home", "home"), ("away", "away")]:
            sf = team_feats.copy()
            sf.columns = [f"{side}_{c}" if c not in ("date", "team") else c for c in sf.columns]
            sf.rename(columns={"date": "date", "team": tc}, inplace=True)
            match_df = pd.merge_asof(
                match_df.sort_values("date"), sf.sort_values("date"),
                by=tc, on="date", direction="backward")

        match_df["off_vs_def"] = match_df["home_pf_avg_5"] - match_df["away_pa_avg_5"]
        match_df["rest_diff"] = match_df["home_rest"] - match_df["away_rest"]

        # 天气特征
        match_df["is_dome"] = match_df["roof"].apply(
            lambda x: 1.0 if isinstance(x, str) and x.strip().lower() in ("dome", "closed") else (0.0 if pd.notna(x) else np.nan))
        match_df["temp"] = pd.to_numeric(match_df["temp"], errors="coerce")
        match_df["wind"] = pd.to_numeric(match_df["wind"], errors="coerce")
        outdoor = match_df["is_dome"] == 0
        mask_t = outdoor & match_df["temp"].isna()
        mask_w = outdoor & match_df["wind"].isna()
        if mask_t.any():
            match_df.loc[mask_t, "temp"] = match_df.loc[outdoor, "temp"].median()
        if mask_w.any():
            match_df.loc[mask_w, "wind"] = match_df.loc[outdoor, "wind"].median()
        match_df.loc[match_df["is_dome"] == 1, "temp"] = 20.0
        match_df.loc[match_df["is_dome"] == 1, "wind"] = 0.0

        match_df = match_df.ffill().fillna(0)

        n_hist = len(combined) - len(today_df)
        today_start = min(n_hist, len(match_df))
        X = match_df.iloc[today_start:].copy()
        # 确保所有训练时使用的特征都存在（缺失特征补0，保持列顺序一致）
        for col in self.feat_cols:
            if col not in X.columns:
                X[col] = 0.0
        return X[self.feat_cols].fillna(0)

    def _build_fb_features(self, odds_data: List[Dict]) -> np.ndarray:
        """构建足球预测特征（与训练流水线一致）。"""
        hist = _load_fb_history()
        hist = _add_fb_market_value_features(hist)

        today_rows = []
        for g in odds_data:
            today_rows.append({
                "date": pd.Timestamp.now(tz="UTC").tz_localize(None),
                "home": g["home_team"], "away": g["away_team"],
                "home_goals": np.nan, "away_goals": np.nan,
            })
        today_df = pd.DataFrame(today_rows)

        combined = pd.concat([hist, today_df], ignore_index=True).sort_values("date").reset_index(drop=True)
        # win 列用于联赛宏观特征
        combined["win"] = (combined["home_goals"] > combined["away_goals"]).astype(int)

        from src.features.elo import compute_elo
        combined = compute_elo(combined, K=30)
        team_feats = _fb_process_team_stats(combined)
        combined = _add_fb_market_value_features(combined)

        match_df = combined[["date", "home", "away", "home_elo", "away_elo", "elo_diff"]].copy()
        if "competition" in combined.columns:
            match_df["competition"] = combined["competition"].values

        for side, tc in [("home", "home"), ("away", "away")]:
            sf = team_feats.copy()
            sf.columns = [f"{side}_{c}" if c not in ("date", "team") else c for c in sf.columns]
            sf.rename(columns={"date": "date", "team": tc}, inplace=True)
            match_df = pd.merge_asof(
                match_df.sort_values("date"), sf.sort_values("date"),
                by=tc, on="date", direction="backward")

        match_df["off_vs_def"] = match_df["home_gf_ewm5"] - match_df["away_ga_ewm5"]
        match_df["rest_diff"] = match_df["home_rest_days"] - match_df["away_rest_days"]
        match_df["midweek"] = (pd.to_datetime(match_df["date"]).dt.dayofweek >= 4).astype(int)
        match_df["away_travel_km"] = match_df.apply(lambda r: _team_distance(r["home"], r["away"]), axis=1)
        match_df["season_stage"] = pd.to_datetime(match_df["date"]).dt.month.map({
            8: 0, 9: 0, 10: 0, 11: 1, 12: 1, 1: 1, 2: 2, 3: 2, 4: 2, 5: 3,
        }).fillna(-1)

        if "home_sos_elo_5" in match_df.columns and "away_sos_elo_5" in match_df.columns:
            match_df["sos_elo_diff"] = match_df["home_sos_elo_5"] - match_df["away_sos_elo_5"]
        if "home_home_win_rate_5" in match_df.columns and "away_away_win_rate_5" in match_df.columns:
            match_df["home_away_win_diff"] = match_df["home_home_win_rate_5"] - match_df["away_away_win_rate_5"]

        if "home_win_streak" in match_df.columns and "home_avg_margin_5" in match_df.columns:
            match_df["home_momentum_quality"] = match_df["home_win_streak"] * match_df["home_avg_margin_5"].clip(-3, 3) / 3
            match_df["away_momentum_quality"] = match_df["away_win_streak"] * match_df["away_avg_margin_5"].clip(-3, 3) / 3
            match_df["momentum_quality_diff"] = match_df["home_momentum_quality"] - match_df["away_momentum_quality"]

        if "home_gf_volatility_5" in match_df.columns and "away_ga_volatility_5" in match_df.columns:
            match_df["margin_volatility_interaction"] = match_df["home_gf_volatility_5"] * match_df["away_ga_volatility_5"]

        # 累计行程 + 休息优势
        team_away = match_df[["date", "away", "away_travel_km"]].copy()
        team_away["cum_travel_3"] = team_away.groupby("away")["away_travel_km"].transform(
            lambda x: x.shift(1).rolling(3, min_periods=1).sum())
        match_df["away_cum_travel_3"] = pd.merge_asof(
            match_df.sort_values("date"), team_away.sort_values("date"),
            by="away", left_on="date", right_on="date", direction="backward")["cum_travel_3"].fillna(0)
        r_diff = match_df["rest_diff"].clip(-3, 3) / 3
        t_3 = match_df["away_cum_travel_3"].clip(0, 3000) / 3000
        match_df["rest_advantage"] = r_diff - t_3 + match_df["midweek"] * 0.3

        # 对手近期状态
        team_latest = team_feats.sort_values("date").groupby("team").last().reset_index()
        form_lookup = dict(zip(team_latest["team"], team_latest["points_5"]))
        match_df["home_opp_points_5"] = match_df["away"].map(form_lookup).fillna(0)
        match_df["away_opp_points_5"] = match_df["home"].map(form_lookup).fillna(0)

        for col in ["home_market_value", "away_market_value", "market_value_diff"]:
            if col in combined.columns:
                match_df[col] = combined[col].values

        # xG 预期进球特征
        try:
            from src.features.xg_pipeline import build_xg_features, merge_xg_into_match
            xg_df = build_xg_features(seasons=[2024, 2023])
            if not xg_df.empty:
                match_df = merge_xg_into_match(match_df, xg_df)
        except Exception as e:
            logger.warning("  ⚠️ xG特征构建失败: %s", e)

        # H2H 特征（简化版，仅基于 combined 数据）
        try:
            h2h_cache = {}
            for _, r in combined.sort_values("date").iterrows():
                key = tuple(sorted([r["home"], r["away"]]))
                if key not in h2h_cache:
                    h2h_cache[key] = []
                h2h_cache[key].append({
                    "home": r["home"], "away": r["away"],
                    "home_goals": r["home_goals"], "away_goals": r["away_goals"],
                    "date": r["date"],
                })
            h2h_rows = []
            for _, row in match_df.iterrows():
                h, a = row["home"], row["away"]
                prior = [m for m in h2h_cache.get(tuple(sorted([h, a])), [])
                         if isinstance(m["date"], pd.Timestamp) and m["date"] < row["date"]]
                last_5 = prior[-5:]
                if len(last_5) >= 2:
                    h_wins = sum(1 for m in last_5 if (m["home"] == h and m["home_goals"] > m["away_goals"]) or
                                 (m["away"] == h and m["away_goals"] > m["home_goals"]))
                    a_wins = sum(1 for m in last_5 if (m["home"] == a and m["home_goals"] > m["away_goals"]) or
                                 (m["away"] == a and m["away_goals"] > m["home_goals"]))
                    draws = len(last_5) - h_wins - a_wins
                    avg_total = sum(m["home_goals"] + m["away_goals"] for m in last_5) / len(last_5)
                else:
                    h_wins = a_wins = draws = 0
                    avg_total = 0.0
                h2h_rows.append({"h2h_home_wins": h_wins, "h2h_away_wins": a_wins,
                                 "h2h_draws": draws, "h2h_avg_total_goals": avg_total})
            if h2h_rows:
                h2h_df = pd.DataFrame(h2h_rows, index=match_df.index)
                match_df = pd.concat([match_df, h2h_df], axis=1)
                match_df["h2h_total"] = match_df["h2h_home_wins"] + match_df["h2h_away_wins"] + match_df["h2h_draws"]
                match_df["h2h_dominance"] = (match_df["h2h_home_wins"] - match_df["h2h_away_wins"]) / match_df["h2h_total"].clip(lower=1)
                if "home_home_win_rate_5" in match_df.columns:
                    match_df["h2h_form_x"] = match_df["h2h_dominance"] * match_df["home_home_win_rate_5"]
        except Exception as e:
            logger.warning("  ⚠️ H2H特征构建失败: %s", e)

        # 联赛宏观趋势特征
        if "competition" in match_df.columns:
            try:
                comp_df = combined.sort_values("date").copy()
                comp_df["_goals_total"] = comp_df["home_goals"] + comp_df["away_goals"]
                for col, src in [("league_home_win_rate", "win"), ("league_avg_home_goals", "home_goals"),
                                 ("league_avg_away_goals", "away_goals")]:
                    if src in comp_df.columns:
                        comp_df[col] = comp_df.groupby("competition")[src].transform(
                            lambda x: x.shift(1).expanding(min_periods=5).mean())
                comp_df["league_avg_goals"] = comp_df.groupby("competition")["_goals_total"].transform(
                    lambda x: x.shift(1).expanding(min_periods=5).mean())
                comp_df["league_home_adv"] = comp_df.groupby("competition")["_home_adv"].transform(
                    lambda x: x.shift(1).expanding(min_periods=5).mean()) if "_home_adv" in comp_df.columns else 0
                lc = ["date", "home", "away", "league_home_win_rate", "league_avg_goals",
                      "league_home_adv", "league_avg_home_goals", "league_avg_away_goals"]
                lc = [c for c in lc if c in comp_df.columns]
                match_df = match_df.merge(comp_df[lc], on=["date", "home", "away"], how="left")
            except Exception as e:
                logger.warning("  ⚠️ 联赛宏观特征构建失败: %s", e)

        match_df = match_df.ffill().fillna(0)

        n_hist = len(combined) - len(today_df)
        today_start = min(n_hist, len(match_df))
        X = match_df.iloc[today_start:].copy()
        for col in self.feat_cols:
            if col not in X.columns:
                X[col] = 0.0
        return X[self.feat_cols].fillna(0)

    def predict(self, odds_data: List[Dict]) -> List[Dict]:
        """对 Odds API 返回的比赛数据做完整预测。"""
        X = self._build_features(odds_data)
        market_map = extract_market_probs(odds_data)
        sharp_map = extract_sharp_market_probs(odds_data)  # sharp consensus 市场概率

        # 最优赔率公司查询
        from src.core.normalizer import find_best_odds, get_bookmaker_list

        results = []
        for i, game in enumerate(odds_data):
            if i >= len(X):
                break
            home = game["home_team"]
            away = game["away_team"]
            market = market_map.get(f"{home} @ {away}", {})

            # 查询最优赔率公司
            h2h_best_odds, h2h_bm, _ = find_best_odds(game, market_type='h2h')
            spread_best_odds, spread_bm, spread_pt = find_best_odds(game, market_type='spreads')
            total_best_odds, total_bm, total_pt = find_best_odds(game, market_type='totals')
            all_bookies = get_bookmaker_list(game)

            features = X[i:i+1]
            sharp_mkt = sharp_map.get(f"{home} @ {away}", {})
            has_sharp = not sharp_mkt.get("sharp_unavailable", True)
            spread_total_mkt = _extract_spread_total_market_probs(game)

            pred = {
                "home_team": home,
                "away_team": away,
                "sport_key": game.get("sport_key", ""),
                "commence_time": game.get("commence_time", ""),
                "market_home_prob": market.get("market_home_prob", 0.5),
                "market_away_prob": market.get("market_away_prob", 0.5),
                "market_draw_prob": market.get("market_draw_prob", 0.0),
                "sharp_home_prob": sharp_mkt.get("sharp_home_prob") if has_sharp else None,
                "sharp_away_prob": sharp_mkt.get("sharp_away_prob") if has_sharp else None,
                "sharp_draw_prob": sharp_mkt.get("sharp_draw_prob") if has_sharp else None,
                "sharp_available": has_sharp,
                "home_odds": market.get("home_odds", 0),
                "away_odds": market.get("away_odds", 0),
                "n_bookmakers": market.get("n_bookmakers", 0),
                "home_prob_std": market.get("home_prob_std", 0),
                "away_prob_std": market.get("away_prob_std", 0),
                "recommended_bookmaker": h2h_bm or "",
                "spread_bookmaker": spread_bm or "",
                "total_bookmaker": total_bm or "",
                "best_home_odds": h2h_best_odds,
                "spread_point": spread_pt,
                "total_point": total_pt,
                "spread_odds": spread_best_odds,
                "total_odds": total_best_odds,
                "all_bookmakers": all_bookies,
                "model_version": self.model_version,
            }

            for target in ["win", "spread_result", "total_result"]:
                # 统一使用校准后的集成模型（VotingClassifier + CalibratedClassifierCV）
                model = self.models.get(target)
                if model is not None:
                    try:
                        # 按目标过滤特征列（如 BB total_result 排除 ovrundr）
                        tf = features[self._get_feature_cols(target)]
                        prob = model.predict_proba(tf)[0, 1]
                    except Exception:
                        prob = 0.5
                else:
                    prob = 0.5

                # 裁剪原始概率到安全范围
                raw_prob = float(np.clip(prob, 0.02, 0.98))

                # 确定市场参考概率用于收缩
                if target == "win":
                    # 优先使用 sharp consensus 作为市场参考概率
                    if has_sharp and sharp_mkt.get("sharp_home_prob") is not None:
                        mkt_prob = sharp_mkt["sharp_home_prob"]
                    else:
                        mkt_prob = pred["market_home_prob"]
                elif target == "spread_result":
                    mkt_prob = spread_total_mkt.get("spread_home_prob", 0.5)
                else:
                    mkt_prob = spread_total_mkt.get("total_over_prob", 0.5)

                # 动态收缩：模型越不确定，越向市场回归
                shrunk = dynamic_shrinkage(raw_prob, mkt_prob)
                pred[f"{target}_prob"] = shrunk
                pred[f"{target}_raw"] = raw_prob  # 暴露原始概率（用于下游多结果概率计算）

                # EV 基于收缩后概率 vs 市场概率（使用 sharp consensus 时也用于 EV）
                ev = shrunk - mkt_prob
                pred[f"{target}_ev"] = float(ev)
                pred[f"{target}_edge"] = ev / mkt_prob if mkt_prob > 0 else 0.0

            results.append(pred)
        return results


def predict_sport(sport: str, odds_data: List[Dict]) -> List[Dict]:
    predictor = EnsemblePredictor(sport)
    return predictor.predict(odds_data)
