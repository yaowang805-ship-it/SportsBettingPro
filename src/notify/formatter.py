#!/usr/bin/env python3
"""
推荐格式化模块 - 统一的推荐消息格式
符合用户需求：中文、主队在前、时间、概率对比、EV、金额
"""
from datetime import datetime
from typing import Dict, List, Optional
from enum import Enum

from config.logging_config import get_logger
logger = get_logger(__name__)

class MarketType(Enum):
    """投注市场类型"""
    WIN = "胜平负"
    SPREAD = "让球"
    TOTAL = "大小球"
    H2H = "胜负"

class Recommendation:
    """单条推荐对象"""

    def __init__(
        self,
        sport: str,
        league: str,
        home_team: str,
        away_team: str,
        market_type: MarketType,
        market_detail: str,
        odds: float,
        model_prob: float,
        market_prob: float,
        ev: float,
        stake: float,
        match_time: Optional[datetime] = None,
        home_is_favorite: bool = True,
        bookmaker: str = "",
    ):
        """
        初始化推荐
        
        Args:
            sport: 运动类型（NBA, 足球）
            league: 联赛
            home_team: 主队名称
            away_team: 客队名称
            market_type: 市场类型
            market_detail: 市场详情（如"主胜"、"让3.5分"等）
            odds: 赔率
            model_prob: 模型概率
            market_prob: 市场概率
            ev: 期望值 (EV)
            stake: 建议注额
            match_time: 比赛时间
            home_is_favorite: 主队是否是热门
        """
        self.sport = sport
        self.league = league
        self.home_team = home_team
        self.away_team = away_team
        self.market_type = market_type
        self.market_detail = market_detail
        self.odds = odds
        self.model_prob = model_prob
        self.market_prob = market_prob
        self.ev = ev
        self.stake = stake
        self.match_time = match_time
        self.home_is_favorite = home_is_favorite
        self.bookmaker = bookmaker

    def format_compact(self) -> str:
        """
        紧凑格式（用于列表）
        
        Returns:
            str: 格式化的推荐字符串
        """
        time_str = f" | {self.match_time.strftime('%Y-%m-%d %H:%M')}" if self.match_time else ""
        
        bm_str = f" | {self.bookmaker}" if self.bookmaker else ""
        return (
            f"• **{self.home_team} vs {self.away_team}**{time_str}\n"
            f"  {self.market_type.value} {self.market_detail} | "
            f"赔率 {self.odds:.2f} | "
            f"模型 {self.model_prob:.1%} vs 市场 {self.market_prob:.1%} | "
            f"EV {self.ev:+.1%} | 💰 ¥{self.stake:.0f}{bm_str}"
        )
    
    def format_detailed(self) -> str:
        """
        详细格式
        
        Returns:
            str: 详细的推荐格式
        """
        time_str = f"🕐 {self.match_time.strftime('%Y年%m月%d日 %H:%M')}" if self.match_time else ""
        
        return (
            f"## {self.home_team} vs {self.away_team}\n"
            f"**【{self.league} · {self.market_type.value}】**\n"
            f"{time_str}\n\n"
            f"📊 **市场分析**\n"
            f"- 投注选项: {self.market_detail}\n"
            f"- 赔率: {self.odds:.2f}\n"
            f"- 模型评估: {self.model_prob:.1%}\n"
            f"- 市场隐含概率: {self.market_prob:.1%}\n"
            f"- 期望值 (EV): {self.ev:+.1%}\n"
            f"- **建议注额: ¥{self.stake:.0f}**"
        )
        if self.bookmaker:
            text += f"\n- 🏦 推荐平台: **{self.bookmaker}**"
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'sport': self.sport,
            'league': self.league,
            'home_team': self.home_team,
            'away_team': self.away_team,
            'market_type': self.market_type.value,
            'market_detail': self.market_detail,
            'odds': float(self.odds),
            'model_prob': float(self.model_prob),
            'market_prob': float(self.market_prob),
            'ev': float(self.ev),
            'stake': float(self.stake),
            'match_time': self.match_time.isoformat() if self.match_time else None,
            'bookmaker': self.bookmaker,
        }


