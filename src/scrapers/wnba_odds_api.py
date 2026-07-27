"""WNBA 赔率对比 — 通过 the-odds-api 获取市场公平价（Pinnacle 不覆盖 WNBA）。

用法: python3 -m src.scrapers.wnba_odds_api
"""
import json, os, sys, time
from pathlib import Path
from collections import defaultdict

SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_DIR))

from config.settings import ODDS_API_KEYS, DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

WNBA_COMPARISON_FILE = DATA_DIR / "bb_vs_wnba_comparison.json"
BB_EXTRACTED = DATA_DIR / "bb_odds_extracted.json"
TEAM_MAP_FILE = DATA_DIR / "team_name_map.json"

# BB中文→英文映射（补全 team_name_map 不足）
_WNBA_TEAM_MAP = {
    "华盛顿神秘人 (女)": "Washington Mystics", "康涅狄格阳光 (女)": "Connecticut Sun",
    "明尼苏达山猫 (女)": "Minnesota Lynx", "多伦多节奏 (女)": "Toronto Tempo",
    "西雅图风暴 (女)": "Seattle Storm", "印第安纳狂热 (女)": "Indiana Fever",
    "洛杉矶火花 (女)": "Los Angeles Sparks", "纽约自由人 (女)": "New York Liberty",
    "拉斯维加斯王牌 (女)": "Las Vegas Aces", "波特兰火焰 (女)": "Portland Flame",
    "达拉斯飞翼 (女)": "Dallas Wings", "亚特兰大梦想 (女)": "Atlanta Dream",
    "菲尼克斯水星 (女)": "Phoenix Mercury", "金州瓦尔基里 (女)": "Golden State Valkyries",
    "芝加哥天空 (女)": "Chicago Sky",
}


def _get_best_odds(match_data, market_key):
    """从多个 bookmaker 中取每个结果的最高赔率。"""
    best = {}
    for bm in match_data.get("bookmakers", []):
        for mk in bm.get("markets", []):
            if mk.get("key") == market_key:
                for o in mk.get("outcomes", []):
                    name = o.get("name")
                    price = o.get("price")
                    if name and price and (name not in best or price > best[name]):
                        best[name] = price
    return best


def _de_vig(prices: dict) -> dict:
    """去抽水： multiplicative proportional method。"""
    if len(prices) < 2:
        return prices
    total = sum(1.0 / p for p in prices.values())
    if total <= 0:
        return prices
    return {k: round(v * total, 4) for k, v in prices.items()}


def compare_wnba():
    """对比 BB WNBA vs the-odds-api 市场公平价，产出 comparison 文件。"""
    import requests

    if not ODDS_API_KEYS:
        logger.warning("无 ODDS_API_KEY，跳过 WNBA 对比")
        return None

    # 1. 读取 BB WNBA 数据
    if not BB_EXTRACTED.exists():
        logger.warning("无 BB 数据")
        return None
    bb_all = json.loads(BB_EXTRACTED.read_text())
    wnba_matches = [m for m in bb_all.get("matches", []) if "WNBA" in m.get("league", "")]
    if not wnba_matches:
        logger.info("无 BB WNBA 比赛")
        return None
    logger.info(f"BB WNBA: {len(wnba_matches)} 场")

    # 2. 从 the-odds-api 获取 WNBA 赔率
    # 使用可用配额最多的 key
    key = ODDS_API_KEYS[1] if len(ODDS_API_KEYS) > 1 else (ODDS_API_KEYS[0] if ODDS_API_KEYS else None)
    if not key:
        return None

    try:
        resp = requests.get(
            "https://api.the-odds-api.com/v4/sports/basketball_wnba/odds",
            params={
                "apiKey": key,
                "regions": "us",
                "markets": "h2h,spreads,totals",
                "oddsFormat": "decimal",
            },
            timeout=15,
        )
        remaining = resp.headers.get("x-requests-remaining", "?")
        logger.info(f"the-odds-api WNBA: {resp.status_code} (剩余{remaining}次)")
        if resp.status_code != 200:
            logger.error(f"the-odds-api 失败: {resp.status_code}")
            return None
        odds_data = resp.json()
    except Exception as e:
        logger.error(f"the-odds-api 超时: {e}")
        return None

    # 3. 加载队名映射
    tm = {}
    if TEAM_MAP_FILE.exists():
        tm = json.loads(TEAM_MAP_FILE.read_text())
    tm.update(_WNBA_TEAM_MAP)

    # 4. 逐场对比
    details = []
    for bb_m in wnba_matches:
        bb_home = bb_m.get("home", "").strip()
        bb_away = bb_m.get("away", "").strip()
        en_home = tm.get(bb_home, bb_home)
        en_away = tm.get(bb_away, bb_away)

        # 匹配 the-odds-api 比赛
        match = None
        for om in odds_data:
            h = om.get("home_team", "")
            a = om.get("away_team", "")
            if (en_home.lower() in h.lower() or h.lower() in en_home.lower()) and \
               (en_away.lower() in a.lower() or a.lower() in en_away.lower()):
                match = om
                break

        if not match:
            continue

        entry = {
            "sport": "basketball",
            "league": "WNBA",
            "home_bb": bb_home,
            "away_bb": bb_away,
            "home_pin": match.get("home_team", ""),
            "away_pin": match.get("away_team", ""),
            "match_score": 1.0,
            "match_type": "name",
            "opportunities": [],
            "handicap": [],
            "over_under": [],
        }

        # 1X2
        best_ml = _get_best_odds(match, "h2h")
        bb_ml = bb_m.get("odds_ft", {}).get("ml", [])
        if len(best_ml) >= 2 and len(bb_ml) >= 2:
            fair = _de_vig(best_ml)
            # 需要匹配正确的方向
            for label, bb_odds, side in [
                (bb_home, bb_ml[0], en_home),
                (bb_away, bb_ml[1], en_away),
            ]:
                fair_price = fair.get(side, 0)
                if fair_price > 0:
                    ev = round((bb_odds - fair_price) / fair_price * 100, 2)
                    if ev > 1:
                        entry["opportunities"].append({
                            "designation": label,
                            "bb_odds": bb_odds,
                            "pin_odds": best_ml.get(side, 0),
                            "fair_price": fair_price,
                            "ev_pct": ev,
                            "_market": "1x2",
                        })

        if entry["opportunities"] or entry["handicap"] or entry["over_under"]:
            details.append(entry)

    # 5. 构建输出
    output = {
        "version": "1.0",
        "source": "the-odds-api (market consensus)",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "wnba_matches_total": len(wnba_matches),
        "matched_matches": len(details),
        "details": details,
    }
    WNBA_COMPARISON_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    logger.info(f"WNBA 对比完成: {len(details)} 条")
    return output


def main():
    result = compare_wnba()
    if result:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("无 WNBA +EV 机会")


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
