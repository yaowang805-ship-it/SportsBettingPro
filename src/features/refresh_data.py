"""从 ESPN 刷新历史比赛数据，保持训练数据新鲜。

专业博彩系统每日更新历史数据，确保模型基于最新比赛训练。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from datetime import datetime, timezone

from config.logging_config import get_logger
logger = get_logger(__name__)


def refresh_basketball_history(days_back: int = 14):
    """从 ESPN 拉取近期 NBA 比分，追加到 basketball_history.csv。"""
    from src.fetchers.espn_scores import fetch_espn_scores

    csv_path = ROOT / "data" / "storage" / "basketball_history.csv"
    if not csv_path.exists():
        logger.warning("basketball_history.csv 不存在，跳过")
        return

    existing = pd.read_csv(csv_path)
    existing.columns = [c.strip().lower() for c in existing.columns]
    existing['date'] = pd.to_datetime(existing['date'], utc=True, format='mixed')
    max_date = existing['date'].max()
    logger.info("当前篮球数据最新日期: %s", max_date.strftime("%Y-%m-%d"))

    # 拉取ESPN数据
    scores = fetch_espn_scores("NBA", days_back=days_back)
    if not scores:
        logger.info("ESPN 无新比赛数据")
        return

    new_rows = []
    now_utc = pd.Timestamp.now(tz=timezone.utc)
    for s in scores:
        home, away = s["home_team"], s["away_team"]
        home_score, away_score = s["home_score"], s["away_score"]
        game_date = pd.to_datetime(s.get("game_date"), utc=True) if s.get("game_date") else (now_utc - pd.Timedelta(days=1))

        # 去重：匹配队名（不区分大小写）
        exists = ((existing['home'].str.lower().str.strip() == home.lower()) &
                  (existing['away'].str.lower().str.strip() == away.lower())).any()
        if not exists:
            new_rows.append({
                'date': game_date,
                'home': home,
                'away': away,
                'home_score': home_score,
                'away_score': away_score,
            })
            logger.info("  新增: %s %s-%s %s", home, home_score, away_score, away)

    if not new_rows:
        logger.info("无新比赛需要追加")
        return

    new_df = pd.DataFrame(new_rows)
    updated = pd.concat([existing, new_df], ignore_index=True)
    updated.to_csv(csv_path, index=False)
    logger.info("✅ 篮球数据已更新: %d → %d 场", len(existing), len(updated))


def refresh_football_history(days_back: int = 14):
    """从 ESPN 拉取近期足球比分，追加到 football_history.csv。"""
    from src.fetchers.espn_scores import fetch_espn_scores

    csv_path = ROOT / "data" / "storage" / "football_history.csv"
    if not csv_path.exists():
        logger.warning("football_history.csv 不存在，跳过")
        return

    existing = pd.read_csv(csv_path)
    existing.columns = [c.strip().lower() for c in existing.columns]
    existing['date'] = pd.to_datetime(existing['date'], utc=True, format='mixed')
    max_date = existing['date'].max()
    logger.info("当前足球数据最新日期: %s", max_date.strftime("%Y-%m-%d"))

    leagues = ["英超", "西甲", "德甲", "意甲", "法甲"]
    new_rows = []
    for league in leagues:
        scores = fetch_espn_scores(league, days_back=days_back)
        for s in scores:
            home, away = s["home_team"], s["away_team"]
            home_score, away_score = s["home_score"], s["away_score"]
            game_date = pd.to_datetime(s.get("game_date"), utc=True) if s.get("game_date") else (pd.Timestamp.now(tz=timezone.utc) - pd.Timedelta(days=1))

            exists = ((existing['home'].str.lower() == home.lower()) &
                      (existing['away'].str.lower() == away.lower()) &
                      (existing['date'].dt.strftime('%Y-%m-%d') == game_date.strftime('%Y-%m-%d'))).any()
            if not exists:
                new_rows.append({
                    'date': game_date,
                    'home': home,
                    'away': away,
                    'home_score': home_score,
                    'away_score': away_score,
                })
                logger.info("  [%s] 新增: %s %s-%s %s", league, home, home_score, away_score, away)

    if not new_rows:
        logger.info("无新足球比赛需要追加")
        return

    new_df = pd.DataFrame(new_rows)
    updated = pd.concat([existing, new_df], ignore_index=True)
    updated.to_csv(csv_path, index=False)
    logger.info("✅ 足球数据已更新: %d → %d 场", len(existing), len(updated))


def rebuild_all_features():
    """重新构建特征并重新训练所有模型。"""
    logger.info("=" * 50)
    logger.info("  重新构建特征...")
    logger.info("=" * 50)

    from src.features.bb_pipeline import build_bb_features
    from src.features.football_pipeline import build_football_features

    try:
        build_bb_features()
        logger.info("✅ 篮球特征构建完成")
    except Exception as e:
        logger.error("❌ 篮球特征构建失败: %s", e)

    try:
        build_football_features()
        logger.info("✅ 足球特征构建完成")
    except Exception as e:
        logger.error("❌ 足球特征构建失败: %s", e)

    logger.info("=" * 50)
    logger.info("  重新训练模型...")
    logger.info("=" * 50)

    from src.models.ensemble_trainer import train_sport_ensemble
    try:
        train_sport_ensemble('bb')
        logger.info("✅ 篮球模型重训练完成")
    except Exception as e:
        logger.error("❌ 篮球模型重训练失败: %s", e)
    try:
        train_sport_ensemble('fb')
        logger.info("✅ 足球模型重训练完成")
    except Exception as e:
        logger.error("❌ 足球模型重训练失败: %s", e)

    from src.models.auto_retrain import mark_training_complete
    mark_training_complete()
    logger.info("✅ 全部特征重建与模型重训练完成")


if __name__ == '__main__':
    from config.logging_config import setup_logging
    setup_logging(log_level='INFO', log_to_file=False)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--sport', choices=['bb', 'fb', 'all'], default='all')
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--rebuild', action='store_true', help='刷新数据后重建特征并重训练')
    args = parser.parse_args()

    if args.sport in ('bb', 'all'):
        refresh_basketball_history(args.days)
    if args.sport in ('fb', 'all'):
        refresh_football_history(args.days)

    if args.rebuild:
        rebuild_all_features()
