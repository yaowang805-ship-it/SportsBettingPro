# SportsBettingPro — 项目指南

## 当前策略（2026-07-12）

**核心方向：BB体育 vs Pinnacle +EV（比价套利）**
- BB体育（早盘赔率）vs Pinnacle（公平价参考）→ +EV 机会
- 发现机会 → 钉钉推送 → 虚拟投注

**不做什么：**
- ❌ 不做 ML 预测模型（已暂停，2026-07 已清理 2GB+ 遗留 ML 数据）
- ❌ 不做 NFL（美式足球 BB 冠军盘口无法解析）
- ❌ 不使用 BSD API 或 the-odds-api（已停用）
- ❌ 不重复已经完成的工作（先查记忆）

## 数据源

| 用途 | 数据源 | 配额 | 状态 |
|---|---|---|---|
| 投注平台赔率 | BB体育 (pc.x14ff.com SPA) | 免费 | ✅ 主力 |
| 公平价参考 | Pinnacle (guest.api.arcadia.pinnacle.com) | 免费无限 | ✅ |
| 赛果/结算 | ESPN (sports-skills) + football-data.org | 免费无限 | ✅ |

## 系统架构

```
BB体育 (pc.x14ff.com SPA) ──→ bb_extract_odds.py (--all-sports) ──→ bb_odds_extracted.json
                    │              └── bb_extract_x14ff.js (文本解析)
                    ↓
Pinnacle API ──────────────→ bb_vs_pinnacle.py ──→ bb_vs_pinnacle_comparison.json
                                      ↓
                              bb_ev_push.py → 钉钉推送 (+EV，运动分组+联赛分组+开赛时间)
                                      │        ↑ 公平价 = 去抽水价 | 溢价 = (BB - 公平价) / 公平价
                                      ↓
                              bb_virtual_bet.py → virtual_portfolio.json (每日¥1万自动投注)
                                      │         ↑ 每日预算跟踪，Kelly基于公平价
                                      ↓
                              auto_settle.py → ESPN自动结算
                                      ↓
                              daily_settlement.py → 钉钉推送 (每日晨间结算报告)
```

### 每日流程

| 时间(北京) | 事件 | 说明 |
|---|---|---|
| 08:57 | main.py | auto_settle + 风控报告 (LaunchAgent) |
| 09:00 | daily_settlement.py | 推送昨日结算报告 (LaunchAgent) |
| 14:00/18:00/22:00/02:00 | 手动扫描+推送 | 见下方操作流程 |

### 操作流程（每天第一次扫描之后）

```bash
bb_extract_odds.py --all-sports    # 1. 提取BB体育赔率（Chrome需打开）
bb_vs_pinnacle.py                  # 2. 对比Pinnacle计算+EV
bb_ev_push.py                      # 3. 钉钉推送 + 自动投注¥1万
```

第二次及以后的扫描只推送到钉钉即可，不需要重复投注（每日预算已用完）：
```bash
bb_ev_push.py --no-bet             # 只推送不投注
```

### 结算报告

每天上午 09:00 LaunchAgent 自动推送结算报告到钉钉，包含：
- 昨日新增结算（赢/输/盈亏）
- 累计统计（总投注、胜率、ROI、总盈亏）
- 待结算清单
- 当日预算使用情况
```

## 关键文件

- `src/scrapers/bb_extract_odds.py` — BB体育 赔率提取（AppleScript + Chrome），`--all-sports` 多运动
- `src/scrapers/bb_extract_x14ff.js` — pc.x14ff.com SPA 文本解析器（唯一 JS 提取器）
- `src/scrapers/bb_vs_pinnacle.py` — BB vs Pinnacle 对比引擎（含去抽水公平价计算）
- `src/betting/bb_virtual_bet.py` — 虚拟投注执行（¥10,000 每日预算，按日重置，Kelly基于公平价）
- `src/betting/bb_settle.py` — BB体育 投注结算
- `src/report/bb_ev_push.py` — 钉钉推送（运动分组 + 联赛分组 + 开赛时间 + Kelly 分配 + 自动投注）
- `src/report/daily_settlement.py` — 每日晨间结算报告（LaunchAgent 09:00 自动推送，含止损状态）
- `src/report/periodic_report.py` — 联赛维度周报/月报（按联赛分析 ROI/胜率/盈亏，LaunchAgent 定时推送）
- `src/monitor/auto_settle.py` — ESPN 自动结算
- `src/monitor/performance.py` — 投注结算+盈亏监控
- `src/risk/manager.py` — 组合风控（含 DynamicStaking）
- `main.py` — 每日流水线（清理后：仅跑 performance + health_check + 风控）
- `scripts/daily_settlement_push.sh` — LaunchAgent 脚本，09:00 推送结算报告

## 关键决策

1. **BB体育 主力** — 只能在 BB体育 下注，只对比 BB体育 vs Pinnacle。
2. **BSD API + the-odds-api 已停用** — 用不了其他零售平台，对比了也没用。
3. **pc.x14ff.com SPA 提取** — 单次导航到早盘页面，通过 SPA 点击切换运动（sportId: 足球=1, 篮球=3, 网球=5, 美式足球=6, 棒球=7）。
4. **运动支持** — 足球(3-way)、篮球/棒球/网球(2-way)。美式足球在 champion 盘口，无法解析。
5. **扫描频次** — 每天 4 次（14/18/22/02 北京时间），覆盖欧洲全天比赛。
6. **开赛时间** — 推送中显示北京时间（由 Pinnacle 的 UTC start_time 转换）。
7. **公平价 = 去抽水赔率** — Pinnacle 抽水后的真实公平价，高于 Pinnacle 实际赔率。溢价 = (BB - 公平价) / 公平价。
8. **虚拟投注** — 每日固定 ¥10,000 预算（按日重置），Kelly 分数 0.25（基于公平价），单注 max 2%，每日上限 50 笔。
9. **止损机制** — 连输 3 天 → 预算减半；连输 5 天 → 停投。基于按日汇总盈亏自动计算。
10. **每日结算报告** — LaunchAgent 每天 09:00 推送，含昨日盈亏、累计ROI、待结算清单、止损状态。
11. **周报/月报** — LaunchAgent 周报（周日 21:00）+ 月报（每月 1 日 10:00），按联赛维度分析 ROI/胜率/盈亏。

## 编码约定

- 中文注释
- 钉钉通知用 `send_dingtalk()` 函数
- 新功能先查记忆和 CLAUDE.md 确认未重复
- 禁用 "BB体育" 关键词（钉钉内容安全过滤）
