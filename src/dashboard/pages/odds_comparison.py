"""赔率对比面板 — 跨 Bookmaker H2H/Spread/Total 对比。"""
import json

import pandas as pd
import streamlit as st

from src.dashboard.config import SNAPSHOT_DIR

_SHARP_BOOKS = frozenset({
    "pinnacle", "betfair", "smarkets", "matchbook", "betcris",
})


def _load_snapshot():
    path = SNAPSHOT_DIR / "last_snapshot.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _is_sharp(book: str) -> bool:
    book_clean = book.lower().replace(" ", "").replace("-", "").replace("_", "")
    return any(s in book_clean for s in _SHARP_BOOKS)


def _best_odds(per_book: dict, home_key: str, away_key: str):
    """返回 (最佳主/主队赔率, 最佳客/客队赔率) 和对应的 bookmaker。"""
    best_home, best_home_book = 0, ""
    best_away, best_away_book = 0, ""
    for book, odds in per_book.items():
        h = odds.get(home_key, 0)
        a = odds.get(away_key, 0)
        if h > best_home:
            best_home, best_home_book = h, book
        if a > best_away:
            best_away, best_away_book = a, book
    return best_home, best_home_book, best_away, best_away_book


def _render_h2h(match_key: str, match: dict):
    per_book = match.get("per_book", {})
    rows = []
    for book, odds in sorted(per_book.items()):
        h = odds.get("h2h_home")
        a = odds.get("h2h_away")
        if h is None and a is None:
            continue
        rows.append({
            "Bookmaker": f"{'⭐ ' if _is_sharp(book) else ''}{book.title()}",
            "主胜": h if h else "",
            "客胜": a if a else "",
        })

    if not rows:
        st.caption("无 H2H 赔率数据")
        return

    df = pd.DataFrame(rows)

    best_home, best_home_book, best_away, best_away_book = _best_odds(
        per_book, "h2h_home", "h2h_away"
    )
    n_book = match.get("n_bookmakers", len(per_book))

    col1, col2, col3 = st.columns(3)
    col1.metric("Bookmaker 数", str(n_book))
    col2.metric("最佳主胜", f"{best_home:.3f}" if best_home else "—")
    col3.metric("最佳客胜", f"{best_away:.3f}" if best_away else "—")

    def _highlight_best(val, col):
        if col == "主胜" and isinstance(val, (int, float)) and val == best_home:
            return "background-color: #1b5e20; color: white"
        if col == "客胜" and isinstance(val, (int, float)) and val == best_away:
            return "background-color: #1b5e20; color: white"
        return ""

    styled = df.style.apply(
        lambda row: [_highlight_best(row["主胜"], "主胜"),
                     _highlight_best(row["客胜"], "客胜")],
        axis=1, subset=["主胜", "客胜"]
    )
    st.dataframe(styled, use_container_width=True, hide_index=True,
                 column_config={"Bookmaker": st.column_config.TextColumn(width="medium")})


def _calc_best_total(per_book: dict):
    """遍历 per_book 找到 total_point 出现最多的线以及其上/下赔率最优值。"""
    lines = {}
    for book, odds in per_book.items():
        tp = odds.get("total_point")
        ov = odds.get("over_odds")
        if tp is not None and ov is not None:
            tp_key = f"{tp:.1f}"
            if tp_key not in lines:
                lines[tp_key] = {"line": tp, "best_over": 0, "best_under": 0}
            if ov > lines[tp_key]["best_over"]:
                lines[tp_key]["best_over"] = ov
            # under = same odds for most books
            un = odds.get("under_odds") or odds.get("over_odds")
            if un and un > lines[tp_key]["best_under"]:
                lines[tp_key]["best_under"] = un
    if not lines:
        return None, None, None
    # pick most common line
    best_line = max(lines.values(), key=lambda x: x["best_over"] + x["best_under"])
    return best_line["line"], best_line["best_over"], best_line["best_under"]


def _render_spread(match_key: str, match: dict):
    per_book = match.get("per_book", {})
    rows = []
    for book, odds in sorted(per_book.items()):
        sp = odds.get("spread_point")
        so = odds.get("spread_odds")
        if sp is None and so is None:
            continue
        rows.append({
            "Bookmaker": f"{'⭐ ' if _is_sharp(book) else ''}{book.title()}",
            "让分数": f"{sp:+.1f}" if sp is not None else "",
            "赔率": so if so else "",
        })

    if not rows:
        st.caption("无让分赔率数据")
        return

    df = pd.DataFrame(rows)
    best_odds = 0
    for r in rows:
        if isinstance(r["赔率"], (int, float)) and r["赔率"] > best_odds:
            best_odds = r["赔率"]

    def _highlight(val):
        if isinstance(val, (int, float)) and val == best_odds:
            return "background-color: #1b5e20; color: white"
        return ""

    styled = df.style.applymap(_highlight, subset=["赔率"])
    st.dataframe(styled, use_container_width=True, hide_index=True,
                 column_config={"Bookmaker": st.column_config.TextColumn(width="medium")})


