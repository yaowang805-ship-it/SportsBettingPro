"""赔率/队名归一化纯工具函数(无任何网络/API 依赖)。

2026-10-18 从 odds_api_io(Betfair) 与 pinnacle_api(旧 arcadia) 迁移而来,
供结算/匹配/队名归一化等纯本地用途复用。系统已切 pinnapi 单锚, 本模块与
任何数据源 API 无关。
"""
import re


# ── 赔率转换 ────────────────────────────────────────────────


def us_to_decimal(us_price):
    """美式赔率 → 十进制。"""
    if us_price is None:
        return None
    if us_price > 0:
        return round(1 + us_price / 100, 4)
    else:
        return round(1 - 100 / us_price, 4)


def get_decimal_price(price_obj: dict) -> float:
    """从 Pinnacle 价格对象中提取十进制赔率。

    兼容两种 API 格式:
    - 新格式: {"price": 147} (美式赔率)
    - 旧格式: {"price_decimal": 2.47} (十进制)
    """
    # 新格式: price (US odds)
    us_price = price_obj.get("price")
    if us_price is not None and isinstance(us_price, (int, float)):
        return us_to_decimal(us_price)
    # 旧格式: price_decimal
    dec = price_obj.get("price_decimal")
    if dec is not None and isinstance(dec, (int, float)) and dec > 1:
        return float(dec)
    return None


# ── 队名归一化 ──────────────────────────────────────────────

_COUNTRY_ALIAS = {
    "czechrepublic": "czechia", "korearepublic": "southkorea", "republicofkorea": "southkorea",
    "koreadpr": "northkorea", "dprkorea": "northkorea", "chinapr": "china",
    "unitedstates": "usa", "unitedstatesofamerica": "usa", "greatbritain": "england",
    "cotedivoire": "ivorycoast", "capeverde": "caboverde",
}


def _norm_team(name):
    # 2026-10-14 网球/羽毛球球员名带国家码后缀 "(USA)"/"(AUT)"/"(GBR)" (BB 侧), Pin 侧是干净名
    # (无国家码), 不剥离则 "Tommy Paul (USA)"→tommypaulusa 永远配不上 Pin 的 tommypaul。
    # 只剥尾部 2-3 大写字母国家码(实测 198/198 网球都带), 不误伤 "(Games)"(5字母) 等 Pin 事件后缀。
    name = re.sub(r'\s*\([A-Z]{2,3}\)\s*$', '', (name or '').strip())
    n = re.sub(r'[^a-z0-9]', '', name.lower())
    # 俱乐部后缀归一(与 pinnacle_live._norm_team 同口径, 2026-09-29 补): FC/SC/CF 等去后缀
    # 2026-10-17 修: 之前 cfc/afc 在 fc 前, "Sanga FC"(sangafc) 被 afc 误剥成 "sang"(少个 a, 配不上 Pin 的 sanga)。
    # 去掉 cfc/afc(与 fc 冲突且作后缀罕见), fc 正常剥。代价: "Sunderland AFC" 会剥成 sunderlanda, 但影响小。
    for suf in ("footballclub", "club", "fc", "sc", "cf", "cd", "ac"):
        if n.endswith(suf) and len(n) > len(suf) + 3:
            n = n[:-len(suf)]
            break
    n = n.replace("saint", "st")
    return _COUNTRY_ALIAS.get(n, n)


# ── 队名模糊匹配 ────────────────────────────────────────────


def _match_score(h1, a1, h2, a2):
    """队名模糊匹配得分(0-100)。精确=100, 主客互换=99, rapidfuzz 双向取大。"""
    from rapidfuzz import fuzz
    nh1, na1, nh2, na2 = _norm_team(h1), _norm_team(a1), _norm_team(h2), _norm_team(a2)
    if nh1 == nh2 and na1 == na2:
        return 100.0
    if nh1 == na2 and na1 == nh2:
        return 99.0
    s1 = fuzz.ratio(nh1, nh2) + fuzz.ratio(na1, na2)
    s2 = fuzz.ratio(nh1, na2) + fuzz.ratio(na1, nh2)
    return max(s1, s2) / 2.0


def _match_score_orient(h1, a1, h2, a2):
    """队名匹配 → (得分, swapped)。swapped=True 表示 BB 主客与 odds-api.io 相反。"""
    from rapidfuzz import fuzz
    nh1, na1, nh2, na2 = _norm_team(h1), _norm_team(a1), _norm_team(h2), _norm_team(a2)
    if nh1 == nh2 and na1 == na2:
        return 100.0, False
    if nh1 == na2 and na1 == nh2:
        return 99.0, True
    s1 = fuzz.ratio(nh1, nh2) + fuzz.ratio(na1, na2)
    s2 = fuzz.ratio(nh1, na2) + fuzz.ratio(na1, nh2)
    return max(s1, s2) / 2.0, s2 > s1
