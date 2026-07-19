# SportsBettingPro — 指挥官作战手册

## 当前策略（2026-07-15 指挥官模式）

**核心方向：BB体育 vs Pinnacle +EV（比价套利）**
- BB体育（早盘赔率）vs Pinnacle（公平价参考）→ +EV 机会
- 发现机会 → 钉钉推送 → 真实投注（用户执行）
- **目标：月利润 ¥50,000**

**不做什么：**
- ❌ 不做 ML 预测模型
- ❌ 不碰中国足球联赛
- ❌ BSD API / the-odds-api 不用于比价（有 quota），但可用于赛果结算
- ❌ 不重复已经完成的工作（先查记忆）

## 数据源

| 用途 | 数据源 | 方式 | 状态 |
|---|---|---|---|
| 投注平台赔率 | BB体育 (api.infv1.com) | 直接 HTTP API | ✅ 主力 |
| 公平价参考 | Pinnacle (guest.api.arcadia.pinnacle.com) | HTTP API | ✅ |
| 赛果/结算 | ESPN + football-data.org + The Odds API + 直播吧 | 多源聚合 | ✅ |

## 系统架构

```
BB API (api.infv1.com) ──────→ bb_api_fetcher.py ──→ bb_odds_extracted.json
                                   └── type=2 (72小时), requests 直连
                                              ↓
Pinnacle API ─────────────────────→ bb_vs_pinnacle.py ──→ bb_vs_pinnacle_comparison.json
                                   ├── period=0 → FT 对比
                                   └── period=1 → HT 对比
                                              ↓
                              bb_ev_push.py → 钉钉推送
                                   ├── ≥2% EV, 按开赛时间排序
                                   ├── DNS 绕过直连（真实IP+SNI）
                                   └── --no-bet = 只推送不投注
```

### 操作流程

```bash
# 全量提取 → 对比 → 推送（第一次扫描含投注）
python3 -m src.scrapers.bb_api_fetcher --all-sports
python3 -m src.scrapers.bb_vs_pinnacle
python3 -m src.report.bb_ev_push

# 后续扫描只推送（当日预算已用）
python3 -m src.report.bb_ev_push --no-bet
```

## 当前参数

| 参数 | 值 | 说明 |
|---|---|---|
| 日预算 | ¥50,000 | 支撑月利润¥50k目标 |
| Kelly 分数 | 0.25 | 保守投注 |
| T1 最小 EV | 2% | 优先推送高置信度 |
| T2 最小 EV | 2% | 主流联赛 |
| T3 最小 EV | 2% | 低级别联赛 + 网球 |
| EV 上限 | 20% | 防极端值假阳性 |
| 单注上限 | 2% | ¥1,000 |
| 每日最多 | 50 笔 | |

## 关键文件

- `src/scrapers/bb_api_fetcher.py` — BB API 直连提取（Chrome 无依赖）
- `src/scrapers/bb_vs_pinnacle.py` — 对比引擎（去抽水公平价, FT+HT）
- `src/betting/bb_virtual_bet.py` — 虚拟投注（¥50,000日预算）
- `src/report/bb_ev_push.py` — 钉钉推送（格式优化: 置信度+时间排序）
- `src/monitor/auto_settle.py` — ESPN 自动结算
- `config/dingtalk.py` — 钉钉直连（真实IP+SNI 绕过 DNS 劫持）

## 关键决策

1. **BB API 直连** — `api.infv1.com` POST + Authorization token（从 Chrome LevelDB 提取）
2. **type=2** — 返回未来72小时比赛（658场），type=3 仅当天（26场）
3. **钉钉直连** — Shadowrocket VPN 劫持 DNS，硬编码真实IP `161.117.107.66` + SNI
4. **requests 库** — Python 3.14 urllib 有 IncompleteRead bug，大响应截断
5. **置信度标记** — ✓ = 队名匹配(≥0.95), ◷ = 时间匹配
6. **Tier门槛** — T1≥2%, T2≥2%, T3≥3%, EV_CAP=20%
7. **止损机制** — 连输3天预算减半，5天停投
