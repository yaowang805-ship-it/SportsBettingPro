"""V5.1 Pinnacle Odds Archiver — 每次对比拉取赔率自动存档到本地DB

Built, not bought. 每次 bb_vs_pinnacle 拉取 Pinnacle 赔率时自动存档。
一个月可积累 60万+条记录，替代付费数据集。
"""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path("/Users/wangyao/SportsBettingPro/data/storage")
ARCHIVE_DB = DATA_DIR / "pinnacle_odds_archive.db"


def _init():
    conn = sqlite3.connect(str(ARCHIVE_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS odds_archive (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sport TEXT, league_id INTEGER, league_name TEXT, matchup_id INTEGER,
        home TEXT, away TEXT, market_type TEXT, designation TEXT,
        period INTEGER DEFAULT 0, points REAL, price REAL,
        fetched_at TEXT, match_start TEXT,
        UNIQUE(matchup_id, market_type, designation, period, points))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_sport ON odds_archive(sport, league_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_date ON odds_archive(fetched_at)")
    conn.commit()
    return conn


def archive_matchups(sport, league_id, league_name, matchups, markets):
    """Archive matchups to local DB. Called from bb_vs_pinnacle._fetch_one."""
    conn = _init()
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for mu in (matchups or []):
        if not isinstance(mu, dict):
            continue
        mu_id = mu.get("id") or mu.get("matchup_id")
        if not mu_id:
            continue
        participants = mu.get("participants", [])
        parent = mu.get("parent", {})
        parent_parts = parent.get("participants", []) if parent else []
        home, away = "", ""
        for p in (parent_parts or participants):
            a = str(p.get("alignment", ""))
            n = str(p.get("name", ""))
            if "home" in a:
                home = n
            elif "away" in a:
                away = n
        if not home or not away:
            continue
        start = mu.get("startTime") or mu.get("start_time") or ""
        for mkt in mu.get("moneyline", []) + mu.get("spread", []) + mu.get("total", []):
            if not isinstance(mkt, dict):
                continue
            mt = mkt.get("type", mkt.get("market_type", ""))
            pd = mkt.get("period", 0)
            for p in mkt.get("prices", []):
                try:
                    price = float(p.get("price", p.get("decimal", p.get("price_decimal", 0))))
                    if price <= 0:
                        continue
                    des = p.get("designation", "")
                    pts = p.get("points", p.get("handicap"))
                    conn.execute("""INSERT OR IGNORE INTO odds_archive
                        (sport,league_id,league_name,matchup_id,home,away,market_type,designation,period,points,price,fetched_at,match_start)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sport, league_id, league_name, mu_id, home, away,
                         mt, des, pd, pts, price, now, start))
                    inserted += 1
                except (ValueError, TypeError):
                    pass
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM odds_archive").fetchone()[0]
    conn.close()
    return inserted, total


def get_archive_stats():
    conn = sqlite3.connect(str(ARCHIVE_DB))
    total = conn.execute("SELECT COUNT(*) FROM odds_archive").fetchone()[0]
    rows = conn.execute(
        "SELECT sport, COUNT(DISTINCT league_id) as leagues, COUNT(*) as records "
        "FROM odds_archive GROUP BY sport").fetchall()
    conn.close()
    return {"total": total, "by_sport": {s: {"leagues": l, "records": r} for s, l, r in rows}}
