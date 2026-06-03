"""Transfermarkt 数据客户端：获取球队市场价值和球员数据。

依赖：pip install transfermarkt-wrapper
"""
import asyncio
import functools
from typing import Optional

import aiohttp
from tmkt import TMKT

# 联赛代码 -> 名称映射（用于 team_search 时限定范围）
LEAGUE_IDS = {
    'GB1': 'Premier League',
    'ES1': 'LaLiga',
    'L1':  'Bundesliga',
    'IT1': 'Serie A',
    'FR1': 'Ligue 1',
}


@functools.lru_cache(maxsize=200)
def get_team_market_value(team_name: str) -> Optional[float]:
    """根据球队名称查询 Transfermarkt 市场价值（单位：EUR）。

    返回 squad 总市值，失败时返回 None。
    结果会缓存，避免重复 API 调用。
    """
    try:
        result = asyncio.run(asyncio.wait_for(_search_team(team_name), timeout=12))
        if result:
            return float(result['mw'])
        return None
    except Exception as e:
        print(f"⚠️ Transfermarkt 查询失败 [{team_name}]: {e}")
        return None


async def _search_team(team_name: str):
    """异步执行 team_search。"""
    c = TMKT()
    try:
        result = await c.team_search(team_name)
        await c.close()
        if result and len(result) > 0:
            # 第一个结果通常是主队（排除青年队、二队）
            for r in result:
                name = r.get('name', '')
                # 过滤掉青年队/二队/女队
                if not any(kw in name for kw in ['U19', 'U21', 'U23', 'U20', 'U18', 'II', 'Women', 'Tula', 'Ceska']):
                    # 检查名字是否匹配（忽略 ~ ID 部分）
                    base_name = name.split(' ~')[0].strip()
                    if team_name.lower() in base_name.lower() or base_name.lower() in team_name.lower():
                        return r
            # 如果没找到精确匹配，返回第一个非青年队结果
            for r in result:
                name = r.get('name', '')
                if not any(kw in name for kw in ['U19', 'U21', 'U23', 'U20', 'U18', 'II', 'Women', 'Tula', 'Ceska']):
                    return r
            return None
        return None
    except Exception:
        await c.close()
        return None


def get_squad_market_values(team_name: str) -> Optional[dict]:
    """获取球队阵容及每个球员的市场价值。

    返回: {'squad_value': float, 'players': [{'name':str, 'position':str, 'market_value':float}, ...]}
    或 None（查询失败）。
    慎用：对 squad 中每个 playerId 再发起一次 API 调用，速度较慢。
    """
    try:
        result = asyncio.run(_fetch_squad_values(team_name))
        return result
    except Exception as e:
        print(f"⚠️ Transfermarkt 阵容查询失败 [{team_name}]: {e}")
        return None


async def _fetch_squad_values(team_name: str):
    """异步获取 squad 市值详情。"""
    c = TMKT()
    try:
        search = await c.team_search(team_name)
        if not search:
            await c.close()
            return None
        club_id = search[0]['id']

        squad_data = await c.get_club_squad(str(club_id))
        if not squad_data.get('success'):
            await c.close()
            return None

        squad = squad_data.get('data', {}).get('squad', [])
        total = 0.0
        players_info = []
        for member in squad:
            pid = member['playerId']
            player = await c.get_player(pid)
            if player.get('success'):
                pdata = player.get('data', {})
                mv = pdata.get('marketValue', 0) or 0
                # 部分 player 返回的 marketValue 可能是 dict
                if isinstance(mv, dict):
                    mv = mv.get('value', 0) or 0
                total += float(mv)
                players_info.append({
                    'name': pdata.get('name', ''),
                    'position': pdata.get('position', ''),
                    'market_value': float(mv),
                })
            await asyncio.sleep(0.1)  # 限速

        await c.close()
        return {'squad_value': total, 'players': players_info}
    except Exception:
        await c.close()
        return None
