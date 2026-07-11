"""输出校准器 — 确保所有面向用户的内容为中文。

用法:
    from src.report.validator import validate_output
    issues = validate_output(text, context="桌面文件")
    if issues:
        for i in issues: log.warning("校准: %s", i)
"""
import re
from typing import List

# 已知英文队名（如果出现在输出中说明未转换）
EN_TEAM_PATTERNS = [
    # 常见足球俱乐部
    r'\bFC\b', r'\bUnited\b', r'\bCity\b', r'\bReal\b', r'\bAthletic\b',
    r'\bInter\b', r'\bMilan\b', r'\bChelsea\b', r'\bArsenal\b', r'\bLiverpool\b',
    r'\bBarcelona\b', r'\bMadrid\b', r'\bJuventus\b', r'\bBayern\b', r'\bDortmund\b',
    r'\bPSG\b', r'\bRoma\b', r'\bNapoli\b', r'\bTottenham\b',
    # 国家队/常见英文名
    r'\bEngland\b', r'\bFrance\b', r'\bGermany\b', r'\bSpain\b', r'\bItaly\b',
    r'\bBrazil\b', r'\bArgentina\b', r'\bNetherlands\b', r'\bPortugal\b',
    r'\bBelgium\b', r'\bCroatia\b', r'\bSwitzerland\b', r'\bPoland\b',
    r'\bSouth\b', r'\bKorea\b', r'\bJapan\b', r'\bAustralia\b', r'\bIran\b',
    # 系统前缀
    r'\bshop\b', r'\bline_shop\b', r'\bfootball_\b', r'\bWorld_Cup\b',
]

# 英文结果标签
EN_OUTCOMES = ['home', 'away', 'draw', '^yes$', '^no$', 'over_', 'under_',
               'line_shopping', '1X', 'X2']

# 需要跳过（中文体育文本中的正常词汇）
SKIP_PATTERNS = [
    r'\bvs\b',           # A vs B 是正常体育用语
    r'\bmin\b',          # "15min后"
    r'\bh\b',            # "48h后"
    r'@',                # "¥100 @ 1.5"
    r'¥',                # 货币符号
    r'\+¥', r'\-¥',     # 金额
    r'\d+\.\d+',         # 数字（赔率）
]


def validate_output(text: str, context: str = "") -> List[str]:
    """扫描文本，返回发现的中文化问题列表。

    Args:
        text: 待检查的文本
        context: 描述来源（用于日志）

    Returns:
        问题描述列表，空列表表示通过
    """
    issues = []
    lines = text.split('\n')

    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith('=') or stripped.startswith('-') or stripped.startswith('【'):
            continue

        # 检查是否包含任何中文
        has_cn = bool(re.search(r'[一-鿿]', stripped))

        for pat in EN_TEAM_PATTERNS:
            matches = re.finditer(pat, stripped, re.IGNORECASE)
            for m in matches:
                word = m.group()
                # 跳过在中文行中偶尔出现的英文（如 "vs"）
                if has_cn and word.lower() in ('vs',):
                    continue
                # 跳过数字/金额相关的模式
                if re.match(r'^\d+\.?\d*$', word):
                    continue
                issues.append(f"[{context}:L{lineno}] 英文队名? '{word}' → {stripped[:60]}")
                break  # 一行只报第一个

        # 检查英文结果标签
        for pat in EN_OUTCOMES:
            if re.search(pat, stripped, re.IGNORECASE):
                # 确认不是中文行里的正常字符
                if not has_cn:
                    issues.append(f"[{context}:L{lineno}] 英文标签 '{pat}' → {stripped[:60]}")
                    break

    return issues


def assert_all_chinese(text: str, context: str = "") -> None:
    """断言全部为中文，发现英文直接抛出异常（用于测试）。"""
    issues = validate_output(text, context)
    if issues:
        msg = f"❌ 校准失败 ({context}):\n" + "\n".join(issues)
        raise AssertionError(msg)
