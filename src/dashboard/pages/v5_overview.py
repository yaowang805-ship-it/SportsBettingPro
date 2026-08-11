"""V5 实时监控 — P&L, ROI, 运动/盘口拆分."""
import streamlit as st
import json, time
from datetime import datetime
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "storage"

def load_tracked_bets():
    f = DATA_DIR / "tracked_bets.json"
    if f.exists():
        return json.loads(f.read_text()).get("bets", [])
    return []

def render():
    st.title("V5 实时监控")
    bets = load_tracked_bets()
    if not bets:
        st.warning("暂无追踪数据")
        return

    settled = [b for b in bets if b.get("status") == "settled" and b.get("result") in ("won","lost")]
    pending = [b for b in bets if b.get("status") == "pending"]

    # KPI row
    c1,c2,c3,c4,c5 = st.columns(5)
    won = sum(1 for b in settled if b["result"]=="won")
    lost = sum(1 for b in settled if b["result"]=="lost")
    total_stake = sum(b.get("stake",0) for b in settled)
    total_profit = sum(b.get("profit",0) or 0 for b in settled)
    roi = total_profit/total_stake*100 if total_stake>0 else 0

    c1.metric("总投注", f"{len(bets)}笔")
    c2.metric("已结算", f"{len(settled)}笔 ({won}W/{lost}L)")
    c3.metric("胜率", f"{won/(won+lost)*100:.1f}%" if (won+lost)>0 else "N/A")
    c4.metric("ROI", f"{roi:+.1f}%")
    c5.metric("盈亏", f"¥{total_profit:+,.0f}")

    # P&L curve
    if settled:
        st.subheader("累计盈亏")
        cum_profit = []
        cum = 0
        for b in sorted(settled, key=lambda x: x.get("settled_at","")):
            cum += b.get("profit",0) or 0
            cum_profit.append(cum)
        st.line_chart(cum_profit)

    # By sport
    st.subheader("按运动")
    by_sport = defaultdict(lambda: {"bets":0,"stake":0,"profit":0,"won":0,"lost":0})
    for b in settled:
        s = b.get("sport","?")
        by_sport[s]["bets"] += 1
        by_sport[s]["stake"] += b.get("stake",0)
        by_sport[s]["profit"] += b.get("profit",0) or 0
        if b["result"]=="won": by_sport[s]["won"]+=1
        else: by_sport[s]["lost"]+=1
    for s,d in sorted(by_sport.items(), key=lambda x:-x[1]["profit"]):
        wr = d["won"]/(d["won"]+d["lost"])*100 if (d["won"]+d["lost"])>0 else 0
        r = d["profit"]/d["stake"]*100 if d["stake"]>0 else 0
        st.write(f"**{s}**: {d['bets']}笔 WR={wr:.0f}% ROI={r:+.1f}% 盈亏=¥{d['profit']:+,.0f}")

    # By market
    st.subheader("按盘口")
    by_mkt = defaultdict(lambda: {"bets":0,"profit":0})
    for b in settled:
        m = b.get("sub_market","?")
        by_mkt[m]["bets"]+=1
        by_mkt[m]["profit"]+=b.get("profit",0) or 0
    for m,d in sorted(by_mkt.items(), key=lambda x:-x[1]["profit"]):
        st.write(f"**{m}**: {d['bets']}笔 盈亏=¥{d['profit']:+,.0f}")

    # A/B comparison
    st.subheader("A/B 对比 (Simple Kelly vs V5 Matrix)")
    simple_stake = sum(b.get("simple_stake",0) or 0 for b in settled)
    simple_profit = sum(b.get("profit_simple",0) or 0 for b in settled)
    simple_roi = simple_profit/simple_stake*100 if simple_stake>0 else 0
    c1,c2,c3 = st.columns(3)
    c1.metric("Simple Kelly", f"ROI {simple_roi:+.1f}%", f"¥{simple_profit:+,.0f}")
    c2.metric("V5 Matrix", f"ROI {roi:+.1f}%", f"¥{total_profit:+,.0f}")
    c3.metric("差异", f"{roi-simple_roi:+.1f}%")
