# SportsBettingPro - 职业级博彩系统

## 系统概要

这是一个**媲美职业博彩团队的AI驱动投注系统**，覆盖 NBA、足球五大联赛、世界杯和欧洲杯，集数据采集、特征工程、机器学习模型、风险管理、半自动下单、月度重训练为一体。

---

## 核心特性

### 1. **数据驱动的预测**
- NBA: 基于进攻/防守效率、节奏、主客场、B2B等特征
- 足球: 基于xG、伤停情况、球队势头、赛程负荷、历史交手等
- 国际赛事: 世界杯/欧洲杯专用特征（小组赛vs淘汰赛、国家队稳定性等）

### 2. **职业级风险管理**
- **凯利公式**: 根据期望值和概率自动计算最优下注额
- **仓位控制**: 单场最高5%，总仓位不超30%
- **回撤限制**: 日限-10%，月限-25%，自动停盘告警
- **市场概率混合**: 70%市场概率 + 30%模型概率，降低模型过拟合风险

### 3. **自动重训练与月度回测**
- 每30天自动检测并重新训练所有模型
- 计算 Brier Score、Accuracy、Logloss 等性能指标
- 历史回测验证策略稳定性

### 4. **半自动下单系统**
- 生成每日推荐并保存为JSON
- 交互式命令行审核每一条推荐
- 记录下注结果、损益、备注
- 实时风险检查（仓位、回撤、损失限额）

### 5. **钉钉实时通知**
- 每日推荐摘要
- 模型健康度告警
- 投资组合状态更新

---

## 快速开始

### 1. 配置环境变量
```bash
cp .env.example .env
```

编辑 `.env` 文件，填入：
```
ODDS_API_KEY=your_key_here
DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=...
DEFAULT_BUDGET=1000
KELLY_FRACTION=0.25
MIN_EDGE=0.06
```

### 2. 完整部署（首次）
```bash
bash full_deploy.sh
```
- 训练篮球和足球模型
- 运行回测评估
- 生成今日推荐

### 3. 每日自动化
```bash
bash run_daily.sh
# 或
python main.py
```
依次执行：
- 自动模型重训练（如需要）
- 职业级推荐生成
- 风险检查
- 钉钉通知

### 4. 半自动投注审核
```bash
python src/predict/semi_auto_bettor.py
```
交互式流程：
- [1] 显示今日推荐
- [2] 记录投注结果（won/lost/pending）
- [3] 查看投资组合状态
- [4] 退出

---

## 目录结构

```
SportsBettingPro/
├── config/
│   ├── settings.py          # 全局配置（支持.env）
│   └── strategy.py
├── src/
│   ├── core/
│   │   ├── models.py        # 标准化比赛/赔率模型
│   │   ├── risk.py          # 期望值、凯利计算
│   │   ├── evaluation.py    # Brier、Sharpe等评估指标
│   │   └── normalizer.py    # 赔率API标准化
│   ├── features/
│   │   ├── bb_pipeline.py        # NBA特征生成
│   │   ├── football_pipeline.py  # 足球特征生成
│   │   └── tournament_pipeline.py # 世界杯/欧洲杯特征
│   ├── models/
│   │   ├── train_models.py       # 统一训练器
│   │   ├── auto_retrain.py       # 月度自动重训练
│   │   ├── train_all.py          # 备用训练脚本
│   │   └── train_football.py     # 足球模型训练
│   ├── predict/
│   │   ├── professional_daily.py  # 职业级每日推荐
│   │   ├── semi_auto_bettor.py    # 半自动投注审核
│   │   ├── daily_all.py           # NBA推荐
│   │   ├── daily_fb.py            # 足球推荐
│   │   └── global_top5.py         # 全球Top5
│   ├── backtest/
│   │   └── backtest_runner.py     # 回测性能评估
│   ├── risk/
│   │   └── manager.py        # 风险管理与仓位控制
│   └── monitor/
│       ├── performance.py     # 赛后监控
│       └── health_monitor.py  # 模型健康度检查
├── data/
│   ├── raw/              # 原始历史数据
│   ├── processed/        # 清理+特征化后的数据
│   └── storage/          # 运行时数据（推荐、状态、历史）
├── models/               # 训练好的模型与特征列
├── main.py               # 统一入口
├── full_deploy.sh        # 完整部署脚本
├── run_daily.sh          # 日常运行脚本
└── quick_start.sh        # 快速启动脚本
```

---

## 工作流程

### 日常流程
1. **08:00** - `run_daily.sh` 触发 `main.py`
2. 检查是否需要月度重训练 → 自动执行 `train_models.py`
3. 运行 `professional_daily.py` 生成推荐
4. 检查风险管理状态（仓位、回撤、损失限额）
5. 推送推荐至钉钉
6. **用户审核**: 启动 `semi_auto_bettor.py` 进行人工复审

