import requests, pandas as pd
from datetime import datetime
import time, os

from config.settings import ODDS_API_KEY as API_KEY
REGIONS = "us"
MARKETS = "h2h,spreads,totals"
OUTPUT = "data/odds/historical_odds_full.csv"

# 只处理足球五大联赛
SPORTS = [
    ("soccer_epl", "英超"),
    ("soccer_spain_la_liga", "西甲"),
    ("soccer_germany_bundesliga", "德甲"),
    ("soccer_italy_serie_a", "意甲"),
    ("soccer_france_ligue_one", "法甲"),
]

# 加载已完成日期
skip_df = pd.read_csv("data/odds/skip_dates.csv") if os.path.exists("data/odds/skip_dates.csv") else pd.DataFrame()

# 从比分文件中提取有效日期
fb = pd.read_csv("data/storage/football_history_clean.csv")
fb['date_only'] = pd.to_datetime(fb['date']).dt.date

LEAGUE_MAP = {
    "soccer_epl": ["E0", "Premier League", "英超"],
    "soccer_spain_la_liga": ["SP1", "La Liga", "西甲"],
    "soccer_germany_bundesliga": ["D1", "Bundesliga", "德甲"],
    "soccer_italy_serie_a": ["I1", "Serie A", "意甲"],
    "soccer_france_ligue_one": ["F1", "Ligue 1", "法甲"],
}

league_col = next((c for c in fb.columns if 'league' in c.lower() or 'div' in c.lower()), None)

all_rows = []
if os.path.exists(OUTPUT):
    existing = pd.read_csv(OUTPUT)
    all_rows = existing.to_dict("records")
    print(f"📂 已有 {len(all_rows)} 场，断点续传")

for sport_key, sport_name in SPORTS:
    # 获取该联赛有效日期
    if league_col:
        mask = fb[league_col].astype(str).str.strip().str.lower().isin([n.lower() for n in LEAGUE_MAP[sport_key]])
    else:
        mask = pd.Series(True, index=fb.index)
    
    all_dates = sorted(fb.loc[mask, 'date_only'].unique())
    
    # 移除已完成日期
    done = set()
    if not skip_df.empty:
        done = set(skip_df[skip_df['sport_key'] == sport_key]['date'].unique())
    
    remaining = [d for d in all_dates if d.isoformat() not in done]
    
    if not remaining:
        print(f"✅ {sport_name} 已完成，跳过")
        continue
    
    print(f"📌 {sport_name} 剩余 {len(remaining)} 天（总计 {len(all_dates)}，已完成 {len(done)}）")
    
    for i, d_obj in enumerate(remaining):
        d_str = d_obj.isoformat()
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/?apiKey={API_KEY}&regions={REGIONS}&markets={MARKETS}&date={d_str}T12:00:00Z"
        
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                count = 0
                for game in data:
                    home = game.get("home_team", "")
                    away = game.get("away_team", "")
                    commence = game.get("commence_time", "")[:10]
                    bookmakers = game.get("bookmakers", [])
                    if not bookmakers:
                        continue
                    markets = []
                    for bm in bookmakers:
                        m = bm.get("markets", [])
                        if m:
                            markets = m
                            break
                    
                    home_odds = spread_point = spread_odds = total_point = over_odds = None
                    for market in markets:
                        key = market.get("key", "")
                        outcomes = market.get("outcomes", [])
                        if key == "h2h":
                            for out in outcomes:
                                if out.get("name", "") == home:
                                    home_odds = out.get("price")
                        elif key == "spreads":
                            for out in outcomes:
                                if out.get("name", "") == home:
                                    spread_point = out.get("point")
                                    spread_odds = out.get("price")
                        elif key == "totals":
                            total_point = market.get("point") or (outcomes[0].get("point") if outcomes else None)
                            for out in outcomes:
                                if out.get("name") == "Over":
                                    over_odds = out.get("price")
                    
                    if home_odds:
                        all_rows.append({
                            "date": commence, "sport_key": sport_key, "sport_name": sport_name,
                            "home": home, "away": away,
                            "home_odds": home_odds, "spread_point": spread_point,
                            "spread_odds": spread_odds, "total_point": total_point, "over_odds": over_odds,
                        })
                        count += 1
                
                print(f"✅ {d_str} {sport_name} {count}场 (累计{len(all_rows)})")
                time.sleep(0.3)
                
            elif resp.status_code == 429:
                print("⏳ 速率限制，等待10秒...")
                time.sleep(10)
                continue
            else:
                print(f"⚠️ {d_str} {sport_name} HTTP {resp.status_code}")
                time.sleep(0.5)
                
        except Exception as e:
            print(f"❌ {d_str} {sport_name} {type(e).__name__}")
            time.sleep(1)
        
        if (i+1) % 20 == 0 and all_rows:
            pd.DataFrame(all_rows).to_csv(OUTPUT, index=False)
            print(f"💾 断点保存 (累计{len(all_rows)})")

pd.DataFrame(all_rows).to_csv(OUTPUT, index=False)
print(f"\n🎉 完成！总计 {len(all_rows)} 场")
