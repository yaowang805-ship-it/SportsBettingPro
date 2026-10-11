"""回归守卫(2026-10-11): 释放路径必经 REAL_ROI_GATE。

10-03 事故 = 手动释放格 continue 绕过 REAL_ROI_GATE(实盘ROI交叉验证), 放出逆向选择格
(足球独赢客 30-70%) 连亏 7 天才被发现。这类错 slippage 看不出来、账目对不上, 只能靠
结构不变量测试捕获「代码形状」而非「业务数值」——阈值怎么调都不误报, 所以不会被关掉。

第 1 层: AST 结构不变量(防「又有人加了个 continue 跳过闸门」)。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "scripts" / "compute_market_release.py"

_MANUAL_CONTAINERS = ("MANUAL_OBSERVE_RELEASE", "MANUAL_OBSERVE_RELEASE_LIMITED")


def _find_manual_loops(tree, src):
    """找 main() 里「会 append 到 observe_released」的 for _mkey in MANUAL_OBSERVE_RELEASE{_LIMITED} 循环。

    注意: 还有一处 for _mkey in MANUAL_OBSERVE_RELEASE 是构建 _manual_dirs(方向级跳过集合),
    不 append observe_released, 不该被误判。所以用「body 含 observe_released.append」过滤。
    """
    loops = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            for sub in ast.walk(node):
                if isinstance(sub, ast.For):
                    t, it = sub.target, sub.iter
                    if (isinstance(t, ast.Name) and t.id == "_mkey"
                            and isinstance(it, ast.Name)
                            and it.id in _MANUAL_CONTAINERS):
                        body_src = ast.get_source_segment(src, sub) or ""
                        if "observe_released.append" in body_src:
                            loops.append((it.id, sub, body_src))
    return loops


def test_manual_release_passes_real_roi_gate():
    """结构不变量: 手动释放循环必须调用 _manual_gate_blocks(REAL_ROI_GATE 单一出口)。"""
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    loops = _find_manual_loops(tree, src)
    assert loops, "没找到手动释放循环(append observe_released), 检查 AST 匹配逻辑"
    for name, loop, body_src in loops:
        assert "_manual_gate_blocks" in body_src, (
            f"手动释放循环 {name} 缺 REAL_ROI_GATE 调用 → 会重演 10-03 事故:\n{body_src[:400]}")


def test_data_driven_loops_also_gate():
    """数据驱动循环(早盘+滚球)也必须引用 REAL_ROI_GATE_N_MIN(≥2 处)。"""
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    n = sum(1 for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "REAL_ROI_GATE_N_MIN")
    assert n >= 2, f"REAL_ROI_GATE_N_MIN 引用 {n} 处, 应≥2(早盘+滚球数据驱动各一)"


def test_escape_hatch_exists():
    """回退开关 ENFORCE_REAL_ROI_GATE_ON_MANUAL 存在(默认 true), 且被 _manual_gate_blocks 消费。"""
    src = SRC.read_text(encoding="utf-8")
    assert "ENFORCE_REAL_ROI_GATE_ON_MANUAL" in src, "缺回退开关"
    # _manual_gate_blocks 必须读这个开关(回退路径)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_manual_gate_blocks":
            body_src = ast.get_source_segment(src, node) or ""
            assert "ENFORCE_REAL_ROI_GATE_ON_MANUAL" in body_src, "_manual_gate_blocks 没读回退开关"
            return
    assert False, "没找到 _manual_gate_blocks"
