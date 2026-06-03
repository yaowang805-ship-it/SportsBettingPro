"""统一预测引擎 — 整合特征流水线 + 集成模型 + 市场赔率。

用法:
    from src.predict.ensemble_predictor import EnsemblePredictor
    predictor = EnsemblePredictor('bb')
    predictions = predictor.predict(odds_data)
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

import joblib
import numpy as np
import pandas as pd

from config.settings import MODEL_DIR, DATA_DIR
from src.core.calibration import dynamic_shrinkage

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


def _team_rolling_stats(df, goal_cols=("gf", "ga")):
    """从原始比赛记录构建球队级滚动统计。"""
    gf_col, ga_col = goal_cols
    home = df[["date", "home", gf_col, ga_col]].copy()
    home.columns = ["date", "team", "gf", "ga"]
    home["is_home"] = 1
    away = df[["date", "away", ga_col, gf_col]].copy()
    away.columns = ["date", "team", "gf", "ga"]
    away["is_home"] = 0
    team = pd.concat([home, away], ignore_index=True).sort_values(["team", "date"])

    for w in [3, 10]:
        team[f"gf_avg_{w}"] = team.groupby("team")["gf"].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f"ga_avg_{w}"] = team.groupby("team")["ga"].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f"net_rating_{w}"] = team[f"gf_avg_{w}"] - team[f"ga_avg_{w}"]

    team["gf_ewm5"] = team.groupby("team")["gf"].transform(
        lambda x: x.shift(1).ewm(span=5, adjust=False).mean())
    team["ga_ewm5"] = team.groupby("team")["ga"].transform(
        lambda x: x.shift(1).ewm(span=5, adjust=False).mean())
    team["opp_def_strength"] = team["ga_ewm5"]
    team["is_win"] = (team["gf"] > team["ga"]).astype(int)
    team["win_rate_10"] = team.groupby("team")["is_win"].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    team["rest_days"] = team.groupby("team")["date"].diff().dt.days.fillna(3)
    team["b2b"] = (team["rest_days"] == 1).astype(int)

    # 趋势斜率特征（动量）
    for w in [5, 10]:
        team[f"net_rating_slope_{w}"] = team.groupby("team")["net_rating_3"].transform(
            lambda x: x.shift(1).rolling(w, min_periods=2).apply(_slope, raw=True))

    feat_cols = ["date", "team", "gf_avg_3", "gf_avg_10",
                 "ga_avg_3", "ga_avg_10",
                 "net_rating_3", "net_rating_10",
                 "gf_ewm5", "ga_ewm5", "opp_def_strength", "win_rate_10", "rest_days", "b2b",
                 "net_rating_slope_5", "net_rating_slope_10"]
    return team[feat_cols]


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
        new["date"] = pd.to_datetime(new["date"], utc=True).dt.tz_localize(None)
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
    return df[["date", "home", "away", "home_goals", "away_goals"]]


# ── CLV 预测 Edge 特征注入 ────────────────────────────────

def _add_pred_edge_features(match_df, sport="football"):
    """注入球队级历史 Edge 特征（CLV 代理）。

    与训练流水线 bb_pipeline.py / football_pipeline.py 中的逻辑一致。
    """
    try:
        from src.monitor.clv_tracker import compute_team_edge_features
        from src.core.team_names import cn_to_feature_name

        edges = compute_team_edge_features(sport=sport)
        if edges:
            eng_edges = {}
            for cn, e in edges.items():
                en = cn_to_feature_name(cn, sport=sport)
                eng_edges[en] = e

            match_df["home_pred_edge"] = match_df["home"].str.lower().map(eng_edges).fillna(0)
            match_df["away_pred_edge"] = match_df["away"].str.lower().map(eng_edges).fillna(0)
            match_df["pred_edge_diff"] = match_df["home_pred_edge"] - match_df["away_pred_edge"]
        else:
            match_df["home_pred_edge"] = 0.0
            match_df["away_pred_edge"] = 0.0
            match_df["pred_edge_diff"] = 0.0
    except Exception:
        match_df["home_pred_edge"] = 0.0
        match_df["away_pred_edge"] = 0.0
        match_df["pred_edge_diff"] = 0.0
    return match_df


# ── 市场价值 / 伤病特征注入（足球） ──────────────────────────

def _add_fb_market_value_features(match_df):
    """向比赛 DataFrame 注入球队市值特征。"""
    from src.features.transfermarkt_client import get_team_market_value
    from src.features.football_pipeline import _TM_NAME_MAP, _load_tm_cache, _save_tm_cache

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
        self.prefix = "model_bb" if sport == "bb" else "model_fb"

        feat_file = MODEL_DIR_PATH / f"{self.prefix}_features.json"
        with open(feat_file) as f:
            self.feat_cols = json.load(f)
        logger.info("  加载特征列: %s 个", len(self.feat_cols))

        self.models = {}
        self._individual_models = {}   # {target: [(name, model, weight), ...]}
        for target in ["win", "spread_result", "total_result"]:
            self._try_load_weighted(target)
            if target not in self.models:
                path = MODEL_DIR_PATH / f"{self.prefix}_{target}_ensemble.pkl"
                if path.exists():
                    self.models[target] = joblib.load(path)
                    logger.info("  加载模型: %s", path.name)

    def _try_load_weighted(self, target: str):
        """尝试加载单模型+动态权重方案（优先级高于等权集成）。"""
        meta_path = MODEL_DIR_PATH / f"{self.prefix}_{target}_ensemble_meta.json"
        if not meta_path.exists():
            return
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            weights = meta.get("ensemble_weights", {})
            if not weights:
                return
            base_models = meta.get("base_models", [])
            loaded = []
            for name in base_models:
                w = weights.get(name)
                if w is None or w <= 0:
                    continue
                model_path = MODEL_DIR_PATH / f"{self.prefix}_{target}_{name}.pkl"
                if not model_path.exists():
                    continue
                model = joblib.load(model_path)
                loaded.append((name, model, w))
            if len(loaded) < 2:
                return
            self._individual_models[target] = loaded
            names_str = ", ".join(f"{n}({w:.3f})" for n, _, w in loaded)
            logger.info("  加载动态权重模型 [%s]: %s", target, names_str)
            # 不用在这个阶段设置 self.models[target]，在 predict 中用加权路径
        except Exception as e:
            logger.warning("  动态权重加载失败 (%s): %s", target, e)

    def _build_features(self, odds_data: List[Dict]) -> np.ndarray:
        """构建预测特征矩阵。"""
        if self.sport == "bb":
            return self._build_bb_features(odds_data)
        return self._build_fb_features(odds_data)

    def _build_bb_features(self, odds_data: List[Dict]) -> np.ndarray:
        """构建篮球预测特征。"""
        hist = _load_bb_history()

        today_rows = []
        for g in odds_data:
            today_rows.append({
                "date": pd.Timestamp.now(tz="UTC").tz_localize(None),
                "home": g["home_team"], "away": g["away_team"],
                "home_score": np.nan, "away_score": np.nan,
                "home_goals": np.nan, "away_goals": np.nan,
            })
        today_df = pd.DataFrame(today_rows)

        combined = pd.concat([hist, today_df], ignore_index=True).sort_values("date").reset_index(drop=True)

        # ELO 评级
        from src.features.elo import compute_elo
        combined = compute_elo(combined, K=20)

        team_feats = _team_rolling_stats(combined, goal_cols=("home_goals", "away_goals"))

        match_df = combined[["date", "home", "away", "home_elo", "away_elo", "elo_diff"]].copy()
        for side, tc in [("home", "home"), ("away", "away")]:
            sf = team_feats.copy()
            sf.columns = [f"{side}_{c}" if c not in ("date", "team") else c for c in sf.columns]
            sf.rename(columns={"date": "date", "team": tc}, inplace=True)
            match_df = pd.merge_asof(
                match_df.sort_values("date"), sf.sort_values("date"),
                by=tc, on="date", direction="backward")

        match_df["off_vs_def"] = match_df["home_gf_ewm5"] - match_df["away_ga_ewm5"]
        match_df["b2b_diff"] = match_df["home_b2b"] - match_df["away_b2b"]
        match_df["rest_diff"] = match_df["home_rest_days"] - match_df["away_rest_days"]

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
                # 球队全名 → 伤病权重映射
                def _injured_score(team_name):
                    # Try direct team name lookup first
                    team_clean = team_name.strip()
                    abbr = TEAM_ABBR.get(team_clean, '')
                    if not abbr:
                        # Fallback: reverse lookup from TEAM_ABBR
                        rev = {v.lower(): k for k, v in TEAM_ABBR.items()}
                        full = rev.get(team_clean.lower(), '')
                        abbr = TEAM_ABBR.get(full, '')
                    return team_scores.get(abbr, 0.0)
                match_df['home_injured'] = match_df['home'].map(_injured_score).fillna(0.0)
                match_df['away_injured'] = match_df['away'].map(_injured_score).fillna(0.0)
                match_df['injured_diff'] = match_df['home_injured'] - match_df['away_injured']
                # 二值特征：有无关键伤病
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

        # ── CLV Edge 特征 ──
        _add_pred_edge_features(match_df, sport="nba")

        match_df = match_df.ffill().fillna(0)

        # 注入天气与行程特征
        try:
            from src.features.weather_features import add_weather_to_df
            match_df = add_weather_to_df(match_df)
        except Exception:
            pass

        today_start = len(hist)
        X = match_df.iloc[today_start:]
        avail = [c for c in self.feat_cols if c in X.columns]
        return X[avail].fillna(0).values

    def _build_fb_features(self, odds_data: List[Dict]) -> np.ndarray:
        """构建足球预测特征。"""
        hist = _load_fb_history()
        # 注入市值特征
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

        # ELO 评级
        from src.features.elo import compute_elo
        combined = compute_elo(combined, K=30)

        team_feats = _team_rolling_stats(combined, goal_cols=("home_goals", "away_goals"))

        # 足球额外特征：市值
        combined = _add_fb_market_value_features(combined)

        match_df = combined[["date", "home", "away", "home_elo", "away_elo", "elo_diff"]].copy()
        for side, tc in [("home", "home"), ("away", "away")]:
            sf = team_feats.copy()
            sf.columns = [f"{side}_{c}" if c not in ("date", "team") else c for c in sf.columns]
            sf.rename(columns={"date": "date", "team": tc}, inplace=True)
            match_df = pd.merge_asof(
                match_df.sort_values("date"), sf.sort_values("date"),
                by=tc, on="date", direction="backward")

        match_df["off_vs_def"] = match_df["home_gf_ewm5"] - match_df["away_ga_ewm5"]
        match_df["b2b_diff"] = match_df["home_b2b"] - match_df["away_b2b"]
        match_df["rest_diff"] = match_df["home_rest_days"] - match_df["away_rest_days"]

        # 市值特征
        for col in ["home_market_value", "away_market_value", "market_value_diff"]:
            if col in combined.columns:
                match_df[col] = combined[col].values

        # ── xG 预期进球特征 ──
        try:
            from src.features.xg_pipeline import build_xg_features, merge_xg_into_match
            xg_df = build_xg_features(seasons=[2024, 2023])
            if not xg_df.empty:
                match_df = merge_xg_into_match(match_df, xg_df)
        except Exception as e:
            logger.warning("  ⚠️ xG特征构建失败: %s", e)

        # ── CLV Edge 特征 ──
        _add_pred_edge_features(match_df, sport="football")

        match_df = match_df.ffill().fillna(0)

        # 注入天气与行程特征
        try:
            from src.features.weather_features import add_weather_to_df
            match_df = add_weather_to_df(match_df)
        except Exception:
            pass

        today_start = len(hist)
        X = match_df.iloc[today_start:]
        avail = [c for c in self.feat_cols if c in X.columns]
        return X[avail].fillna(0).values

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
            }

            for target in ["win", "spread_result", "total_result"]:
                ind_models = self._individual_models.get(target)
                if ind_models:
                    # 动态权重路径：加权各子模型预测
                    try:
                        probs = []
                        weights = []
                        for _name, model, weight in ind_models:
                            p = model.predict_proba(features)[0, 1]
                            probs.append(p)
                            weights.append(weight)
                        prob = float(np.average(probs, weights=weights))
                    except Exception:
                        prob = 0.5
                else:
                    # 标准集成路径（等权 + 校准）
                    model = self.models.get(target)
                    if model is not None:
                        try:
                            prob = model.predict_proba(features)[0, 1]
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
                else:
                    mkt_prob = 0.5  # 让分/大小球市场近似 50/50

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
