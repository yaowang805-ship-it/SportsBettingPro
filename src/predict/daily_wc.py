#!/usr/bin/env python3
"""世界杯每日推荐 — WC 集成模型 + the-odds-api 赔率。"""
import sys
import json
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging, get_logger
setup_logging()
logger = get_logger(__name__)

import joblib
import numpy as np
import pandas as pd

from config.settings import MODEL_DIR, DATA_DIR
# 确保反序列化时可找到自定义类

# ── 48 支世界杯参赛队（Odds API 名称） ──
WC_TEAMS_ODDS = [
    "Algeria", "Argentina", "Australia", "Austria", "Belgium", "Bosnia & Herzegovina",
    "Brazil", "Canada", "Cape Verde", "Colombia", "Croatia", "Curaçao",
    "Czech Republic", "DR Congo", "Ecuador", "Egypt", "England", "France",
    "Germany", "Ghana", "Haiti", "Iran", "Iraq", "Ivory Coast", "Japan", "Jordan",
    "Mexico", "Morocco", "Netherlands", "New Zealand", "Norway", "Panama",
    "Paraguay", "Portugal", "Qatar", "Saudi Arabia", "Scotland", "Senegal",
    "South Africa", "South Korea", "Spain", "Sweden", "Switzerland", "Tunisia",
    "Turkey", "USA", "Uruguay", "Uzbekistan",
]

# Odds API → results.csv 队名映射
WC_TEAM_MAP = {
    "USA": "United States",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Curaçao": "Curacao",
}

# 特征列（与 wc_pipeline.py / model_wc_features.json 一致）
FEAT_COLS = [
    'home_elo', 'away_elo', 'elo_diff',
    'home_gf_avg_3', 'home_gf_avg_5', 'home_gf_avg_10',
    'home_ga_avg_3', 'home_ga_avg_5', 'home_ga_avg_10',
    'home_win_rate_5', 'home_win_rate_10', 'home_draw_rate_5', 'home_draw_rate_10',
    'home_rest_days', 'home_net_5', 'home_opp_elo',
    'home_margin_5', 'home_margin_10', 'home_margin_volatility_5',
    'home_gf_volatility_5', 'home_scoring_streak', 'home_last_game_won',
    'home_form_trend_3_10', 'home_sos_elo_5',
    'away_gf_avg_3', 'away_gf_avg_5', 'away_gf_avg_10',
    'away_ga_avg_3', 'away_ga_avg_5', 'away_ga_avg_10',
    'away_win_rate_5', 'away_win_rate_10', 'away_draw_rate_5', 'away_draw_rate_10',
    'away_rest_days', 'away_net_5', 'away_opp_elo',
    'away_margin_5', 'away_margin_10', 'away_margin_volatility_5',
    'away_gf_volatility_5', 'away_scoring_streak', 'away_last_game_won',
    'away_form_trend_3_10', 'away_sos_elo_5',
    'form_diff_5', 'net_5_diff', 'rest_diff', 'gf_avg_5_diff', 'ga_avg_5_diff',
    'total_avg_5', 'opp_elo_diff',
    'margin_diff_5', 'sos_elo_diff', 'gf_vol_diff', 'total_avg_10',
    'attack_vs_defence', 'defence_vs_attack', 'is_neutral',
]

MODEL_DIR_PATH = Path(MODEL_DIR) if isinstance(MODEL_DIR, str) else MODEL_DIR
DATA_DIR_PATH = Path(DATA_DIR) if isinstance(DATA_DIR, str) else DATA_DIR

# WC 专属保守兜底
WC_EV_THRESHOLD = 0.05     # EV > 5% 才推荐
WC_PROB_CAP = 0.15         # 模型概率不超过市场 + 15pp


def _odds_to_csv(name: str) -> str:
    """Odds API 名称 → results.csv 名称。"""
    return WC_TEAM_MAP.get(name, name)


def _load_models():
    """加载两个训练好的 WC 集成模型。"""
    models = {}
    for target in ['home_win', 'over_2.5']:
        path = MODEL_DIR_PATH / f"model_wc_{target}_ensemble.pkl"
        if path.exists():
            models[target] = joblib.load(path)
            logger.info("  加载模型: %s", path.name)
        else:
            logger.warning("  ⚠️ 模型不存在: %s", path)
    return models


