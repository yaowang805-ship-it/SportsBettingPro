# SportsBettingPro — 指挥官作战手册

## 当前策略（2026-07-31 V4 更新）

**核心方向：BB体育 vs Pinnacle +EV（比价套利）**
- BB体育（早盘赔率）vs Pinnacle（公平价参考）→ +EV 机会
- 发现机会 → 钉钉推送 → 真实投注（用户执行）
- **目标：月利润 ¥50,000**

**不做什么：**
- ❌ 不做 ML 预测模型
- ❌ 不碰中国足球联赛
- ❌ the-odds-api 不用于比价（有 quota），已从流水线移除
- ❌ 不重复已经完成的工作（先查记忆）
- ❌ 乒乓/羽/排/拳击 零Pinnacle数据运动封杀 (V5.1)
- ❌ ITF/Challenger/W系列网球封杀 (V5)

**铁律（用户 2026-08-16 明确要求，永远执行）：**
- 🔴 **遇到问题永远先解决根因，绝不逃避/移除/封杀了事。** 盘口出问题先查 API 原始结构确认根因，结构不对等就做正确转换对齐，结构一样就修提取/匹配，实在无法对齐才降级且必须在代码注释写清原因。
- 🔴 **Token 效率铁律（2026-08-25 用户明确要求）：深度思考最耗 token，按价值分流——例行查询/简单问题不深想，只有真 bug 才深想一次到位；少重复读文件、精准 grep 定位、合并命令、回复只给结论+关键数字。**

## 数据源

| 用途 | 数据源 | 方式 | 状态 |
|---|---|---|---|
| 投注平台赔率 | BB体育 (api.infv1.com) | 直接 HTTP API | ✅ 仅BB |
| 公平价参考 | Pinnacle (guest.api.arcadia.pinnacle.com) | HTTP API | ✅ |
| 赛果/结算 | ESPN + football-data.org + 直播吧 | 多源聚合 | ✅ |
| **权重标定** | **football-data.co.uk Pinnacle 收盘** | **111K场/20联赛/13季** | ✅ |

## 系统架构

```
BB体育 API (api.infv1.com, user-token) ──→ bb_api_fetcher.py ──→ bb_odds_extracted.json
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
| 日预算 | ¥20,000 | 稳定不变 |
| Kelly 分数 | 0.50 | 稳妥半凯利 |
| 单注上限 | 6% (¥1200) | |
| 每日最多 | 100 笔 | 质量优先 |
| EV 上限 | 12% (动态) | max(12, (odds-1)×20) |

### V4 权重矩阵 (`config/weight_matrix_v4.py`) — 全量 Pinnacle 历史数据驱动

**核心公式**: `半凯利仓位% = max(0, 实际胜率×BB赔率-1) / (BB赔率-1) × 0.5`
**数据源**: **完全外部数据，零笔结算数据参与**

| 运动 | 数据源 | 数据量 | 赔率区间数 |
|---|---|---|---|
| ⚽ 足球 1X2 | football-data.co.uk Pinnacle 收盘 | **111,225 场** (20联赛×13季) | 30 细桶 |
| ⚽ 足球 OU | football-data.co.uk Pinnacle 收盘 | **46,727 场** | 30 细桶 |
| 🎾 网球 | Pinnacle 收盘赔率 | **5,013 场** (5赛事级) | 16 细桶 |
| 🏀 NBA | 模型回测 | **57,504 场** (15季) | 14 细桶 |
| ⚾ MLB | SBR + OddsPortal + Vegas | **70,905 场** (16 桶) | 16 细桶 |
| 🏈 NFL | SBR 收盘赔率 | **5,904 场** (2011-2021) | 27 细桶 |
| 🏒 NHL | Kaggle ESPN 收盘 | **6,817 场** (2004-2025) | 10 细桶 |
| 🥊 拳击 | Betfair | **663 场** | 保守 |
| 🥋 UFC | BookMaker 收盘 | **521 场** | 保守 |
| 🏀 非NBA篮球 | Betfair | **24,000 场** | fallback |

### 投注公式

```
投注额 = 日预算 × V4_Kelly% × Kelly倍率 × 蒸汽 × 连亏
```

其中 V4_Kelly% **已经是最优解**（Pinnacle 历史数据 + BB 溢价校准），Kelly 倍率/蒸汽/连亏仅做边际调整。

### 封杀规则

| 规则 | 原因 |
|---|---|
| DC (双重机会) | Pinnacle 无对应盘口，0% 历史胜率 |
| HTFT (半全场) | BB/Pin 定义不一致 |
| MMA/拳击 | V4.2+ 条件允许: name匹配+高分+低赔率 → 小额（不再封杀） |
| 赔率 >20.0 | 111K Pinnacle 数据确认全部负期望 |

## 关键文件

- `config/weight_matrix_v4.py` — **V4 权重矩阵（全量外部数据驱动）**
- `data/pinnacle_historical/` — 381 个 Pinnacle 历史 CSV (Git LFS 管理大文件)
- `src/scrapers/bb_api_fetcher.py` — BB API 直连提取
- `src/scrapers/bb_vs_pinnacle.py` — 对比引擎（去抽水公平价, FT+HT）
- `src/report/bb_ev_push.py` — 钉钉推送 + V4 投注
- `src/betting/bb_virtual_bet.py` — 虚拟投注
- `src/monitor/auto_settle.py` — ESPN 自动结算
- `config/dingtalk.py` — 钉钉直连

## 关键决策

1. **BB API 直连** — `api.infv1.com` (BB体育真实API, user-token头)
2. **type=2** — 返回未来72小时比赛
3. **钉钉直连** — 硬编码真实IP `161.117.107.66` + SNI
4. **置信度标记** — ✓ = 队名匹配(≥0.95), ◷ = 时间匹配
5. **止损机制** — 连输3天预算减半，5天停投
6. **权重矩阵** — 全量 Pinnacle 外部数据驱动，逐联赛逐赔率区间独立
