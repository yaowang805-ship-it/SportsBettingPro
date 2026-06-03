#!/usr/bin/env python3
"""
每日投注金额验证模块
确保每日推荐总金额达到最小要求。
"""
import logging
from typing import List, Tuple, Optional
from datetime import datetime, date
from config.settings import MIN_DAILY_STAKE as CONFIG_MIN_DAILY_STAKE

logger = logging.getLogger(__name__)

class DailyBudgetValidator:
    """每日投注金额验证器"""
    
    MIN_DAILY_STAKE = CONFIG_MIN_DAILY_STAKE  # 最小每日建议注额（元）
    OPTIMAL_DAILY_STAKE = int(CONFIG_MIN_DAILY_STAKE * 1.2)  # 目标推荐总额（元）
    
    def __init__(self, min_daily_stake: float = MIN_DAILY_STAKE):
        """
        初始化验证器
        
        Args:
            min_daily_stake: 最小每日投注金额（元）
        """
        self.min_daily_stake = min_daily_stake
    
    def validate_recommendations(
        self,
        recommendations: List,
        raise_on_insufficient: bool = False,
    ) -> Tuple[bool, dict]:
        """
        验证推荐是否满足每日最小投注金额要求
        
        Args:
            recommendations: 推荐列表（每个推荐需要 'stake' 字段）
            raise_on_insufficient: 不足时是否抛出异常
            
        Returns:
            tuple: (是否满足, 详细信息字典)
        """
        total_stake = sum(rec.get('stake', 0) for rec in recommendations)
        is_valid = total_stake >= self.min_daily_stake
        
        details = {
            'date': date.today().isoformat(),
            'total_stake': total_stake,
            'required_stake': self.min_daily_stake,
            'is_valid': is_valid,
            'shortage': max(0, self.min_daily_stake - total_stake),
            'surplus': max(0, total_stake - self.min_daily_stake),
            'recommendation_count': len(recommendations),
            'avg_stake_per_bet': total_stake / len(recommendations) if recommendations else 0,
            'status': '✅ 满足要求' if is_valid else '❌ 未达最小值',
        }
        
        if not is_valid and raise_on_insufficient:
            raise ValueError(
                f"日投注金额不足: ¥{total_stake:.0f} < ¥{self.min_daily_stake:.0f}"
                f"（缺¥{details['shortage']:.0f}）"
            )
        
        return is_valid, details
    
    def adjust_stakes(
        self,
        recommendations: List[dict],
        target_stake: Optional[float] = None,
    ) -> List[dict]:
        """
        按比例调整推荐金额以达到目标总额
        
        Args:
            recommendations: 推荐列表
            target_stake: 目标总投注额（元）
            
        Returns:
            list: 调整后的推荐列表
        """
        target_stake = target_stake or self.OPTIMAL_DAILY_STAKE
        
        if not recommendations:
            return recommendations
        
        current_total = sum(rec.get('stake', 0) for rec in recommendations)
        
        if current_total == 0:
            # 平均分配
            stake_per_rec = target_stake / len(recommendations)
            return [
                {**rec, 'stake': stake_per_rec, '_adjusted': True}
                for rec in recommendations
            ]
        
        # 按比例缩放
        scale_factor = target_stake / current_total
        adjusted = []
        
        for rec in recommendations:
            new_stake = rec.get('stake', 0) * scale_factor
            adjusted.append({
                **rec,
                'stake': new_stake,
                '_adjusted': True,
                '_scale_factor': scale_factor,
            })
        
        return adjusted
    
    def format_validation_report(self, details: dict) -> str:
        """
        格式化验证报告
        
        Args:
            details: 验证详情字典
            
        Returns:
            str: 格式化的报告
        """
        lines = [
            f"\n📊 每日投注金额验证报告 ({details['date']})",
            "=" * 60,
            f"推荐数量: {details['recommendation_count']} 条",
            f"总建议注额: ¥{details['total_stake']:.2f}",
            f"平均单笔: ¥{details['avg_stake_per_bet']:.2f}",
            f"最小要求: ¥{details['required_stake']:.2f}",
            "",
            f"验证结果: {details['status']}",
        ]
        
        if details['shortage'] > 0:
            lines.append(f"⚠️ 缺少: ¥{details['shortage']:.2f}")
        elif details['surplus'] > 0:
            lines.append(f"✅ 超额: ¥{details['surplus']:.2f}")
        
        lines.append("=" * 60 + "\n")
        
        return "\n".join(lines)
    
    def get_alert_message(self, details: dict) -> Optional[str]:
        """
        获取需要的提示消息
        
        Args:
            details: 验证详情字典
            
        Returns:
            str: 提示消息（如果需要的话）
        """
        if details['is_valid']:
            return None
        
        shortage = details['shortage']
        return (
            f"⚠️ 每日投注金额不足\n"
            f"当前: ¥{details['total_stake']:.0f}\n"
            f"需要: ¥{details['required_stake']:.0f}\n"
            f"缺少: ¥{shortage:.0f}"
        )


def validate_daily_recommendations(
    nba_recommendations: List[dict],
    football_recommendations: List[dict],
    min_stake: float = CONFIG_MIN_DAILY_STAKE,
) -> dict:
    """
    快捷函数：验证每日推荐总金额
    
    Args:
        nba_recommendations: NBA推荐列表
        football_recommendations: 足球推荐列表
        min_stake: 最小投注金额
        
    Returns:
        dict: 验证结果
    """
    validator = DailyBudgetValidator(min_daily_stake=min_stake)
    
    # 合并推荐
    all_recommendations = nba_recommendations + football_recommendations
    
    is_valid, details = validator.validate_recommendations(all_recommendations)
    
    return {
        'is_valid': is_valid,
        'details': details,
        'validator': validator,
        'all_recommendations': all_recommendations,
    }


if __name__ == "__main__":
    # 测试示例
    logging.basicConfig(level=logging.INFO)
    
    print("🧪 每日投注金额验证模块测试\n")
    
    validator = DailyBudgetValidator(min_daily_stake=10000)
    
    # 测试 1: 不足的情况
    print("测试 1: 不足的推荐")
    insufficient_recs = [
        {'stake': 3000},
        {'stake': 2500},
        {'stake': 2000},
    ]
    
    is_valid, details = validator.validate_recommendations(insufficient_recs)
    print(validator.format_validation_report(details))
    
    alert = validator.get_alert_message(details)
    if alert:
        print(alert)
    
    # 测试 2: 满足的情况
    print("\n测试 2: 满足的推荐")
    sufficient_recs = [
        {'stake': 3000},
        {'stake': 3500},
        {'stake': 4500},
    ]
    
    is_valid, details = validator.validate_recommendations(sufficient_recs)
    print(validator.format_validation_report(details))
    
    # 测试 3: 调整金额
    print("\n测试 3: 调整金额以达到目标")
    original_recs = [
        {'name': 'Rec 1', 'stake': 2000, 'ev': 0.05},
        {'name': 'Rec 2', 'stake': 1500, 'ev': 0.03},
        {'name': 'Rec 3', 'stake': 1000, 'ev': 0.04},
    ]
    
    print(f"原始总额: ¥{sum(r['stake'] for r in original_recs):.0f}")
    
    adjusted = validator.adjust_stakes(original_recs, target_stake=12000)
    print(f"调整后总额: ¥{sum(r['stake'] for r in adjusted):.0f}")
    
    for rec in adjusted:
        print(f"  {rec['name']}: ¥{rec['stake']:.0f} (原 ¥{original_recs[[r['name'] for r in original_recs].index(rec['name'])]['stake']:.0f})")
