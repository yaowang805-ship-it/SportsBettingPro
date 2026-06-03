import json
from pathlib import Path
from config.settings import DATA_DIR

def fetch_football_odds():
    """获取足球赔率（当前从本地缓存读取，后续接入BSD API）"""
    cache_file = DATA_DIR / 'football_odds.json'
    if cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)
    return []
