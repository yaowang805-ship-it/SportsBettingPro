#!/usr/bin/env python3
"""
Generate 已结算统计.txt and 待结算投注.txt on Desktop
from virtual_portfolio.json
"""

import json
import os
import re
from datetime import datetime, timezone
from collections import defaultdict

from src.core.team_names import cn_team

# ===== Paths =====
JSON_PATH = '/Users/wangyao/SportsBettingPro/data/storage/virtual_portfolio.json'
SETTLED_PATH = '/Users/wangyao/Desktop/已结算统计.txt'
PENDING_PATH = '/Users/wangyao/Desktop/待结算投注.txt'

# ===== Chinese market name mapping =====
MARKET_CN = {
    'home': '主胜',
    'away': '客胜',
    'draw': '平局',
    'yes': '双方进球',
    'no': '不进球',
    '1X': '主队不败',
    'X2': '客队不败',
    'line_shopping': '综合机会',
}

# ===== English -> Chinese team name mapping (fallback for settled bets) =====
EXTRA_TEAM_CN = {
    'England': '英格兰',
    'Ghana': '加纳',
    'Jordan': '约旦',
    'Algeria': '阿尔及利亚',
    'Portugal': '葡萄牙',
    'Uzbekistan': '乌兹别克斯坦',
    'Panama': '巴拿马',
    'Croatia': '克罗地亚',
    'Colombia': '哥伦比亚',
    'DR Congo': '刚果金',
    'Scotland': '苏格兰',
    'Brazil': '巴西',
    'Morocco': '摩洛哥',
    'Haiti': '海地',
    'Switzerland': '瑞士',
    'Canada': '加拿大',
    'Bosnia & Herzegovina': '波黑',
    'Qatar': '卡塔尔',
    'South Africa': '南非',
    'South Korea': '韩国',
    'Tunisia': '突尼斯',
    'Netherlands': '荷兰',
    'Japan': '日本',
    'Sweden': '瑞典',
    'Ecuador': '厄瓜多尔',
    'Germany': '德国',
    'Czechia': '捷克',
    'Mexico': '墨西哥',
    'Curacao': '库拉索',
    "Cote d'Ivoire": '科特迪瓦',
    "Côte d'Ivoire": '科特迪瓦',
    'Paraguay': '巴拉圭',
    'Australia': '澳大利亚',
    'Turkiye': '土耳其',
    'Türkiye': '土耳其',
    'USA': '美国',
    'Senegal': '塞内加尔',
    'Iraq': '伊拉克',
    'Norway': '挪威',
    'France': '法国',
    'Uruguay': '乌拉圭',
    'Spain': '西班牙',
    'Egypt': '埃及',
    'Iran': '伊朗',
    'Belgium': '比利时',
    'New Zealand': '新西兰',
    'Austria': '奥地利',
    'Argentina': '阿根廷',
    'Cabo Verde': '佛得角',
    'Saudi Arabia': '沙特阿拉伯',
    'Curaçao': '库拉索',
    # Chinese Super League
    'Chongqing Tonglianglong FC': '重庆铜梁龙',
    'Tianjin Jinmen Tiger': '天津津门虎',
    'Shenzhen Peng City': '深圳鹏城',
    'Chengdu Rongcheng': '成都蓉城',
    'Henan FC': '河南队',
    'Shanghai Port': '上海海港',
    'Beijing Guoan': '北京国安',
    'Wuhan Three Towns': '武汉三镇',
    'Qingdao Hainiu': '青岛海牛',
    'Yunnan Yukun': '云南玉昆',
    # Others (keep English if no Chinese available)
    'Rochedale Rovers': 'Rochedale Rovers',
    'Gold Coast Knights': 'Gold Coast Knights',
    'Eastern Suburbs': 'Eastern Suburbs',
    'Olympic FC': 'Olympic FC',
    'RS Berkane': 'RS Berkane',
    'AS FAR Rabat': 'AS FAR Rabat',
    'Union Touarga Sport': 'Union Touarga Sport',
    "Difaâ Hassani El-Jadidi": "Difaâ Hassani El-Jadidi",
}


