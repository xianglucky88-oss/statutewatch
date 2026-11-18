"""实质差异比较（diff）。

输入两个内容版本的条款列表，输出逐条差异结论：
- 变更类型：reorder / split / merge / added / removed / replaced / cosmetic；
- 可信度：high / medium / low，OCR 不确定或弱对齐降级，缺页附近的删除禁止报确定删除；
- 每条结论附 evidence_fragment_ids，可回查 raw 原始片段；
- 数字、单位、否定词变化单独标注；指纹仅用于辅助对齐，碰撞时退回文本比对。

对齐策略（两阶段）：
1. 条号锚定：同条号先配对，保证"位置变化但条号不变"识别为 reorder 而非替换；
   条号相同但文本变化判为 replaced，若位置同时移动则置 also_reordered；
2. 残余块对齐：未锚定条文用 SequenceMatcher 按文本指纹配对，按块尺寸区分
   1:1 替换、1:n 拆分、n:1 合并、n:m 替换、纯新增、纯删除。

缺页/解析失败（gap_positions）只让缺口相邻条文的结论降级，不会把整篇删除都报不确定。
规则版本由 rule_version 选择（见 config.RULE_CHANGELOG），历史结论可用旧规则复现。
"""
from __future__ import annotations

import difflib

from . import config
from .models import ChangeType, Confidence, DiffItem
from .normalizer import punctuation_only_equal, substantive_changes


def process(record):
    """旧流水线兼容入口（差异比较是跨记录阶段，不参与单记录旧链）。"""
    raise NotImplementedError("diff is a cross-version stage; use diff_versions()")


def _evidence(old_clauses, new_clauses) -> list[str]:
    return [c.fragment_id for c in (old_clauses or []) + (new_clauses or [])]


def _confidence(old_clauses, new_clauses, weak=False, near_gap=False) -> Confidence:
    ocr = any(c.has_ocr_uncertain for c in (old_clauses or []) + (new_clauses or []))
    if near_gap:
        return Confidence.LOW
    if weak or ocr:
        return Confidence.MEDIUM
    return Confidence.HIGH


def _substance_detail(old_text: str, new_text: str) -> tuple[dict, str]:
    sub = substantive_changes(old_text, new_text)
    notes = []
    if sub["changed_numbers"]:
        notes.append(f"数字变化={sub['changed_numbers']}")
    if sub["changed_units"]:
        notes.append(f"单位变化={sub['changed_units']}")
    if sub["negation_changed"]:
        notes.append("否定词变化")
    return sub, "；".join(notes)


def _replacement_item(o, n, near_gap, weak=False) -> DiffItem:
    sub, detail = _substance_detail(o.clean, n.clean)
    reordered = o.position != n.position
    if reordered:
        detail = (detail + "；" if detail else "") + "条文位置同时移动"
    if punctuation_only_equal(o.clean, n.clean):
        return DiffItem(
            change_type=ChangeType.COSMETIC,
            confidence=Confidence.HIGH,
            old_clause_no=o.clause_no, new_clause_no=n.clause_no,
            old_position=o.position, new_position=n.position,
            old_text=o.raw, new_text=n.raw,
            detail="仅版式/标点/空白差异",
            also_reordered=reordered,
            evidence_fragment_ids=_evidence([o], [n]),
        )
    return DiffItem(
        change_type=ChangeType.REPLACED,
        confidence=_confidence([o], [n], weak=weak, near_gap=near_gap),
        old_clause_no=o.clause_no, new_clause_no=n.clause_no,
        old_position=o.position, new_position=n.position,
        old_text=o.raw, new_text=n.raw, detail=detail,
        changed_numbers=sub["changed_numbers"],
        changed_units=sub["changed_units"],
        negation_changed=sub["negation_changed"],
        also_reordered=reordered,
        evidence_fragment_ids=_evidence([o], [n]),
    )


def diff_versions(old_version: dict, new_version: dict,
                  rule_version: str = config.RULE_VERSION,
                  fingerprint_collisions: set | None = None) -> list[DiffItem]:
    """比较两个版本。版本字典至少含 clauses 与 gaps（normalizer 产物）。"""
    if rule_version == "2026.0":
        return _legacy_line_diff(old_version, new_version)
    return _clause_diff(old_version, new_version, fingerprint_collisions or set())


