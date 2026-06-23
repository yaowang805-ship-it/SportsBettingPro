# SportsBettingPro — 项目指南

## 当前策略（2026-06-22）

**核心方向：Line Shopping +EV（比价套利）**
- Pinnacle（sharp）去抽水概率 vs 零售最佳赔率 → +EV 机会
- 发现机会 → 钉钉推送 → 虚拟投注

**不做什么：**
- ❌ 不做 ML 预测模型（已暂停，run_all.py 注释掉）
- ❌ 不做 NFL/其他体育，只做足球和篮球
- ❌ 不处理 the-odds-api 配额问题（配额已耗尽，不付费升级）
- ❌ 不做评分卡/模型基线更新
- ❌ 不重复已经完成的工作（先查记忆）

## 数据源

| 用途 | 数据源 | 配额 | 状态 |
|---|---|---|---|
| 足球赔率(line shopping) | BSD API (bzzoiro) | 免费无限 | ✅ 主力 |
| 足球赔率(主流水线) | BSD API | 免费无限 | ✅ |
| 篮球赔率 | odds-api.io (仅 Bet365) | 免费有限 | ⚠️ 无Pinnacle |
| 赛果/比分 | ESPN (sports-skills) | 免费无限 | ✅ |
| 足球历史数据 | football-data.co.uk | 免费 | ✅ 本地CSV |

## 系统架构

```
BSD API ─→ ev_monitor.py (30min) ─→ line_shopping.py ─→ line_shopping_results.json
                                      ↓
                                   钉钉推送 (+EV 机会)
                                      ↓
                              main.py (每日8:57) → 全流程 + 风控概览
```

## 关键文件

- `src/betting/line_shopping.py` — Pinnacle vs 零售 +EV 扫描
- `src/betting/place_line_shops.py` — 虚拟投注执行
- `src/monitor/ev_monitor.py` — 定时扫描 + 钉钉推送
- `src/monitor/auto_settle.py` — 自动结算
- `src/dashboard/components/virtual_portfolio.py` — 虚拟组合
- `src/betting/paper_trader.py` — 就绪评估（目前 GO）
- `main.py` — 每日流水线

## 关键决策

1. **ML 模型暂停** — 历史回测 (10,012 笔, ROI +137%) 证明 Line Shopping 比 ML 更有效。run_all.py 注释掉。
2. **只做足球篮球** — 不扩展其他体育。
3. **BSD API 主力** — 免费无限量，含 Pinnacle + 10+ 博彩公司。the-odds-api 配额耗尽不续。
4. **PaperTrader GO** — 7/7 检查通过，随时可以上线真钱。但先看两笔真实投注结果。
5. **+EV 监控** — 每 30 分钟 cron 扫描，新机会钉钉推送。

## 编码约定

- 中文注释
- 钉钉通知用 `send_alert()` 模式（main.py 中有现成函数）
- 新功能先查记忆和 CLAUDE.md 确认未重复
