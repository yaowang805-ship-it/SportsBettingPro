"""Betfair Exchange 执行器 — 基于 betfairlightweight 实现真下单。

流程:
  1. 证书认证登录
  2. 通过赛事 + 队伍 + 市场类型定位 Betfair 市场
  3. 获取实时赔率，验证滑点
  4. 下 LIMIT 订单
  5. 查询订单状态与结算

用法:
    executor = BetfairExecutor()
    result = executor.place_bet(order)
    if result.status == "accepted":
        print(f"已下单: {result.external_id}")

环境变量:
    BETFAIR_USERNAME     — Betfair 用户名
    BETFAIR_PASSWORD     — Betfair 密码
    BETFAIR_APP_KEY      — Betfair 应用密钥
    BETFAIR_CERT_PATH    — 客户端证书路径 (.p12 或 .crt/.key 对)
    BETFAIR_CERT_KEY     — 客户端证书密钥路径（如果与证书分离）
"""

from typing import Optional, Dict, List, Tuple
from datetime import datetime, timedelta
from pathlib import Path

from src.betting.base import BaseExecutor
from src.betting.models import BetOrder, BetResult
from config.logging_config import get_logger
from config.settings import (
    BETFAIR_USERNAME, BETFAIR_PASSWORD, BETFAIR_CERT_PATH,
    BETFAIR_API_KEY, MAX_ODDS_SLIPPAGE, PRE_BET_ODDS_VALIDATION,
)

logger = get_logger(__name__)

# ── Betfair 事件类型 ID ──────────────────────────────────
# 这些是 Betfair Exchange 的固定 ID
EVENT_TYPE_IDS = {
    "americanfootball_nfl": 6422,  # American Football
    "basketball_nba": 7522,        # Basketball
    "basketball_wnba": 7522,
    "basketball_euroleague": 7522,
    "soccer_epl": 1,               # Soccer
    "soccer_spain_la_liga": 1,
    "soccer_germany_bundesliga": 1,
    "soccer_italy_serie_a": 1,
    "soccer_france_ligue_one": 1,
    "soccer_brazil_campeonato": 1,
    "soccer_netherlands_eredivisie": 1,
    "soccer_portugal_primeira_liga": 1,
    "soccer_usa_mls": 1,
    "soccer_england_championship": 1,
}

# Betfair 市场类型 → 我们的 market_type
# Match Odds = h2h, Points Over/Under = totals, Spread = handicaps
MARKET_TYPE_MAP = {
    "h2h": "MATCH_ODDS",
    "win": "MATCH_ODDS",
    "spread": "SPREAD",
    "total": "TOTAL_POINTS",
    "totals": "TOTAL_POINTS",
}

# ── 球队名称映射（Odds API 名称 → Betfair 名称） ──────
# Betfair 的 runner 名称可能与 Odds API 不同
TEAM_NAME_OVERRIDES = {
    # NBA
    "LA Clippers": "LA Clippers",
    "Los Angeles Clippers": "LA Clippers",
    "LA Lakers": "LA Lakers",
    "Los Angeles Lakers": "LA Lakers",
    "Philadelphia 76ers": "Philadelphia 76ers",
    "San Antonio Spurs": "San Antonio Spurs",
    "Golden State Warriors": "Golden State Warriors",
    "Miami Heat": "Miami Heat",
    "Boston Celtics": "Boston Celtics",
    "Milwaukee Bucks": "Milwaukee Bucks",
    "Phoenix Suns": "Phoenix Suns",
    "Brooklyn Nets": "Brooklyn Nets",
    "Denver Nuggets": "Denver Nuggets",
    "Dallas Mavericks": "Dallas Mavericks",
    "New York Knicks": "New York Knicks",
    "Atlanta Hawks": "Atlanta Hawks",
    "Chicago Bulls": "Chicago Bulls",
    "Cleveland Cavaliers": "Cleveland Cavaliers",
    "Houston Rockets": "Houston Rockets",
    "Indiana Pacers": "Indiana Pacers",
    "Memphis Grizzlies": "Memphis Grizzlies",
    "Minnesota Timberwolves": "Minnesota Timberwolves",
    "New Orleans Pelicans": "New Orleans Pelicans",
    "Oklahoma City Thunder": "Oklahoma City Thunder",
    "Orlando Magic": "Orlando Magic",
    "Portland Trail Blazers": "Portland Trail Blazers",
    "Sacramento Kings": "Sacramento Kings",
    "Toronto Raptors": "Toronto Raptors",
    "Utah Jazz": "Utah Jazz",
    "Washington Wizards": "Washington Wizards",
    "Charlotte Hornets": "Charlotte Hornets",
    "Detroit Pistons": "Detroit Pistons",
    # Football (EPL)
    "Manchester United": "Manchester United",
    "Manchester City": "Manchester City",
    "Liverpool": "Liverpool",
    "Chelsea": "Chelsea",
    "Arsenal": "Arsenal",
    "Tottenham": "Tottenham Hotspur",
    "Tottenham Hotspur": "Tottenham Hotspur",
    "Leicester City": "Leicester City",
    "Aston Villa": "Aston Villa",
    "Newcastle United": "Newcastle United",
    "West Ham United": "West Ham United",
    "Everton": "Everton",
    "Wolverhampton Wanderers": "Wolverhampton",
    "Wolves": "Wolverhampton",
    "Brighton & Hove Albion": "Brighton",
    "Brighton": "Brighton",
    "Crystal Palace": "Crystal Palace",
    "Southampton": "Southampton",
    "Fulham": "Fulham",
    "Brentford": "Brentford",
    "Nottingham Forest": "Nottingham Forest",
    "AFC Bournemouth": "Bournemouth",
    "Bournemouth": "Bournemouth",
    "Ipswich Town": "Ipswich",
    # NFL
    "Washington Commanders": "Washington Commanders",
    "Washington Football Team": "Washington Commanders",
}