def _build_feature_matrix(odds_data: list) -> tuple:
    """构建世界杯预测特征矩阵。

    从 results.csv 重新计算当前 ELO + 滚动特征，
    确保特征值与训练时一致且反映球队最新状态。

    Returns:
        (X, match_keys): 特征矩阵 + 比赛标识列表
    """
    # 加载原始数据
    df = pd.read_csv(ROOT / 'data/storage/results.csv')
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date', 'home_score', 'away_score']).copy()

    # 归一化队名
    df['home_team'] = df['home_team'].str.normalize('NFKD').str.encode('ascii', errors='ignore').str.decode('utf-8')
    df['away_team'] = df['away_team'].str.normalize('NFKD').str.encode('ascii', errors='ignore').str.decode('utf-8')
    _RM = {v: k for k, v in WC_TEAM_MAP.items()}
    for side in ['home_team', 'away_team']:
        df[side] = df[side].map(_RM).fillna(df[side])
        df[side] = df[side].map(WC_TEAM_MAP).fillna(df[side])

    # 只保留涉及世界杯参赛队的比赛
    csv_names = [_odds_to_csv(t) for t in WC_TEAMS_ODDS]
    df = df[df['home_team'].isin(csv_names) | df['away_team'].isin(csv_names)].copy()

    # 8年回溯
    cutoff = pd.Timestamp.now() - pd.DateOffset(years=8)
    df = df[df['date'] >= cutoff].copy()
    df = df.sort_values('date').reset_index(drop=True)

    # ── ELO 时序计算 ──
    def _tournament_weight(tournament):
        t = str(tournament).lower()
        if 'world cup' in t and 'qualification' not in t:
            return 3.0
        if 'world cup qualification' in t:
            return 2.0
        if any(x in t for x in ('euro', 'copa am', 'african cup', 'asian cup', 'gold cup')):
            return 2.0
        if 'nations league' in t:
            return 1.5
        if 'friendly' in t:
            return 0.5
        return 1.0

    elo_ratings = {}
    for _, row in df.iterrows():
        home, away = row['home_team'], row['away_team']
        home_elo = elo_ratings.get(home, 1500.0)
        away_elo = elo_ratings.get(away, 1500.0)
        hg, ag = float(row['home_score']), float(row['away_score'])
        K = _tournament_weight(row.get('tournament', 'Friendly'))
        margin = abs(hg - ag)
        m = min(margin, 5) ** 0.5
        K_adjusted = K * 20 * m  # 标准国际足球 K≈20-30
        if hg > ag:
            hr, ar = 1.0, 0.0
        elif hg == ag:
            hr, ar = 0.5, 0.5
        else:
            hr, ar = 0.0, 1.0

        home_exp = 1.0 / (1.0 + 10.0 ** ((away_elo - home_elo) / 400.0))
        away_exp = 1.0 / (1.0 + 10.0 ** ((home_elo - away_elo) / 400.0))

        elo_ratings[home] = home_elo + K_adjusted * (hr - home_exp)
        elo_ratings[away] = away_elo + K_adjusted * (ar - away_exp)

    current_elo = dict(elo_ratings)
    logger.info("  ELO 计算完成: %d 支球队", len(current_elo))

    # ── 球队展开视图 ──
    home = df[['date', 'home_team', 'away_team', 'home_score', 'away_score']].copy()
    home.columns = ['date', 'team', 'opponent', 'gf', 'ga']
    home['is_home'] = 1
    away = df[['date', 'away_team', 'home_team', 'away_score', 'home_score']].copy()
    away.columns = ['date', 'team', 'opponent', 'gf', 'ga']
    away['is_home'] = 0
    team = pd.concat([home, away], ignore_index=True).sort_values(['team', 'date'])

    # 对手 ELO（匹配训练时的 opp_elo 特征，基于比赛时点的 ELO）
    elo_lookup = {}
    for _, row in df.iterrows():
        h, a = row['home_team'], row['away_team']
        h_elo = elo_ratings.get(h, 1500.0)
        a_elo = elo_ratings.get(a, 1500.0)
        elo_lookup[(row['date'], h, a)] = (h_elo, a_elo)

    def _opp_elo_for_team(team_df):
        vals = []
        for _, r in team_df.iterrows():
            # 训练流水线: 主队 key=(date,team,opponent), 客队 key=(date,opponent,team)
            key = (r['date'], r['team'], r['opponent']) if r['is_home'] else (r['date'], r['opponent'], r['team'])
            elos = elo_lookup.get(key)
            opp_elo = elos[1] if r['is_home'] else elos[0] if elos else 1500.0
            vals.append(opp_elo)
        return pd.Series(vals, index=team_df.index)

    team['opp_elo'] = team.groupby('team', group_keys=False).apply(_opp_elo_for_team)

    for w in [3, 5, 10]:
        team[f'gf_avg_{w}'] = team.groupby('team')['gf'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'ga_avg_{w}'] = team.groupby('team')['ga'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())

    team['is_win'] = (team['gf'] > team['ga']).astype(int)
    team['is_draw'] = (team['gf'] == team['ga']).astype(int)
    for w in [5, 10]:
        team[f'win_rate_{w}'] = team.groupby('team')['is_win'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'draw_rate_{w}'] = team.groupby('team')['is_draw'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())

    team['rest_days'] = team.groupby('team')['date'].diff().dt.days.fillna(14)
    team['net_5'] = team.groupby('team')['gf'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean()
    ) - team.groupby('team')['ga'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean())

    # ── 增强特征（margin, volatility, streak） ──
    team['margin'] = team['gf'] - team['ga']
    for w in [5, 10]:
        team[f'margin_{w}'] = team.groupby('team')['margin'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
    team['margin_volatility_5'] = team.groupby('team')['margin'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).std()).fillna(0)
    team['gf_volatility_5'] = team.groupby('team')['gf'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).std()).fillna(0)
    team['scoring_streak'] = team.groupby('team')['gf'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1)
        .apply(lambda s: (s > 0).astype(int).sum(), raw=True))
    team['last_game_won'] = team.groupby('team')['is_win'].transform(
        lambda x: x.shift(1).rolling(1).max())
    team['win_rate_3'] = team.groupby('team')['is_win'].transform(
        lambda x: x.shift(1).rolling(3, min_periods=1).mean())
    team['form_trend_3_10'] = team['win_rate_3'].fillna(0.5) - team['win_rate_10'].fillna(0.5)
    # 赛程强度
    for w in [5, 10]:
        team[f'sos_elo_{w}'] = team.groupby('team')['opp_elo'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean()).fillna(1500)

    # 取每支球队的最新特征
    team_latest = {}
    for team_odds in WC_TEAMS_ODDS:
        team_csv = _odds_to_csv(team_odds)
        rows = team[team['team'] == team_csv]
        if len(rows) == 0:
            team_latest[team_odds] = {}
            continue
        last = rows.iloc[-1]
        feat = {c: last[c] if pd.notna(last[c]) else 0.0 for c in
                ['gf_avg_3', 'gf_avg_5', 'gf_avg_10',
                 'ga_avg_3', 'ga_avg_5', 'ga_avg_10',
                 'win_rate_5', 'win_rate_10', 'draw_rate_5', 'draw_rate_10',
                 'rest_days', 'net_5', 'opp_elo',
                 'margin_5', 'margin_10', 'margin_volatility_5',
                 'gf_volatility_5', 'scoring_streak', 'last_game_won',
                 'form_trend_3_10', 'sos_elo_5']}
        team_latest[team_odds] = feat

    # ── 构建特征向量 ──
    rows_list = []
    match_keys = []
    for g in odds_data:
        home, away = g['home_team'], g['away_team']
        h_csv, a_csv = _odds_to_csv(home), _odds_to_csv(away)
        hf = team_latest.get(home, {})
        af = team_latest.get(away, {})
        home_elo_val = current_elo.get(h_csv, 1500.0)
        away_elo_val = current_elo.get(a_csv, 1500.0)

        def _v(d, key, default=0.0):
            v = d.get(key, default)
            return v if pd.notna(v) else default

        row = {
            'home_elo': home_elo_val,
            'away_elo': away_elo_val,
            'elo_diff': home_elo_val - away_elo_val,
            'home_gf_avg_3': _v(hf, 'gf_avg_3', 1),
            'home_gf_avg_5': _v(hf, 'gf_avg_5', 1),
            'home_gf_avg_10': _v(hf, 'gf_avg_10', 1),
            'home_ga_avg_3': _v(hf, 'ga_avg_3', 1),
            'home_ga_avg_5': _v(hf, 'ga_avg_5', 1),
            'home_ga_avg_10': _v(hf, 'ga_avg_10', 1),
            'home_win_rate_5': _v(hf, 'win_rate_5', 0.5),
            'home_win_rate_10': _v(hf, 'win_rate_10', 0.5),
            'home_draw_rate_5': _v(hf, 'draw_rate_5', 0.25),
            'home_draw_rate_10': _v(hf, 'draw_rate_10', 0.25),
            'home_rest_days': _v(hf, 'rest_days', 7),
            'home_net_5': _v(hf, 'net_5', 0),
            'home_opp_elo': _v(hf, 'opp_elo', 1500),
            'home_margin_5': _v(hf, 'margin_5', 0),
            'home_margin_10': _v(hf, 'margin_10', 0),
            'home_margin_volatility_5': _v(hf, 'margin_volatility_5', 0),
            'home_gf_volatility_5': _v(hf, 'gf_volatility_5', 0),
            'home_scoring_streak': _v(hf, 'scoring_streak', 0),
            'home_last_game_won': _v(hf, 'last_game_won', 0),
            'home_form_trend_3_10': _v(hf, 'form_trend_3_10', 0),
            'home_sos_elo_5': _v(hf, 'sos_elo_5', 1500),
            'away_gf_avg_3': _v(af, 'gf_avg_3', 1),
            'away_gf_avg_5': _v(af, 'gf_avg_5', 1),
            'away_gf_avg_10': _v(af, 'gf_avg_10', 1),
            'away_ga_avg_3': _v(af, 'ga_avg_3', 1),
            'away_ga_avg_5': _v(af, 'ga_avg_5', 1),
            'away_ga_avg_10': _v(af, 'ga_avg_10', 1),
            'away_win_rate_5': _v(af, 'win_rate_5', 0.5),
            'away_win_rate_10': _v(af, 'win_rate_10', 0.5),
            'away_draw_rate_5': _v(af, 'draw_rate_5', 0.25),
            'away_draw_rate_10': _v(af, 'draw_rate_10', 0.25),
            'away_rest_days': _v(af, 'rest_days', 7),
            'away_net_5': _v(af, 'net_5', 0),
            'away_opp_elo': _v(af, 'opp_elo', 1500),
            'away_margin_5': _v(af, 'margin_5', 0),
            'away_margin_10': _v(af, 'margin_10', 0),
            'away_margin_volatility_5': _v(af, 'margin_volatility_5', 0),
            'away_gf_volatility_5': _v(af, 'gf_volatility_5', 0),
            'away_scoring_streak': _v(af, 'scoring_streak', 0),
            'away_last_game_won': _v(af, 'last_game_won', 0),
            'away_form_trend_3_10': _v(af, 'form_trend_3_10', 0),
            'away_sos_elo_5': _v(af, 'sos_elo_5', 1500),
        }
        # 交互特征
        row['form_diff_5'] = row['home_win_rate_5'] - row['away_win_rate_5']
        row['net_5_diff'] = row['home_net_5'] - row['away_net_5']
        row['rest_diff'] = row['home_rest_days'] - row['away_rest_days']
        row['gf_avg_5_diff'] = row['home_gf_avg_5'] - row['away_gf_avg_5']
        row['ga_avg_5_diff'] = row['home_ga_avg_5'] - row['away_ga_avg_5']
        row['total_avg_5'] = row['home_gf_avg_5'] + row['away_gf_avg_5']
        row['opp_elo_diff'] = row['home_opp_elo'] - row['away_opp_elo']
        row['margin_diff_5'] = row['home_margin_5'] - row['away_margin_5']
        row['sos_elo_diff'] = row['home_sos_elo_5'] - row['away_sos_elo_5']
        row['gf_vol_diff'] = row['home_gf_volatility_5'] - row['away_gf_volatility_5']
        row['total_avg_10'] = row['home_gf_avg_10'] + row['away_gf_avg_10']
        row['attack_vs_defence'] = row['home_gf_avg_5'] * row['away_ga_avg_5']
        row['defence_vs_attack'] = row['home_ga_avg_5'] * row['away_gf_avg_5']
        row['is_neutral'] = 1

        rows_list.append(row)
        match_keys.append(f"{home} @ {away}")

    X = pd.DataFrame(rows_list, columns=FEAT_COLS)
    return X, match_keys


