import os
import json
import requests
import datetime
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any, Optional

START_DATE = "20040101"
END_DATE = "20251210"
OUTPUT_DIR = "data"
CHUNK_DAYS = 7
MAX_THREADS = 10
SCOREBOARD_URL = "http://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
SUMMARY_URL = "http://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary"
TIMEOUT_SECONDS = 10

def generate_date_chunks(start_str: str, end_str: str, chunk_size_days: int) -> List[str]:
    """
    Splits a date range into smaller chunks formatted as string ranges.

    Args:
        start_str (str): Start date in YYYYMMDD format.
        end_str (str): End date in YYYYMMDD format.
        chunk_size_days (int): Number of days per chunk.

    Returns:
        List[str]: A list of date range strings (e.g., '20241001-20241007').
    """
    start = datetime.datetime.strptime(start_str, "%Y%m%d")
    end = datetime.datetime.strptime(end_str, "%Y%m%d")
    chunks = []
    current = start
    while current <= end:
        chunk_end = current + datetime.timedelta(days=chunk_size_days - 1)
        if chunk_end > end:
            chunk_end = end
        s_fmt = current.strftime("%Y%m%d")
        e_fmt = chunk_end.strftime("%Y%m%d")
        chunks.append(f"{s_fmt}-{e_fmt}")
        current = chunk_end + datetime.timedelta(days=1)
    return chunks

def extract_team_stats(team_data: Dict[str, Any], header_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parses raw team dictionary to extract flattened statistics and record info.

    Args:
        team_data (Dict[str, Any]): Raw team data from the boxscore.
        header_info (Dict[str, Any]): lookup dictionary containing scores and records.

    Returns:
        Dict[str, Any]: A flat dictionary of team attributes and statistics.
    """
    team_id = team_data.get('team', {}).get('id')
    
    stats_list = team_data.get('statistics', [])
    stats_map = {item['name']: item['displayValue'] for item in stats_list}

    team_header = header_info.get(team_id, {})

    return {
        'team_id': team_id,
        'team_name': team_data.get('team', {}).get('displayName'),
        'home_away': team_data.get('homeAway'),
        'score': team_header.get('score', 0),
        'winner': team_header.get('winner', False),
        'record_summary': team_header.get('record', ''),
        'shots': stats_map.get('shotsTotal'),
        'power_play_goals': stats_map.get('powerPlayGoals'),
        'power_play_opportunities': stats_map.get('powerPlayOpportunities'),
        'faceoff_win_pct': stats_map.get('faceoffPercent'),
        'hits': stats_map.get('hits'),
        'blocked_shots': stats_map.get('blockedShots'),
        'pim': stats_map.get('penaltyMinutes'),
        'giveaways': stats_map.get('giveaways'),
        'takeaways': stats_map.get('takeaways')
    }

def fetch_game_summary(game_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieves and parses detailed game summary data from the API.

    Args:
        game_id (str): The unique identifier for the game event.

    Returns:
        Optional[Dict[str, Any]]: Parsed game data dictionary or None if failed.
    """
    url = f"{SUMMARY_URL}?event={game_id}"
    try:
        response = requests.get(url, timeout=TIMEOUT_SECONDS)
        if response.status_code == 200:
            data = response.json()
            
            header_map = {}
            competitions = data.get('header', {}).get('competitions', [{}])[0]
            date = competitions.get('date')
            
            for comp in competitions.get('competitors', []):
                t_id = comp.get('team', {}).get('id')
                record = next((r.get('summary') for r in comp.get('record', []) if r.get('type') == 'total'), "0-0-0")
                
                header_map[t_id] = {
                    'score': comp.get('score'),
                    'winner': comp.get('winner', False),
                    'record': record
                }

            game_info = {
                'game_id': game_id,
                'date': date,
                'venue': data.get('gameInfo', {}).get('venue', {}).get('fullName'),
                'attendance': data.get('gameInfo', {}).get('attendance')
            }

            pickcenter = data.get('pickcenter', [])
            if pickcenter:
                primary_pick = pickcenter[0]
                game_info['spread'] = primary_pick.get('spread')
                game_info['over_under'] = primary_pick.get('overUnder')
                
                home_odds = primary_pick.get('homeTeamOdds', {})
                away_odds = primary_pick.get('awayTeamOdds', {})
                if home_odds.get('favorite'):
                    game_info['favorite_moneyline'] = home_odds.get('moneyLine')
                else:
                    game_info['favorite_moneyline'] = away_odds.get('moneyLine')

            season_series = data.get('seasonseries', [])
            if season_series:
                series_data = season_series[0]
                game_info['season_series_summary'] = series_data.get('summary')
                game_info['season_series_competitors'] = series_data.get('competitors')
            
            officials = data.get('gameInfo', {}).get('officials', [])
            game_info['officials'] = [
                {'name': o.get('displayName'), 'position': o.get('position', {}).get('name')} 
                for o in officials
            ]

            teams_boxscore = data.get('boxscore', {}).get('teams', [])
            teams_stats = []
            for team in teams_boxscore:
                teams_stats.append(extract_team_stats(team, header_map))
            
            game_info['teams_stats'] = teams_stats
            
            return game_info
            
        return None
    except Exception as e:
        return None

def process_date_chunk(date_range: str) -> None:
    """
    Downloads and saves game data for a specific date range.

    Args:
        date_range (str): The date range string (YYYYMMDD-YYYYMMDD).

    Returns:
        None
    """
    file_path = os.path.join(OUTPUT_DIR, f"nhl_detailed_{date_range}.json")
    if os.path.exists(file_path):
        return

    scoreboard_url = f"{SCOREBOARD_URL}?dates={date_range}"
    game_ids = []
    
    try:
        sb_response = requests.get(scoreboard_url, timeout=TIMEOUT_SECONDS)
        if sb_response.status_code == 200:
            sb_data = sb_response.json()
            events = sb_data.get('events', [])
            game_ids = [event['id'] for event in events]
        else:
            return
    except Exception as e:
        return

    if not game_ids:
        return

    detailed_games = []
    with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        future_to_game = {executor.submit(fetch_game_summary, gid): gid for gid in game_ids}
        
        for future in as_completed(future_to_game):
            result = future.result()
            if result:
                detailed_games.append(result)

    if detailed_games:
        with open(file_path, "w") as f:
            json.dump(detailed_games, f, indent=2)

def main() -> None:
    """
    Main entry point for orchestration of data download.

    Args:
        None

    Returns:
        None
    """
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    chunks = generate_date_chunks(START_DATE, END_DATE, CHUNK_DAYS)

    for i, chunk in enumerate(chunks):
        process_date_chunk(chunk)
        time.sleep(1)

if __name__ == "__main__":
    main()