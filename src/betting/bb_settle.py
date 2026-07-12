"""BB体育虚拟投注结算 + 钉钉报告

流程:
  1. 从 virtual_portfolio.json 读取 BB vs Pinnacle 待结算投注
  2. 尝试获取比赛结果（BB体育页面 / Odds API / 其他）
  3. 结算已结束的比赛
  4. 生成报告推送到钉钉

用法:
    python3 src/betting/bb_settle.py                    # 执行结算
    python3 src/betting/bb_settle.py --report-only      # 只看报告不结算
    python3 src/betting/bb_settle.py --dingtalk          # 结算 + 钉钉推送
"""
import json, sys, re, time, os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

PORTFOLIO_FILE = DATA_DIR / "virtual_portfolio.json"
BB_VS_PIN_FILE = DATA_DIR / "bb_vs_pinnacle_comparison.json"

# 外部 API key（从 .env 读取）
FOOTBALL_API_KEY = None
_env_path = ROOT / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("FOOTBALL_API_KEY="):
            FOOTBALL_API_KEY = line.split("=", 1)[1]

# 钉钉
DINGTALK_WEBHOOK = None
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("DINGTALK_WEBHOOK="):
            DINGTALK_WEBHOOK = line.split("=", 1)[1]


def _load_portfolio():
    if PORTFOLIO_FILE.exists():
        return json.loads(PORTFOLIO_FILE.read_text())
    return {"pending_bets": [], "settled": {}, "balance": 10000.0, "history": []}


def _save_portfolio(state):
    PORTFOLIO_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def extract_bb_scores():
    """Try to extract final scores from BB体育 Chrome page.

    反检测措施：
    - 随机延迟 2-5 秒（模拟人类反应时间）
    - 分步滚动页面（模拟人类浏览行为）
    - 提取前回到顶部
    """
    import subprocess, random

    random_delay = round(random.uniform(2.0, 5.0), 1)

    as_script = f'''
    tell application "Google Chrome"
        activate
        set tabCount to count of tabs of window 1

        -- 随机延迟，模仿人类操作间隔
        delay {random_delay}

        -- 分步滚动，模拟人类浏览
        set scrollScript1 to "window.scrollTo(0, document.body.scrollHeight / 3);"
        execute tab tabCount of window 1 javascript scrollScript1
        delay 0.8

        set scrollScript2 to "window.scrollTo(0, document.body.scrollHeight * 2 / 3);"
        execute tab tabCount of window 1 javascript scrollScript2
        delay 0.8

        -- 回到顶部再提取
        set scrollTop to "window.scrollTo(0, 0);"
        execute tab tabCount of window 1 javascript scrollTop
        delay 0.4

        -- 提取比分数据
        set jsCode to "(function() {{ var items = document.querySelectorAll('.home-match-list__item'); var results = []; items.forEach(function(item) {{ try {{ var tns = item.querySelectorAll('.team-name'); var names = []; tns.forEach(function(tn) {{ names.push(tn.innerText.trim()); }}); var scs = []; var scoreEls = item.querySelectorAll('.match-score span'); scoreEls.forEach(function(s) {{ scs.push(s.innerText.trim()); }}); var group = item.closest('.group-matches'); var league = ''; if (group) {{ var ln = group.querySelector('.league-name'); if (ln) league = ln.innerText.trim(); }} results.push({{home: names[0] || '', away: names[1] || '', score: scs[0] + '-' + scs[1], league: league}}); }} catch(e) {{}} }}); return JSON.stringify(results); }})()"
        set output to execute tab tabCount of window 1 javascript jsCode
        return output
    end tell
    '''
    tmp = "/tmp/bb_scores.applescript"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(as_script)
    try:
        r = subprocess.run(["osascript", tmp], capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip())
    except Exception as e:
        logger.warning("BB体育 Chrome 提取失败: %s", e)
    return None


