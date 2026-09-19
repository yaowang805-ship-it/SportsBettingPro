# SportsBettingPro — 指挥官作战手册（2026-09-19 更新）

## 当前策略

**核心：BB体育（软书/猎物） vs Betfair Exchange（公平价）+ Sbobet（置信度）比价套利**
- 公平价 = Betfair Exchange 中间价（odds-api.io，P2P 无抽水）；Sbobet = 置信度开关（不进定价，只确认方向）
- 两种盈利方式：① **lead-lag 时间差**（SBO/Betfair 先动 → BB 滞后跟随 → 抢窗口）② **定价偏差散户迎合**（BB 系统性迎合散户）
- 用户只在 BB 体育投注，Betfair 只当尺子（不扣佣金）

## 数据源

| 用途 | 数据源 | 方式 | 状态 |
|---|---|---|---|
| 猎物赔率 | BB体育 + FB体育（同账户） | HTTP API（getList/下单） | ✅ |
| 公平价 | Betfair Exchange（odds-api.io） | WS 实时推送 + REST 兜底 | ✅ |
| 置信度 | Sbobet（odds-api.io） | WS 实时推送 | ✅ |
| Pin | guest API | 已暂停（15min CDN 陈旧，只留 CLV 差异） | ⏸️ |

## 架构

```
odds-api.io WS（Sbobet+Betfair, live+prematch）──→ odds_ws.py 缓存（落盘 odds_ws_cache.json）
                                                        ↓ get_odds 优先读 WS 缓存(0ms)
  ┌─ 滚球：second_level_monitor.py（getList type=1 每 2s → 匹配 Betfair 公平价 → 下单）
  └─ 早盘：bb_vs_pinnacle.py（bb_odds_extracted.json 快照 → Betfair 公平价 → 机会入库）
                                ↓
                   bb_auto_bet.py place_single_bet（下单 + 验价 + 二次验价）
```

## 关键参数（2026-09-19）

| 参数 | 值 | 说明 |
|---|---|---|
| bankroll 基准 | **¥10000**（读 `data/storage/bankroll_base.txt`） | 固定基准，不随余额缩水（余额×30% 是顺周期陷阱） |
| Kelly 分数 | 0.50 | 半凯利（单一事实来源 config.constants） |
| 滚球单注上限 | ¥300（MAX_STAKE） | 高溢价可顶格 |
| 早盘 cap | 观察库释放 150/300 | observe_release_caps |
| stake<30 不投 | 铁律 | 冷门单 Kelly<30 直接跳过（不兜底抬到 30） |
| 回撤熔断 | 7天亏超¥1000→半仓 | 已加归零时间戳（drawdown_reset_ts.txt） |

## 铁律（永远执行）

- 🔴 **遇到问题先解决根因，绝不逃避/移除/封杀**（结构不对等做转换对齐，实在无法才降级并写清原因）
- 🔴 **永不建模型**：只用 BB vs Betfair/SBO 比价套利，CLV 只作验证工具
- 🔴 **换节点由用户手动**：self_heal 只告警不自动换节点
- 🔴 **stake<30 不投**，<30 的冷门单跳过
- 🔴 **同场同盘口只下一注**（互斥锁 check_sub_market_bet）
- 🔴 **Token 效率**：按价值分流，简单问题直接答，改动只改必要处

## 关键文件

- `src/scrapers/odds_api_io.py` — Betfair/SBO 公平价提取（含两路归一化 `_fair_two_way`）
- `src/scrapers/odds_ws.py` — WS 实时推送缓存
- `src/scrapers/bb_vs_pinnacle.py` — 早盘比价
- `src/scrapers/second_level_monitor.py` — 滚球监控 + 下单（含熔断/bankroll/互斥）
- `src/scrapers/pinnacle_live.py` — 滚球数据拉取（fetch_bb_live_matches 等）
- `src/betting/bb_auto_bet.py` — 下单（place_single_bet + 二次验价 + 互斥锁）
- `data/storage/bankroll_base.txt` — 本金基准（手动改）
- `data/storage/bet_sub_record.json` — 同场同盘口互斥记录

## 最近关键改动（2026-09-19）

1. 公平价两路归一化（修 hc/ou 隐含和>1 致对立下注根因）
2. 同场同盘口互斥锁（防让球主↔客、大小 over↔under 同时下）
3. 下单失败二次验价（重拉实时价，edge 还在则重试）
4. bankroll 改固定基准 ¥10000（修「余额×30%」顺周期陷阱）
5. 熔断归零重算（drawdown_reset_ts.txt）

详见记忆 [[fair-price-mutex-reverify-20260919]]、[[bankroll-fixed-base-20260919]]、[[pin-vs-betfair-accuracy-20260919]]
