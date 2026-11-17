#!/usr/bin/env python3
"""校准流水线编排 — 宽样本已结算量达标后自动推进诊断→校准→显著性检验。

每天 build+settle 后调用(见 calibration_wide_daily.sh)。检查 calibration_wide.csv 里已结算
方向级样本量, 达标(≥N_SETTLED)才自动跑:
  1. compare_devig_methods.py  四种去水 log-loss/ECE 对比(诊断去水 bias vs 选择 bias)
  2. calibrate_wide.py --save  宽样本分桶校准 + 时间外验证, 通过则写校准表
  3. permutation_test_clv.py   CLV 显著性置换检验

未达标只打印进度, 不跑(避免小样本出假结论)。幂等: 达标后每天重跑无害(结论随样本更新)。
"""
import csv
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WIDE_CSV = ROOT / "data" / "storage" / "calibration_wide.csv"
PY = str(ROOT / ".venv312" / "bin" / "python")

N_SETTLED = 200   # 方向级已结算样本门槛(对齐 Bailey & López de Prado CLV 有效性)
N_MATCHES = 80    # 比赛数门槛(方向级 home/draw/away 同场不独立)


def _progress():
    settled = 0
    matches = set()
    if not WIDE_CSV.exists():
        return 0, 0
    with open(WIDE_CSV, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("y") not in ("", None):
                settled += 1
                matches.add(r["bb_match_id"])
    return settled, len(matches)


def main():
    settled, matches = _progress()
    print(f"[orchestrate] 宽样本已结算: 方向级 {settled} 行 / 比赛 {matches} 场")
    if settled < N_SETTLED or matches < N_MATCHES:
        print(f"[orchestrate] 未达标(需 方向级≥{N_SETTLED} 且 比赛≥{N_MATCHES}), 继续积累")
        return

    steps = [
        ("compare_devig_methods.py", []),
        ("calibrate_wide.py", ["--save"]),
        ("permutation_test_clv.py", []),
    ]
    for script, args in steps:
        path = ROOT / "scripts" / script
        print(f"\n[orchestrate] ===== 跑 {script} =====")
        try:
            r = subprocess.run([PY, str(path)] + args, cwd=str(ROOT),
                               capture_output=True, text=True, timeout=600)
            print(r.stdout)
            if r.stderr:
                print(r.stderr[-2000:])
        except Exception as e:
            print(f"[orchestrate] {script} 失败: {type(e).__name__} {e}")


if __name__ == "__main__":
    main()
