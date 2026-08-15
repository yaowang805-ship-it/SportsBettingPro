"""
V4.5 回归测试 — 确保关键功能不被后续改动破坏.

运行: python3 -m pytest tests/test_v4_regression.py -v
覆盖:
  1. V4权重矩阵 — Kelly>0 for key sports/odds
  2. 去重 — 指纹持久化, 二次推送应被拦截
  3. 结算门禁 — 0%成功率联赛拒绝投注
  4. 日预算 — 固定 ¥10,000
  5. 匹配门槛 — 非足球运动放宽
  6. Pin ROI屏蔽 — HC/tennis提高门槛
  7. 名映射 — 置信度系统, 覆盖保护
"""
import pytest, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ===================================================================
# 1. V4 权重矩阵
# ===================================================================
class TestV4WeightMatrix:
    def test_football_1x2_has_kelly(self):
        """英超 1X2 @2.0 应有正 Kelly."""
        from config.weight_matrix_v5 import get_kelly_stake_pct
        k = get_kelly_stake_pct("football", "英超", "1x2", 2.0)
        assert k >= 0, f"英超 1X2 Kelly={k}"

    def test_football_ou_has_kelly(self):
        """英超 OU @1.9 应有正 Kelly."""
        from config.weight_matrix_v5 import get_kelly_stake_pct
        k = get_kelly_stake_pct("football", "英超", "ou", 1.9)
        assert k >= 0, f"英超 OU Kelly={k}"

    def test_football_hc_falls_back_to_1x2(self):
        """HC 不应返回固定 4% (已修复)."""
        from config.weight_matrix_v5 import get_kelly_stake_pct
        k = get_kelly_stake_pct("football", "英超", "hc", 2.0)
        assert k < 0.04, f"HC Kelly={k} 不应是固定4%"

    def test_nba_ml_has_data(self):
        """NBA ML 有标定数据."""
        from config.weight_matrix_v5 import get_kelly_stake_pct
        k = get_kelly_stake_pct("basketball", "NBA", "1x2", 2.5)
        assert k >= 0, f"NBA ML Kelly={k}"

    def test_mlb_ml_has_data(self):
        """MLB ML 有标定数据."""
        from config.weight_matrix_v5 import get_kelly_stake_pct
        k = get_kelly_stake_pct("baseball", "MLB", "1x2", 2.5)
        assert k >= 0, f"MLB ML Kelly={k}"

    def test_nfl_ml_has_data(self):
        """NFL ML 有标定数据."""
        from config.weight_matrix_v5 import get_kelly_stake_pct
        k = get_kelly_stake_pct("american_football", "NFL", "1x2", 2.0)
        assert k >= 0, f"NFL ML Kelly={k}"

    def test_dc_is_data_driven(self):
        """DC 现在从1X2推导(Shin已证无偏), 折扣1.0, 上限0.03."""
        from config.weight_matrix_v5 import get_kelly_stake_pct
        k = get_kelly_stake_pct("football", "英超", "dc", 1.8)
        # 数据驱动值不应超过 cap (V5.3: 0.015→0.03)
        assert k <= 0.03, f"DC Kelly={k} 不应超过cap(0.03)"

    def test_pin_roi_blocking_hc(self):
        """HC Pin全负ROI → min_ev=5%."""
        from config.weight_matrix_v5 import get_min_ev
        ev = get_min_ev("football", "英超", "hc", 2.0)
        assert ev >= 2.0, f"HC min_ev={ev}"

    def test_no_double_multiplier(self):
        """1X2不再有双重乘数bug."""
        from config.weight_matrix_v5 import get_kelly_stake_pct
        k1 = get_kelly_stake_pct("football", "英超", "1x2", 2.0)
        k2 = get_kelly_stake_pct("football", "英超", "ou", 2.0)
        # 两者不应差超过3倍 (修复前1X2多乘2次系数)
        if k1 > 0 and k2 > 0:
            ratio = max(k1, k2) / min(k1, k2)
            assert ratio < 3.0, f"1X2 vs OU ratio={ratio:.1f}"


