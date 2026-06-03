#!/usr/bin/env python3
"""国际赛事（世界杯/欧洲杯）特征生成器。"""
import pandas as pd
import numpy as np
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORAGE_DIR = ROOT / 'data' / 'storage'
PROCESSED_DIR = ROOT / 'data' / 'processed'
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)


def build_tournament_features():
    """为世界杯和欧洲杯生成特征。"""
    wc_matches = STORAGE_DIR / 'wc_matches.csv'
    output_wc = PROCESSED_DIR / 'wc_features.csv'
    
    if not wc_matches.exists():
        print(f"❌ 世界杯数据不存在: {wc_matches}")
        return
    
    # 加载世界杯比赛
    df = pd.read_csv(wc_matches)
    
    # 标准化列
    df.columns = [c.strip().lower() for c in df.columns]
    
    # 需要的列：date, home_team, away_team, home_score, away_score
    required = ['date', 'home_team', 'away_team', 'home_score', 'away_score']
    available = [c for c in df.columns if any(r in c for r in ['date', 'home', 'away', 'goal', 'score'])]
    
    if not df.empty:
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        df['home_team'] = df['home_team'].str.strip().str.lower()
        df['away_team'] = df['away_team'].str.strip().str.lower()
        df['win'] = (pd.to_numeric(df['home_score'], errors='coerce') > 
                     pd.to_numeric(df['away_score'], errors='coerce')).astype(int)
        
        # 基础统计
        for w in [3, 5, 10]:
            df[f'home_gf_avg_{w}'] = df.groupby('home_team')['home_score'].transform(
                lambda x: pd.to_numeric(x, errors='coerce').shift(1).rolling(w, min_periods=1).mean())
            df[f'home_ga_avg_{w}'] = df.groupby('home_team')['away_score'].transform(
                lambda x: pd.to_numeric(x, errors='coerce').shift(1).rolling(w, min_periods=1).mean())
            df[f'away_gf_avg_{w}'] = df.groupby('away_team')['away_score'].transform(
                lambda x: pd.to_numeric(x, errors='coerce').shift(1).rolling(w, min_periods=1).mean())
            df[f'away_ga_avg_{w}'] = df.groupby('away_team')['home_score'].transform(
                lambda x: pd.to_numeric(x, errors='coerce').shift(1).rolling(w, min_periods=1).mean())
        
        # 赛事特征
        df['days_since_last_match_home'] = df.groupby('home_team')['date'].diff().dt.days.fillna(7)
        df['days_since_last_match_away'] = df.groupby('away_team')['date'].diff().dt.days.fillna(7)
        df['is_knockout'] = df['date'].dt.month >= 6  # 假设6月以后是淘汰赛
        
        df = df.fillna(0)
        df.to_csv(output_wc, index=False)
        print(f"✅ 世界杯特征已保存至 {output_wc}")
    else:
        print("⚠️ 世界杯数据为空")


if __name__ == '__main__':
    build_tournament_features()
