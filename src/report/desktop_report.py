"""桌面报告生成 — 更新 ~/Desktop/已结算统计.txt，含校准检查。"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging, get_logger
from config.settings import DATA_DIR
from src.core.team_names import cn_team
from src.report.validator import validate_output

logger = get_logger(__name__)

OUTCOMES = sorted(['line_shopping', 'over_3.5', 'over_2.5', 'under_3.5', 'under_2.5', 'under_1.5',
                   'home', 'away', 'draw', 'yes', 'no', '1X', 'X2'], key=len, reverse=True)
PREFIXES = {'football', 'line', 'shop', 'World', 'Cup', '2026'}

# ID → (主队, 客队) 手动映射 — 多词队名无法算法解析
TEAM_FIX = {
    'football_World_Cup_2026_South_Africa_South_Korea_home': ('South Africa', 'South Korea'),
    'football_World_Cup_2026_South_Africa_South_Korea_away': ('South Africa', 'South Korea'),
    'line_shop_South_Africa_South_Korea_draw': ('South Africa', 'South Korea'),
    'line_shop_South_Africa_South_Korea_over_3.5': ('South Africa', 'South Korea'),
    'football_World_Cup_2026_Colombia_DR_Congo_away': ('Colombia', 'DR Congo'),
    'football_World_Cup_2026_Colombia_DR_Congo_draw': ('Colombia', 'DR Congo'),
    'line_shop_Bosnia_&_Herzegovina_Qatar_over_3.5': ('Bosnia & Herzegovina', 'Qatar'),
    'line_shop_Bosnia_&_Herzegovina_Qatar_under_1.5': ('Bosnia & Herzegovina', 'Qatar'),
    'line_shop_Bosnia_&_Herzegovina_Qatar_over_2.5': ('Bosnia & Herzegovina', 'Qatar'),
    'line_shop_Bosnia_&_Herzegovina_Qatar_yes': ('Bosnia & Herzegovina', 'Qatar'),
    'line_shop_Bosnia_&_Herzegovina_Qatar_away': ('Bosnia & Herzegovina', 'Qatar'),
    'football_World_Cup_2026_Bosnia_&_Herzegovina_Qatar_home': ('Bosnia & Herzegovina', 'Qatar'),
    # Botola 摩洛哥联赛 — 多段队名
    'line_shop_RS_Berkane_AS_FAR_Rabat_draw': ('RS Berkane', 'AS FAR Rabat'),
    'line_shop_RS_Berkane_AS_FAR_Rabat_home': ('RS Berkane', 'AS FAR Rabat'),
    'line_shop_RS_Berkane_AS_FAR_Rabat_away': ('RS Berkane', 'AS FAR Rabat'),
    'line_shop_Union_Touarga_Sport_Difaâ_Hassani_El-Jadidi_home': ('Union Touarga Sport', 'Difaâ Hassani El-Jadidi'),
    'line_shop_Union_Touarga_Sport_Difaâ_Hassani_El-Jadidi_away': ('Union Touarga Sport', 'Difaâ Hassani El-Jadidi'),
    'line_shop_Union_Touarga_Sport_Difaâ_Hassani_El-Jadidi_draw': ('Union Touarga Sport', 'Difaâ Hassani El-Jadidi'),
}

MKT_CN = {
    'home': '主胜', 'away': '客胜', 'draw': '平局',
    'yes': '双方进球', 'no': '不进球',
    '1X': '主队不败', 'X2': '客队不败',
    'line_shopping': '综合机会',
}


def _market_cn(mt):
    if mt in MKT_CN:
        return MKT_CN[mt]
    p = mt.split('_')
    if p[0] == 'over':
        return '大' + p[1]
    if p[0] == 'under':
        return '小' + p[1]
    return mt


def _parse_teams(bid):
    """从 bet ID 解析出 (主队, 客队, 结果)。"""
    if bid in TEAM_FIX:
        home, away = TEAM_FIX[bid]
        for oc in OUTCOMES:
            if bid.endswith('_' + oc):
                return home, away, oc
        return home, away, ''
    parts = bid.split('_')
    merged = []
    i = 0
    while i < len(parts):
        if parts[i] == '&' and i > 0 and i < len(parts) - 1:
            merged[-1] = merged[-1] + ' & ' + parts[i + 1]
            i += 2
        else:
            merged.append(parts[i])
            i += 1
    parts = merged
    outcome = ''
    for oc in OUTCOMES:
        oc_p = oc.split('_')
        if len(parts) >= len(oc_p) and parts[-len(oc_p):] == oc_p:
            outcome = oc
            parts = parts[:-len(oc_p)]
            break
    while parts and parts[0] in PREFIXES:
        parts.pop(0)
    if not parts:
        return '?', '?', outcome
    if len(parts) == 2:
        return parts[0], parts[1], outcome
    if len(parts) == 3:
        return parts[0], ' '.join(parts[1:]), outcome
    return ' '.join(parts[:-1]), parts[-1], outcome


def _sport_from_bid(bid):
    return 'nba' if ('nba' in bid.lower() or 'basketball' in bid.lower()) else 'football'


def _sport_from_bet(b):
    s = b.get('sport', '')
    l = b.get('league', '')
    return 'nba' if (s in ('nba', 'basketball') or 'nba' in l.lower()) else 'football'


def _fmt_odds(odds):
    """赔率格式：2位精度去尾零。"""
    s = f'{odds:.2f}'
    if s.endswith('.00'):
        return s[:-3]
    return s.rstrip('0').rstrip('.')


def _fmt_time(ct, now):
    if not ct:
        return ''
    try:
        dt = datetime.fromisoformat(ct.replace('Z', '+00:00'))
        h = (dt - now).total_seconds() / 3600
        if h > 48:
            return f'{h:.0f}h后'
        if h > 24:
            return f'{h:.0f}h后'
        if h > 1:
            return f'{h:.0f}h后'
        if h > 0:
            return f'{int(h * 60)}min后'
        if h > -3:
            return '已开赛'
        return '已结束'
    except Exception:
        return ct[:16]


def generate() -> str:
    """生成完整报告文本。"""
    vp_file = DATA_DIR / "virtual_portfolio.json"
    if not vp_file.exists():
        return "❌ virtual_portfolio.json 不存在"

    vp = json.loads(vp_file.read_text())
    history = vp.get('history', [])
    pending = vp.get('pending_bets', [])
    balance = vp.get('balance', 0)
    now = datetime.now(timezone.utc)
    output = []

    # ── 已结算 ──
    output.append('=' * 55)
    output.append('  已结算统计')
    output.append('=' * 55)
    output.append('')

    wins = [h for h in history if h.get('profit', 0) > 0]
    losses = [h for h in history if h.get('profit', 0) < 0]
    n_void = len(history) - len(wins) - len(losses)

    def _write_bets(bets, is_win):
        total_stake = 0
        total_profit = 0
        for i, h in enumerate(bets, 1):
            home, away, oc = _parse_teams(h['id'])
            sp = _sport_from_bid(h['id'])
            h_cn, a_cn = cn_team(home, sp), cn_team(away, sp)
            od = _fmt_odds(h['odds'])
            sk = round(h['stake'])
            pf = h['profit']
            total_stake += h['stake']
            total_profit += pf
            if is_win:
                output.append(f'  {i:>2}. {h_cn} vs {a_cn:<12} | {_market_cn(oc):<6} | ¥{sk:<4} @ {od:<6} | +¥{pf:<.2f}')
            else:
                output.append(f'  {i:>2}. {h_cn} vs {a_cn:<12} | {_market_cn(oc):<6} | ¥{sk:<4} @ {od:<6} | ¥-{abs(pf):<.2f}')
        return total_stake, abs(total_profit)

    output.append(f'【盈利  共 {len(wins)} 笔】')
    output.append('-' * 55)
    tw, pw = _write_bets(wins, True)
    output.append('-' * 55)
    output.append(f' 投入 ¥{tw:.0f}  盈利 +¥{pw:.2f}')
    output.append('')

    output.append(f'【亏损  共 {len(losses)} 笔】')
    output.append('-' * 55)
    tl, pl = _write_bets(losses, False)
    output.append('-' * 55)
    output.append(f' 投入 ¥{tl:.0f}  亏损 ¥-{pl:.2f}')
    output.append('')

    total_stake = tw + tl
    total_profit = pw - pl
    n = len(wins) + len(losses)
    output.append('=' * 55)
    void_note = f' ({n_void}笔作废)' if n_void else ''
    output.append(f' 总笔数: {n:>2}   | {len(wins)}胜 {len(losses)}负{void_note}')
    output.append(f' 胜率:   {len(wins) / n * 100:.1f}%')
    output.append(f' 总投入: ¥{total_stake:.2f}')
    output.append(f' 净利润: ¥{total_profit:+.2f}')
    output.append(f' ROI:    {total_profit / total_stake * 100:+.2f}%')
    output.append('=' * 55)
    output.append('')
    output.append('')

    # ── 未结算 ──
    output.append('=' * 55)
    output.append(f'  未结算统计（{len(pending)} 笔）')
    output.append('=' * 55)
    output.append('')

    pm = defaultdict(list)
    for b in pending:
        home = b.get('home_team', b.get('home_cn', ''))
        away = b.get('away_team', b.get('away_cn', ''))
        sp = _sport_from_bet(b)
        pm[(cn_team(home, sp), cn_team(away, sp))].append(b)

    output.append('各比赛投注详情：')
    output.append('-' * 55)
    i = 0
    tps = 0
    two = 0.0

    for (home, away), bets in sorted(pm.items(), key=lambda x: sum(b.get('stake', 0) for b in x[1]), reverse=True):
        ms = sum(b.get('stake', 0) for b in bets)
        tps += ms
        ct = next((b.get('commence_time', '') for b in bets if b.get('commence_time')), bets[0].get('created_at', ''))
        ts = _fmt_time(ct, now)
        i += 1
        output.append(f'  {i}. {home} vs {away}  |  合计 ¥{ms:.0f}  |  {ts}')
        for b in bets:
            mt = _market_cn(b.get('market_type', ''))
            od = b.get('odds', 0)
            sk = b.get('stake', 0)
            two += od * sk
            output.append(f'     {mt:<10} @ {od:<6} ¥{sk:.0f}')

    avg_odds = two / tps if tps else 0
    output.append('')
    output.append('-' * 55)
    output.append(f' 合计: {len(pending)} 笔投注, 总敞口 ¥{tps:.0f}, 平均赔率 {avg_odds:.2f}')
    output.append(f' 余额: ¥{balance:.2f}')
    output.append('=' * 55)

    return '\n'.join(output)


def update_desktop_file():
    """生成报告并写入桌面文件，含校准检查。"""
    setup_logging()

    text = generate()

    # 校准检查
    issues = validate_output(text, context="桌面报告")
    if issues:
        for iss in issues:
            logger.warning("校准: %s", iss)
        logger.warning("桌面报告包含 %d 个中文化问题，请修复", len(issues))
    else:
        logger.info("校准通过：全部中文")

    desktop = Path.home() / "Desktop" / "已结算统计.txt"
    desktop.write_text(text, encoding='utf-8')
    logger.info("桌面文件已更新: %s", desktop)
    return len(issues)


def main():
    n = update_desktop_file()
    if n:
        print(f"⚠️  校准发现 {n} 个问题")
    else:
        print("✅ 桌面报告已更新，校准通过")


if __name__ == "__main__":
    main()