def load_data():
    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def build_team_map(state):
    """Build English -> Chinese team name map from all available sources."""
    team_cn = dict(EXTRA_TEAM_CN)
    for bet in state.get('pending_bets', []):
        for key in ['home_team', 'away_team']:
            name = bet.get(key, '')
            cn = bet.get(key + '_cn', '') or name
            if cn and name not in team_cn:
                team_cn[name] = cn
    # Fallback: use cn_team() for any remaining English-only names
    for name in list(team_cn.keys()):
        if team_cn[name] == name or not team_cn[name]:
            for sport in ('football', 'nba'):
                cn = cn_team(name, sport)
                if cn != name:
                    team_cn[name] = cn
                    break
    return team_cn


def cn_name(team_map, name):
    """Get Chinese team name, fallback to English."""
    return team_map.get(name, name)


def translate_market(market_type, broad_market=None):
    """Translate a market_type to Chinese display string."""
    if broad_market == 'corners_1x2':
        base = MARKET_CN.get(market_type, market_type)
        return '角球-' + base
    if broad_market == 'btts':
        return MARKET_CN.get(market_type, market_type)
    if market_type.startswith('over_'):
        return '大' + market_type.split('_', 1)[1]
    elif market_type.startswith('under_'):
        return '小' + market_type.split('_', 1)[1]
    return MARKET_CN.get(market_type, market_type)


def format_odds(odds):
    """Format odds nicely: no trailing zeros, at least 1 digit after decimal if not integer."""
    s = f"{odds:.4f}".rstrip('0').rstrip('.')
    # If it was an integer (e.g. 15.00 -> "15"), keep as is
    # If it has decimals (e.g. 1.34 -> "1.34"), keep
    return s


def format_stake(stake):
    """Format stake as integer if whole, or with decimals."""
    if stake == int(stake):
        return str(int(stake))
    return f"{stake:.2f}"


def format_profit(profit):
    """Format profit with sign and ¥."""
    if profit >= 0:
        return f"+¥{profit:.2f}"
    else:
        return f"¥{profit:.2f}"


def parse_team_part(team_str, team_map):
    """Split a team string like 'England_Ghana' into (home_cn, away_cn)
    using known team names for disambiguation."""
    # Build set of ID-form team names (spaces -> underscores)
    id_names = set()
    for eng_name in team_map:
        id_names.add(eng_name.replace(' ', '_'))
    # Sort by length descending for greedy match
    id_names = sorted(id_names, key=lambda x: (len(x), x), reverse=True)

    # Try longest-prefix match for home team
    for tn in id_names:
        if team_str == tn:
            # Single team only
            en = tn.replace('_', ' ')
            return cn_name(team_map, en), ''
        if team_str.startswith(tn + '_'):
            home = tn
            rest = team_str[len(tn) + 1:]
            # Check if rest is a known team (exact match)
            if rest in id_names:
                away_en = rest.replace('_', ' ')
                return cn_name(team_map, home.replace('_', ' ')), cn_name(team_map, away_en)
            # Try to find rest as known team
            for tn2 in id_names:
                if rest == tn2:
                    away_en = tn2.replace('_', ' ')
                    return cn_name(team_map, home.replace('_', ' ')), cn_name(team_map, away_en)

    # Fallback: split into two equal-ish parts
    parts = team_str.split('_')
    mid = len(parts) // 2
    home_en = ' '.join(parts[:mid])
    away_en = ' '.join(parts[mid:])
    return cn_name(team_map, home_en), cn_name(team_map, away_en)


def parse_bet_id(bet_id, team_map):
    """Parse a bet ID to extract (home_cn, away_cn, market_cn)."""
    # Strip known prefixes
    modified = bet_id
    for prefix in ['line_shop_', 'football_World_Cup_2026_']:
        if modified.startswith(prefix):
            modified = modified[len(prefix):]
            break

    # Identify market suffix
    # First try complex over/under markets
    m = re.search(r'_(over|under)_[\d.]+$', modified)
    if m:
        team_str = modified[:m.start()]
        market_str = modified[m.start() + 1:]
    else:
        # Simple markets
        for sm in ['line_shopping', 'home', 'away', 'draw', 'yes', 'no', '1X', 'X2']:
            if modified.endswith('_' + sm):
                team_str = modified[:-(len(sm) + 1)]
                market_str = sm
                break
        else:
            # Cannot identify market, use whole string
            team_str = modified
            market_str = ''

    home_cn, away_cn = parse_team_part(team_str, team_map)
    market_cn = translate_market(market_str)
    return home_cn, away_cn, market_cn


