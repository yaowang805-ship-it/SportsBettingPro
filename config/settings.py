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


def _is_placeholder_webhook(url: str) -> bool:
    lower = (url or '').lower()
    return 'your_token' in lower or 'example' in lower or 'replace_me' in lower or 'your_' in lower

if DINGTALK_WEBHOOK and _is_placeholder_webhook(DINGTALK_WEBHOOK):
    DINGTALK_WEBHOOK = None


# 非投注推荐信息每日推送次数上限 (2026-08-17 用户要求: 健康报告/日报等别刷屏)
_NON_BETTING_DAILY_LIMIT = 2
_NON_BETTING_QUOTA_FILE = DATA_DIR / "non_betting_push_quota.json"


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


def send_dingtalk(title: str, body: str, timeout: int = 10) -> bool:
    """统一钉钉推送，返回 True=成功。

    委托给 config.dingtalk 的直连实现（绕过 Shadowrocket DNS 劫持）。
    自动确保正文包含机器人关键词。所有推送请走此函数。
    投注推荐(+EV)不受限; 其余信息每天最多推 2 次。
    """
    from config.dingtalk import send_dingtalk as _real_send
    if not DINGTALK_WEBHOOK:
        return False
    # 非投注推荐信息每日限流
    if not _is_betting_push(title) and not _non_betting_quota_ok():
        return False
    # 确保关键词存在
    if DINGTALK_KEYWORD not in body:
        body = f"**{DINGTALK_KEYWORD} · {title}**\n\n{body}"
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