def _render_total(match_key: str, match: dict):
    per_book = match.get("per_book", {})
    rows = []
    for book, odds in sorted(per_book.items()):
        tp = odds.get("total_point")
        ov = odds.get("over_odds")
        ov_label = odds.get("over_label", "大")
        un_label = odds.get("under_label", "小")
        if tp is None and ov is None:
            continue
        rows.append({
            "Bookmaker": f"{'⭐ ' if _is_sharp(book) else ''}{book.title()}",
            "盘口": f"{tp:.1f}" if tp is not None else "",
            f"{ov_label}赔率": ov if ov else "",
            f"{un_label}赔率": ov if ov else "",  # most books same odds for over/under
        })

    if not rows:
        st.caption("无大小赔率数据")
        return

    df = pd.DataFrame(rows)

    best_over, best_under = 0, 0
    for r in rows:
        if isinstance(r.get(f"{ov_label}赔率"), (int, float)):
            best_over = max(best_over, r[f"{ov_label}赔率"])
        if isinstance(r.get(f"{un_label}赔率"), (int, float)):
            best_under = max(best_under, r[f"{un_label}赔率"])

    def _highlight(val, col):
        if col == f"{ov_label}赔率" and isinstance(val, (int, float)) and val == best_over:
            return "background-color: #1b5e20; color: white"
        if col == f"{un_label}赔率" and isinstance(val, (int, float)) and val == best_under:
            return "background-color: #1b5e20; color: white"
        return ""

    styled = df.style.apply(
        lambda row: [_highlight(row[f"{ov_label}赔率"], f"{ov_label}赔率"),
                     _highlight(row[f"{un_label}赔率"], f"{un_label}赔率")],
        axis=1, subset=[f"{ov_label}赔率", f"{un_label}赔率"]
    )
    st.dataframe(styled, use_container_width=True, hide_index=True,
                 column_config={"Bookmaker": st.column_config.TextColumn(width="medium")})


def render():
    st.header("📋 赔率对比")

    snapshot = _load_snapshot()
    if not snapshot:
        st.warning("暂无赔率快照 — 运行 `python main.py` 或 `python fetchers/odds_api.py` 生成。")
        return

    # 按运动类型分组（使用数据特征推断）
    nba_matches = {}
    football_matches = {}
    other_matches = {}
    for k, v in snapshot.items():
        has_spread = v.get("spread_point") is not None
        n_book = v.get("n_bookmakers", 0)
        # NBA 比赛有 spread + 更多 bookmaker
        if has_spread and n_book >= 7:
            nba_matches[k] = v
        elif has_spread:
            nba_matches[k] = v
        elif any(team in k for team in ("FC ", " AFC", "BVB", "Real ", "FC ", "United", "City", " Juventus")):
            football_matches[k] = v
        elif not has_spread:
            football_matches[k] = v
        else:
            other_matches[k] = v

    # 盘口切换
    market_mode = st.radio(
        "盘口类型",
        ["H2H 胜负", "Spread 让分", "Total 大小"],
        horizontal=True,
    )

    def _render_group(title: str, matches: dict):
        if not matches:
            return
        st.subheader(title)
        sorted_matches = sorted(
            matches.items(),
            key=lambda x: x[1].get("commence_time", ""),
        )
        for match_key, match in sorted_matches:
            home = match.get("home_team", "")
            away = match.get("away_team", "")
            ct = match.get("commence_time", "")
            ts = ""
            if ct:
                try:
                    dt = pd.to_datetime(ct).tz_convert("Asia/Shanghai")
                    ts = dt.strftime("%m/%d %H:%M")
                except Exception:
                    ts = ct[:16]

            label = f"{home} vs {away}"
            if ts:
                label += f"  ⏰{ts}"

            with st.expander(label):
                if market_mode == "H2H 胜负":
                    _render_h2h(match_key, match)
                elif market_mode == "Spread 让分":
                    _render_spread(match_key, match)
                else:
                    _render_total(match_key, match)

    _render_group("🏀 NBA", nba_matches)
    _render_group("⚽ 足球", football_matches)
    _render_group("🌍 其他", other_matches)

    # 快照时间
    ts = max(
        (m.get("timestamp", "") for m in snapshot.values()),
        default="",
    )
    if ts:
        st.caption(f"快照时间: {ts[:19].replace('T', ' ')}")
