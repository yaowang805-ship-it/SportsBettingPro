import os
import json
import pandas as pd
import numpy as np
from glob import glob
from typing import Any

# --- CONFIGURATION ---
DEFAULT_REST_DAYS = 3
MAX_REST_DAYS = 7
ROLLING_WINDOWS = [3, 10, 30]

STATS_COLS = [
    "shots", "power_play_goals", "power_play_opportunities",
    "faceoff_win_pct", "hits", "blocked_shots", "pim", "giveaways", "takeaways"
]

def parse_record(record_str: str):
    """Parses 'W-L-OTL' string into winning percentage."""
    try:
        parts = record_str.split('-')
        w = int(parts[0])
        l = int(parts[1])
        otl = int(parts[2]) if len(parts) > 2 else 0
        total_games = w + l + otl
        if total_games == 0: return np.nan
        return (2 * w + otl) / (2 * total_games)
    except:
        return np.nan

def parse_record_components(record_str: str):
    """Parses 'W-L-OTL' string into individual components."""
    try:
        parts = record_str.split('-')
        w = int(parts[0])
        l = int(parts[1])
        otl = int(parts[2]) if len(parts) > 2 else 0
        return w, l, otl
    except:
        return np.nan, np.nan, np.nan

def safe_float(x: Any) -> float:
    """Safely converts to float, returning NaN for missing data."""
    try:
        if x is None or x == '': return np.nan
        return float(x)
    except:
        return np.nan

def extract_officials(officials_list):
    """Extract officials information into separate fields."""
    if not officials_list:
        return None, None, []
    
    referees = []
    linesmen = []
    
    for official in officials_list:
        name = official.get('name', '')
        position = official.get('position', '').lower()
        
        if 'referee' in position:
            referees.append(name)
        elif 'linesman' in position or 'linesmen' in position:
            linesmen.append(name)
    
    referee_str = '|'.join(referees) if referees else None
    linesman_str = '|'.join(linesmen) if linesmen else None
    
    return referee_str, linesman_str, referees + linesmen

def load_and_flatten_data(directory: str) -> pd.DataFrame:
    """
    Reads JSON files and flattens them into a Tidy DataFrame (one row per team-game).
    Now includes additional features: officials, betting lines, series info, and record details.
    """
    files = glob(os.path.join(directory, "*.json"))
    
    if not files:
        print(f"Error: No JSON files found in directory: {directory}")
        return pd.DataFrame()
    
    print(f"Found {len(files)} JSON files")
    rows = []

    for file in files:
        try:
            with open(file, "r") as f:
                games_list = json.load(f)

            if not isinstance(games_list, list): 
                print(f"Warning: {file} does not contain a list, skipping")
                continue

            for game in games_list:
                game_id = game.get("game_id")
                if not game_id:
                    continue
                    
                try:
                    date = pd.to_datetime(game.get("date"))
                except:
                    print(f"Warning: Invalid date in game {game_id}")
                    continue
                
                venue = game.get("venue")
                attendance = safe_float(game.get("attendance"))
                
                # NEW: Extract betting lines and series information
                spread = safe_float(game.get("spread"))
                over_under = safe_float(game.get("over_under"))
                favorite_moneyline = safe_float(game.get("favorite_moneyline"))
                season_series_summary = game.get("season_series_summary")
                
                # NEW: Extract officials
                officials = game.get("officials", [])
                referees, linesmen, all_officials = extract_officials(officials)
                num_officials = len(all_officials)
                
                teams = game.get("teams_stats", [])
                if len(teams) != 2:
                    print(f"Warning: Game {game_id} does not have exactly 2 teams (has {len(teams)})")
                    continue

                for i, team in enumerate(teams):
                    opponent = teams[1] if i == 0 else teams[0]
                    
                    team_score = safe_float(team.get("score"))
                    opp_score = safe_float(opponent.get("score"))
                    
                    if pd.isna(team_score) or pd.isna(opp_score):
                        print(f"Warning: Missing scores in game {game_id} for team {team.get('team_name', 'Unknown')}")
                        continue
                    
                    # NEW: Parse record components
                    record_str = team.get("record_summary", "0-0-0")
                    wins, losses, otl = parse_record_components(record_str)
                    
                    row = {
                        "game_id": game_id,
                        "date": date,
                        "season": date.year + 1 if date.month > 8 else date.year,
                        "venue": venue,
                        "attendance": attendance,
                        "team_id": team.get("team_id"),
                        "team_name": team.get("team_name"),
                        "is_home": 1 if team.get("home_away") == "home" else 0,
                        "won": 1 if team_score > opp_score else 0,
                        "goals_for": team_score,
                        "goals_against": opp_score,
                        "pre_game_point_pct": parse_record(record_str),
                        
                        # NEW: Betting lines
                        "spread": spread,
                        "over_under": over_under,
                        "favorite_moneyline": favorite_moneyline,
                        
                        # NEW: Series information
                        "season_series_summary": season_series_summary,
                        
                        # NEW: Record components
                        "record_summary": record_str,
                        "record_wins": wins,
                        "record_losses": losses,
                        "record_otl": otl,
                        
                        # NEW: Officials
                        "referees": referees,
                        "linesmen": linesmen,
                        "num_officials": num_officials,
                    }

                    for stat in STATS_COLS:
                        row[stat] = safe_float(team.get(stat))

                    rows.append(row)
                    
        except Exception as e:
            print(f"Error processing file {file}: {e}")
            import traceback
            traceback.print_exc()

    df = pd.DataFrame(rows)
    
    if df.empty:
        print("Warning: No valid data extracted from JSON files.")
        return df
    
    print(f"Extracted {len(rows)} rows from {len(files)} files")
    print(f"Columns: {df.columns.tolist()}")
    
    # Check for duplicates
    if 'game_id' in df.columns and 'team_id' in df.columns:
        dup_count = df.duplicated(subset=['game_id', 'team_id']).sum()
        if dup_count > 0:
            print(f"Warning: Found {dup_count} duplicate game-team combinations. Removing duplicates.")
            df = df.drop_duplicates(subset=['game_id', 'team_id'], keep='first')
    
        df = df.sort_values(["team_id", "date"]).reset_index(drop=True)
    
    return df

