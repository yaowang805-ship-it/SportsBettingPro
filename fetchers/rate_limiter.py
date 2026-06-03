import json
import datetime
from pathlib import Path
from config.settings import DATA_DIR

COUNTER_FILE = DATA_DIR / 'api_counter.json'

class APIRateLimiter:
    def __init__(self, name, daily_limit=2):
        self.name = name
        self.daily_limit = daily_limit

    def _load(self):
        if not COUNTER_FILE.exists():
            return {}
        with open(COUNTER_FILE) as f:
            return json.load(f)

    def _save(self, data):
        with open(COUNTER_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    def check_and_increment(self):
        today = str(datetime.date.today())
        data = self._load()
        if data.get('date') != today:
            data = {'date': today, 'counts': {}}
        counts = data.get('counts', {})
        used = counts.get(self.name, 0)
        if used >= self.daily_limit:
            raise RuntimeError(f"{self.name} API 已达每日限制 {self.daily_limit} 次")
        counts[self.name] = used + 1
        data['counts'] = counts
        self._save(data)
        return used + 1

    def remaining(self):
        today = str(datetime.date.today())
        data = self._load()
        if data.get('date') != today:
            return self.daily_limit
        return self.daily_limit - data.get('counts', {}).get(self.name, 0)

    def can_call(self):
        today = str(datetime.date.today())
        data = self._load()
        if data.get('date') != today:
            return True
        used = data.get('counts', {}).get(self.name, 0)
        return used < self.daily_limit
