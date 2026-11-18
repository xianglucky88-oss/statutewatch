"""版式噪声清理与条文边界切分（normalizer）。

职责边界：只做"采集文本 → 可比对条文"的规范化，不做身份判断、不做差异结论。

硬性约束（来自需求）：
1. 清理只去除版式噪声（全角空格、制表符、页眉页脚、行号、重复空白等），
   数字、单位、否定词、标点和条文边界必须原样保留语义；
2. OCR 不确定标记（??、[?]、<unclear>、□ 等）原样保留，绝不"猜补"修正；
3. 缺页/解析失败标记（【缺页…】等）保留为 Gap 证据，其附近的删除不能报确定删除；
4. 每条条文保留 raw（原始片段）与 clean（清理后文本），差异结论回查 raw。
"""
from __future__ import annotations

import re

from .models import Clause, Gap

# ---- OCR / 缺页标记 -------------------------------------------------------
# OCR 不确定：连续问号、括号问号、乱码方块、<unclear> 等。命中即标记，文本原样保留。
OCR_MARKER_RE = re.compile(
    r"(?:\[\?+\]|（\?+）|\(\?+\)|【\?+】|<unclear>.*?</unclear>"
    r"|[□�]|[?？]{2,})"
)
# 缺页 / 局部解析失败的内嵌标记。
GAP_MARKER_RE = re.compile(r"【(?:缺页|缺失|无法识别|解析失败|OCR[^】]*)[^】]*】")

# ---- 版式噪声 -------------------------------------------------------------
_FULLWIDTH_SPACE_RE = re.compile(r"[　\xa0\t]+")
_MULTI_SPACE_RE = re.compile(r" {2,}")
_SOFT_HYPHEN_RE = re.compile(r"\xad")
_TRAILING_SPACE_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_MULTI_BLANK_LINE_RE = re.compile(r"\n{3,}")
# 行首独立行号：数字后直接跟空白且无标点（条号通常带"条/./、"，不会误伤）。
_LINE_NO_RE = re.compile(r"^[ \t]*\d{1,4}[ \t]+(?=\S)", re.MULTILINE)
# 页眉页脚：第 X 页 / - X - / Page X，整行删除。
_PAGE_FOOTER_RE = re.compile(
    r"^[ \t]*(?:第\s*\d+\s*页|[-–—]\s*\d+\s*[-–—]|[Pp]age\s+\d+)[ \t]*$",
    re.MULTILINE,
)

# ---- 条文边界 -------------------------------------------------------------
# 第X条（中文数字/阿拉伯数字）；阿拉伯编号 1. / 1、；括号编号（1）/（1）。
_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?m)^[ \t]*(?:第[一二三四五六七八九十百零〇\d]+条"
    r"|\d+(?:\.\d+)*[.、]"
    r"|[（(]\d+[）)])"
)

# ---- 实质内容 token（数字/单位/否定词；差异模块共用） ----------------------
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*%?")
# 长单位在前，避免"平方米"被"米"截断。
UNITS = (
    "千瓦时", "平方米", "立方米", "分贝", "毫克", "微克", "千克", "公里",
    "厘米", "毫米", "毫升", "万元", "亿元", "个月", "小时", "分钟",
    "吨", "米", "升", "度", "日", "天", "年", "秒", "倍",
    "%", "‰", "℃",
)
NEGATIONS = ("不得", "禁止", "严禁", "不予", "不再", "不", "无", "未", "非", "勿")
_UNIT_RE = re.compile("|".join(sorted(UNITS, key=len, reverse=True)))
_NEGATION_RE = re.compile("|".join(NEGATIONS))

_PUNCT_SPACE_RE = re.compile(r"[\s，。；：、,.；;:：！!？?（）()【】\[\]\"'‘’“”·…—\-]+")


def clean_layout(text: str) -> str:
    """清理版式噪声。不删除任何数字/单位/否定词/OCR 标记。"""
    text = _SOFT_HYPHEN_RE.sub("", text)
    text = _PAGE_FOOTER_RE.sub("", text)
    text = _FULLWIDTH_SPACE_RE.sub(" ", text)
    text = _LINE_NO_RE.sub("", text)
    text = _TRAILING_SPACE_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_BLANK_LINE_RE.sub("\n\n", text)
    return text.strip()


def has_ocr_uncertain(text: str) -> bool:
    return bool(OCR_MARKER_RE.search(text))


def embedded_gaps(text: str) -> list[Gap]:
    return [Gap(locator=m.group(0), reason="embedded-marker")
            for m in GAP_MARKER_RE.finditer(text)]