def create_rolling_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Creates lagged rolling features to prevent data leakage.
    All features represent data from BEFORE the current game.
    """
    df = df.copy()
    
    # Rest Days
    df['prev_date'] = df.groupby('team_id')['date'].shift(1)
    df['rest_days'] = (df['date'] - df['prev_date']).dt.days
    df['rest_days'] = df['rest_days'].fillna(DEFAULT_REST_DAYS).clip(upper=MAX_REST_DAYS)
    df.drop(columns=['prev_date'], inplace=True)

    # Cumulative Season Stats (all shifted by 1 to exclude current game)
    grouped = df.groupby(['team_id', 'season'])
    df['season_games_played'] = grouped.cumcount()
    
    # Shift all cumulative stats
    df['season_wins'] = grouped['won'].shift(1).fillna(0).cumsum()
    df['season_goals_for'] = grouped['goals_for'].shift(1).fillna(0).cumsum()
    df['season_goals_against'] = grouped['goals_against'].shift(1).fillna(0).cumsum()
    
    # Win percentage with safe division
    df['season_win_pct'] = np.where(
        df['season_games_played'] > 0,
        df['season_wins'] / df['season_games_played'],
        np.nan
    )
    
    # Goal differential
    df['season_goal_diff'] = df['season_goals_for'] - df['season_goals_against']

    # Rolling Averages (exclude current game with shift)
    rolling_cols = STATS_COLS + ["goals_for", "goals_against"]
    
    for w in ROLLING_WINDOWS:
        for col in rolling_cols:
            # Shift first, then rolling
            shifted = df.groupby(['team_id', 'season'])[col].shift(1)
            rolled = shifted.groupby([df['team_id'], df['season']]).rolling(
                window=w, min_periods=1
            ).mean().reset_index(level=[0,1], drop=True)
            
            df[f"roll_{w}_{col}"] = rolled

    return df

def merge_opponent_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Properly merges opponent features by matching home/away teams.
    """
    # Identify feature columns (exclude raw game stats and identifiers)
    feature_cols = [c for c in df.columns if 
                    c.startswith('roll_') or 
                    c.startswith('season_') or 
                    c == 'rest_days' or 
                    c == 'pre_game_point_pct' or
                    c.startswith('record_')]
    
    # Split into home and away
    home_df = df[df['is_home'] == 1].copy()
    away_df = df[df['is_home'] == 0].copy()
    
    # Rename away features with 'opp_' prefix
    away_features = away_df[['game_id', 'team_id', 'team_name'] + feature_cols].copy()
    away_features.columns = ['game_id', 'opp_team_id', 'opp_team_name'] + [f'opp_{c}' for c in feature_cols]
    
    # Rename home features with 'opp_' prefix
    home_features = home_df[['game_id', 'team_id', 'team_name'] + feature_cols].copy()
    home_features.columns = ['game_id', 'opp_team_id', 'opp_team_name'] + [f'opp_{c}' for c in feature_cols]
    
    # Merge home teams with away opponent features
    home_merged = home_df.merge(away_features, on='game_id', how='left')
    
    # Merge away teams with home opponent features  
    away_merged = away_df.merge(home_features, on='game_id', how='left')
    
    # Combine
    df_final = pd.concat([home_merged, away_merged], ignore_index=True)
    df_final = df_final.sort_values(['date', 'game_id', 'is_home'], ascending=[True, True, False]).reset_index(drop=True)
    
    # Create differential features
    df_final['rest_diff'] = df_final['rest_days'] - df_final['opp_rest_days']
    df_final['win_pct_diff'] = df_final['season_win_pct'] - df_final['opp_season_win_pct']
    df_final['goal_diff_diff'] = df_final['season_goal_diff'] - df_final['opp_season_goal_diff']
    
    # NEW: Record differential features
    df_final['record_wins_diff'] = df_final['record_wins'] - df_final['opp_record_wins']
    df_final['record_losses_diff'] = df_final['record_losses'] - df_final['opp_record_losses']
    
    # Rolling goal differential features
    for w in ROLLING_WINDOWS:
        df_final[f'roll_{w}_goal_diff'] = df_final[f'roll_{w}_goals_for'] - df_final[f'roll_{w}_goals_against']
        df_final[f'opp_roll_{w}_goal_diff'] = df_final[f'opp_roll_{w}_goals_for'] - df_final[f'opp_roll_{w}_goals_against']
    
    return df_final

