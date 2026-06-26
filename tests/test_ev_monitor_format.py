"""钉钉推送格式回归测试 — 防止修改代码导致输出变英文/丢金额/格式乱。"""
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from src.monitor.ev_monitor import _build_dingtalk_body, _calc_stakes, _outcome_label


def _make_opp(**overrides):
    """生成一条样本机会，默认值覆盖主流场景。"""
    now = datetime.now(timezone.utc)
    base = {
        "market": "1x2",
        "sport": "football",
        "league": "CSL",
        "home_team": "Shanghai Port",
        "away_team": "Henan FC",
        "outcome": "home",
        "outcome_label": "主胜",
        "commence_time": (now + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_prob": 0.55,
        "odds": 2.10,
        "edge_pct": 15.5,
        "_ev": 0.155,
    }
    base.update(overrides)
    return base


@pytest.fixture
def tmp_dir():
    """每个测试独立的临时目录作为 DATA_DIR。"""
    tmp = Path(tempfile.mkdtemp())
    # 写入空的虚拟组合
    vp = tmp / "virtual_portfolio.json"
    vp.write_text(json.dumps({
        "pending_bets": [],
        "history": [],
        "balance": 10000.0,
    }))
    return tmp


class TestOutcomeLabel:
    """_outcome_label 返回中文。"""

    def test_uses_outcome_label_field(self):
        assert _outcome_label({"outcome_label": "主胜"}) == "主胜"

    def test_falls_back_to_outcome_map(self):
        assert _outcome_label({"outcome": "home"}) == "主胜"
        assert _outcome_label({"outcome": "away"}) == "客胜"
        assert _outcome_label({"outcome": "draw"}) == "平局"
        assert _outcome_label({"outcome": "over"}) == "大"
        assert _outcome_label({"outcome": "under"}) == "小"

    def test_unknown_outcome_raw(self):
        assert _outcome_label({"outcome": "something_strange"}) == "something_strange"


class TestCalcStakes:
    """_calc_stakes 正确计算投注金额。"""

    def test_returns_stake_field(self, tmp_dir):
        with patch("src.monitor.ev_monitor.DATA_DIR", tmp_dir):
            result = _calc_stakes([_make_opp()])
        assert len(result) == 1
        assert "stake" in result[0]
        # 应该有正金额
        assert result[0]["stake"] > 0

    def test_low_ev_gets_zero_stake(self, tmp_dir):
        with patch("src.monitor.ev_monitor.DATA_DIR", tmp_dir):
            result = _calc_stakes([_make_opp(_ev=0.01, edge_pct=0.5)])
        assert result[0]["stake"] == 0

    def test_respects_max_per_bet(self, tmp_dir):
        """单注不超过 MAX_PER_BET (2000)。"""
        with patch("src.monitor.ev_monitor.DATA_DIR", tmp_dir):
            result = _calc_stakes([_make_opp(model_prob=0.95, odds=1.10, _ev=0.2, edge_pct=20.0)])
        assert result[0]["stake"] <= 2000

    def test_multiple_opps_all_have_stake(self, tmp_dir):
        with patch("src.monitor.ev_monitor.DATA_DIR", tmp_dir):
            result = _calc_stakes([_make_opp(edge_pct=10, _ev=0.1) for _ in range(5)])
        assert len(result) == 5
        for r in result:
            assert "stake" in r


class TestBuildDingTalkBody:
    """_build_dingtalk_body 输出格式校验。"""

    def _build(self, opps, tmp_dir):
        with patch("src.monitor.ev_monitor.DATA_DIR", tmp_dir):
            return _build_dingtalk_body(opps)

    # ── 中文队名 ──

    def test_team_names_are_chinese(self, tmp_dir):
        body = self._build([_make_opp()], tmp_dir)
        assert "上海海港" in body, "应有中文队名"
        assert "河南" in body, "应有中文队名"

    def test_no_raw_english_names(self, tmp_dir):
        """不能出现 API 原始英文队名。"""
        body = self._build([_make_opp()], tmp_dir)
        assert "Shanghai Port" not in body
        assert "Henan FC" not in body

    def test_foreign_teams_also_chinese(self, tmp_dir):
        opp = _make_opp(home_team="Arsenal", away_team="Chelsea")
        body = self._build([opp], tmp_dir)
        assert "阿森纳" in body
        assert "切尔西" in body

    # ── 金额 ──

    def test_contains_yen_symbol(self, tmp_dir):
        body = self._build([_make_opp()], tmp_dir)
        assert "¥" in body, "钉钉推送应有金额"

    def test_no_zero_stake_when_no_ev(self, tmp_dir):
        """无 EV 的机会不显示金额。"""
        opp = _make_opp(_ev=-0.01, edge_pct=-1.0)
        body = self._build([opp], tmp_dir)
        # 金额可能是 0，线上不显示
        if "¥0" in body:
            pytest.fail("不应该显示 ¥0 金额")

    # ── 格式标志 ──

    def test_contains_format_markers(self, tmp_dir):
        body = self._build([_make_opp()], tmp_dir)
        assert "📊 投注推荐" in body
        assert "公平价" in body
        assert "日预算 ¥" in body
        assert "条机会" in body

    def test_line_pair_format(self, tmp_dir):
        """每条机会占两行：标题行 + 详情行。"""
        opps = [_make_opp(edge_pct=10.0), _make_opp(edge_pct=12.0, home_team="Arsenal", away_team="Chelsea")]
        body = self._build(opps, tmp_dir)
        lines = body.split("\n")
        numbered = [l for l in lines if l.strip() and l.strip()[0].isdigit()]
        assert len(numbered) == 2, "应有 2 条编号行"
        assert all("." in l for l in numbered), "编号行应有序号"

    # ── 市场标签 ──

    def test_market_tags_chinese(self, tmp_dir):
        opp = _make_opp(market="over_under", outcome="over", outcome_label="大2.5")
        body = self._build([opp], tmp_dir)
        assert "大小" in body or "大2.5" in body

    @pytest.mark.parametrize("market,tag", [
        ("1x2", "独赢"),
        ("over_under", "大小"),
        ("btts", "进球"),
        ("draw_no_bet", "无平"),
    ])
    def test_known_market_tags(self, tmp_dir, market, tag):
        opp = _make_opp(market=market)
        body = self._build([opp], tmp_dir)
        assert tag in body

    # ── 时间标签 ──

    def test_time_tag_minutes(self, tmp_dir):
        """1 小时内显示分钟。"""
        now = datetime.now(timezone.utc)
        ct = (now + timedelta(minutes=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
        opp = _make_opp(commence_time=ct)
        body = self._build([opp], tmp_dir)
        assert "分钟后" in body

    def test_time_tag_started(self, tmp_dir):
        """已开赛标签。"""
        now = datetime.now(timezone.utc)
        ct = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        opp = _make_opp(commence_time=ct)
        body = self._build([opp], tmp_dir)
        assert "已开赛" in body

    # ── 汇总行 ──

    def test_summary_line(self, tmp_dir):
        body = self._build([_make_opp()], tmp_dir)
        assert "日预算 ¥10000" in body
        assert "共 1 条机会" in body

    def test_waiting_count(self, tmp_dir):
        """超过 30 小时的比赛显示两段式等待。"""
        now = datetime.now(timezone.utc)
        ct = (now + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        opp = _make_opp(commence_time=ct)
        body = self._build([opp], tmp_dir)
        assert "两段式等待" in body or ">30h" in body

    def test_all_opps_included_in_count(self, tmp_dir):
        opps = [_make_opp() for _ in range(7)]
        body = self._build(opps, tmp_dir)
        assert "共 7 条机会" in body

    # ── 边界：空列表 ──

    def test_empty_opps(self, tmp_dir):
        body = self._build([], tmp_dir)
        assert "📊 投注推荐" in body
        assert "共 0 条机会" in body

    # ── 各种体育类型 ──

    def test_basketball_team_names(self, tmp_dir):
        opp = _make_opp(sport="basketball", league="NBA",
                        home_team="Los Angeles Lakers", away_team="Golden State Warriors")
        body = self._build([opp], tmp_dir)
        assert "湖人" in body
        assert "勇士" in body
        # NBA 球队不使用 football 映射
        assert "Los Angeles Lakers" not in body