def hours_until(commence_time_str):
    """Calculate hours remaining until commence_time."""
    try:
        dt = datetime.fromisoformat(commence_time_str.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        diff = dt - now
        hours = diff.total_seconds() / 3600
        if hours < 0:
            return None  # already started
        return hours
    except Exception:
        return None


def format_time_remaining(hours):
    """Format hours into human-readable string."""
    if hours is None:
        return '已开赛'
    if hours < 1:
        mins = int(hours * 60)
        return f'{mins}min后'
    if hours < 24:
        return f'{int(hours)}h后'
    days = int(hours / 24)
    h = int(hours % 24)
    return f'{days}d{h}h后'


def generate_settled_file(state, team_map):
    """Generate 已结算统计.txt."""
    history = state.get('history', [])
    pending = state.get('pending_bets', [])

    wins = []
    losses = []
    void_count = 0
    void_stake = 0

    for entry in history:
        profit = entry.get('profit', 0)
        status = entry.get('status', '')
        stake = entry.get('stake', 0)

        bet_id = entry.get('id', '')
        home_cn, away_cn, market_cn = parse_bet_id(bet_id, team_map)

        if status == 'void' or profit == 0:
            void_count += 1
            void_stake += stake
            continue

        item = {
            'home': home_cn,
            'away': away_cn,
            'market': market_cn,
            'stake': stake,
            'odds': entry.get('odds', 0),
            'profit': profit,
        }
        if profit > 0:
            wins.append(item)
        else:
            losses.append(item)

    lines = []
    lines.append('=' * 55)
    lines.append('  已结算统计')
    lines.append('=' * 55)
    lines.append('')

    # --- Winning section ---
    win_stake = sum(w['stake'] for w in wins)
    win_profit = sum(w['profit'] for w in wins)
    lines.append(f'【盈利  共 {len(wins)} 笔】')
    lines.append('-' * 55)
    for i, w in enumerate(wins, 1):
        match_str = f"{w['home']} vs {w['away']}"
        lines.append(
            f"  {i:2d}. {match_str:<22s} | {w['market']:<8s} | "
            f"¥{format_stake(w['stake']):>5s} @ {format_odds(w['odds']):>6s} | "
            f"{format_profit(w['profit'])}"
        )
    lines.append('-' * 55)
    lines.append(f' 投入 ¥{win_stake:.0f}  盈利 +¥{win_profit:.2f}')
    lines.append('')

    # --- Losing section ---
    loss_stake = sum(l['stake'] for l in losses)
    loss_profit = sum(l['profit'] for l in losses)
    lines.append(f'【亏损  共 {len(losses)} 笔】')
    lines.append('-' * 55)
    for i, l in enumerate(losses, 1):
        match_str = f"{l['home']} vs {l['away']}"
        lines.append(
            f"  {i:2d}. {match_str:<22s} | {l['market']:<8s} | "
            f"¥{format_stake(l['stake']):>5s} @ {format_odds(l['odds']):>6s} | "
            f"{format_profit(l['profit'])}"
        )
    lines.append('-' * 55)
    lines.append(f' 投入 ¥{loss_stake:.0f}  亏损 {format_profit(loss_profit)}')
    lines.append('')

    # --- Summary ---
    total_bets = len(wins) + len(losses)
    total_stake = win_stake + loss_stake + void_stake
    total_profit = win_profit + loss_profit  # void contributes 0
    win_rate = (len(wins) / total_bets * 100) if total_bets > 0 else 0
    roi = (total_profit / total_stake * 100) if total_stake > 0 else 0

    lines.append('=' * 55)
    lines.append(
        f' 总笔数: {total_bets}   | {len(wins)}胜 {len(losses)}负 '
        f'({void_count}笔作废)'
    )
    lines.append(f' 胜率:   {win_rate:.1f}%')
    lines.append(f' 总投入: ¥{total_stake:.2f}')
    lines.append(f' 净利润: {format_profit(total_profit)}')
    lines.append(f' ROI:    {roi:+.2f}%')
    lines.append('=' * 55)
    lines.append('')
    lines.append('')

    # --- Pending (未结算) section ---
    lines.append('=' * 55)
    lines.append(f'  未结算统计（{len(pending)} 笔）')
    lines.append('=' * 55)
    lines.append('')
    lines.append('各比赛投注详情：')
    lines.append('-' * 55)

    # Group pending bets by match
    match_groups = defaultdict(list)
    for bet in pending:
        home = team_map.get(bet.get('home_team', ''), bet.get('home_cn', bet.get('home_team', '')))
        away = team_map.get(bet.get('away_team', ''), bet.get('away_cn', bet.get('away_team', '')))
        match_key = f"{home} vs {away}"

        market_type = bet.get('market_type', '')
        broad_market = bet.get('market', None)
        market_cn = translate_market(market_type, broad_market)

        hours = hours_until(bet.get('commence_time', ''))
        time_str = format_time_remaining(hours)

        entry = {
            'match': match_key,
            'home': home,
            'away': away,
            'market': market_cn,
            'stake': bet.get('stake', 0),
            'odds': bet.get('odds', 0),
            'time_remaining': time_str,
            'hours': hours,
        }
        match_groups[match_key].append(entry)

    # Sort match groups: earliest first (None = already started = last)
    def sort_key(item):
        h = item[1][0]['hours']
        if h is None:
            return float('inf')
        return h

    sorted_matches = sorted(match_groups.items(), key=sort_key)

    match_num = 0
    for match_key, bets_in_match in sorted_matches:
        match_num += 1
        total_match_stake = sum(b['stake'] for b in bets_in_match)
        time_str = bets_in_match[0]['time_remaining']

        lines.append(
            f"  {match_num:2d}. {match_key:<24s} | "
            f"合计 ¥{total_match_stake:.0f}  |  {time_str}"
        )

        # Sort bets in match by stake descending
        for b in sorted(bets_in_match, key=lambda x: -x['stake']):
            lines.append(
                f"     {b['market']:<10s} @ {format_odds(b['odds']):>6s}   "
                f"¥{format_stake(b['stake']):>5s}"
            )

    lines.append('-' * 55)
    total_pending_stake = sum(
        bet.get('stake', 0) for bet in pending
    )
    avg_odds = 0
    if pending:
        weighted = sum(
            bet.get('stake', 0) * bet.get('odds', 0) for bet in pending
        )
        avg_odds = weighted / total_pending_stake if total_pending_stake > 0 else 0
    lines.append(
        f' 合计: {len(pending)} 笔投注, 总敞口 ¥{total_pending_stake:.0f}, '
        f'平均赔率 {avg_odds:.2f}'
    )
    balance = state.get('balance', 0)
    lines.append(f' 余额: ¥{balance:.2f}')
    lines.append('=' * 55)

    content = '\n'.join(lines)
    with open(SETTLED_PATH, 'w', encoding='utf-8') as f:
        f.write(content)

    return len(wins) + len(losses) + void_count, len(pending)


def generate_pending_file(state, team_map):
    """Generate 待结算投注.txt."""
    pending = state.get('pending_bets', [])
    balance = state.get('balance', 0)

    if not pending:
        content = '暂无待结算投注。\n'
        with open(PENDING_PATH, 'w', encoding='utf-8') as f:
            f.write(content)
        return 0

    # Group by match
    match_groups = defaultdict(list)
    for bet in pending:
        home = team_map.get(bet.get('home_team', ''), bet.get('home_cn', bet.get('home_team', '')))
        away = team_map.get(bet.get('away_team', ''), bet.get('away_cn', bet.get('away_team', '')))
        match_key = f"{home} vs {away}"

        market_type = bet.get('market_type', '')
        broad_market = bet.get('market', None)
        market_cn = translate_market(market_type, broad_market)
        model_prob = bet.get('model_prob', 0)
        odds = bet.get('odds', 0)
        stake = bet.get('stake', 0)

        # Edge calculations
        if model_prob > 0 and odds > 0:
            edge_pct = (odds * model_prob - 1) * 100
            fair_odds = 1.0 / model_prob
            expected_value = stake * (odds * model_prob - 1)
        else:
            edge_pct = 0
            fair_odds = odds
            expected_value = 0

        entry = {
            'market': market_cn,
            'stake': stake,
            'odds': odds,
            'edge': edge_pct,
            'fair_odds': fair_odds,
            'expected_value': expected_value,
            'model_prob': model_prob,
        }
        match_groups[match_key].append(entry)

    # For each match, get commence_time info
    match_info = {}
    for bet in pending:
        home = team_map.get(bet.get('home_team', ''), bet.get('home_cn', bet.get('home_team', '')))
        away = team_map.get(bet.get('away_team', ''), bet.get('away_cn', bet.get('away_team', '')))
        key = f"{home} vs {away}"
        if key not in match_info:
            ct = bet.get('commence_time', '')
            try:
                dt = datetime.fromisoformat(ct.replace('Z', '+00:00'))
                time_str = dt.strftime('%m-%d %H:%M')
            except Exception:
                time_str = '时间未知'
            hours = hours_until(ct)
            status = '未开赛' if hours is not None and hours > 0 else '已开赛'
            match_info[key] = (time_str, status)

    lines = []
    lines.append('=' * 72)
    lines.append(f'  待结算投注明细')
    lines.append(f'  余额: ¥{balance:.2f}  |  待结算: {len(pending)} 笔')
    lines.append('=' * 72)
    lines.append('')

    total_stake_all = 0
    total_win_all = 0
    total_loss_all = 0

    for match_key, bets_in_match in match_groups.items():
        time_str, status = match_info.get(match_key, ('', ''))
        total_stake = sum(b['stake'] for b in bets_in_match)
        total_win = sum(b['stake'] * (b['odds'] - 1) for b in bets_in_match)
        total_loss = total_stake

        total_stake_all += total_stake
        total_win_all += total_win
        total_loss_all += total_loss

        lines.append('-' * 72)
        lines.append(f'  {match_key}')
        lines.append(f'  开赛: {time_str}   |  本场投入: ¥{total_stake:.0f}')
        lines.append('-' * 72)

        for b in sorted(bets_in_match, key=lambda x: -x['stake']):
            edge_str = f"{b['edge']:+.1f}%" if b['model_prob'] > 0 else "N/A"
            fair_str = f"{b['fair_odds']:.2f}" if b['model_prob'] > 0 else "N/A"
            ev_str = f"¥{b['expected_value']:+.2f}" if b['model_prob'] > 0 else "N/A"

            lines.append(
                f"  {b['market']:<10s} ¥{b['stake']:>6.0f}  @ {b['odds']:<6.2f}  "
                f"Edge {edge_str:<7s}  公平价 {fair_str:<5s}  预期 {ev_str}"
            )

        lines.append(
            f"  合计: 全赢 +¥{total_win:.0f}  全输 -¥{total_loss:.0f}"
        )
        lines.append('')

    lines.append('=' * 72)
    lines.append(
        f'  总计: {len(pending)} 笔  |  投入 ¥{total_stake_all:.0f}  |  '
        f'全赢 +¥{total_win_all:.0f}  |  全输 -¥{total_loss_all:.0f}'
    )
    lines.append(
        f'  余额 ¥{balance:.2f}  ->  最大 ¥{balance + total_win_all:.0f}  /  '
        f'最小 ¥{balance - total_loss_all:.0f}'
    )
    lines.append('=' * 72)

    content = '\n'.join(lines)
    with open(PENDING_PATH, 'w', encoding='utf-8') as f:
        f.write(content)

    return len(pending)


def main():
    state = load_data()
    team_map = build_team_map(state)

    settled_count, pending_count = generate_settled_file(state, team_map)
    pending_count2 = generate_pending_file(state, team_map)

    print(f"已结算统计.txt updated ({settled_count} settled, {pending_count} pending)")
    print(f"待结算投注.txt updated ({pending_count2} pending bets)")
    print(f"Files written to Desktop.")


if __name__ == '__main__':
    main()
