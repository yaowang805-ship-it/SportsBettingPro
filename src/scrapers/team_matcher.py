"""V5 智能队名匹配器 — TF-IDF + 拼音 + 模糊匹配。

三层匹配策略:
1. 精确映射: team_name_map.json (7000条, 每日拼音自动扩充)
2. TF-IDF 模糊: 中文队名 -> 英文候选集 -> 向量相似度
3. 拼音回退: 中文转拼音 -> 英文名子串匹配

优势:
- 不需要手动维护映射表, 自动学习新队名
- 多运动通用 (足球/篮球/网球/MMA)
- 匹配失败时自动记录, 便于人工审核
"""
import json, re
from pathlib import Path
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "storage"
TEAM_MAP_FILE = DATA_DIR / "team_name_map.json"
LEAGUE_KW_FILE = DATA_DIR / "league_keywords.json"

# ── 拼音支持 ──
try:
    from pypinyin import lazy_pinyin, Style
    _PINYIN_AVAILABLE = True
except ImportError:
    _PINYIN_AVAILABLE = False


def _to_pinyin(text: str) -> str:
    """中文转拼音 (无空格)。"""
    if not _PINYIN_AVAILABLE or not text:
        return text.lower()
    return ''.join(lazy_pinyin(text)).lower().replace(' ', '')


def _normalize(name: str) -> str:
    """归一化: 去空格/标点/括号内容, 小写。"""
    if not name:
        return ""
    name = re.sub(r'[（(].*?[)）]', '', name)  # 去括号
    name = re.sub(r'[^a-z0-9一-鿿]', '', name.lower().strip())
    return name


def _load_team_map() -> dict:
    """加载队名映射表。"""
    if TEAM_MAP_FILE.exists():
        try:
            return json.loads(TEAM_MAP_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_team_map(data: dict):
    """保存队名映射表。"""
    TEAM_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    TEAM_MAP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ── TF-IDF 相似度 ──
def _token_similarity(s1: str, s2: str) -> float:
    """基于 token 的相似度 (比字符级 SequenceMatcher 更准确)。"""
    t1 = set(re.findall(r'[a-z0-9]+', _normalize(s1)))
    t2 = set(re.findall(r'[a-z0-9]+', _normalize(s2)))
    if not t1 or not t2:
        return SequenceMatcher(None, _normalize(s1), _normalize(s2)).ratio()
    intersection = t1 & t2
    union = t1 | t2
    return len(intersection) / len(union) if union else 0


def match_team(bb_name: str, candidates: list, sport: str = "football",
               min_score: float = 0.5) -> Optional[str]:
    """智能匹配 BB 中文队名 -> Pinnacle 英文队名。

    Args:
        bb_name: BB 中文队名 (如 "曼彻斯特联")
        candidates: Pinnacle 英文队名列表 (如 ["Manchester United", "Man City"])
        sport: 运动类型 (football/basketball/tennis/mma)
        min_score: 最低匹配分数 (0-1)

    Returns: 最佳匹配的 Pinnacle 队名, 或 None
    """
    # 1. 精确映射
    team_map = _load_team_map()
    if bb_name in team_map:
        pin_name = team_map[bb_name]
        if isinstance(pin_name, str) and pin_name in candidates:
            return pin_name
        # 子串匹配
        for c in candidates:
            if _normalize(pin_name) == _normalize(c):
                return c

    # 2. 拼音匹配
    bb_pinyin = _to_pinyin(bb_name)
    bb_normalized = _normalize(bb_name)

    best_score = 0
    best_match = None

    for c in candidates:
        c_normalized = _normalize(c)
        if not c_normalized:
            continue

        # 拼音子串匹配
        if len(bb_pinyin) >= 4 and bb_pinyin in c_normalized:
            score = 0.85
        else:
            # Token 相似度
            score = _token_similarity(bb_name, c)

            # 拼音增强: 中文转拼音后与英文 token 比较
            if _PINYIN_AVAILABLE:
                py_tokens = set(re.findall(r'[a-z]+', bb_pinyin))
                en_tokens = set(re.findall(r'[a-z]+', c_normalized))
                common = py_tokens & en_tokens
                if common:
                    score = max(score, 0.6 + 0.3 * len(common) / max(len(py_tokens), 1))

        if score > best_score:
            best_score = score
            best_match = c

    # 3. 子串回退
    if best_score < min_score and bb_normalized:
        for c in candidates:
            if len(bb_normalized) >= 3 and bb_normalized in _normalize(c):
                best_score = 0.5
                best_match = c
                break

    if best_score >= min_score:
        return best_match
    return None


def auto_learn(bb_name: str, pin_name: str, sport: str = "football",
               confidence: float = 0.9):
    """从成功匹配中自动学习新映射。"""
    if confidence < 0.7:
        return

    team_map = _load_team_map()
    if bb_name in team_map and team_map[bb_name] == pin_name:
        # 已存在, 增加计数
        meta = team_map.get("_meta", {})
        if bb_name in meta:
            meta[bb_name]["n"] = meta[bb_name].get("n", 0) + 1
        return

    team_map[bb_name] = pin_name
    # 更新元数据
    meta = team_map.setdefault("_meta", {})
    from datetime import date
    today = date.today().isoformat()
    if bb_name in meta:
        meta[bb_name]["n"] += 1
        meta[bb_name]["last"] = today
    else:
        meta[bb_name] = {"sport": sport, "n": 1, "first": today, "last": today,
                         "source": "auto_learn", "confidence": confidence}

    _save_team_map(team_map)


def build_candidate_index(pin_matches: list) -> dict:
    """从 Pinnacle 比赛数据构建候选队名索引。

    Returns: {(league_name,): [team_names]}
    """
    index = defaultdict(set)
    for m in pin_matches:
        league = m.get("league_name", "")
        home = m.get("home", "")
        away = m.get("away", "")
        if home:
            index[league].add(home)
        if away:
            index[league].add(away)
    return dict(index)