def _extract_market_probs(odds_data: list) -> dict:
    """提取 H2H 和大小球市场概率（含平局去抽水）。"""
    results = {}
    for game in odds_data:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        bk = game.get("bookmakers", [])
        key = f"{home} @ {away}"

        home_prices, draw_prices, away_prices = [], [], []
        total_by_point = {}  # {point: [(over_price, under_price), ...]}

        for bm in bk:
            for mkt in bm.get("markets", []):
                if mkt.get("key") == "h2h":
                    oss = mkt.get("outcomes", [])
                    hp = next((o["price"] for o in oss if o.get("name", "").strip().lower() == home.lower()), None)
                    dp = next((o["price"] for o in oss if o.get("name", "").strip().lower() == "draw"), None)
                    ap = next((o["price"] for o in oss if o.get("name", "").strip().lower() == away.lower()), None)
                    if hp and dp and ap:
                        home_prices.append(hp)
                        draw_prices.append(dp)
                        away_prices.append(ap)
                elif mkt.get("key") == "totals":
                    oss = mkt.get("outcomes", [])
                    ov = next((o["price"] for o in oss if o.get("name") == "Over"), None)
                    un = next((o["price"] for o in oss if o.get("name") == "Under"), None)
                    pt = oss[0].get("point", 2.5) if oss else 2.5
                    if ov and un:
                        total_by_point.setdefault(pt, []).append((ov, un))

        if not home_prices:
            continue

        n = len(home_prices)
        home_probs, away_probs = [], []
        for i in range(n):
            imp_h, imp_d, imp_a = 1.0 / home_prices[i], 1.0 / draw_prices[i], 1.0 / away_prices[i]
            tot = imp_h + imp_d + imp_a
            home_probs.append(imp_h / tot)
            away_probs.append(imp_a / tot)

        info = {
            "market_home_prob": float(np.mean(home_probs)),
            "market_away_prob": float(np.mean(away_probs)),
            "home_odds": max(home_prices),
            "away_odds": max(away_prices),
            "n_bookmakers": n,
        }

        if total_by_point:
            # 取最常出现的盘口值（众数），避免不同盘口值混在一起取均值
            best_pt = max(total_by_point, key=lambda pt: len(total_by_point[pt]))
            pairs = total_by_point[best_pt]
            over_probs = []
            all_over_prices = []
            for ov, un in pairs:
                imp_o, imp_u = 1.0 / ov, 1.0 / un
                over_probs.append(imp_o / (imp_o + imp_u))
                all_over_prices.append(ov)
            info["total_over_prob"] = float(np.mean(over_probs))
            info["total_odds"] = max(all_over_prices)
            info["total_point"] = float(best_pt)

        results[key] = info
    return results