class RecommendationFormatter:
    """推荐格式化器"""
    
    @staticmethod
    def format_recommendations_for_dingtalk(
        recommendations: List[Recommendation],
        title: str = "投注推荐",
        sport_name: str = "",
        max_items: int = 10,
    ) -> str:
        """
        格式化推荐为钉钉 Markdown 消息
        
        Args:
            recommendations: 推荐列表
            title: 消息标题
            sport_name: 运动类型名称
            max_items: 最多显示数量
            
        Returns:
            str: 钉钉 Markdown 格式的消息
        """
        if not recommendations:
            return (
                f"✅ 已完成{sport_name}推荐分析\n\n"
                f"⚠️ 今日未发现符合策略的正期望值投注机会，系统保持谨慎。"
            )
        
        lines = [f"## 【投注推荐】{title}\n"]
        
        # 按EV降序
        sorted_recs = sorted(recommendations, key=lambda x: x.ev, reverse=True)
        
        total_stake = 0
        for i, rec in enumerate(sorted_recs[:max_items], 1):
            lines.append(f"### {i}. {rec.home_team} vs {rec.away_team}")
            lines.append(f"**【{rec.league} · {rec.market_type.value}】**")
            
            if rec.match_time:
                lines.append(f"🕐 {rec.match_time.strftime('%Y年%m月%d日 %H:%M')}")
            
            lines.append(f"投注: {rec.market_detail} | 赔率 {rec.odds:.2f}")
            lines.append(
                f"概率对比: 模型 {rec.model_prob:.1%} > 市场 {rec.market_prob:.1%}"
            )
            lines.append(f"**EV: {rec.ev:+.1%}**")
            lines.append(f"💰 建议注额: **¥{rec.stake:.0f}**")
            if rec.bookmaker:
                lines.append(f"🏦 推荐平台: {rec.bookmaker}")
            lines.append("")
            
            total_stake += rec.stake
        
        # 统计信息
        lines.append("\n---")
        lines.append(f"📊 **总计**: {len(sorted_recs[:max_items])} 条推荐 | "
                    f"总建议注额: ¥{total_stake:.0f}")
        lines.append("*系统自动生成 · 仅供参考*")
        
        return "\n".join(lines)
    
    @staticmethod
    def format_daily_summary(
        nba_recs: List[Recommendation],
        football_recs: List[Recommendation],
    ) -> str:
        """
        格式化每日推荐总结
        
        Args:
            nba_recs: NBA推荐列表
            football_recs: 足球推荐列表
            
        Returns:
            str: 格式化的总结
        """
        lines = [
            "## 【投注推荐】今日全球推荐汇总\n",
            f"📅 {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}\n"
        ]
        
        total_recs = len(nba_recs) + len(football_recs)
        total_stake = sum(r.stake for r in nba_recs) + sum(r.stake for r in football_recs)
        
        # NBA 部分
        if nba_recs:
            lines.append("### 🏀 NBA 推荐")
            for rec in nba_recs[:5]:
                lines.append(
                    f"• {rec.home_team} vs {rec.away_team} | "
                    f"{rec.market_detail} @ {rec.odds:.2f} | "
                    f"EV {rec.ev:+.1%} | ¥{rec.stake:.0f}"
                )
            lines.append("")
        else:
            lines.append("### 🏀 NBA 推荐\n⚠️ 暂无推荐\n")
        
        # 足球部分
        if football_recs:
            lines.append("### ⚽ 足球推荐")
            for rec in football_recs[:5]:
                lines.append(
                    f"• {rec.home_team} vs {rec.away_team} ({rec.league}) | "
                    f"{rec.market_detail} @ {rec.odds:.2f} | "
                    f"EV {rec.ev:+.1%} | ¥{rec.stake:.0f}"
                )
            lines.append("")
        else:
            lines.append("### ⚽ 足球推荐\n⚠️ 暂无推荐\n")
        
        # 统计
        lines.append("\n---")
        lines.append(f"📊 今日统计: {total_recs} 条推荐 | 总建议注额: ¥{total_stake:.0f}")
        lines.append("*系统自动生成 · 仅供参考*")
        
        return "\n".join(lines)
    
    @staticmethod
    def calculate_daily_total(recommendations: List[Recommendation]) -> Dict:
        """
        计算每日投注统计
        
        Args:
            recommendations: 推荐列表
            
        Returns:
            dict: 统计信息
        """
        if not recommendations:
            return {
                'count': 0,
                'total_stake': 0,
                'avg_stake': 0,
                'avg_ev': 0,
                'max_ev': 0,
                'min_ev': 0,
            }
        
        stakes = [r.stake for r in recommendations]
        evs = [r.ev for r in recommendations]
        
        return {
            'count': len(recommendations),
            'total_stake': sum(stakes),
            'avg_stake': sum(stakes) / len(stakes),
            'avg_ev': sum(evs) / len(evs),
            'max_ev': max(evs),
            'min_ev': min(evs),
        }


if __name__ == "__main__":
    # 测试示例
    sample_recs = [
        Recommendation(
            sport="NBA",
            league="NBA",
            home_team="洛杉矶湖人",
            away_team="波士顿凯尔特人",
            market_type=MarketType.WIN,
            market_detail="主胜",
            odds=2.50,
            model_prob=0.52,
            market_prob=0.40,
            ev=0.055,
            stake=500,
            match_time=datetime(2026, 5, 14, 10, 30),
        ),
        Recommendation(
            sport="足球",
            league="英超",
            home_team="曼彻斯特联",
            away_team="阿森纳",
            market_type=MarketType.H2H,
            market_detail="主胜",
            odds=2.80,
            model_prob=0.42,
            market_prob=0.35,
            ev=0.032,
            stake=400,
            match_time=datetime(2026, 5, 14, 20, 0),
        ),
    ]
    
    formatter = RecommendationFormatter()
    logger.info("%s", "=" * 70)
    logger.info("钉钉格式:")
    logger.info("%s", "=" * 70)
    logger.info(formatter.format_recommendations_for_dingtalk(sample_recs, "今日精选推荐"))
    logger.info("\n%s", "=" * 70)
    logger.info("每日总结格式:")
    logger.info("%s", "=" * 70)
    logger.info(formatter.format_daily_summary(sample_recs[:1], sample_recs[1:]))