# ── 下注方向映射 ──────────────────────────────────
BETFAIR_SIDE = {"back": "BACK", "lay": "LAY"}


class BetfairExecutor(BaseExecutor):
    """Betfair Exchange 执行器。

    使用 betfairlightweight 库与 Betfair API 交互。
    未配置凭据时以降级模式运行（返回明确错误而非崩溃）。
    """

    def __init__(self, username: str = "", password: str = "",
                 app_key: str = "", cert_path: str = "",
                 max_slippage: float = MAX_ODDS_SLIPPAGE):
        self.username = username or BETFAIR_USERNAME
        self.password = password or BETFAIR_PASSWORD
        self.app_key = app_key or BETFAIR_API_KEY
        self.cert_path = cert_path or BETFAIR_CERT_PATH
        self.max_slippage = max_slippage
        self._client = None
        self._session_token: Optional[str] = None
        self._last_login_attempt = None
        self._login_cooldown = timedelta(minutes=5)

    # ── 下注方向解析 ──────────────────────────────

    @staticmethod
    def _parse_side(market_detail: str, market_type: str) -> tuple:
        """从 market_detail 解析下注方向。

        Returns:
            (is_home: bool, is_over: bool)
            - is_home: 对主队下注（MATCH_ODDS / SPREAD）
            - is_over: 对大分下注（TOTAL_POINTS）
            MATCH_ODDS/SPREAD 时 is_over 无意义，TOTAL_POINTS 时 is_home 无意义。
        """
        if not market_detail:
            return True, False
        mt = (market_type or "").upper()
        md = market_detail.strip()
        # 总分市场
        if mt in ("TOTAL", "TOTALS", "OVER_UNDER"):
            if md.startswith("小") or "under" in md.lower():
                return False, False
            return False, True  # 大分 / Over
        # 主客方向
        if md.startswith("客") or md.startswith("away"):
            return False, False
        return True, False

    # ── 认证 ──────────────────────────────────────────

    @property
    def _is_configured(self) -> bool:
        """检查是否配置了 Betfair 凭据。"""
        return bool(self.username and self.password and self.app_key)

    def _login(self) -> bool:
        """登录 Betfair Exchange。

        Returns:
            True 登录成功，False 失败（凭据缺失或错误）。
        """
        if not self._is_configured:
            logger.warning("Betfair 未配置: BETFAIR_USERNAME/PASSWORD/APP_KEY 缺失")
            return False

        # 登录冷却
        now = datetime.now()
        if self._last_login_attempt and (now - self._last_login_attempt) < self._login_cooldown:
            return self._session_token is not None
        self._last_login_attempt = now

        try:
            from betfairlightweight import APIClient
            certs = None
            cert_files = None

            if self.cert_path:
                p = Path(self.cert_path)
                if p.exists():
                    if p.suffix == ".p12":
                        certs = str(p)
                    elif p.suffix in (".crt", ".cert", ".pem"):
                        # .crt + .key 对
                        key_path = p.with_suffix(".key")
                        if key_path.exists():
                            cert_files = (str(p), str(key_path))
                        else:
                            cert_files = str(p)

            self._client = APIClient(
                username=self.username,
                password=self.password,
                app_key=self.app_key,
                certs=certs,
                cert_files=cert_files,
            )
            self._client.login()
            self._session_token = self._client.session_token
            logger.info("✅ Betfair 登录成功")
            return True

        except Exception as e:
            logger.warning("⚠️ Betfair 登录失败: %s", e)
            self._client = None
            self._session_token = None
            return False

    def _ensure_logged_in(self) -> bool:
        """确保已登录，必要时重新登录。"""
        if self._client and self._session_token:
            if not self._client.session_expired():
                return True
        return self._login()

    # ── 赛事/市场查找 ──────────────────────────────

    def _resolve_team_name(self, name: str) -> str:
        """将系统队名转换为 Betfair runner 名。"""
        return TEAM_NAME_OVERRIDES.get(name.strip(), name.strip())

    def _get_event_type_id(self, sport_key: str) -> Optional[int]:
        """获取 Betfair 事件类型 ID。"""
        return EVENT_TYPE_IDS.get(sport_key)

    def _build_time_range(self, match_time: Optional[datetime],
                          hours_buffer: int = 6) -> dict:
        """构建赛事时间过滤范围。"""
        if not match_time:
            now = datetime.utcnow()
            return {"from": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to": (now + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        return {
            "from": (match_time - timedelta(hours=hours_buffer)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": (match_time + timedelta(hours=hours_buffer)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def _find_event(self, sport_key: str, home_team: str, away_team: str,
                    match_time: Optional[datetime] = None) -> Optional[dict]:
        """在 Betfair 中定位比赛事件。

        Returns:
            {"event_id": str, "event_name": str, "market_ids": {market_type: market_id}, ...}
            或 None
        """
        if not self._ensure_logged_in():
            return None

        try:
            event_type_id = self._get_event_type_id(sport_key)
            if not event_type_id:
                logger.warning("未知运动类型: %s", sport_key)
                return None

            bf_home = self._resolve_team_name(home_team)
            bf_away = self._resolve_team_name(away_team)

            # 1. 查找事件
            from betfairlightweight.filters import market_filter
            time_range = self._build_time_range(match_time)

            ev_filter = market_filter(
                event_type_ids=[event_type_id],
                market_start_time=time_range,
            )

            events = self._client.betting.list_events(
                filter=ev_filter,
                max_results=100,
            )

            if not events:
                logger.debug("Betfair 未找到赛事: %s vs %s", bf_home, bf_away)
                return None

            # 2. 匹配球队
            matched_event = None
            for ev in events:
                name = ev.event.name.lower() if ev.event and ev.event.name else ""
                if bf_home.lower() in name and bf_away.lower() in name:
                    matched_event = ev
                    break

            if not matched_event:
                logger.debug("Betfair 未匹配到: %s vs %s (在 %d 个事件中)",
                             bf_home, bf_away, len(events))
                return None

            event_id = matched_event.event.id
            logger.info("✅ 定位赛事: %s (id=%s)", matched_event.event.name, event_id)

            # 3. 获取市场的目录
            market_catalogue = self._client.betting.list_market_catalogue(
                filter=market_filter(event_ids=[event_id]),
                market_projection=["COMPETITION", "MARKET_DESCRIPTION", "RUNNER_DESCRIPTION"],
                max_results=50,
            )

            market_ids = {}
            for mc in market_catalogue:
                market_ids[mc.market_name] = {
                    "market_id": mc.market_id,
                    "runners": {r.runner_name: r.selection_id
                                for r in (mc.runners or [])},
                }

            return {
                "event_id": event_id,
                "event_name": matched_event.event.name,
                "market_catalogue": market_catalogue,
                "market_ids": market_ids,
                "bf_home": bf_home,
                "bf_away": bf_away,
            }
        except Exception as e:
            logger.warning("Betfair 赛事查找失败: %s", e)
            return None

    # ── 赔率获取 ──────────────────────────────────

    def fetch_live_odds(self, sport: str, home: str, away: str,
                        market_type: str = "h2h") -> Optional[float]:
        """获取 Betfair 上指定市场的最佳赔率。

        Args:
            sport: sport_key (如 "basketball_nba")
            home, away: 队伍全名
            market_type: h2h / spread / total

        Returns:
            最佳可下注赔率（decimal），或 None
        """
        event = self._find_event(sport, home, away)
        if not event:
            return None

        bf_market = MARKET_TYPE_MAP.get(market_type, "MATCH_ODDS")
        market_info = event["market_ids"].get(bf_market)
        if not market_info:
            logger.debug("Betfair 上无 %s 市场", bf_market)
            return None

        try:
            book = self._client.betting.list_market_book(
                market_ids=[market_info["market_id"]],
                price_projection={"priceData": ["EX_BEST_OFFERS"]},
            )
            if not book:
                return None

            runner_name = event["bf_home"] if market_type in ("h2h", "spread") else "Over"
            runner_id = market_info["runners"].get(runner_name)
            if not runner_id:
                runner_name = event["bf_away"]
                runner_id = market_info["runners"].get(runner_name)

            if not runner_id or not book[0].runners:
                return None

            for runner in book[0].runners:
                if runner.selection_id == runner_id:
                    # 取最佳可下注赔率（ex.best_offer_to_back）
                    if runner.ex and runner.ex.available_to_back:
                        best_price = runner.ex.available_to_back[0].price
                        logger.info("Betfair %s %s: %.2f", bf_market, runner_name, best_price)
                        return float(best_price)
                    # 降级：取 last_price_traded
                    if runner.last_price_traded:
                        return float(runner.last_price_traded)
            return None
        except Exception as e:
            logger.warning("Betfair 获取赔率失败: %s", e)
            return None

    # ── 下单 ──────────────────────────────────────

    def place_bet(self, order: BetOrder) -> BetResult:
        """在 Betfair Exchange 上下单。

        流程:
          1. 定位赛事+市场
          2. 获取实时赔率
          3. 验证滑点（如果启用 PRE_BET_ODDS_VALIDATION）
          4. 下 LIMIT 订单
        """
        # 未配置时返回明确错误
        if not self._is_configured:
            return BetResult(
                prediction_id=order.prediction_id,
                external_id="",
                status="error",
                executed_odds=order.odds,
                executed_stake=0,
                error_message="Betfair 未配置: 设置 BETFAIR_USERNAME/PASSWORD/APP_KEY",
            )

        if not self._ensure_logged_in():
            return BetResult(
                prediction_id=order.prediction_id,
                external_id="",
                status="error",
                executed_odds=order.odds,
                executed_stake=0,
                error_message="Betfair 登录失败",
            )

        try:
            # 从 market_detail 解析下注方向
            is_home, is_over = self._parse_side(order.market_detail, order.market_type)

            # 1. 定位赛事
            event = self._find_event(
                order.sport, order.home_team, order.away_team,
                order.match_time
            )
            if not event:
                return BetResult(
                    prediction_id=order.prediction_id,
                    external_id="",
                    status="rejected",
                    executed_odds=order.odds,
                    executed_stake=0,
                    error_message=f"Betfair 未找到赛事: {order.home_team} vs {order.away_team}",
                )

            # 2. 锁定市场
            market_type_key = order.market_type.upper() if order.market_type else "H2H"
            bf_market_name = MARKET_TYPE_MAP.get(
                market_type_key.lower() if market_type_key else "h2h",
                "MATCH_ODDS"
            )
            market_info = event["market_ids"].get(bf_market_name)
            if not market_info:
                # 尝试其他市场名
                for alt_name in [bf_market_name, "MATCH_ODDS"]:
                    market_info = event["market_ids"].get(alt_name)
                    if market_info:
                        bf_market_name = alt_name
                        break

            if not market_info:
                return BetResult(
                    prediction_id=order.prediction_id,
                    external_id="",
                    status="rejected",
                    executed_odds=order.odds,
                    executed_stake=order.stake,
                    error_message=f"Betfair 上无 {bf_market_name} 市场",
                )

            # 3. 选择 runner（哪支队伍/哪个方向）
            # TOTAL_POINTS → "Over" / "Under"；其他 → 队伍名
            if bf_market_name == "TOTAL_POINTS":
                runner_name = "Over" if is_over else "Under"
            else:
                runner_name = event["bf_home"] if is_home else event["bf_away"]
            selection_id = market_info["runners"].get(runner_name)

            if not selection_id:
                return BetResult(
                    prediction_id=order.prediction_id,
                    external_id="",
                    status="rejected",
                    executed_odds=order.odds,
                    executed_stake=order.stake,
                    error_message=f"Betfair 上未找到 runner: {runner_name}",
                )

            # 4. 获取实时赔率做滑点验证
            live_odds = None
            if PRE_BET_ODDS_VALIDATION:
                live_odds = self.fetch_live_odds(
                    order.sport, order.home_team, order.away_team,
                    market_type_key.lower() if market_type_key else "h2h"
                )
                if live_odds:
                    is_valid, reason = self.validate_odds(order.odds, live_odds, self.max_slippage)
                    if not is_valid:
                        return BetResult(
                            prediction_id=order.prediction_id,
                            external_id="",
                            status="rejected",
                            executed_odds=live_odds,
                            executed_stake=0,
                            error_message=f"滑点验证失败: {reason}",
                        )
                    logger.info("✅ 滑点验证通过: 推荐 %.2f, 实时 %.2f (%s)",
                                order.odds, live_odds, reason)
                else:
                    logger.warning("⚠️ 无法获取 Betfair 实时赔率，使用推荐赔率下单")

            # 5. 下单
            from betfairlightweight.filters import place_instruction, limit_order

            instructions = [place_instruction(
                order_type="LIMIT",
                selection_id=selection_id,
                side="BACK",
                limit_order=limit_order(
                    price=live_odds or order.odds,
                    size=order.stake,
                    persistence_type="LAPSE",  # 赛事开始时若未完全成交则取消
                ),
            )]

            result = self._client.betting.place_orders(
                market_id=market_info["market_id"],
                instructions=instructions,
            )

            # 6. 处理结果
            if result and result.status == "SUCCESS":
                placed = result.place_instructions[0]
                external_id = placed.bet_id if placed.bet_id else ""
                status = "accepted" if placed.status == "EXECUTABLE" else "rejected"
                executed_odds = float(placed.average_price_matched or live_odds or order.odds)
                executed_stake = float(placed.size_matched or 0)

                logger.info("✅ Betfair 下单成功: %s (id=%s, odds=%.2f, stake=%.0f)",
                            status, external_id, executed_odds, executed_stake)
                return BetResult(
                    prediction_id=order.prediction_id,
                    external_id=external_id,
                    status=status,
                    executed_odds=executed_odds,
                    executed_stake=executed_stake,
                )
            else:
                err_msg = result.status if result else "订单返回为空"
                logger.warning("Betfair 下单失败: %s", err_msg)
                return BetResult(
                    prediction_id=order.prediction_id,
                    external_id="",
                    status="error",
                    executed_odds=order.odds,
                    executed_stake=0,
                    error_message=str(err_msg),
                )

        except Exception as e:
            logger.exception("Betfair 下单异常: %s", e)
            return BetResult(
                prediction_id=order.prediction_id,
                external_id="",
                status="error",
                executed_odds=order.odds,
                executed_stake=0,
                error_message=str(e),
            )

    # ── 订单管理 ──────────────────────────────────

    def cancel_bet(self, external_id: str) -> bool:
        """取消未成交订单。"""
        if not self._ensure_logged_in():
            return False
        try:
            from betfairlightweight.filters import cancel_instruction
            instructions = [cancel_instruction(bet_id=external_id)]
            result = self._client.betting.cancel_orders(instructions=instructions)
            if result and result.status == "SUCCESS":
                logger.info("✅ Betfair 取消订单: %s", external_id)
                return True
            logger.warning("Betfair 取消订单失败: %s", external_id)
            return False
        except Exception as e:
            logger.warning("Betfair 取消订单异常: %s", e)
            return False

    def get_bet_status(self, external_id: str) -> str:
        """查询订单状态。

        Returns:
            won / lost / void / pending / unknown
        """
        if not self._ensure_logged_in():
            return "unknown"
        try:
            orders = self._client.betting.list_current_orders(
                bet_ids=[external_id],
            )
            if not orders or not orders.orders:
                return "unknown"

            current = orders.orders[0]
            status_map = {
                "EXECUTABLE": "pending",
                "EXECUTABLE_COMPLETE": "pending",
                "EXECUTION_COMPLETE": "pending",
            }
            return status_map.get(current.status.value, "unknown")
        except Exception as e:
            logger.debug("Betfair 查询订单状态失败: %s", e)
            return "unknown"

    def settle_bet(self, external_id: str) -> Optional[BetResult]:
        """检查已结算订单。

        只能查询已 SETTLED 的订单。未结算返回 None。
        """
        if not self._ensure_logged_in():
            return None
        try:
            cleared = self._client.betting.list_cleared_orders(
                bet_status="SETTLED",
                bet_ids=[external_id],
            )
            if not cleared or not cleared.orders:
                return None

            item = cleared.orders[0]
            profit = float(item.profit if item.profit else 0)
            status = "won" if profit > 0 else "lost" if profit < 0 else "void"

            return BetResult(
                prediction_id="",
                external_id=external_id,
                status=status,
                executed_odds=float(item.price_matched or 0),
                executed_stake=float(item.stake if item.stake else 0),
                profit=profit,
                settled_at=datetime.utcnow(),
            )
        except Exception as e:
            logger.debug("Betfair 结算查询失败: %s", e)
            return None

    # ── 连接管理 ──────────────────────────────────

    def logout(self):
        """显式登出。"""
        if self._client:
            try:
                self._client.client_logout()
                logger.info("Betfair 已登出")
            except Exception:
                pass
            self._client = None
            self._session_token = None
