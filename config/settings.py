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

ODDS_API_KEY = os.getenv('ODDS_API_KEY')
ODDS_API_KEY_2 = os.getenv('ODDS_API_KEY_2') or os.getenv('ODDS_API_KEY_BACKUP') or ''
ODDS_API_KEY_3 = os.getenv('ODDS_API_KEY_3') or ''
ODDS_API_IO_KEY = os.getenv('ODDS_API_IO_KEY')
BASKETBALL_API_KEY = os.getenv('BASKETBALL_API_KEY', ODDS_API_KEY)
FOOTBALL_ODDS_API_KEY = os.getenv('FOOTBALL_ODDS_API_KEY', ODDS_API_KEY)
FOOTBALL_API_KEY = os.getenv('FOOTBALL_API_KEY')
OPENWEATHERMAP_API_KEY = os.getenv('OPENWEATHERMAP_API_KEY')
BDL_API_KEY = os.getenv('BDL_API_KEY')
FOOTBALL_DATA_API_KEY = os.getenv('FOOTBALL_DATA_API_KEY', FOOTBALL_API_KEY)
BSD_API_KEY = os.getenv('BSD_API_KEY')
DINGTALK_WEBHOOK = os.getenv('DINGTALK_WEBHOOK')


def _is_placeholder_webhook(url: str) -> bool:
    lower = (url or '').lower()
    return 'your_token' in lower or 'example' in lower or 'replace_me' in lower or 'your_' in lower

if DINGTALK_WEBHOOK and _is_placeholder_webhook(DINGTALK_WEBHOOK):
    DINGTALK_WEBHOOK = None

SHRINK_BB = float(os.getenv('SHRINK_BB', '0.846'))
SHRINK_FB = float(os.getenv('SHRINK_FB', '0.808'))
DEFAULT_BUDGET = int(os.getenv('DEFAULT_BUDGET', '10000'))
MAX_BET_PCT = float(os.getenv('MAX_BET_PCT', '0.25'))

# ===== 职业资金管理参数 =====
MAX_SINGLE_BET_PCT = float(os.getenv('MAX_SINGLE_BET_PCT', '0.05'))          # 单场最高 5%
MAX_TOTAL_EXPOSURE = float(os.getenv('MAX_TOTAL_EXPOSURE', '0.30'))          # 总仓位上限 30%
KELLY_FRACTION = float(os.getenv('KELLY_FRACTION', '0.25'))              # 1/4 凯利
MIN_EDGE = float(os.getenv('MIN_EDGE', '0.06'))
MIN_DAILY_STAKE = int(os.getenv('MIN_DAILY_STAKE', str(max(100, int(DEFAULT_BUDGET * 0.1)))))   # 最小每日建议注额，默认预算10%
SPORTS_API_TIMEOUT = int(os.getenv('SPORTS_API_TIMEOUT', '30'))

# ===== 自动下注执行配置 =====
BETTING_PLATFORM = os.getenv('BETTING_PLATFORM', 'selenium')         # selenium | betfair
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
MAX_ODDS_SLIPPAGE = float(os.getenv('MAX_ODDS_SLIPPAGE', '0.05'))     # 5% 赔率偏差自动拒绝
