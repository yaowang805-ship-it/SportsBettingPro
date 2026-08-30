import json
import os
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / 'data' / 'storage'
MODEL_DIR = BASE_DIR / 'models'

ENV_FILE = BASE_DIR / '.env'


def _load_env_file(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        text = line.strip()
        if not text or text.startswith('#') or '=' not in text:
            continue
        key, value = text.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


_load_env_file(ENV_FILE)

DINGTALK_WEBHOOK = os.getenv('DINGTALK_WEBHOOK')
DINGTALK_KEYWORD = '投注推荐'  # 钉钉机器人关键词，所有消息必须包含
DATABASE_URL = os.getenv('DATABASE_URL', '')  # 空=SQLite, postgresql://user:pass@host/db
# OddsPapi 赔率聚合API(355 bookmaker, 含 Betfair Exchange/Pinnacle/Circa) — 双锚交叉验证用
ODDSPAPI_KEY = os.getenv('ODDSPAPI_KEY', '')
ODDSPAPI_BASE = 'https://api.oddspapi.io/v4'


def _is_placeholder_webhook(url: str) -> bool:
    lower = (url or '').lower()
    return 'your_token' in lower or 'example' in lower or 'replace_me' in lower or 'your_' in lower

if DINGTALK_WEBHOOK and _is_placeholder_webhook(DINGTALK_WEBHOOK):
    DINGTALK_WEBHOOK = None


# 非投注推荐信息每日推送次数上限 (2026-08-17 用户要求: 健康报告/日报等别刷屏)
# 合法日报: 数据日报1 + 结算报告1 + 健康报告2 + CLV日报1 = 5, 留1余量给自愈报告
_NON_BETTING_DAILY_LIMIT = 6
_NON_BETTING_QUOTA_FILE = DATA_DIR / "non_betting_push_quota.json"

# 全局防重复(用户 2026-08-23 铁律: 所有消息都不能短时间内重复发)。
# 非投注消息(告警/日报)按标题节流 —— 同一标题在 _TITLE_COOLDOWN_SEC 内只发一次。
# 投注推荐(title 含 +EV/投注推荐/机会)每次内容都不同, 不受此限制。
_TITLE_COOLDOWN_FILE = DATA_DIR / "dingtalk_title_cooldown.json"
_TITLE_COOLDOWN_SEC = 30 * 60   # 30 分钟


def _is_betting_push(title: str) -> bool:
    """投注推荐(标题含 +EV/投注推荐/机会)不受每日次数限制。"""
    t = title or ""
    return "+EV" in t or "投注推荐" in t or "机会" in t


def _non_betting_quota_ok() -> bool:
    """非投注推荐信息每天最多推 _NON_BETTING_DAILY_LIMIT 次。持久化到文件, 防重启绕过。"""
    today = time.strftime("%Y-%m-%d")
    q = {}
    try:
        q = json.loads(_NON_BETTING_QUOTA_FILE.read_text())
    except (OSError, ValueError):
        pass
    if q.get("date") != today:
        q = {"date": today, "count": 0}
    if q.get("count", 0) >= _NON_BETTING_DAILY_LIMIT:
        return False
    q["count"] = q.get("count", 0) + 1
    try:
        _NON_BETTING_QUOTA_FILE.write_text(json.dumps(q, ensure_ascii=False))
    except OSError:
        pass
    return True


def _title_cooldown_ok(title: str) -> bool:
    """非投注消息同标题短时间防重复: 同一标题 _TITLE_COOLDOWN_SEC 内只发一次。返回 True=可发。"""
    key = (title or "").strip() or "untitled"
    now = time.time()
    m = {}
    try:
        m = json.loads(_TITLE_COOLDOWN_FILE.read_text())
    except (OSError, ValueError):
        pass
    # 清理过期项
    m = {k: v for k, v in m.items() if now - v < _TITLE_COOLDOWN_SEC}
    if key in m:
        return False
    m[key] = now
    try:
        _TITLE_COOLDOWN_FILE.write_text(json.dumps(m, ensure_ascii=False))
    except OSError:
        pass
    return True


def send_dingtalk(title: str, body: str, timeout: int = 10, urgent: bool = False) -> bool:
    """统一钉钉推送，返回 True=成功。

    委托给 config.dingtalk 的直连实现（绕过 Shadowrocket DNS 劫持）。
    自动确保正文包含机器人关键词。所有推送请走此函数
    —— 直接 import config.dingtalk.send_dingtalk 的签名是 (content, msgtype, title)，
    与本函数的 (title, body, timeout) 不兼容且不注入关键词，混用会静默失效
    (2026-08-21 查出 3 处告警因此从未送达)。

    投注推荐(+EV)不受限; 其余信息每天最多推 _NON_BETTING_DAILY_LIMIT 次。

    urgent=True: 故障类告警(看门狗/静默失效/封禁)跳过每日配额 —— 配额是防例行日报
    刷屏的, 不该让故障告警被日报挤掉而静默丢失。
    防重复: 非投注消息同标题 30 分钟内只发一次(用户 2026-08-23 铁律"所有消息都不能
    短时间内重复发"), 投注推荐每次内容不同不受限。
    """
    from config.dingtalk import send_dingtalk as _real_send
    if not DINGTALK_WEBHOOK:
        return False
    # 非投注消息(告警/日报)按标题节流: 同标题短时间只发一次(防自愈看门狗刷屏)
    if not _is_betting_push(title) and not _title_cooldown_ok(title):
        return False
    # 非投注推荐信息每日限流(urgent 故障告警除外)
    if not urgent and not _is_betting_push(title) and not _non_betting_quota_ok():
        return False
    # 确保关键词存在。关键词只作钉钉机器人校验, 不放抬头(抬头应是 title 本身),
    # 否则所有消息抬头都是"投注推荐"(2026-08-25 用户反馈)。
    if DINGTALK_KEYWORD not in body:
        if _is_betting_push(title):
            # 投注推荐正文已有 header(如 "全量扫描·24-72h +EV 机会"), 不重复 prepend title,
            # 否则抬头和正文 header 重复(2026-08-26 用户反馈)。
            body = f"{body}\n\n---\n*（{DINGTALK_KEYWORD}）*"
        else:
            body = f"**{title}**\n\n{body}\n\n---\n*（{DINGTALK_KEYWORD}）*"
    return _real_send(body, msgtype="markdown", title=title)

# 日预算默认值与 config.constants.BANKROLL 保持一致 (¥20,000)
DEFAULT_BUDGET = int(os.getenv('DEFAULT_BUDGET', '20000'))

# ===== 职业资金管理参数 =====
# 单注仓位上限与 config.constants.MAX_STAKE_PCT 保持一致 (6%)
MAX_SINGLE_BET_PCT = float(os.getenv('MAX_SINGLE_BET_PCT', '0.06'))
MAX_TOTAL_EXPOSURE = float(os.getenv('MAX_TOTAL_EXPOSURE', '0.30'))
# Kelly 分数统一由 config.constants.KELLY_FRACTION 控制，此处不再重复定义





# ===== 比分获取器 API keys（供 multi_source_scores.py 使用） =====
ODDS_API_KEYS = [v for k, v in sorted(os.environ.items()) if k.startswith("ODDS_API_KEY_")]
BSD_API_KEY = os.getenv("BSD_API_KEY", "")
BALLDONTLIE_API_KEY = os.getenv("BALLDONTLIE_API_KEY", "")
FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_API_KEY", "")
PRE_BET_ODDS_VALIDATION = os.getenv('PRE_BET_ODDS_VALIDATION', 'true').lower() == 'true'
MAX_ODDS_SLIPPAGE = float(os.getenv('MAX_ODDS_SLIPPAGE', '0.05'))


# ===== 文件安全写入工具 =====
import shutil

def safe_save_json(filepath, data, min_size_bytes=10):
    """原子写入 JSON 文件（tmp→验证→备份→rename）。
    防止写入空数据或损坏导致数据丢失。
    """
    filepath = Path(filepath)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    if len(content) < min_size_bytes:
        raise ValueError(f'拒绝写入 {filepath.name}: 数据太小 ({len(content)} 字节), 可能为空或损坏')

    # 1. 写临时文件
    tmp = filepath.with_suffix(filepath.suffix + '.tmp')
    tmp.write_text(content)

    # 2. 验证临时文件可读
    try:
        json.loads(tmp.read_text())
    except json.JSONDecodeError as e:
        tmp.unlink()
        raise ValueError(f'临时文件验证失败 {filepath.name}: {e}')

    # 3. 备份旧文件
    if filepath.exists():
        bak = filepath.with_suffix(filepath.suffix + '.bak')
        shutil.copy2(str(filepath), str(bak))

    # 4. 原子替换
    tmp.replace(filepath)


def safe_load_json(filepath, default=None, max_age_hours=None):
    """安全加载 JSON，文件损坏/过时时返回 default。"""
    filepath = Path(filepath)
    if not filepath.exists():
        return default

    # 时效性检查
    if max_age_hours:
        age_h = (time.time() - filepath.stat().st_mtime) / 3600
        if age_h > max_age_hours:
            return default

    try:
        data = json.loads(filepath.read_text())
        # 空数据视为损坏
        if isinstance(data, dict) and len(data) == 0:
            return default
        if isinstance(data, list) and len(data) == 0:
            return data  # 空列表可以接受
        return data
    except (json.JSONDecodeError, OSError) as e:
        # 尝试从 .bak 恢复
        bak = filepath.with_suffix(filepath.suffix + '.bak')
        if bak.exists():
            try:
                return json.loads(bak.read_text())
            except Exception:
                pass
        return default