def _cn(name: str) -> str:
    from src.core.team_names import WC_CN
    return WC_CN.get(name, name)


def main():
    logger.info("=" * 60)
    logger.info("🏆 世界杯每日预测 - %s", datetime.now().strftime('%Y-%m-%d %H:%M'))

    # 加载模型
    models = _load_models()
    if not models:
        logger.error("❌ 无可用模型，退出")
        sys.exit(1)

    # 拉取 WC 赔率
    from fetchers.odds_api import fetch_odds_api
    try:
        odds_data = fetch_odds_api('soccer_fifa_world_cup', force=True, markets='h2h,totals')
        logger.info("✅ 世界杯赔率: %d 场", len(odds_data))
    except Exception as e:
        logger.error("❌ 赔率拉取失败: %s", e)
        sys.exit(1)

    if not odds_data:
        logger.warning("⚠️ 无世界杯赔率数据")
        sys.exit(0)

    # 预测特征
    X, match_keys = _build_feature_matrix(odds_data)
    logger.info("✅ 特征构建: %d 场", len(X))

    # 市场概率
    market_map = _extract_market_probs(odds_data)

    # 逐场预测
    from src.risk.manager import RiskManager
    from src.predict.ev_verification import log_prediction
    from src.notify.dingtalk import get_notifier
    from src.notify.formatter import Recommendation, MarketType, RecommendationFormatter
    from src.dashboard.components.virtual_portfolio import auto_place_bets
    from src.monitor.clv_tracker import capture_opening_odds

    rm = RiskManager()
    recs = []
    current_exposure = 0.0

    for i, game in enumerate(odds_data):
        if i >= len(X):
            break
        home = game["home_team"]
        away = game["away_team"]
        mk = f"{home} @ {away}"
        market = market_map.get(mk, {})
        if not market:
            continue

        features = X.iloc[i:i+1]
        home_odds = market.get("home_odds", 0)
        mkt_home = market.get("market_home_prob", 0.5)
        mkt_over = market.get("total_over_prob", 0.5)
        total_odds = market.get("total_odds", 0)
        total_pt = market.get("total_point", 2.5)

        # 模型预测
        candidates = []

        # ── 主胜预测 ──
        if "home_win" in models:
            try:
                win_prob = models["home_win"].predict_proba(features)[0, 1]
                win_prob = float(np.clip(win_prob, 0.02, 0.98))
                # WC 保守兜底：模型概率不显著高于市场
                win_prob = min(win_prob, mkt_home + WC_PROB_CAP)
                win_ev = win_prob - mkt_home

                if win_ev > WC_EV_THRESHOLD and home_odds > 0 and market.get("n_bookmakers", 0) >= 3:
                    kelly = (win_prob * home_odds - 1) / (home_odds - 1) if home_odds > 1 else 0
                    stake = rm.get_max_stake(win_prob, home_odds, current_exposure, input_is_prob=True) if kelly > 0 else 0
                    can_bet, _ = rm.can_place_bet(stake, 0.0)
                    if stake > 5 and can_bet:
                        candidates.append({
                            "type": "主胜", "odds": home_odds,
                            "model_prob": win_prob, "mkt_prob": mkt_home,
                            "ev": win_ev, "stake": stake,
                        })
            except Exception as e:
                logger.debug("  ⚠️ %s 主胜预测失败: %s", mk, e)

        # ── 大小球预测 ──
        if "over_2.5" in models and total_odds > 0:
            try:
                over_prob = models["over_2.5"].predict_proba(features)[0, 1]
                over_prob = float(np.clip(over_prob, 0.02, 0.98))
                # WC 保守兜底
                over_prob = min(over_prob, mkt_over + WC_PROB_CAP)
                over_ev = over_prob - mkt_over

                if over_ev > WC_EV_THRESHOLD:
                    kelly = (over_prob * total_odds - 1) / (total_odds - 1) if total_odds > 1 else 0
                    stake = rm.get_max_stake(over_prob, total_odds, current_exposure, input_is_prob=True) if kelly > 0 else 0
                    can_bet, _ = rm.can_place_bet(stake, 0.0)
                    if stake > 5 and can_bet:
                        candidates.append({
                            "type": f"大 {total_pt:.1f}", "odds": total_odds,
                            "model_prob": over_prob, "mkt_prob": mkt_over,
                            "ev": over_ev, "stake": stake,
                        })

                # 小分
                under_prob = 1.0 - over_prob
                under_mkt = 1.0 - mkt_over
                under_prob = min(under_prob, under_mkt + WC_PROB_CAP)
                under_ev = under_prob - under_mkt
                if under_ev > WC_EV_THRESHOLD:
                    under_odds = 1.0 / under_mkt if under_mkt > 0 else total_odds
                    kelly = (under_prob * under_odds - 1) / (under_odds - 1) if under_odds > 1 else 0
                    stake = rm.get_max_stake(under_prob, under_odds, current_exposure, input_is_prob=True) if kelly > 0 else 0
                    if stake > 5:
                        candidates.append({
                            "type": f"小 {total_pt:.1f}", "odds": round(under_odds, 2),
                            "model_prob": under_prob, "mkt_prob": under_mkt,
                            "ev": under_ev, "stake": stake,
                        })
            except Exception as e:
                logger.debug("  ⚠️ %s 大小球预测失败: %s", mk, e)

        if not candidates:
            continue

        best = max(candidates, key=lambda x: x["ev"])
        match_time = pd.to_datetime(game["commence_time"]).tz_convert("Asia/Shanghai")
        time_str = match_time.strftime("%m/%d %H:%M")
        logger.info("🏆 %s vs %s | %s | 模型:%s 市场:%s 赔率:%.2f EV:%s 注额:%.0f ⏰%s",
                    _cn(home), _cn(away), best['type'],
                    "{:.1%}".format(best['model_prob']),
                    "{:.1%}".format(best['mkt_prob']),
                    best['odds'], "{:+.1%}".format(best['ev']),
                    best['stake'], time_str)

        rec_entry = {**game, **best, "time_str": time_str,
                     "home_cn": _cn(home), "away_cn": _cn(away),
                     "sport": "world_cup", "league": "世界杯",
                     "market": best["type"]}
        recs.append(rec_entry)

        log_prediction(
            sport="world_cup", league="soccer_fifa_world_cup",
            home_team=home, away_team=away,
            market_type="胜负" if "胜" in best["type"] else "大小球",
            market_detail=best["type"],
            odds=best["odds"], model_prob=best["model_prob"],
            market_prob=best["mkt_prob"], ev=best["ev"],
            stake=best["stake"], match_time=match_time,
            source="daily_wc", home_team_cn=_cn(home), away_team_cn=_cn(away),
        )
        current_exposure += best["stake"] / max(rm.current_balance, 1.0)

    # 联合凯利组合优化（替代逐个调 get_max_stake 的启发式分散调整）
    if recs:
        recs = rm.batch_optimize(recs)

    # 钉钉通知
    notifier = get_notifier()
    if not recs:
        logger.warning("⚠️ 今日无世界杯推荐")
        msg = notifier.build_markdown_message(
            "【投注推荐】无世界杯推荐",
            "✅ 已完成世界杯推荐分析\n\n⚠️ 今日未检测到符合策略的正期望值投注。"
        )
        notifier.send(msg, "无世界杯推荐通知")
    else:
        rec_objs = []
        for r in recs:
            rec_objs.append(Recommendation(
                sport="世界杯", league="世界杯",
                home_team=_cn(r["home_team"]), away_team=_cn(r["away_team"]),
                market_type=MarketType.H2H if "主胜" in r["type"] else MarketType.TOTAL,
                market_detail=r["type"],
                odds=r["odds"], model_prob=r["model_prob"],
                market_prob=r["mkt_prob"], ev=r["ev"],
                stake=r["stake"],
                match_time=pd.to_datetime(r["commence_time"]),
                bookmaker=r.get("recommended_bookmaker", ""),
            ))
        formatter = RecommendationFormatter()
        msg_text = formatter.format_recommendations_for_dingtalk(rec_objs, title="世界杯 每日推荐", sport_name="世界杯")
        msg = notifier.build_markdown_message("【投注推荐】🏆 世界杯 推荐", msg_text)
        notifier.send(msg, f"{len(rec_objs)}条世界杯推荐")

    # 保存推荐记录
    output = {
        "date": datetime.now().isoformat(),
        "sport": "world_cup",
        "total_games": len(odds_data),
        "recommendations": [
            {k: r[k] for k in ("home_team", "away_team", "type", "model_prob", "mkt_prob", "odds", "ev", "stake", "commence_time")}
            for r in recs
        ],
    }
    out_path = DATA_DIR_PATH / "daily_wc_recommendations.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    logger.info("✅ 推荐已保存至 %s", out_path)

    # 同步到虚拟投注组合
    auto_place_bets(recs)
    logger.info("✅ 已同步 %d 条推荐到虚拟投注组合", len(recs))

    # 记录开盘价（用于 CLV 追踪）
    for r in recs:
        match_key = f"{r['home_team']} @ {r['away_team']} {pd.to_datetime(r['commence_time']).strftime('%Y-%m-%d')}"
        market_key = "h2h" if "主胜" in r.get("type", "") else "totals"
        capture_opening_odds(match_key, market_key, r["odds"], r.get("recommended_bookmaker", ""), "世界杯")


if __name__ == "__main__":
    main()
