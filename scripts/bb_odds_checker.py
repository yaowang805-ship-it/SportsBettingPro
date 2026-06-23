#!/usr/bin/env python3
"""BB体育 赔率抓取 + 与 Pinnacle 公平价对比。
用法: python3 scripts/bb_odds_checker.py

首次使用:
  1. 登录 BB体育
  2. 按 Cmd+Option+I → Network → 找 user/base 请求
  3. 从 Request Headers 复制 Authorization 后面的 token
  4. 运行: python3 scripts/bb_odds_checker.py
  5. 按提示粘贴 token
"""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.core.team_names import cn_team

API_BASE = "https://api.nsvip9.com/v1"

OUTCOME_CN = {"home": "主胜", "draw": "平局", "away": "客胜"}


def api_call(path: str, params: dict, token: str) -> dict:
    url = f"{API_BASE}/{path}"
    data = json.dumps(params).encode()
    req = Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        resp = urlopen(req, timeout=15)
        return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode()
        return {"code": e.code, "message": body[:200]}
    except Exception as e:
        return {"code": -1, "message": str(e)}


def get_token() -> str:
    token_file = Path(__file__).parent.parent / "data" / "storage" / "bb_token.txt"
    saved = ""
    if token_file.exists():
        saved = token_file.read_text().strip()
        print(f"  上次保存的 token: {saved[:20]}...")
        use_saved = input("  用上次的? (Y/n): ").strip().lower()
        if use_saved in ("", "y", "yes"):
            return saved

    print()
    token = input("  粘贴 token (Authorization Bearer 后面的值): ").strip()
    if token:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token)
    return token