def _legacy_line_diff(old_version, new_version) -> list[DiffItem]:
    """2026.0 基线规则：整篇 clean 文本行级比对，不做条款对齐（历史复现对照）。"""
    old_lines = "\n".join(c.clean for c in old_version["clauses"]).splitlines()
    new_lines = "\n".join(c.clean for c in new_version["clauses"]).splitlines()
    items: list[DiffItem] = []
    for line in difflib.unified_diff(old_lines, new_lines, lineterm=""):
        if line[:2] in ("++", "--", "@@ "):
            continue
        items.append(DiffItem(
            change_type=ChangeType.REPLACED if line[0] in "+-" else ChangeType.COSMETIC,
            confidence=Confidence.MEDIUM,
            old_text=line[1:] if line[0] == "-" else "",
            new_text=line[1:] if line[0] == "+" else "",
            detail="2026.0 基线行级比对",
            evidence_fragment_ids=_evidence(
                old_version["clauses"], new_version["clauses"]),
        ))
    return items


def _clause_diff(old_version, new_version, collisions) -> list[DiffItem]:
    old_all = old_version["clauses"]
    new_all = new_version["clauses"]
    # 缺页/解析失败位置：只有缺口相邻条文降级，不搞整篇连坐。
    old_gap_pos = set(old_version.get("gap_positions", set()))
    new_gap_pos = set(new_version.get("gap_positions", set()))

    def near_gap(clauses) -> bool:
        return any(c.position in old_gap_pos or c.position in new_gap_pos
                   for c in clauses)

    # ---- 阶段 1：条号锚定 ------------------------------------------------
    # ---- 辅助：相似度 / 包含度 -------------------------------------------
    def _ratio(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()

    def _contained(part: str, whole: str) -> bool:
        part, whole = part.strip(), whole.strip()
        if len(part) < 2:
            return False
        # 字符重叠率：拆分后条文通常是原条文的子片段，允许标点/连接词差异。
        overlap = sum(1 for ch in part if ch in whole)
        return overlap / len(part) >= 0.8

    anchored_old: set = set()
    anchored_new: set = set()
    items: list[DiffItem] = []

    # ---- 阶段 0：拆分 / 合并（基数 1:n 或 n:1，先于条号锚定） -------------
    if len(old_all) == 1 and len(new_all) > 1:
        o = old_all[0]
        if all(_contained(n.clean, o.clean) or _ratio(n.clean, o.clean) >= 0.45
               for n in new_all):
            anchored_old.add(o.fragment_id)
            for n in new_all:
                anchored_new.add(n.fragment_id)
            items.append(DiffItem(
                change_type=ChangeType.SPLIT,
                confidence=_confidence(old_all, new_all,
                                       near_gap=near_gap(old_all + new_all)),
                old_clause_no=o.clause_no,
                new_clause_no=",".join(n.clause_no for n in new_all),
                old_position=o.position, new_position=new_all[0].position,
                old_text=o.raw, new_text="\n".join(n.raw for n in new_all),
                detail=f"1 条拆分为 {len(new_all)} 条",
                evidence_fragment_ids=_evidence(old_all, new_all),
            ))
    elif len(new_all) == 1 and len(old_all) > 1:
        n = new_all[0]
        if all(_contained(o.clean, n.clean) or _ratio(o.clean, n.clean) >= 0.45
               for o in old_all):
            anchored_new.add(n.fragment_id)
            for o in old_all:
                anchored_old.add(o.fragment_id)
            items.append(DiffItem(
                change_type=ChangeType.MERGE,
                confidence=_confidence(old_all, new_all,
                                       near_gap=near_gap(old_all + new_all)),
                old_clause_no=",".join(o.clause_no for o in old_all),
                new_clause_no=n.clause_no,
                old_position=old_all[0].position, new_position=n.position,
                old_text="\n".join(o.raw for o in old_all), new_text=n.raw,
                detail=f"{len(old_all)} 条合并为 1 条",
                evidence_fragment_ids=_evidence(old_all, new_all),
            ))

    old_by_no: dict = {}
    for c in old_all:
        old_by_no.setdefault(c.clause_no, []).append(c)
    new_by_no: dict = {}
    for c in new_all:
        new_by_no.setdefault(c.clause_no, []).append(c)
    old_by_text: dict = {}
    for c in old_all:
        old_by_text.setdefault(c.clean, []).append(c)

    for no in old_by_no.keys() & new_by_no.keys():
        oc_list, nc_list = old_by_no[no], new_by_no[no]
        if len(oc_list) != 1 or len(nc_list) != 1:
            continue  # 同号多条（异常编号）留给阶段 2
        o, n = oc_list[0], nc_list[0]
        anchored_old.add(o.fragment_id)
        anchored_new.add(n.fragment_id)
        if o.clean == n.clean:
            if o.position != n.position:
                items.append(DiffItem(
                    change_type=ChangeType.REORDER,
                    confidence=_confidence([o], [n], near_gap=near_gap([o, n])),
                    old_clause_no=o.clause_no, new_clause_no=n.clause_no,
                    old_position=o.position, new_position=n.position,
                    old_text=o.raw, new_text=n.raw,
                    detail="条款内容未变，位置重排",
                    evidence_fragment_ids=_evidence([o], [n]),
                ))
        else:
            # 条号相同文本变化 → 替换；指纹碰撞时同样以文本复核为准。
            items.append(_replacement_item(o, n, near_gap=near_gap([o, n])))

    # 条号未锚定但文本完全相同 → 纯重排（条号可能被改写）。
    for n in new_all:
        if n.fragment_id in anchored_new:
            continue
        for o in old_by_text.get(n.clean, []):
            if o.fragment_id not in anchored_old:
                anchored_old.add(o.fragment_id)
                anchored_new.add(n.fragment_id)
                items.append(DiffItem(
                    change_type=ChangeType.REORDER,
                    confidence=_confidence([o], [n], near_gap=near_gap([o, n])),
                    old_clause_no=o.clause_no, new_clause_no=n.clause_no,
                    old_position=o.position, new_position=n.position,
                    old_text=o.raw, new_text=n.raw,
                    detail="条文内容未变，条号/位置重排",
                    evidence_fragment_ids=_evidence([o], [n]),
                ))
                break

    # ---- 阶段 2：残余块对齐 ----------------------------------------------
    old_rest = [c for c in old_all if c.fragment_id not in anchored_old]
    new_rest = [c for c in new_all if c.fragment_id not in anchored_new]

    # 2a. 高相似度文本预配对（仅 1:1 场景）：条号不同不影响"同一条文演变"判断。
    paired_old: set = set()
    paired_new: set = set()

    if len(old_rest) == 1 and len(new_rest) == 1:
        o, n = old_rest[0], new_rest[0]
        if _ratio(o.clean, n.clean) >= 0.55:
            paired_old.add(o.fragment_id)
            paired_new.add(n.fragment_id)
            if o.clean == n.clean:
                items.append(DiffItem(
                    change_type=ChangeType.REORDER,
                    confidence=_confidence([o], [n], near_gap=near_gap([o, n])),
                    old_clause_no=o.clause_no, new_clause_no=n.clause_no,
                    old_position=o.position, new_position=n.position,
                    old_text=o.raw, new_text=n.raw,
                    detail="条文内容未变，条号变化",
                    evidence_fragment_ids=_evidence([o], [n]),
                ))
            else:
                items.append(_replacement_item(o, n, near_gap=near_gap([o, n])))

    old_rest = [c for c in old_rest if c.fragment_id not in paired_old]
    new_rest = [c for c in new_rest if c.fragment_id not in paired_new]

    def key(c):
        # 残余块只信任条款指纹；无指纹（直造数据/未入库）时给唯一键，
        # 由 opcodes 走纯增删，避免不相似文本被误配成替换。
        fp = getattr(c, "fingerprint", "")
        if fp and fp not in collisions:
            return ("fp", fp)
        return ("uid", c.fragment_id)

    sm = difflib.SequenceMatcher(a=[key(c) for c in old_rest],
                                 b=[key(c) for c in new_rest], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        oc, nc = old_rest[i1:i2], new_rest[j1:j2]
        if tag == "equal":
            for o, n in zip(oc, nc):
                if o.clean != n.clean:  # 指纹碰撞 → 强制文本复核，真实修改不消失。
                    items.append(_replacement_item(o, n, near_gap=near_gap([o, n]),
                                                   weak=True))
                elif o.clause_no != n.clause_no or o.position != n.position:
                    items.append(DiffItem(
                        change_type=ChangeType.REORDER,
                        confidence=_confidence([o], [n], near_gap=near_gap([o, n])),
                        old_clause_no=o.clause_no, new_clause_no=n.clause_no,
                        old_position=o.position, new_position=n.position,
                        old_text=o.raw, new_text=n.raw,
                        detail="条文移动且条号变化",
                        evidence_fragment_ids=_evidence([o], [n]),
                    ))
            continue

        if tag == "insert":
            for n in nc:
                items.append(DiffItem(
                    change_type=ChangeType.ADDED,
                    confidence=_confidence([], [n], near_gap=near_gap([n])),
                    new_clause_no=n.clause_no, new_position=n.position,
                    new_text=n.raw, detail="新增条款",
                    evidence_fragment_ids=_evidence([], [n]),
                ))
            continue

        if tag == "delete":
            for o in oc:
                # 缺页相邻或 OCR 不确定 → 删除只能报 low，不能写成确定删除。
                uncertain = bool(near_gap([o])) or o.has_ocr_uncertain
                items.append(DiffItem(
                    change_type=ChangeType.REMOVED,
                    confidence=Confidence.LOW if uncertain else Confidence.HIGH,
                    old_clause_no=o.clause_no, old_position=o.position,
                    old_text=o.raw,
                    detail=("旧版本缺页/解析失败标记相邻，删除不确定，需人工回查"
                            if uncertain else "旧版本条款在新版本中消失"),
                    evidence_fragment_ids=_evidence([o], []),
                ))
            continue

        # replace：按块尺寸区分 拆 / 并 / 1:1 替换 / 多对多替换。
        fingerprint_aligned = all(
            getattr(c, "fingerprint", "") and getattr(c, "fingerprint", "") not in collisions
            for c in oc + nc)
        if len(oc) == 1 and len(nc) == 1:
            if fingerprint_aligned:
                items.append(_replacement_item(oc[0], nc[0],
                                               near_gap=near_gap(oc + nc)))
            else:
                # 无可信对齐依据（无指纹/未配对）→ 不猜替换，按增删报告。
                for o in oc:
                    items.append(DiffItem(
                        change_type=ChangeType.REMOVED,
                        confidence=_confidence([o], [], near_gap=near_gap([o])),
                        old_clause_no=o.clause_no, old_position=o.position,
                        old_text=o.raw, detail="无可靠对齐依据，按删除报告待人工确认",
                        evidence_fragment_ids=_evidence([o], []),
                    ))
                for n in nc:
                    items.append(DiffItem(
                        change_type=ChangeType.ADDED,
                        confidence=_confidence([], [n], near_gap=near_gap([n])),
                        new_clause_no=n.clause_no, new_position=n.position,
                        new_text=n.raw, detail="无可靠对齐依据，按新增报告待人工确认",
                        evidence_fragment_ids=_evidence([], [n]),
                    ))
        elif len(oc) == 1 and len(nc) > 1:
            o = oc[0]
            items.append(DiffItem(
                change_type=ChangeType.SPLIT,
                confidence=_confidence(oc, nc, near_gap=near_gap(oc + nc)),
                old_clause_no=o.clause_no,
                new_clause_no=",".join(n.clause_no for n in nc),
                old_position=o.position, new_position=nc[0].position,
                old_text=o.raw, new_text="\n".join(n.raw for n in nc),
                detail=f"1 条拆分为 {len(nc)} 条",
                evidence_fragment_ids=_evidence(oc, nc),
            ))
        elif len(oc) > 1 and len(nc) == 1:
            n = nc[0]
            items.append(DiffItem(
                change_type=ChangeType.MERGE,
                confidence=_confidence(oc, nc, near_gap=near_gap(oc + nc)),
                old_clause_no=",".join(o.clause_no for o in oc),
                new_clause_no=n.clause_no,
                old_position=oc[0].position, new_position=n.position,
                old_text="\n".join(o.raw for o in oc), new_text=n.raw,
                detail=f"{len(oc)} 条合并为 1 条",
                evidence_fragment_ids=_evidence(oc, nc),
            ))
        else:
            sub, detail = _substance_detail(
                "\n".join(o.clean for o in oc),
                "\n".join(n.clean for n in nc),
            )
            items.append(DiffItem(
                change_type=ChangeType.REPLACED,
                confidence=_confidence(oc, nc, weak=True, near_gap=near_gap(oc + nc)),
                old_clause_no=",".join(o.clause_no for o in oc),
                new_clause_no=",".join(n.clause_no for n in nc),
                old_position=oc[0].position, new_position=nc[0].position,
                old_text="\n".join(o.raw for o in oc),
                new_text="\n".join(n.raw for n in nc),
                detail=f"多对多替换：{detail}",
                changed_numbers=sub["changed_numbers"],
                changed_units=sub["changed_units"],
                negation_changed=sub["negation_changed"],
                evidence_fragment_ids=_evidence(oc, nc),
            ))

    items.sort(key=lambda it: (it.old_position if it.old_position is not None
                               else (it.new_position or 0),
                               it.change_type.value))
    return items
