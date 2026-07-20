import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# 日常运行和缓存均使用 data/storage 目录
DATA_DIR = BASE_DIR / 'data' / 'storage'
RAW_DATA_DIR = BASE_DIR / 'data' / 'raw'
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


def send_dingtalk(title: str, body: str, timeout: int = 10) -> bool:
    """统一钉钉推送，返回 True=成功。

    委托给 config.dingtalk 的直连实现（绕过 Shadowrocket DNS 劫持）。
    自动确保正文包含机器人关键词。所有推送请走此函数。
    """
    from config.dingtalk import send_dingtalk as _real_send
    if not DINGTALK_WEBHOOK:
        return False
    # 确保关键词存在
    if DINGTALK_KEYWORD not in body:
        body = f"**{DINGTALK_KEYWORD} · {title}**\n\n{body}"
    return _real_send(body, msgtype="markdown", title=title)

DEFAULT_BUDGET = int(os.getenv('DEFAULT_BUDGET', '10000'))

# ===== 职业资金管理参数 =====
MAX_SINGLE_BET_PCT = float(os.getenv('MAX_SINGLE_BET_PCT', '0.05'))
MAX_TOTAL_EXPOSURE = float(os.getenv('MAX_TOTAL_EXPOSURE', '0.30'))
KELLY_FRACTION = float(os.getenv('KELLY_FRACTION', '0.25'))
MIN_EDGE = float(os.getenv('MIN_EDGE', '0.03'))

# ===== 自动下注执行配置 =====
BETTING_PLATFORM = os.getenv('BETTING_PLATFORM', 'selenium')
BETFAIR_API_KEY = os.getenv('BETFAIR_API_KEY', '')
BETFAIR_USERNAME = os.getenv('BETFAIR_USERNAME', '')
BETFAIR_PASSWORD = os.getenv('BETFAIR_PASSWORD', '')
BETFAIR_CERT_PATH = os.getenv('BETFAIR_CERT_PATH', '')
SELENIUM_DRIVER_PATH = os.getenv('SELENIUM_DRIVER_PATH', '')
SELENIUM_HEADLESS = os.getenv('SELENIUM_HEADLESS', 'true').lower() == 'true'
SELENIUM_PLATFORM_URL = os.getenv('SELENIUM_PLATFORM_URL', '')
SELENIUM_PLATFORM_USERNAME = os.getenv('SELENIUM_PLATFORM_USERNAME', '')
SELENIUM_PLATFORM_PASSWORD = os.getenv('SELENIUM_PLATFORM_PASSWORD', '')
RACE_API_KEY = os.getenv('RACE_API_KEY', '')
PRE_BET_ODDS_VALIDATION = os.getenv('PRE_BET_ODDS_VALIDATION', 'true').lower() == 'true'
MAX_ODDS_SLIPPAGE = float(os.getenv('MAX_ODDS_SLIPPAGE', '0.05'))