def split_clauses(body_text: str, doc_id: str) -> list[Clause]:
    """按条文边界切分，保留 raw/clean 双份文本与稳定 fragment_id。"""
    # 缺页/解析失败标记先拆到独立行，避免被并入相邻条文后无法单独识别。
    body_text = GAP_MARKER_RE.sub(lambda m: "\n" + m.group(0) + "\n", body_text)
    matches = list(_CLAUSE_BOUNDARY_RE.finditer(body_text))
    blocks: list[tuple[str, str]] = []
    if not matches:
        blocks.append(("preamble", body_text.strip()))
    else:
        head = body_text[: matches[0].start()].strip()
        if head:
            blocks.append(("preamble", head))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body_text)
            block = body_text[m.start():end].strip()
            clause_no = block.split(None, 1)[0].split("\n", 1)[0].strip()
            blocks.append((clause_no, block))

    clauses: list[Clause] = []
    pos = 0

    def emit(clause_no: str, raw_block: str):
        nonlocal pos
        raw_block = raw_block.strip()
        if not raw_block:
            return
        clean = clean_layout(raw_block)
        clauses.append(Clause(
            clause_no=clause_no,
            raw=raw_block,
            clean=clean,
            position=pos,
            fragment_id=f"frag:{doc_id}:clause:{pos}",
            has_ocr_uncertain=has_ocr_uncertain(raw_block),
        ))
        pos += 1

    for clause_no, raw_block in blocks:
        # 独占整行的缺页标记拆成独立块，不与条文文本混在一起。
        lines = [ln for ln in raw_block.split("\n")]
        body_lines, marker_lines = [], []
        for ln in lines:
            if GAP_MARKER_RE.fullmatch(ln.strip()):
                marker_lines.append(ln.strip())
            else:
                body_lines.append(ln)
        body_block = "\n".join(body_lines).strip()
        if body_block:
            emit(clause_no, body_block)
        for marker in marker_lines:
            emit("gap-marker", marker)
    return clauses


def normalize_document(doc) -> dict:
    """规范化一份 RegulationDocument，返回下游使用的标准结构。"""
    gaps = list(doc.gaps) + embedded_gaps(doc.body_text)
    clauses = split_clauses(doc.body_text, doc.doc_id)
    # 内嵌缺页/解析失败标记：自身条文不可作为正常内容，相邻条文的结论需降级。
    gap_clause_positions: set = set()
    for idx, c in enumerate(clauses):
        if GAP_MARKER_RE.search(c.raw):
            c.has_ocr_uncertain = True
            gap_clause_positions.add(c.position)
            gap_clause_positions.update(
                p for p in (c.position - 1, c.position + 1) if p >= 0
            )
    return {
        "doc_id": doc.doc_id,
        "batch_id": doc.batch_id,
        # gap-marker 伪条文不参与内容对齐，仅作为 Gap 证据保留在 gaps 中；
        # 条文文本内夹带的标记仍随条文原样留存。
        "clauses": [c for c in clauses if c.clause_no != "gap-marker"],
        "gaps": gaps,
        "gap_locators": {g.locator for g in gaps},
        "gap_clause_positions": gap_clause_positions,
        "attachments": list(doc.attachments),
        "source_locator": doc.source_locator,
    }


# ---- 实质 token 提取（差异模块共用） --------------------------------------
def extract_numbers(text: str) -> list[str]:
    return NUMBER_RE.findall(text)


def extract_units(text: str) -> list[str]:
    return _UNIT_RE.findall(text)


def extract_negations(text: str) -> list[str]:
    return _NEGATION_RE.findall(text)


def punctuation_only_equal(a: str, b: str) -> bool:
    """去除空白与标点后相等 → 差异仅为版式/标点噪声（cosmetic）。"""
    return _PUNCT_SPACE_RE.sub("", a) == _PUNCT_SPACE_RE.sub("", b)


def substantive_changes(old: str, new: str) -> dict:
    """比较数字/单位/否定词三类实质 token 的变化。"""
    old_n, new_n = extract_numbers(old), extract_numbers(new)
    old_u, new_u = extract_units(old), extract_units(new)
    old_neg, new_neg = extract_negations(old), extract_negations(new)
    return {
        "changed_numbers": sorted(set(new_n) - set(old_n))
        + sorted(set(old_n) - set(new_n)),
        "changed_units": sorted(set(new_u) - set(old_u))
        + sorted(set(old_u) - set(new_u)),
        "negation_changed": old_neg != new_neg,
    }
