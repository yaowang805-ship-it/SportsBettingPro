"""推送格式回归测试 — 防止格式被意外修改。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.report.bb_ev_push import _validate_format, _FORMAT_MARKERS


def _make_good_body() -> str:
    """模拟一份格式正确的推送内容（bb_ev_push 新格式）。"""
    return (
        "**+EV 投注推荐: 3 场比赛**\n\n"
        "扫描 07/03 09:00 | ≥1% 溢价 | 总额 ¥2,000\n\n"
        "⚽ 足球\n"
        "  世界杯\n"
        "  ##### #1 法国 对 巴西  (07/04 21:00)\n"
        "    [主胜] 公平价: 2.50 | Pinnacle: 2.55 | BB价: 2.62 | 溢价: +8.5% | 投注: ¥800\n"
        "    [大2.5] 公平价: 1.95 | Pinnacle: 2.00 | BB价: 2.10 | 溢价: +5.2% | 投注: ¥300\n\n"
        "来源: BB价2条\n\n"
        "---\n"
        "💡 公平价 = Pinnacle去抽水赔率 | 溢价 = (BB - 公平价) / 公平价 | 赔率实时变动，以 Pinnacle 网站当前价为准"
    )


class TestPushFormat:
    """验证推送格式的关键标记必须存在。"""

    def test_good_body_passes(self):
        body = _make_good_body()
        assert _validate_format(body), "格式正确的body应该通过验证"

    def test_missing_header_fails(self):
        body = _make_good_body()
        body = body.replace("**+EV 投注推荐:", "**正EV 推荐:")
        assert not _validate_format(body), "标题改变应导致验证失败"

    def test_missing_entry_prefix_fails(self):
        body = _make_good_body()
        body = body.replace("##### ", "### ")
        assert not _validate_format(body), "缺少#####应导致验证失败"

    def test_missing_fair_price_fails(self):
        body = _make_good_body()
        body = body.replace("公平价:", "参考价:")
        assert not _validate_format(body), "公平价改为参考价应导致验证失败"

    def test_missing_pinnacle_fails(self):
        body = _make_good_body()
        body = body.replace("Pinnacle:", "网站:")
        assert not _validate_format(body), "Pinnacle改为网站应导致验证失败"

    def test_missing_retail_fails(self):
        body = _make_good_body()
        # "价:" 可能出现在：BB价、公平价、来源行。全部替换掉
        body = body.replace("BB价:", "市场:").replace("来源: BB价", "来源: BB市场")
        body = body.replace("公平价:", "参考价:")
        assert not _validate_format(body), "BB价改为市场应导致验证失败"

    def test_missing_edge_fails(self):
        body = _make_good_body()
        body = body.replace("溢价:", "收益:")
        assert not _validate_format(body), "溢价标签改变应导致验证失败"

    def test_missing_stake_fails(self):
        body = _make_good_body()
        body = body.replace("投注:", "金额:")
        assert not _validate_format(body), "投注改为金额应导致验证失败"

    def test_missing_footer_fails(self):
        body = _make_good_body()
        # 去掉来源行 + 修改尾部说明，确保"来源:"被完全移除
        lines = body.split("\n")
        body = "\n".join(l for l in lines if "来源:" not in l)
        body = body.replace("公平价 = Pinnacle去抽水赔率", "参考价 = 去水价")
        assert not _validate_format(body), "来源:被移除应导致验证失败"

    def test_all_format_markers_defined(self):
        """确认所有_FORMAT_MARKERS键都有非空值。"""
        for key, val in _FORMAT_MARKERS.items():
            assert val, f"{key} 的标记值为空"

    def test_empty_body_fails(self):
        assert not _validate_format(""), "空body应验证失败"
        assert not _validate_format("   "), "空白body应验证失败"

    def test_nonsense_body_fails(self):
        assert not _validate_format("hello world this is a test"), "无关内容应验证失败"
