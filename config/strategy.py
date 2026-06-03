# 策略参数配置文件
# 修改以下数值后，运行 final_master_bets.py 即可生效

# 资金管理
BUDGET = 1000               # 总资金
MAX_SINGLE = 0.08           # 单场最高仓位比例
MAX_TOTAL = 0.30            # 每日总仓位上限
KELLY_FRACTION = 0.25       # 凯利分数（0.25=1/4凯利）

# 模型融合权重（篮球）
MODEL_WEIGHT_BB = 0.50      # 模型信号占比（剩余为市场共识）

# 模型融合权重（足球）
MODEL_WEIGHT_FB = 0.40

# 贝叶斯收缩强度
PRIOR_STRENGTH = 100        # 值越大，模型概率越向市场回归

# 概率偏差上限
MAX_PROB_DEVIATION = 0.10   # 模型概率最多超过市场概率15%

# 最小EV阈值
MIN_EV = 0.0                # 只推荐EV>0的投注
MAX_ODDS = 100.0            # 最大允许赔率（过滤异常值）
MAX_BETS_PER_DAY = 5          # 每日最多推荐5场，分散风险

# 动态凯利：偏差越大，凯利分数越低
KELLY_CONFIDENCE_FACTOR = True  # 是否启用置信度调节

# 足球让球盘专项约束
SPREAD_MODEL_WEIGHT_FB = 0.25   # 让球模型权重单独降至25%
SPREAD_MAX_DEV = 0.08           # 让球概率偏差上限8%
