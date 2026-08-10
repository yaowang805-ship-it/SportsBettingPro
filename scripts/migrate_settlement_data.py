"""Migrate old virtual_portfolio.json → tracked_bets.json.

One-time script to import historical settlement data into the new tracking system.
"""
import json, re, time
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "storage"
VP_FILE = DATA_DIR / "virtual_portfolio.json"
TB_FILE = DATA_DIR / "tracked_bets.json"


def _parse_market(record):
    """Parse market type and sub_market from old record."""
    market_str = record.get("market_type", "")
    if "让分" in market_str or "让球" in market_str:
        sub = "hc"
    elif "大分" in market_str or "小分" in market_str or "大球" in market_str or "小球" in market_str:
        sub = "ou"
    elif "双重" in market_str:
        sub = "dc"
    else:
        sub = "1x2"
    return market_str, sub


def _extract_teams_from_id(record):
    """Try to extract home/away/designation from old id format."""
    rid = record.get("id", "")
    home = record.get("home_cn", "")
    away = record.get("away_cn", "")
    market = record.get("market_type", "")
    league = record.get("league", "")
    sport = record.get("sport", "unknown")
    if not home and not away:
        # Parse from id like "bb_vs_pin_1x2_纳什维尔_亚特兰大联_客胜"
        parts = rid.split("_")
        if len(parts) >= 4:
            sport_map = {"1x2": "football", "handicap": "basketball", "ou": "football"}
            home = parts[-3] if len(parts) >= 4 else ""
            away = parts[-2] if len(parts) >= 3 else ""
    return home, away, market, league, sport


def migrate():
    print("=" * 60)
    print("Migrating virtual_portfolio.json → tracked_bets.json")
    print("=" * 60)

    # Load old data
    vp = json.loads(VP_FILE.read_text())
    history = vp.get("history", [])
    print(f"Old records: {len(history)}")

    # Load or create new data
    if TB_FILE.exists():
        tb = json.loads(TB_FILE.read_text())
    else:
        tb = {"bets": [], "meta": {"created": datetime.now(timezone.utc).isoformat()}}

    existing_ids = {b["push_id"] for b in tb["bets"]}
    migrated = 0
    skipped = 0

    for rec in history:
        rid = rec.get("id", "")
        if not rid:
            skipped += 1
            continue

        # Generate push_id
        home, away, market, league, sport = _extract_teams_from_id(rec)
        market_type, sub_market = _parse_market(rec)
        push_id = f"{sport}|{league or '?'}|{home or '?'}|{away or '?'}|{market or '?'}|{sub_market}|migrated"
        # Simplify to avoid dupes
        push_id = rid[:80]

        if push_id in existing_ids:
            skipped += 1
            continue

        # Determine result
        result = rec.get("result")
        if not result or result in ("?", "unknown"):
            status = rec.get("status", "")
            if status in ("won", "lost", "void", "push"):
                result = status
                if result == "push":
                    result = "void"
            else:
                result = "pending"

        status = "settled" if result in ("won", "lost", "void", "half_won", "half_lost") else "pending"

        # Parse date
        date_str = rec.get("date", "")
        try:
            ts = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            push_time = ts.isoformat()
        except (ValueError, TypeError):
            push_time = ""

        bet = {
            "push_id": push_id,
            "push_time": push_time,
            "push_label": "历史迁移",
            "sport": sport or "unknown",
            "league": league or "",
            "home": home or "",
            "away": away or "",
            "home_pin": "",
            "away_pin": "",
            "designation": market or "",
            "sub_market": sub_market,
            "bb_odds": rec.get("odds", 0),
            "pin_odds": 0,
            "fair_price": 0,
            "ev_pct": 0,
            "stake": rec.get("stake", 0),
            "kelly_pct": 0,
            "tier": 0,
            "match_score": 0,
            "match_epoch": 0,
            "match_time_bb": "",
            "match_time_pin": "",
            "bb_price_source": rec.get("source", "BB"),
            "status": status,
            "result": result if status == "settled" else None,
            "settled_at": date_str if status == "settled" else None,
            "home_score": None,
            "away_score": None,
            "profit": rec.get("profit", 0),
            "settle_source": "migrated",
            "settle_attempts": 1 if status == "settled" else 0,
            "last_settle_attempt": date_str if status == "settled" else None,
        }
        tb["bets"].append(bet)
        existing_ids.add(push_id)
        migrated += 1

    tb["meta"]["migrated_from_vp"] = datetime.now(timezone.utc).isoformat()
    tb["meta"]["migrated_count"] = migrated

    tmp = TB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tb, ensure_ascii=False, indent=2))
    tmp.replace(TB_FILE)

    # Count results
    settled = [b for b in tb["bets"] if b.get("status") == "settled"]
    won = sum(1 for b in settled if b.get("result") == "won")
    lost = sum(1 for b in settled if b.get("result") == "lost")
    void = sum(1 for b in settled if b.get("result") in ("void", "push"))
    total_stake = sum(b["stake"] for b in settled)
    total_profit = sum(b.get("profit", 0) or 0 for b in settled)
    roi = total_profit / total_stake * 100 if total_stake > 0 else 0

    print(f"\nMigrated: {migrated}, Skipped: {skipped}")
    print(f"Total tracked: {len(tb['bets'])}")
    print(f"Settled: {len(settled)} ({won}W/{lost}L/{void}V)")
    print(f"ROI: {roi:.1f}% | Profit: ¥{total_profit:,.0f}")


if __name__ == "__main__":
    migrate()
