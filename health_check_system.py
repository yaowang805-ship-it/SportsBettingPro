"""
一次性系统健康检查脚本 — 枚举所有已知类别的潜在问题。

用法:
    python3 health_check_system.py
    python3 health_check_system.py --fix       # 自动清理 dead config/stale files
    python3 health_check_system.py --verbose   # 详细输出

检测项目:
  1. 死配置 — settings.py 中定义了但未被 src/ 引用的配置项
  2. 过时数据文件 — data/storage/ 中超过 14 天未修改的非核心文件
  3. 关键文件新鲜度 — 核心数据文件是否在合理时间内更新
  4. 导入完整性 — 关键模块能否成功 import
  5. 死代码文件 — src/ 中不被任何活跃模块引用的 .py 文件
  6. 环境变量检查 — .env 中是否有必需变量
  7. 磁盘用量 — data/storage/ 和 models/ 的用量
"""
import importlib
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SRC_DIR = BASE_DIR / "src"
DATA_DIR = BASE_DIR / "data" / "storage"
MODEL_DIR = BASE_DIR / "models"
CONFIG_DIR = BASE_DIR / "config"

# 核心文件 — 必须在合理时间内更新
CRITICAL_FILES = {
    "BB 提取数据": DATA_DIR / "bb_odds_extracted.json",
    "BB 快照": DATA_DIR / "bb_odds_snapshot.json",
    "BB 对比结果": DATA_DIR / "bb_vs_pinnacle_comparison.json",
    "FB 提取数据": DATA_DIR / "bb_odds_extracted_FB.json",
    "FB 对比结果": DATA_DIR / "bb_vs_pinnacle_comparison_FB.json",
    "Pinnacle 联赛结构": DATA_DIR / "pinnacle_league_structure.json",
}

# 这些文件虽然是旧的/不活跃的，但可能仍被引用，需要额外确认
BORDERLINE_PATTERNS = [
    "football_history.csv",
    "history.csv",
    "espn_history.csv",
]

# Settings 中明确可以保留的配置（即使不被 src/ 引用）
SETTINGS_KEEP = {
    "BASE_DIR", "DATA_DIR", "MODEL_DIR", "ENV_FILE",
    "DINGTALK_WEBHOOK", "DINGTALK_KEYWORD", "DATABASE_URL",
    "DEFAULT_BUDGET", "MAX_SINGLE_BET_PCT", "MAX_TOTAL_EXPOSURE",
    "KELLY_FRACTION", "MIN_EDGE",
    "ODDS_API_KEYS", "BSD_API_KEY", "BALLDONTLIE_API_KEY",
    "FOOTBALL_DATA_API_KEY", "PRE_BET_ODDS_VALIDATION", "MAX_ODDS_SLIPPAGE",
    "_load_env_file", "_is_placeholder_webhook", "send_dingtalk",
}


# =====================================================================
# 检查项
# =====================================================================

def c1_dead_config():
    """检测 settings.py 中定义了但未被 src/ 引用的配置。"""
    results = []
    settings_file = CONFIG_DIR / "settings.py"
    if not settings_file.exists():
        return [("SKIP", "settings.py 不存在")]

    # 提取 settings.py 中定义的顶层变量
    content = settings_file.read_text()
    defined = set()
    for m in re.finditer(r'^([A-Z][A-Z0-9_]+)\s*=', content, re.MULTILINE):
        defined.add(m.group(1))
    for m in re.finditer(r'^([a-z_][a-z0-9_]*)\s*=\s*os\.getenv', content, re.MULTILINE):
        defined.add(m.group(1))

    # 查找 src/ 中的引用（不包括 config/ 自身和 __pycache__）
    referenced = set()
    for py_file in SRC_DIR.rglob("*.py"):
        if "__pycache__" in str(py_file):
            continue
        text = py_file.read_text(encoding="utf-8", errors="ignore")
        for name in defined:
            if name in text:
                referenced.add(name)

    dead = defined - referenced - SETTINGS_KEEP
    for name in sorted(dead):
        results.append(("WARN", f"settings.py: {name} 定义了但未被 src/ 引用"))
    if not dead:
        results.append(("OK", "settings.py 无死配置"))

    # 检查 MODELS_DIR 引用但不存在的目录
    return results