### 投注流程
```
推荐生成 → 风险检查 → 人工审核 → 下注 → 结果记录 → 组合更新
↑ 每日                                          ↓ 月度
└──────────────────────---- 回测评估 ─────────────┘
```

### 月度重训练流程
```
检查距上次训练时间
  ├─ ≥30天 → 自动执行 train_models.py
  └─ <30天 → 跳过
```

---

## 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `DEFAULT_BUDGET` | 1000 | 初始资金 |
| `MAX_SINGLE_BET_PCT` | 0.05 | 单场最高下注比例 (5%) |
| `MAX_TOTAL_EXPOSURE` | 0.30 | 总仓位上限 (30%) |
| `KELLY_FRACTION` | 0.25 | 凯利系数 (1/4 Kelly) |
| `MIN_EDGE` | 0.06 | 最小期望值阈值 (6%) |
| `SPORTS_API_TIMEOUT` | 15 | API超时时间 (秒) |

---

## 关键模块说明

### `src/core/risk.py` - 风险计算
```python
from src.core.risk import kelly_stake, blend_with_market, edge_ratio

# 计算最优下注额
stake = kelly_stake(model_prob=0.55, odds=2.0, budget=1000)  # 自动凯利

# 混合市场与模型概率
blended = blend_with_market(model_prob=0.55, market_odds=2.0)  # 70%市场 + 30%模型

# 计算期望值
edge = edge_ratio(model_prob=0.55, odds=2.0)  # +0.05 (5% EV)
```

### `src/risk/manager.py` - 仓位管理
```python
from src.risk.manager import RiskManager

rm = RiskManager(initial_budget=1000)
max_stake = rm.get_max_stake(edge=0.06, odds=2.0)
can_bet, msg = rm.can_place_bet(stake=50, current_exposure_pct=0.15)
rm.record_outcome(stake=50, win=True, odds=2.0)
health = rm.get_health_check()  # 获取ROI、回撤、限额状态
```

### `src/predict/semi_auto_bettor.py` - 半自动投注
```python
from src.predict.semi_auto_bettor import SemiAutoBettor

bettor = SemiAutoBettor()
bettor.display_recommendations()  # 显示今日推荐
bettor.record_bet(rec_index=0, actual_result='won', notes='强势主队')  # 记录投注结果
bettor.show_portfolio_status()  # 查看投资组合
```

---

## 性能指标

系统使用以下指标评估模型质量：

| 指标 | 解释 |
|------|------|
| **Brier Score** | 概率预测准确度（越低越好，0最优） |
| **Accuracy** | 分类准确率（越高越好） |
| **Logloss** | 对数损失（越低越好） |
| **Sharpe Ratio** | 调整风险的回报率（越高越好） |
| **Max Drawdown** | 最大回撤（越低越好） |
| **ROI** | 投资回报率（越高越好） |

---

## 安全建议

1. **不要提交 `.env` 文件** - `.gitignore` 已配置
2. **定期备份** `data/storage/` 目录 - 包含投注历史和绩效数据
3. **监控风险指标** - 日限、月限、回撤
4. **月度复盘** - 检查模型性能、调整策略阈值
5. **不要 over-optimize** - 防止过拟合于历史数据

---

## 故障排查

### Q: 模块导入失败
```bash
source venv/bin/activate
pip install -r requirements.txt
```

### Q: API 请求超时
检查网络连接和 API_KEY 是否有效：
```bash
curl "https://api.the-odds-api.com/v4/sports/basketball_nba/odds?apiKey=YOUR_KEY"
```

### Q: 推荐为空
- 检查是否还有进行中的比赛
- 验证特征文件是否存在
- 检查模型文件是否损坏

### Q: 风险检查失败
查看 `data/storage/risk_state.json` 和账户余额：
```python
from src.risk.manager import RiskManager
rm = RiskManager()
print(rm.get_health_check())
```

---

## 下一步改进

- [ ] 添加多币种支持（USD/CNY 自动转换）
- [ ] 接入更多博彩平台 API（Bet365、1xBet 等）
- [ ] 实现自动下单（需人工授权）
- [ ] 添加更多赛事 - 冰球、棒球、网球等
- [ ] 深度学习模型 (LSTM/Transformer) 用于赛事预测
- [ ] 对标杆博彩公司的 ELO/实力评分系统

---

## 许可与免责

本系统仅供学习和研究使用。博彩涉及风险，请自行承担投注损失。不承诺任何经济收益。

---

## 联系与支持

有问题或建议？记录于 `data/storage/` 目录中的日志文件。

祝投注顺利！🎯
