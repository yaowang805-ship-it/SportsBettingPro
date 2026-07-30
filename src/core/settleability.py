"""结算可行性追踪 — 判断联赛是否可被结算，避免在"盲区"下注。

核心原则：只在能结算的联赛下注。下注必须能学习。

维护 settleable_leagues.json，记录每联赛的结算成功率。
auto_settle 每次结算后更新，bb_ev_push 投注前查询。
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

SETTLEABLE_FILE = DATA_DIR / "settleable_leagues.json"

# 结算验证仅做门禁 (能投/不能投), 权重来自 Pinnacle 历史数据

# 已知可结算的联赛（历史遗留白名单，新数据以 settleable_leagues.json 为准）
KNOWN_SETTLEABLE_LEAGUES = {
    "NBA", "WNBA", "NFL", "MLB", "NPB",
    "英超", "英格兰超级联赛", "西甲", "西班牙甲级联赛",
    "德甲", "德国甲级联赛", "意甲", "意大利甲级联赛",
    "法甲", "法国甲级联赛", "巴甲", "巴西甲级联赛",
    "美职联", "美国职业大联盟", "墨超", "墨西哥超级联赛",
    "阿甲", "阿根廷甲级联赛", "挪超", "挪威超级联赛",
    "俄超", "俄罗斯超级联赛", "瑞典超", "瑞典超级联赛",
    "解放者杯", "南美解放者杯", "欧联", "欧足联欧洲联赛-资格赛",
    "欧协联", "欧足联欧洲协会联赛-资格赛",
    "欧冠", "欧足联欧洲会议联赛-资格赛",
    "南美俱乐部杯", "韩国职业棒球", "KBO",
    "厄瓜多尔甲级联赛", "秘鲁甲级联赛",
    "乌拉圭甲", "乌拉圭甲级联赛",
    "巴拉圭甲级联赛", "巴拉圭甲",
    "智利甲", "智利甲级联赛",
    "哥伦比亚甲级联赛", "阿根廷全国联赛",
    "俄罗斯甲级联赛", "俄罗斯乙级",
    "拉脱维亚超级联赛", "乌兹别克斯坦超级联赛",
    "保加利亚甲级联赛", "挪威甲级联赛",
    "苏格兰联赛杯", "韩国乙级联赛",
    "冰岛丙级联赛", "世界网球",
    "波兰超级联赛", "波兰甲级联赛",
    "美国MLS下级职业赛", "MLS下级",
}

# 已知有运动的 fallback（sport 级别可尝试结算）
SETTLEABLE_SPORTS = {"football", "basketball", "baseball", "tennis", "american_football"}


def _load_settleable() -> dict:
    """加载结算追踪文件。"""
    if SETTLEABLE_FILE.exists():
        try:
            return json.loads(SETTLEABLE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"leagues": {}, "updated": ""}


def _save_settleable(data: dict):
    """保存结算追踪文件。"""
    data["updated"] = datetime.now(timezone.utc).isoformat()
    SETTLEABLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTLEABLE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def is_league_settleable(league: str, sport: str = "") -> bool:
    """判断联赛是否可被结算（允许全額投注）。

    数据驱动 + 白名单双层判断。

    Args:
        league: 联赛名（中文）
        sport: 运动名（英文）

    Returns:
        True 如果该联赛历史上至少成功结算过 1 次
    """
    if not league:
        return False

    # 层0: 静态白名单 — 已知可结算但尚未有成功记录的联赛
    if league in KNOWN_SETTLEABLE_LEAGUES:
        return True

    # 层1: 历史追踪数据 — 有成功记录才可信
    data = _load_settleable()
    league_data = data.get("leagues", {}).get(league)
    if league_data:
        if league_data.get("successes", 0) > 0:
            return True
        return False

    # 层2: 模糊匹配
    for name, entry in data.get("leagues", {}).items():
        if entry.get("successes", 0) > 0:
            if len(name) >= 4 and name in league:
                return True
            if len(league) >= 4 and league in name:
                return True

    return False


def is_league_probationary(league: str, sport: str = "") -> bool:
    """判断联赛是否处于试用期（可小额测试但不可全額下注）。

    试用期联赛：在 LEAGUE_SPORT_MAP 中有映射，但历史上从未成功结算过。
    允许 ¥10-50 的测试投注，成功结算后自动升级为 settleable。
    """
    if not league:
        return False

    # 已经在白名单中 → 不是试用期
    if is_league_settleable(league, sport):
        return False

    # 检查是否在 LEAGUE_SPORT_MAP 中
    try:
        from src.monitor.auto_settle import LEAGUE_SPORT_MAP, SPORT_FALLBACK
        if league in LEAGUE_SPORT_MAP:
            return True
        if sport and sport in SPORT_FALLBACK:
            return True
        # 模糊匹配：前缀匹配（如 "世界网球 - M15" 和 "世界网球 - W15" 共享 "世界网球" 前缀）
        for mapped_league in LEAGUE_SPORT_MAP:
            if len(mapped_league) >= 4 and len(league) >= 4:
                # 前4字匹配
                if mapped_league[:4] == league[:4]:
                    return True
                # 包含匹配
                if mapped_league in league or league in mapped_league:
                    return True
    except ImportError:
        pass

    return False


def _has_pinnacle_matches(league: str) -> bool:
    """检查联赛是否在最近的 Pinnacle 对比中有匹配。"""
    try:
        from config.settings import DATA_DIR
        comp_path = DATA_DIR / "bb_vs_pinnacle_comparison.json"
        if comp_path.exists():
            data = json.loads(comp_path.read_text())
            for d in data.get("details", []):
                if d.get("league") == league:
                    return True
    except (ImportError, json.JSONDecodeError, OSError):
        pass
    return False


def record_settlement(league: str, success: bool):
    """记录联赛结算结果，自动更新追踪文件。

    由 auto_settle 在每个投注结算后调用。

    Args:
        league: 联赛名
        success: True=成功结算, False=尝试但失败
    """
    if not league:
        return
    data = _load_settleable()
    leagues = data.setdefault("leagues", {})
    entry = leagues.setdefault(league, {"attempts": 0, "successes": 0, "success_rate": 0.0})
    entry["attempts"] += 1
    if success:
        entry["successes"] += 1
    entry["success_rate"] = round(entry["successes"] / entry["attempts"], 3)
    _save_settleable(data)


def record_void(league: str):
    """记录联赛投注被作废（结算失败）。

    Args:
        league: 联赛名
    """
    if not league:
        return
    record_settlement(league, success=False)


def get_settleable_stats() -> dict:
    """获取结算追踪统计，供报告使用。"""
    data = _load_settleable()
    leagues = data.get("leagues", {})
    total_attempts = sum(v["attempts"] for v in leagues.values())
    total_successes = sum(v["successes"] for v in leagues.values())
    return {
        "total_leagues": len(leagues),
        "total_attempts": total_attempts,
        "total_successes": total_successes,
        "overall_rate": round(total_successes / total_attempts, 3) if total_attempts else 1.0,
        "leagues": leagues,
    }


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()

    import argparse
    parser = argparse.ArgumentParser(description="结算可行性追踪")
    parser.add_argument("--check", type=str, help="检查指定联赛是否可结算")
    parser.add_argument("--stats", action="store_true", help="显示追踪统计")
    args = parser.parse_args()

    if args.check:
        league = args.check
        ok = is_league_settleable(league)
        print(f"联赛 \"{league}\": {'✅ 可结算' if ok else '❌ 无法结算'}")
    elif args.stats:
        stats = get_settleable_stats()
        print(f"结算追踪: {stats['total_leagues']} 联赛, "
              f"{stats['total_attempts']} 次尝试, "
              f"成功率 {stats['overall_rate']:.1%}")
        for league, entry in sorted(stats["leagues"].items(), key=lambda x: -x[1]["attempts"]):
            print(f"  {league}: {entry['successes']}/{entry['attempts']} ({entry['success_rate']:.1%})")
    else:
        # 测试常用联赛
        test_leagues = [
            ("美国职业大联盟", "football"),
            ("厄瓜多尔甲级联赛", "football"),
            ("秘鲁甲级联赛", "football"),
            ("NBA", "basketball"),
            ("欧足联欧洲联赛-资格赛", "football"),
            ("世界网球 - W15", "tennis"),
            ("阿根廷全国联赛", "football"),
        ]
        for league, sport in test_leagues:
            ok = is_league_settleable(league, sport)
            print(f"  {league}: {'✅' if ok else '❌'}")