def main():
    parser = argparse.ArgumentParser(description="BB体育赔率抓取 + 对比Pinnacle公平价")
    parser.add_argument("--token", help="直接提供 token，跳过交互输入")
    args = parser.parse_args()

    print("=" * 70)
    print("  BB体育 赔率抓取 + +EV 对比")
    print("=" * 70)
    print()

    token = args.token or get_token()
    if not token:
        print("  ❌ 未提供 token")
        sys.exit(1)

    # 1. 获取比赛列表
    print("\n  📡 获取比赛列表...")
    result = api_call("match/getList", {"page": 1, "pageSize": 50}, token)
    if result.get("code") not in (0, 1):
        print(f"  ❌ API 错误: {result.get('message', '未知')}")
        if "login" in str(result).lower():
            print("  💡 token 可能已过期，请重新登录 BB体育 后获取新 token")
        sys.exit(1)

    matches = result.get("data", [])
    print(f"  ✅ 获取到 {len(matches)} 场比赛")

    # 2. 获取每场比赛详情
    print("\n  📡 获取赔率详情...")
    all_odds = []

    def extract_1x2_odds(data: dict) -> dict:
        """从 BB体育 getMatchDetail 响应中提取独赢(1X2)赔率。
        已知结构: data.mg[].mty==1005 (独赢) + pe==1001 (全场) → mks[0].op[]
        op[].ty: 1=主, 2=客, 3=平;  od=小数赔率
        """
        result = {"home_odds": None, "draw_odds": None, "away_odds": None}

        mg_list = data.get("mg", [])
        if not isinstance(mg_list, list):
            return result

        # 找 独赢(1005) + 全场(1001) 的市场组
        group = None
        for g in mg_list:
            if g.get("mty") == 1005 and g.get("pe") == 1001:
                group = g
                break
        if not group:
            return result

        mks_list = group.get("mks", [])
        if not mks_list:
            return result

        op_list = mks_list[0].get("op", [])
        for opt in op_list:
            ty = opt.get("ty")
            od = opt.get("od")
            if ty == 1:
                result["home_odds"] = od
            elif ty == 2:
                result["away_odds"] = od
            elif ty == 3:
                result["draw_odds"] = od

        return result

    for m in matches:
        mid = m.get("id") or m.get("stId")
        detail = api_call("match/getMatchDetail", {"id": mid}, token)
        if detail.get("code") not in (0, 1):
            print(f"  ⏭️ {m.get('homeName','?')} vs {m.get('awayName','?')}: API {detail.get('code')}")
            continue

        if detail.get("success") is False:
            print(f"  ⏭️ {m.get('homeName','?')} vs {m.get('awayName','?')}: 接口返回失败")
            continue

        data = detail.get("data", {})
        odds_data = extract_1x2_odds(data)
        odds_data["match"] = f"{m.get('homeName','?')} vs {m.get('awayName','?')}"
        odds_data["match_id"] = mid
        odds_data["home_name"] = m.get("homeName")
        odds_data["away_name"] = m.get("awayName")
        odds_data["match_time"] = m.get("matchTime")

        if odds_data["home_odds"]:
            all_odds.append(odds_data)
            print(f"  ✅ {odds_data['match']}: {odds_data['home_odds']} / {odds_data['draw_odds']} / {odds_data['away_odds']}")
        else:
            print(f"  ⏭️ {m.get('homeName','?')} vs {m.get('awayName','?')}: 未找到独赢赔率")

    # 计算 BB体育 平均抽水
    bb_vig_rates = []
    for odds in all_odds:
        h, d, a = odds.get("home_odds"), odds.get("draw_odds"), odds.get("away_odds")
        if h and d and a:
            v = 1/float(h) + 1/float(d) + 1/float(a) - 1
            if 0 < v < 0.20:
                bb_vig_rates.append(v)
    bb_avg_vig = sum(bb_vig_rates) / len(bb_vig_rates) if bb_vig_rates else 0.055
    print(f"\n  📊 BB体育 平均抽水: {bb_avg_vig*100:.2f}% (基于 {len(bb_vig_rates)} 场)")

    # 3. 加载系统 Pinnacle 公平价（如果有）
    system_file = Path(__file__).parent.parent / "data" / "storage" / "line_shopping_results.json"
    system_odds = {}
    if system_file.exists():
        try:
            sys_data = json.loads(system_file.read_text())
            for opp in sys_data.get("opportunities", []):
                key = f"{opp['home_team']} vs {opp['away_team']}"
                if key not in system_odds:
                    system_odds[key] = {}
                system_odds[key][opp["outcome"]] = {
                    "fair_price": round(1.0 / opp["model_prob"], 2),
                    "model_prob": opp["model_prob"],
                    "retail_odds": opp["retail_odds"],
                    "retail_bm": opp["retail_bookmaker"],
                }
        except Exception:
            pass

    # 4. 对比输出
    print("\n" + "=" * 70)
    print("  📊 BB体育 vs 公平价(含BB抽水) 对比")
    print("=" * 70)
    print(f"  💡 公平价已按 BB体育 平均抽水 {bb_avg_vig*100:.2f}% 调整")

    has_system = len(system_odds) > 0
    if not has_system:
        print("  ⏭️ 本地无比对数据（先跑一次 main.py 生成）")
        print()

    # 对 team name 做简单标准化，提高匹配率
    def norm_name(n):
        return n.strip().lower().replace("&", "and")

    found_any = False
    for odds in all_odds:
        home_en = odds["home_name"]
        away_en = odds["away_name"]
        home_cn = cn_team(home_en, "football")
        away_cn = cn_team(away_en, "football")
        h_odds, d_odds, a_odds = odds["home_odds"], odds["draw_odds"], odds["away_odds"]

        if not h_odds:
            continue
        found_any = True

        # 计算本场 BB体育 抽水
        imp_h = 1.0 / float(h_odds)
        imp_d = 1.0 / float(d_odds) if d_odds else 0
        imp_a = 1.0 / float(a_odds) if a_odds else 0
        total_imp = imp_h + imp_d + imp_a
        match_vig = (total_imp - 1.0) * 100

        print(f"\n  {home_cn} vs {away_cn}")
        print(f"  BB体育: {h_odds} / {d_odds} / {a_odds} (本场抽水 {match_vig:.2f}%)")
        print(f"  {'':>6} {'BB赔率':>8} {'公平价':>8} {'Edge':>8}")
        print(f"  {'─'*6} {'─'*8} {'─'*8} {'─'*8}")

        for outcome, bb_price in [("home", h_odds), ("draw", d_odds), ("away", a_odds)]:
            if not bb_price:
                continue
            oc_name = OUTCOME_CN[outcome]

            if has_system:
                # 用标准化名称匹配
                key_candidates = [
                    f"{norm_name(home_en)} vs {norm_name(away_en)}",
                    f"{norm_name(home_en)} vs {norm_name(away_en)}",
                ]
                # 也尝试中文键名
                sys_info = None
                for key in [f"{home_en} vs {away_en}",
                            f"{home_cn} vs {away_cn}",
                            f"{norm_name(home_en)} vs {norm_name(away_en)}"]:
                    sys_info = system_odds.get(key, {}).get(outcome)
                    if sys_info:
                        break

                if sys_info:
                    model_prob = sys_info["model_prob"]
                    # 公平价 = 1 / (真实概率 × (1 + BB抽水))
                    adjusted_fair = round(1.0 / (model_prob * (1 + bb_avg_vig)), 2)
                    bb_val = float(bb_price)
                    diff = (bb_val - adjusted_fair) / adjusted_fair * 100

                    if diff > 5:
                        ev_tag = "✅✅ +EV"
                    elif diff > 2:
                        ev_tag = "✅ +EV"
                    elif diff > 0:
                        ev_tag = "⚠️ 微+EV"
                    else:
                        ev_tag = "❌ -EV"
                    print(f"  {oc_name:>4} {bb_val:>8.2f} {adjusted_fair:>8.2f} {diff:>+7.1f}% {ev_tag}")
                else:
                    print(f"  {oc_name:>4} {float(bb_price):>8.2f} {'-':>8} {'无公平价':>12}")
            else:
                print(f"  {oc_name:>4} {float(bb_price):>8.2f} {'-':>8}")

        # 最佳推荐
        if has_system:
            best_edge, best_label, best_price = -999, "", 0
            for outcome, bb_price, label in [
                ("home", h_odds, "主胜"), ("draw", d_odds, "平局"), ("away", a_odds, "客胜")
            ]:
                if not bb_price:
                    continue
                sys_info = None
                for key in [f"{home_en} vs {away_en}",
                            f"{home_cn} vs {away_cn}",
                            f"{norm_name(home_en)} vs {norm_name(away_en)}"]:
                    sys_info = system_odds.get(key, {}).get(outcome)
                    if sys_info:
                        break
                if sys_info:
                    bb_val = float(bb_price)
                    adjusted_fair = 1.0 / (sys_info["model_prob"] * (1 + bb_avg_vig))
                    edge = (bb_val - adjusted_fair) / adjusted_fair * 100
                    if edge > best_edge:
                        best_edge = edge
                        best_label = label
                        best_price = bb_val
            if best_edge > 3:
                print(f"  ⭐ 推荐: {best_label} @ {best_price} — 比公平价高 +{best_edge:.1f}%")

    if not found_any:
        print("\n  ❌ 未找到任何赔率数据")

    print()
    print("=" * 70)
    print(f"  💡 BB体育赔率 > 公平价(含{bb_avg_vig*100:.1f}%抽水) = +EV")
    print("=" * 70)


if __name__ == "__main__":
    main()
