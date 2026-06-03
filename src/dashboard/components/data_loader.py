"""Cached data loaders with graceful fallback for missing files."""
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from src.dashboard.config import DATA_DIR


@st.cache_data(ttl=60, show_spinner=False)
def load_csv(path: Path, required_cols: Optional[list] = None) -> pd.DataFrame:
    """Load CSV with graceful fallback to empty DataFrame."""
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        if required_cols:
            for c in required_cols:
                if c not in df.columns:
                    df[c] = None
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60, show_spinner=False)
def load_json(path: Path) -> Dict[str, Any]:
    """Load JSON with graceful fallback to empty dict."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return __import__("json").load(f)
    except Exception:
        return {}


def data_exists(path: Path) -> bool:
    """Check if a data file exists and is non-empty."""
    return path.exists() and path.stat().st_size > 10


def render_empty_state(title: str, message: str):
    """Render a centered empty-state placeholder."""
    st.info(f"📭 **{title}**\n\n{message}")
    st.caption("数据将在每日流水线 (`python main.py`) 运行后自动生成。")


def _normalize_rec(rec: dict, source_sport: str = "") -> dict:
    """Normalize a recommendation dict from any source format to unified fields."""
    n = {}
    n["sport"] = source_sport or rec.get("sport", "")
    n["home_cn"] = rec.get("home_cn") or rec.get("home_team", "")
    n["away_cn"] = rec.get("away_cn") or rec.get("away_team", "")
    n["league"] = rec.get("league", "")

    # odds: try home_odds for BB format, then odds
    n["odds"] = float(rec.get("odds") or rec.get("home_odds", 0))
    n["stake"] = float(rec.get("stake", 0))

    # model_prob: win_prob for BB, model_prob for FB/legacy
    n["model_prob"] = float(rec.get("model_prob") or rec.get("win_prob", 0))

    # market_prob: market_home_prob for BB, mkt_prob for FB, market_prob for legacy
    n["market_prob"] = float(rec.get("market_prob") or rec.get("market_home_prob") or rec.get("mkt_prob", 0))

    # edge: win_ev for BB, ev for FB, edge for legacy
    n["edge"] = float(rec.get("edge") or rec.get("ev") or rec.get("win_ev", 0))

    # market type
    mkt_type = rec.get("type", rec.get("market", ""))
    if source_sport.upper() == "NBA" or n.get("sport", "").upper() == "NBA":
        n["market"] = mkt_type or "胜负 主胜"
    else:
        n["market"] = mkt_type or "胜平负 主胜"

    return n


@st.cache_data(ttl=120, show_spinner=False)
def load_recommendations() -> List[dict]:
    """Load and merge recommendations from all fresh sources.

    Order of priority:
      1. daily_bb_recommendations.json (NBA, fresh from daily_bb.py)
      2. daily_fb_recommendations.json (football, fresh from daily_fb.py)
      3. data/storage/daily_recommendations.json (legacy fallback)
    """
    all_recs = []

    # 1. NBA — daily_bb_recommendations.json
    bb_path = DATA_DIR / "daily_bb_recommendations.json"
    bb_data = load_json(bb_path)
    if bb_data:
        bb_recs = bb_data if isinstance(bb_data, list) else bb_data.get("recommendations", [])
        for r in bb_recs:
            all_recs.append(_normalize_rec(r, source_sport="NBA"))

    # 2. Football — daily_fb_recommendations.json
    fb_path = DATA_DIR / "daily_fb_recommendations.json"
    fb_data = load_json(fb_path)
    if fb_data:
        fb_recs = fb_data if isinstance(fb_data, list) else fb_data.get("recommendations", [])
        for r in fb_recs:
            all_recs.append(_normalize_rec(r, source_sport="Football"))

    # 3. Legacy fallback — daily_recommendations.json (only if no fresh data)
    if not all_recs:
        legacy_path = DATA_DIR / "daily_recommendations.json"
        legacy_data = load_json(legacy_path)
        if legacy_data:
            legacy_recs = legacy_data if isinstance(legacy_data, list) else legacy_data.get("recommendations", [])
            for r in legacy_recs:
                all_recs.append(_normalize_rec(r))

    return all_recs
