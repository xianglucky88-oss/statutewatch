"""路径与规则版本配置。

规则版本（rule_version）是"清理 + 指纹 + 差异"算法的代次标识：
每次规则升级都会分配新版本号，并随版本/差异行落库，从而保证
历史结论可以用当时的规则重新复现，而不是被新规则静默改写。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "input.json"
REGULATIONS_FIXTURE = ROOT / "fixtures" / "regulations.json"
DB_PATH = ROOT / "statutewatch.db"

# 当前规则版本；历史版本保留在元组中，diff.diff_versions 可显式指定。
RULE_VERSION = "2026.1"
RULE_VERSIONS = ("2026.0", "2026.1")

# 规则变更说明（随库保存，解释每个版本的行为差异，支持历史复现）。
RULE_CHANGELOG = {
    "2026.0": "基线规则：整篇按行 difflib 比对，不做条款对齐与实质 token 分类。",
    "2026.1": "条款边界切分 + 条款指纹对齐；识别重排/拆并/增删/替换，"
              "对数字、单位、否定词变化做实质判定，缺页/OCR 降级可信度。",
}