def c2_stale_data_files():
    """检测 data/storage/ 中超过 30 天未修改的非核心文件。"""
    results = []
    if not DATA_DIR.exists():
        return [("SKIP", "data/storage/ 不存在")]

    core_names = {f.name for f in CRITICAL_FILES.values()}
    cutoff = time.time() - 30 * 86400
    stale = []
    for f in sorted(DATA_DIR.iterdir()):
        if f.is_dir():
            continue
        if f.name in core_names:
            continue
        if f.name.startswith("."):
            continue
        if f.stat().st_mtime < cutoff:
            size_kb = f.stat().st_size / 1024
            age_d = (time.time() - f.stat().st_mtime) / 86400
            stale.append((f.name, age_d, size_kb))

    if stale:
        total_kb = sum(s[2] for s in stale)
        results.append(("WARN", f"{len(stale)} 个文件超过 30 天未修改 ({total_kb:.0f} KB)"))
        for name, age_d, size_kb in sorted(stale, key=lambda x: -x[1]):
            results.append(("INFO", f"  {name}  ({age_d:.0f} 天, {size_kb:.0f} KB)"))
    else:
        results.append(("OK", "data/storage/ 无过时文件"))

    return results


def c3_critical_file_freshness():
    """检查核心数据文件是否存在以及是否在合理时间内更新。"""
    results = []
    now = time.time()
    for label, path in CRITICAL_FILES.items():
        if not path.exists():
            results.append(("FAIL", f"{label} 不存在: {path.name}"))
            continue
        age_h = (now - path.stat().st_mtime) / 3600
        if age_h > 48:
            results.append(("WARN", f"{label} 已 {age_h:.0f} 小时未更新"))
        elif age_h > 12:
            results.append(("INFO", f"{label} {age_h:.0f} 小时前更新"))
        else:
            results.append(("OK", f"{label} {age_h:.1f} 小时前更新"))
    return results


def c4_import_check():
    """检查关键模块能否成功 import。"""
    results = []
    sys.path.insert(0, str(BASE_DIR))
    modules = [
        "config.settings",
        "config.dingtalk",
        "config.logging_config",
        "src.scrapers.bb_api_fetcher",
        "src.scrapers.bb_vs_pinnacle",
        "src.scrapers.bb_incremental_scanner",
        "src.report.bb_ev_push",
        "src.monitor.auto_settle",
        "src.core.pipeline_orchestrator",
    ]
    for mod_name in modules:
        try:
            importlib.import_module(mod_name)
            results.append(("OK", f"import {mod_name}"))
        except Exception as e:
            results.append(("FAIL", f"import {mod_name}: {e}"))
    return results


def c5_dead_py_files():
    """检测 src/ 中不被任何其他活跃模块引用的 .py 文件。

    策略：找到定义函数/类的文件，看是否有其他文件 from/import 它。
    只标记"没有其他文件引用"的模块。
    """
    results = []
    all_py = []
    for f in SRC_DIR.rglob("*.py"):
        if "__pycache__" in str(f) or "/." in str(f):
            continue
        rel = f.relative_to(BASE_DIR)
        all_py.append((rel, f))

    # 构建模块名 → 被引用计数
    module_refs = {}
    for rel, fpath in all_py:
        module_name = str(rel.with_suffix("")).replace("/", ".")
        module_refs[module_name] = 0

    # 统计引用
    for rel, fpath in all_py:
        text = fpath.read_text(encoding="utf-8", errors="ignore")
        for mod in module_refs:
            if mod == str(rel.with_suffix("")).replace("/", "."):
                continue  # 自身引用不算
            # 检查 "from module import" 或 "import module" 或 "import_module('module')"
            if f"from {mod} import" in text or f"import {mod}" in text or f"'{mod}'" in text:
                module_refs[mod] += 1

    known_orphans_ok = {
        # 这些文件是入口点/脚本，不需要被 import
        "src.scrapers.bb_incremental_scanner",
        "src.core.pipeline_orchestrator",
        "src.health_check_system",
        "health_check_system",
    }

    dead = []
    for mod, refs in sorted(module_refs.items()):
        if refs > 0:
            continue
        if mod.startswith("src.") and mod.endswith("__init__"):
            continue
        if mod in known_orphans_ok:
            continue
        dead.append(mod)

    # 只报告可疑的 — 排除了测试文件
    suspicious = [m for m in dead if not m.startswith("tests.") and not m.startswith("test_")]
    if suspicious:
        results.append(("WARN", f"{len(suspicious)} 个模块无引用 (可能死代码)"))
        for m in suspicious[:15]:
            results.append(("INFO", f"  {m}"))
        if len(suspicious) > 15:
            results.append(("INFO", f"  ... 及 {len(suspicious)-15} 个更多"))
    else:
        results.append(("OK", "无孤立模块"))

    return results