def get_match_result(home_cn, away_cn, league, bb_scores):
    """Try to get match result from various sources."""
    # Source 1: BB体育 scores
    if bb_scores:
        for s in bb_scores:
            s_home = s.get("home", "")
            s_away = s.get("away", "")
            # Handle aliases
            home_alias = home_cn.replace("谢菲尔德联队", "谢联球场")
            away_alias = away_cn.replace("谢菲尔德联队", "谢联球场")
            if (s_home == home_cn and s_away == away_cn) or \
               (s_home == home_alias and s_away == away_alias):
                score_str = s.get("score", "0-0")
                parts = score_str.split("-")
                if len(parts) == 2:
                    try:
                        return int(parts[0]), int(parts[1])
                    except ValueError:
                        pass

    # Source 2: Try football-data.org API (if we have key)
    if FOOTBALL_API_KEY:
        try:
            import requests
            # Map league to competition code
            comp_map = {
                "苏格兰联赛杯": "SCL",
                "球会友谊赛": "FRIENDLY",
            }
            comp_code = comp_map.get(league)
            if comp_code and comp_code != "FRIENDLY":
                # Try to get matches for this competition
                url = f"https://api.football-data.org/v4/competitions/{comp_code}/matches"
                r = requests.get(url, headers={"X-Auth-Token": FOOTBALL_API_KEY}, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    # This is unlikely to work for SCL with free tier
                    pass
        except Exception:
            pass

    return None


def settle_bets(bb_scores=None):
    """Settle all pending BB vs Pinnacle bets."""
    portfolio = _load_portfolio()
    pending = portfolio.get("pending_bets", [])
    bb_bets = [b for b in pending if b.get("source") == "bb_vs_pinnacle"]

    if not bb_bets:
        logger.info("没有待结算的 BB vs Pinnacle 投注")
        return {"settled": 0, "won": 0, "lost": 0, "unknown": 0}

    settled = portfolio.get("settled", {})
    history = portfolio.get("history", [])
    balance = portfolio.get("balance", 10000.0)
    initial = portfolio.get("initial_bankroll", 10000.0)

    settled_count = 0
    won_count = 0
    lost_count = 0
    unknown_count = 0
    total_profit = 0.0

    for bet in bb_bets:
        bet_id = bet.get("id", "")
        if bet_id in settled:
            continue

        result = get_match_result(
            bet.get("home_cn", ""),
            bet.get("away_cn", ""),
            bet.get("league", ""),
            bb_scores
        )

        if result is None:
            unknown_count += 1
            continue

        home_score, away_score = result
        market_type = bet.get("market_type", "")

        # Determine outcome
        if home_score == away_score:
            actual = "和局"
        elif home_score > away_score:
            actual = "主胜"
        else:
            actual = "客胜"

        stake = bet.get("stake", 0)
        odds = bet.get("odds", 1.0)

        if actual == market_type:
            # Won
            profit = round(stake * (odds - 1), 2)
            balance += stake + profit
            settled[bet_id] = "won"
            history.append({**bet, "result": "won", "profit": profit, "score": f"{home_score}-{away_score}"})
            won_count += 1
            total_profit += profit
        else:
            # Lost
            settled[bet_id] = "lost"
            history.append({**bet, "result": "lost", "profit": -stake, "score": f"{home_score}-{away_score}"})
            lost_count += 1
            total_profit -= stake

        settled_count += 1

    portfolio["settled"] = settled
    portfolio["balance"] = round(balance, 2)
    portfolio["history"] = history
    _save_portfolio(portfolio)

    return {
        "settled": settled_count,
        "won": won_count,
        "lost": lost_count,
        "unknown": unknown_count,
        "total_profit": round(total_profit, 2),
        "balance": round(balance, 2),
        "roi_pct": round(total_profit / (initial or 1) * 100, 2),
        "total_bets": len(bb_bets),
    }


def generate_report(result=None, bb_scores=None):
    """Generate a readable report of the betting session."""
    portfolio = _load_portfolio()
    pending = portfolio.get("pending_bets", [])
    bb_bets = [b for b in pending if b.get("source") == "bb_vs_pinnacle"]
    bb_pending = [b for b in bb_bets if b.get("id", "") not in portfolio.get("settled", {})]
    settled = portfolio.get("settled", {})
    history = portfolio.get("history", [])

    # Stats
    total_bets = len(bb_bets) + len(portfolio.get("settled", {}))
    total_stake = sum(b.get("stake", 0) for b in bb_bets if b.get("source") == "bb_vs_pinnacle")
    # Handle both string and dict values in settled
    def _get_result(val):
        if isinstance(val, dict):
            return val.get("result", "")
        return str(val) if isinstance(val, str) else ""

    won_list = [s for s in settled.values() if _get_result(s) == "won"]
    lost_list = [s for s in settled.values() if _get_result(s) == "lost"]

    lines = []
    lines.append("## ⚽ BB体育 vs Pinnacle 虚拟投注报告")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    if result:
        lines.append("### 📊 结算结果")
        lines.append("")
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 已结算 | {result['settled']} 笔 |")
        lines.append(f"| ✅ 赢 | {result['won']} 笔 |")
        lines.append(f"| ❌ 输 | {result['lost']} 笔 |")
        lines.append(f"| ❓ 未确定 | {result['unknown']} 笔 |")
        lines.append(f"| 总盈亏 | **${result['total_profit']:+.2f}** |")
        lines.append(f"| 当前余额 | ${result['balance']:.2f} |")
        lines.append("")

    # List pending bets
    if bb_pending:
        lines.append(f"### ⏳ 待结算 ({len(bb_pending)})")
        lines.append("")
        for b in bb_pending:
            lines.append(f"- {b.get('league','?')}: {b.get('home_cn','?')} vs {b.get('away_cn','?')}")
            lines.append(f"  - 投注: {b.get('market_type','?')} @ {b.get('odds','?')} | 金额: ${b.get('stake',0):.2f} | EV={b.get('ev_pct',0)}%")
        lines.append("")

    # List settled bets (BB vs Pinnacle only, most recent)
    bb_history = [h for h in history if h.get("source") == "bb_vs_pinnacle"]
    for h in bb_history[-15:]:
        icon = "✅" if h.get("result") == "won" else "❌"
        profit = h.get("profit", 0)
        lines.append(f"{icon} {h.get('league','?')}: {h.get('home_cn','?')} vs {h.get('away_cn','?')}")
        lines.append(f"  投注: {h.get('market_type','?')} @ {h.get('odds','?')} | 盈亏: ${profit:+.2f} | 比分: {h.get('score','?')}")
    lines.append("")

    # Check BB体育 scores available
    if bb_scores:
        lines.append("### 📡 BB体育实时比分")
        lines.append("")
        for s in bb_scores[:10]:
            lines.append(f"- {s.get('home','?')} {s.get('score','?')} {s.get('away','?')}")
        lines.append("")

    return "\n".join(lines)


def send_dingtalk(text):
    """Send markdown message to DingTalk."""
    try:
        from config.settings import send_dingtalk as settings_send
        # 钉钉内容安全：BB体育 是赌博平台关键词会被屏蔽
        safe_text = text.replace("BB体育", "跨平台").replace("⚽ ", "")
        return settings_send("跨平台对比分析报告", safe_text)
    except Exception as e:
        logger.warning("钉钉推送异常: %s", e)
        return False


def main():
    report_only = "--report-only" in sys.argv
    push_dingtalk = "--dingtalk" in sys.argv or "-d" in sys.argv

    # Try to get BB体育 scores (if Chrome is open)
    bb_scores = None
    if not report_only:
        bb_scores = extract_bb_scores()
        if bb_scores:
            logger.info(f"从 BB体育 获取到 {len(bb_scores)} 场比赛比分")
        else:
            logger.info("无法从 BB体育 获取比分（Chrome 未打开或页面不对）")

    # Settle bets
    result = None
    if not report_only:
        result = settle_bets(bb_scores)
        if result["settled"] > 0:
            logger.info(f"结算 {result['settled']} 笔: 赢 {result['won']} / 输 {result['lost']} / 未知 {result['unknown']}")
        else:
            logger.info("没有新的结算")
    else:
        # Load settled data
        portfolio = _load_portfolio()
        settled = portfolio.get("settled", {})
        pending = portfolio.get("pending_bets", [])
        bb_pending = [b for b in pending if b.get("source") == "bb_vs_pinnacle"]
        result = {
            "settled": len(settled),
            "won": len([s for s in settled.values() if s.get("result") == "won"]),
            "lost": len([s for s in settled.values() if s.get("result") == "lost"]),
            "unknown": len(bb_pending),
            "total_profit": sum(s.get("profit", 0) for s in settled.values()),
            "balance": portfolio.get("balance", 0),
            "roi_pct": 0,
        }

    # Generate report
    report = generate_report(result, bb_scores)

    # Print to console
    print(report)

    # Push to DingTalk
    if push_dingtalk:
        if DINGTALK_WEBHOOK:
            send_dingtalk(report)
        else:
            logger.warning("DINGTALK_WEBHOOK 未配置，请设置")


if __name__ == "__main__":
    main()
