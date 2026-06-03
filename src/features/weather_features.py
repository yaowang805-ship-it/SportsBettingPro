"""天气特征工程 — 利用 OpenWeatherMap API 获取比赛天气数据。

职业博彩系统将天气作为重要特征，尤其对足球（户外）影响显著：
  - 下雨 → 进球数减少，犯规增加
  - 大风 → 传球准确率下降，比赛节奏变慢
  - 高温/低温 → 体能消耗，下半场进球模式变化

用法:
    from src.features.weather_features import get_weather_features, add_weather_to_df
    weather = get_weather_features(lat, lon, match_time)
    df = add_weather_to_df(match_df, city_col='home_city')
"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import requests

from config.logging_config import get_logger
from config.settings import OPENWEATHERMAP_API_KEY

logger = get_logger(__name__)

# 球队 → 城市/场馆坐标映射
TEAM_LOCATIONS = {
    # NBA 球队 (lat, lon)
    "Boston Celtics": (42.3663, -71.0629),
    "Brooklyn Nets": (40.6829, -73.9754),
    "New York Knicks": (40.7505, -73.9934),
    "Philadelphia 76ers": (39.9012, -75.1720),
    "Toronto Raptors": (43.6435, -79.3791),
    "Chicago Bulls": (41.8807, -87.6742),
    "Cleveland Cavaliers": (41.4964, -81.6882),
    "Detroit Pistons": (42.3410, -83.0549),
    "Indiana Pacers": (39.7639, -86.1556),
    "Milwaukee Bucks": (43.0450, -87.9169),
    "Atlanta Hawks": (33.7573, -84.3963),
    "Charlotte Hornets": (35.2251, -80.8392),
    "Miami Heat": (25.7814, -80.1870),
    "Orlando Magic": (28.5393, -81.4370),
    "Washington Wizards": (38.8981, -77.0209),
    "Denver Nuggets": (39.7488, -104.9957),
    "Minnesota Timberwolves": (44.9795, -93.2757),
    "Oklahoma City Thunder": (35.4634, -97.5151),
    "Portland Trail Blazers": (45.5317, -122.6668),
    "Utah Jazz": (40.7683, -111.9011),
    "Golden State Warriors": (37.7503, -122.2032),
    "LA Clippers": (33.9427, -118.3468),
    "Los Angeles Lakers": (33.9427, -118.3468),
    "Phoenix Suns": (33.4457, -112.0712),
    "Sacramento Kings": (38.5801, -121.5006),
    "Dallas Mavericks": (32.7903, -96.8103),
    "Houston Rockets": (29.7508, -95.3622),
    "Memphis Grizzlies": (35.1381, -90.0508),
    "New Orleans Pelicans": (29.9490, -90.0824),
    "San Antonio Spurs": (29.4270, -98.4375),
    # 英超
    "Manchester City": (53.4830, -2.2003),
    "Manchester United": (53.4631, -2.2913),
    "Liverpool": (53.4308, -2.9608),
    "Chelsea": (51.4817, -0.1910),
    "Arsenal": (51.5550, -0.1086),
    "Tottenham Hotspur": (51.6043, -0.0662),
    "Newcastle United": (54.9753, -1.6218),
    "Aston Villa": (52.5093, -1.8847),
    "Everton": (53.4390, -2.9665),
    "West Ham United": (51.5319, -0.0418),
    # 西甲
    "Real Madrid": (40.4531, -3.6884),
    "Barcelona": (41.3809, 2.1228),
    "Atletico Madrid": (40.4360, -3.5994),
    "Sevilla": (37.3840, -5.9705),
    "Valencia": (39.4747, -0.3584),
    # 德甲
    "Bayern Munich": (48.2188, 11.6248),
    "Borussia Dortmund": (51.4925, 7.4512),
    # 意甲
    "Juventus": (45.1094, 7.6415),
    "AC Milan": (45.4781, 9.1240),
    "Inter Milan": (45.4781, 9.1240),
    "AS Roma": (41.9341, 12.4548),
    # 法甲
    "Paris Saint Germain": (48.8414, 2.2530),
    "Marseille": (43.2698, 5.3960),
}

# 城市间距离缓存（英里）
_DISTANCE_CACHE: Dict[str, float] = {}
_WEATHER_CACHE: Dict[str, dict] = {}


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """计算两地的球面距离（英里）。"""
    R = 3959.0  # 地球半径（英里）
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2) ** 2
    return R * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def get_travel_distance(home_team: str, away_team: str) -> float:
    """计算客队行程距离（英里）。"""
    key = f"{home_team}_{away_team}"
    if key in _DISTANCE_CACHE:
        return _DISTANCE_CACHE[key]

    home_loc = TEAM_LOCATIONS.get(home_team)
    away_loc = TEAM_LOCATIONS.get(away_team)
    if not home_loc or not away_loc:
        return 0.0

    dist = _haversine(home_loc[0], home_loc[1], away_loc[0], away_loc[1])
    _DISTANCE_CACHE[key] = dist
    return dist


def get_weather_features(lat: float, lon: float, match_time: Optional[datetime] = None) -> Dict:
    """获取比赛地点的天气特征。

    使用 OpenWeatherMap 当前天气或历史数据。

    Args:
        lat: 纬度
        lon: 经度
        match_time: 比赛时间（None = 当前）

    Returns:
        {temp_c, feels_like_c, humidity_pct, wind_speed_kph,
         precipitation_mm, cloud_pct, weather_condition, is_rain, is_snow}
    """
    cache_key = f"{lat:.2f}_{lon:.2f}"
    if cache_key in _WEATHER_CACHE:
        cached = _WEATHER_CACHE[cache_key]
        # 缓存有效期：30 分钟
        if (datetime.now() - cached["_fetched_at"]).total_seconds() < 1800:
            return {k: v for k, v in cached.items() if k != "_fetched_at"}

    api_key = OPENWEATHERMAP_API_KEY
    if not api_key:
        logger.warning("未设置 OPENWEATHERMAP_API_KEY")
        return {}

    try:
        url = (
            f"https://api.openweathermap.org/data/2.5/weather"
            f"?lat={lat}&lon={lon}&appid={api_key}&units=metric"
        )
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            logger.warning("天气 API 返回 %s: %s", resp.status_code, resp.text[:100])
            return {}

        data = resp.json()
        main = data.get("main", {})
        wind = data.get("wind", {})
        weather = data.get("weather", [{}])[0]
        rain = data.get("rain", {})
        snow = data.get("snow", {})

        condition = weather.get("main", "")
        result = {
            "temp_c": round(main.get("temp", 20.0), 1),
            "feels_like_c": round(main.get("feels_like", 20.0), 1),
            "humidity_pct": main.get("humidity", 50),
            "wind_speed_kph": round(wind.get("speed", 0) * 3.6, 1),  # m/s → kph
            "precipitation_mm": rain.get("1h", rain.get("3h", 0)) or snow.get("1h", snow.get("3h", 0)) or 0,
            "cloud_pct": main.get("clouds", data.get("clouds", {}).get("all", 0)),
            "weather_condition": condition,
            "is_rain": "Rain" in condition or "Drizzle" in condition,
            "is_snow": "Snow" in condition,
            "is_extreme": "Storm" in condition or "Extreme" in condition,
            "_fetched_at": datetime.now(),
        }
        # 简单缓存
        _WEATHER_CACHE[cache_key] = result
        return {k: v for k, v in result.items() if k != "_fetched_at"}

    except Exception as e:
        logger.debug("天气获取失败 (%s, %s): %s", lat, lon, e)
        return {}


def add_weather_to_df(match_df: pd.DataFrame) -> pd.DataFrame:
    """向比赛 DataFrame 注入天气和行程特征。

    支持两种列命名规范：
      - home_team / away_team（标准）
      - home / away（简写，用于 ensemble_predictor）

    Returns:
        增加了 weather_* 和 travel_distance 列的 DataFrame
    """
    df = match_df.copy()

    # 兼容两种列命名规范
    home_col = "home_team" if "home_team" in df.columns else "home" if "home" in df.columns else None
    away_col = "away_team" if "away_team" in df.columns else "away" if "away" in df.columns else None
    if not home_col or not away_col:
        logger.warning("weather_features: 未找到球队名列（需要 home/home_team + away/away_team）")
        for col in get_weather_feature_names():
            df[col] = 0.0 if col not in ("weather_is_rain", "weather_is_snow") else 0
        return df

    # 行程距离
    travel_dists = []
    for _, row in df.iterrows():
        dist = get_travel_distance(str(row.get(home_col, "")), str(row.get(away_col, "")))
        travel_dists.append(dist)
    df["travel_distance_miles"] = travel_dists

    # 天气特征（使用主队城市）
    weather_cols = ["temp_c", "humidity_pct", "wind_speed_kph",
                    "precipitation_mm", "is_rain", "is_snow"]
    for col in weather_cols:
        df[f"weather_{col}"] = 0.0 if col not in ("is_rain", "is_snow", "is_extreme") else 0

    for idx, row in df.iterrows():
        home_team = str(row.get(home_col, ""))
        loc = TEAM_LOCATIONS.get(home_team)
        if not loc:
            continue
        wx = get_weather_features(loc[0], loc[1])
        if not wx:
            continue
        for col in weather_cols:
            val = wx.get(col, 0)
            df.at[idx, f"weather_{col}"] = int(val) if col in ("is_rain", "is_snow") else val

    return df


def get_weather_feature_names() -> List[str]:
    """返回天气特征列名列表，用于模型特征注册。"""
    return [
        "travel_distance_miles",
        "weather_temp_c",
        "weather_humidity_pct",
        "weather_wind_speed_kph",
        "weather_precipitation_mm",
        "weather_is_rain",
        "weather_is_snow",
    ]


if __name__ == "__main__":
    # 测试
    dist = get_travel_distance("Boston Celtics", "Los Angeles Lakers")
    print(f"波士顿 → 洛杉矶: {dist:.0f} 英里")

    loc = TEAM_LOCATIONS.get("Manchester City")
    if loc:
        wx = get_weather_features(loc[0], loc[1])
        print(f"曼彻斯特天气: {wx}")