def c6_disk_usage():
    """检查 data/storage/ 和 models/ 磁盘用量。"""
    results = []
    for label, path in [("data/storage/", DATA_DIR), ("models/", MODEL_DIR)]:
        if not path.exists():
            results.append(("INFO", f"{label} 不存在"))
            continue
        total_kb = sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1024
        file_count = len(list(path.rglob("*")))
        if total_kb > 50000:
            results.append(("WARN", f"{label} {total_kb/1024:.0f} MB ({file_count} 文件)"))
        else:
            results.append(("OK", f"{label} {total_kb/1024:.1f} MB ({file_count} 文件)"))
    return results


def c8_dns_guard():
    """检测 DNS 劫持并自动修复。"""
    results = []
    try:
        from src.core.dns_guard import check_and_fix
        dns_results = check_and_fix()
        for hostname, r in dns_results.items():
            if r["hijacked"]:
                if r["fixed"]:
                    results.append(("WARN", f"DNS劫持已修复: {r['label']} ({r['local_ip']} → {r['real_ips'][0]})"))
                else:
                    results.append(("FAIL", f"DNS劫持无法修复: {r['label']}"))
            else:
                results.append(("OK", f"DNS正常: {r['label']}"))
    except ImportError as e:
        results.append(("SKIP", f"dns_guard 不可用: {e}"))
    except Exception as e:
        results.append(("WARN", f"DNS检查异常: {e}"))
    return results


def c7_env_check():
    """检查 .env 是否包含必需的变量。"""
    results = []
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return [("WARN", ".env 文件不存在")]

    required = ["DINGTALK_WEBHOOK"]
    content = env_file.read_text()
    for var in required:
        if var not in content:
            results.append(("FAIL", f".env 缺少 {var}"))

    # 检查是否仍有占位 token
    if "your_token" in content.lower():
        results.append(("WARN", ".env 中仍有占位 webhook URL"))

    if not results:
        results.append(("OK", ".env 存在且包含必要变量"))
    return results


# =====================================================================
# 报告生成
# =====================================================================

SEVERITY_ORDER = {"FAIL": 0, "WARN": 1, "INFO": 2, "OK": 3, "SKIP": 4}


def print_report(all_results, verbose=False):
    """格式化输出检查报告。"""
    for check_name, results in all_results:
        label = check_name.split("_", 1)[1] if "_" in check_name else check_name
        label = label.replace("_", " ").title()
        print(f"\n── {label} ──")

        worst = "OK"
        for severity, msg in results:
            worst = min(worst, severity, key=lambda s: SEVERITY_ORDER.get(s, 9))

        icon = {"FAIL": "❌", "WARN": "⚠️ ", "OK": "✅", "INFO": "ℹ️ ", "SKIP": "⏭️ "}
        for severity, msg in results:
            if severity == "INFO" and not verbose:
                continue
            print(f"  {icon.get(severity, '·')} {msg}")


def main():
    verbose = "--verbose" in sys.argv

    print("=" * 55)
    print(f"  系统健康检查  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 55)

    checks = [
        ("c8_dns_guard", c8_dns_guard()),
        ("c1_dead_config", c1_dead_config()),
        ("c7_env_check", c7_env_check()),
        ("c4_import_check", c4_import_check()),
        ("c3_critical_file_freshness", c3_critical_file_freshness()),
        ("c2_stale_data_files", c2_stale_data_files()),
        ("c6_disk_usage", c6_disk_usage()),
        ("c5_dead_py_files", c5_dead_py_files()),
    ]

    print_report(checks, verbose)

    # 统计
    all_msgs = [m for _, results in checks for m in results]
    fail = sum(1 for s, _ in all_msgs if s == "FAIL")
    warn = sum(1 for s, _ in all_msgs if s == "WARN")
    info = sum(1 for s, _ in all_msgs if s == "INFO")
    ok = sum(1 for s, _ in all_msgs if s == "OK")

    print(f"\n{'='*55}")
    print(f"  总结: ❌ {fail}  ⚠️  {warn}  ℹ️  {info}  ✅ {ok}")
    print(f"  建议: {'检查 FAIL/WARN 项' if fail + warn > 0 else '系统健康'}")
    print(f"{'='*55}")

    return 1 if fail > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