def clean_final_dataset(df: pd.DataFrame, min_games: int = 5) -> pd.DataFrame:
    """
    Final cleanup: handle missing values and remove early-season games.
    """
    df = df.copy()
    
    # Remove games where teams have played fewer than min_games
    # (these have unreliable rolling features)
    df = df[df['season_games_played'] >= min_games].copy()
    
    # For remaining NaN values in rolling features, fill with season averages
    feature_cols = [c for c in df.columns if c.startswith('roll_') or c.startswith('opp_roll_')]
    
    for col in feature_cols:
        # Fill NaN with 0 (represents no history)
        df[col] = df[col].fillna(0)
    
    # Fill remaining NaN in other numeric columns
    numeric_cols = df.select_dtypes(include=[np.number]).columns
    for col in numeric_cols:
        if df[col].isna().any():
            df[col] = df[col].fillna(df[col].median())
    
    return df

if __name__ == "__main__":
    print("Loading and flattening data with extensive features...")
    df_raw = load_and_flatten_data("./../data")
    
    if df_raw.empty:
        print("No data loaded. Check your data directory.")
    else:
        print(f"Loaded {len(df_raw)} team-game records from {df_raw['game_id'].nunique()} games")
        
        print("\nCreating rolling features...")
        df_features = create_rolling_features(df_raw)
        
        print("Merging opponent features...")
        df_final = merge_opponent_features(df_features)
        
        print("Cleaning final dataset...")
        df_clean = clean_final_dataset(df_final, min_games=3)
        
        print(f"\nFinal dataset: {len(df_clean)} records")
        print(f"Features: {len([c for c in df_clean.columns if c.startswith('roll_') or c.startswith('season_') or 'diff' in c])} feature columns")
        
        # Save to the new filename
        df_clean.to_csv("nhl_data_extensive.csv", index=False)
        print("✓ Saved nhl_data_extensive.csv")
        
        # Basic validation
        print("\n=== Data Quality Checks ===")
        print(f"Missing values: {df_clean.isna().sum().sum()}")
        print(f"Games per team (mean): {df_clean.groupby('team_id').size().mean():.1f}")
        print(f"Date range: {df_clean['date'].min()} to {df_clean['date'].max()}")
        print(f"Win rate: {df_clean['won'].mean():.3f}")
        
        print("\n=== New Features Summary ===")
        print(f"Betting lines (spread) available: {df_clean['spread'].notna().ne(0.0).sum()} records")
        print(f"Over/under available: {df_clean['over_under'].notna().ne(0.0).sum()} records")
        print(f"Moneyline available: {df_clean['favorite_moneyline'].notna().sum()} records")
        print(f"Series summaries available: {df_clean['season_series_summary'].notna().sum()} records")
        print(f"Referee info available: {df_clean['referees'].notna().sum()} records")
        print(f"Linesman info available: {df_clean['linesmen'].notna().sum()} records")