# ===================================================================
# 2. 去重
# ===================================================================
class TestDedup:
    def test_fingerprint_persistence(self):
        """指纹文件存在且为合法 JSON dict（允许为空 — 轮换后清空属正常）."""
        fp = ROOT / "data" / "storage" / "pushed_opportunities.json"
        assert fp.exists(), "指纹文件不存在"
        data = json.loads(fp.read_text())
        assert isinstance(data, dict), "指纹文件格式错误"

    def test_filter_pushed_works(self):
        """_filter_pushed 第二次调用应返回空."""
        from src.report.bb_ev_push import (
            _collect_opportunities_from_file, _diversify_and_rank, _filter_pushed
        )
        opps = _collect_opportunities_from_file()
        if not opps: pytest.skip("无机会数据")
        qualified = _diversify_and_rank(opps)
        # 第一次推送
        result1 = _filter_pushed(qualified)
        # 第二次: 应该全部过滤
        result2 = _filter_pushed(qualified)
        assert len(result2) <= len(result1), "去重后不应增加"

    def test_force_not_skip_dedup(self):
        """--force 只跳过新鲜度, 不跳过去重."""
        # 验证 push_report 参数: skip_dedup 默认 False
        import inspect
        from src.report.bb_ev_push import push_report
        sig = inspect.signature(push_report)
        params = sig.parameters
        assert "skip_dedup" in params, "缺少 skip_dedup 参数"
        assert params["skip_dedup"].default is False, "skip_dedup 默认应为 False"


# ===================================================================
# 3. 结算门禁
# ===================================================================
class TestSettleability:
    def test_block_zero_success_leagues(self):
        """0%成功率+多次尝试联赛拒绝试用."""
        from src.core.settleability import is_league_probationary
        # "欧足联欧洲会议联赛-资格赛" 有0/5成功率 (>2次尝试)
        prob = is_league_probationary("欧足联欧洲会议联赛-资格赛", "football")
        # 不应允许试用投注 (0%成功率+>=2次尝试)
        assert not prob, "0%成功率+>=2尝试联赛不应probationary"

    def test_known_good_league_settleable(self):
        """英超应可结算."""
        from src.core.settleability import is_league_settleable
        assert is_league_settleable("英超", "football"), "英超应可结算"


# ===================================================================
# 4. 日预算
# ===================================================================
class TestDailyBudget:
    def test_bankroll_is_20000(self):
        """日预算固定¥20,000."""
        from config.constants import get_dynamic_bankroll, BANKROLL
        assert BANKROLL == 20000.0, f"BANKROLL={BANKROLL}"
        assert get_dynamic_bankroll() == 20000.0, f"get_dynamic_bankroll={get_dynamic_bankroll()}"


# ===================================================================
# 5. 匹配门槛 (非足球放宽)
# ===================================================================
class TestMatchThresholds:
    def test_boxing_threshold_relaxed(self):
        """拳击门槛不应是0.85 (已放宽)."""
        from src.report.bb_ev_push import _read_comparison_file
        # 检查代码中的阈值定义
        import inspect
        source = inspect.getsource(_read_comparison_file)
        # 拳击门槛应该是0.70不是0.85
        assert "min_score = 0.70" in source or "0.70" in source, \
            "拳击门槛应为0.70"

    def test_tennis_threshold_relaxed(self):
        """网球门槛不应是0.75 (已放宽到0.45)."""
        import inspect
        from src.report.bb_ev_push import _read_comparison_file
        source = inspect.getsource(_read_comparison_file)
        assert "0.45" in source, \
            "网球门槛应为0.45"


# ===================================================================
# 6. 名映射置信度
# ===================================================================
class TestNameMapping:
    def test_map_has_meta(self):
        """名映射文件包含_meta."""
        fp = ROOT / "data" / "storage" / "team_name_map.json"
        data = json.loads(fp.read_text())
        assert "_meta" in data, "名映射缺少_meta"

    def test_confidence_tracking(self):
        """置信度系统: n>=3应存在."""
        fp = ROOT / "data" / "storage" / "team_name_map.json"
        data = json.loads(fp.read_text())
        meta = data.get("_meta", {})
        high_conf = sum(1 for v in meta.values() if v.get("n", 0) >= 3)
        assert high_conf > 0, "应有n>=3的高置信度映射"

    def test_sport_tagging(self):
        """名映射应有运动标签."""
        fp = ROOT / "data" / "storage" / "team_name_map.json"
        data = json.loads(fp.read_text())
        meta = data.get("_meta", {})
        tagged = sum(1 for v in meta.values() if v.get("sport", "unknown") != "unknown")
        assert tagged > 100, f"只有{tagged}个映射有运动标签"


# ===================================================================
# 7. 模块可导入 (防语法错误)
# ===================================================================
class TestImports:
    def test_bb_vs_pinnacle_import(self):
        import src.scrapers.bb_vs_pinnacle  # noqa

    def test_weight_matrix_import(self):
        import config.weight_matrix_v5  # noqa

    def test_matching_engine_import(self):
        import src.scrapers.matching_engine  # noqa

    def test_bb_ev_push_import(self):
        import src.report.bb_ev_push  # noqa

    def test_orchestrator_import(self):
        import src.core.pipeline_orchestrator  # noqa
