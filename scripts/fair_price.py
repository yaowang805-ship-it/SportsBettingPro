#!/usr/bin/env python3
"""Pinnacle 赔率 → 公平价 快速计算器

用法:
    python3 scripts/fair_price.py
    # 输入 Pinnacle 主客赔率，自动算出公平价

    python3 scripts/fair_price.py 1.95 1.85
    # 直接传参: 主队赔率 客队赔率
"""
import sys


def calc_fair(h_odds: float, a_odds: float) -> dict:
    """输入 Pinnacle 主/客赔率，返回去抽水后的公平价。"""
    imp_h = 1.0 / h_odds
    imp_a = 1.0 / a_odds
    vig = imp_h + imp_a - 1.0
    prob_h = imp_h / (1.0 + vig) if vig > 0 else imp_h
    prob_a = imp_a / (1.0 + vig) if vig > 0 else imp_a

    return {
        "pinnacle_home": h_odds,
        "pinnacle_away": a_odds,
        "fair_home": round(1.0 / prob_h, 2),
        "fair_away": round(1.0 / prob_a, 2),
        "prob_home": round(prob_h * 100, 1),
        "prob_away": round(prob_a * 100, 1),
        "vig_pct": round(vig * 100, 2),
    }


def main():
    if len(sys.argv) == 3:
        h = float(sys.argv[1])
        a = float(sys.argv[2])
        r = calc_fair(h, a)
    else:
        print("\nPinnacle 赔率 → 公平价")
        print("=" * 40)
        try:
            h = float(input("  主队赔率: "))
            a = float(input("  客队赔率: "))
        except (ValueError, EOFError):
            print("  输入无效")
            sys.exit(1)
        r = calc_fair(h, a)

    print()
    print(f"  Pinnacle:    {r['pinnacle_home']:.2f} / {r['pinnacle_away']:.2f}")
    print(f"  抽水:        {r['vig_pct']}%")
    print(f"  ─────────────────────")
    print(f"  公平价:      {r['fair_home']:.2f} / {r['fair_away']:.2f}")
    print(f"  真实概率:    {r['prob_home']}% / {r['prob_away']}%")
    print()
    print(f"  判断: BB 主胜 > {r['fair_home']} 或 客胜 > {r['fair_away']} 即为 +EV")
    print()


if __name__ == "__main__":
    main()
