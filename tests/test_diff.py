"""diff：重排/拆并/增删/替换分类、可信度降级、证据回查、规则版本复现。"""
from src.statutewatch.diff import diff_versions
from src.statutewatch.models import ChangeType, Clause, Confidence


def _cl(no, pos, text, ocr=False):
    return Clause(clause_no=no, raw=text, clean=text, position=pos,
                  fragment_id=f"frag:old:{pos}" if pos < 100 else f"frag:new:{pos-100}",
                  has_ocr_uncertain=ocr)


def _bundle(clauses, gaps=None, gap_positions=None):
    return {"clauses": clauses, "gaps": gaps or [],
            "gap_locators": {g["locator"] for g in (gaps or [])},
            "gap_positions": set(gap_positions or [])}


def _types(items):
    return [i.change_type for i in items]


def test_reorder_detected_when_only_position_changes():
    old = _bundle([_cl("第一条", 0, "甲"), _cl("第二条", 1, "乙"), _cl("第三条", 2, "丙")])
    new = _bundle([Clause(clause_no="第一条", raw="甲", clean="甲", position=0,
                          fragment_id="frag:new:0"),
                   Clause(clause_no="第三条", raw="丙", clean="丙", position=1,
                          fragment_id="frag:new:1"),
                   Clause(clause_no="第二条", raw="乙", clean="乙", position=2,
                          fragment_id="frag:new:2")])
    items = diff_versions(old, new)
    assert _types(items).count(ChangeType.REORDER) == 2
    assert all(i.confidence == Confidence.HIGH for i in items)


def test_threshold_and_negation_changes_are_replacements_with_evidence():
    old = _bundle([_cl("第一条", 0, "限值50毫克，不得超标。")])
    new = _bundle([Clause(clause_no="第一条", raw="限值60毫克，可以超标。",
                          clean="限值60毫克，可以超标。", position=0,
                          fragment_id="frag:new:0")])
    items = diff_versions(old, new)
    assert len(items) == 1 and items[0].change_type == ChangeType.REPLACED
    assert set(items[0].changed_numbers) >= {"50", "60"}
    assert items[0].negation_changed is True
    # 结论可回查原始片段。
    assert items[0].evidence_fragment_ids == ["frag:old:0", "frag:new:0"]


def test_add_and_remove_classification():
    old = _bundle([_cl("第一条", 0, "甲"), _cl("第二条", 1, "乙")])
    new = _bundle([Clause(clause_no="第一条", raw="甲", clean="甲", position=0,
                          fragment_id="frag:new:0"),
                   Clause(clause_no="第三条", raw="丙", clean="丙", position=1,
                          fragment_id="frag:new:1")])
    items = diff_versions(old, new)
    types_ = _types(items)
    # 第一条锚定无差异；第二条删除、第三条新增（无指纹时不猜测替换）。
    assert ChangeType.REMOVED in types_ and ChangeType.ADDED in types_
    assert all(t in (ChangeType.REMOVED, ChangeType.ADDED) for t in types_)
    removed = next(i for i in items if i.change_type == ChangeType.REMOVED)
    added = next(i for i in items if i.change_type == ChangeType.ADDED)
    assert removed.old_clause_no == "第二条"
    assert added.new_clause_no == "第三条"
    assert removed.evidence_fragment_ids == ["frag:old:1"]
    assert added.evidence_fragment_ids == ["frag:new:1"]


def test_gap_near_removed_clause_forces_low_confidence():
    old = _bundle(
        [_cl("第一条", 0, "甲"), _cl("第二条", 1, "乙"), _cl("第三条", 2, "丙")],
        gaps=[{"locator": "第3页", "reason": "missing-page"}],
        gap_positions=[1, 2],
    )
    new = _bundle([Clause(clause_no="第一条", raw="甲", clean="甲", position=0,
                          fragment_id="frag:new:0")])
    items = diff_versions(old, new)
    removed = [i for i in items if i.change_type == ChangeType.REMOVED]
    # 缺页相邻 → 不允许报告确定删除。
    assert all(i.confidence == Confidence.LOW for i in removed)
    assert all("不确定" in i.detail for i in removed)


def test_ocr_uncertain_clause_downgrades_to_medium():
    old = _bundle([_cl("第一条", 0, "限值50毫克。")])
    new = _bundle([Clause(clause_no="第一条", raw="限值5??毫克。", clean="限值5??毫克。",
                          position=0, fragment_id="frag:new:0",
                          has_ocr_uncertain=True)])
    items = diff_versions(old, new)
    assert items[0].confidence == Confidence.MEDIUM
    assert "??" in items[0].new_text  # OCR 不确定内容原样保留，未被修正


def test_split_and_merge():
    old = _bundle([_cl("第一条", 0, "总要求：甲内容。乙内容。")])
    new = _bundle([
        Clause(clause_no="第一条", raw="总要求：甲内容。", clean="总要求：甲内容。",
               position=0, fragment_id="frag:new:0"),
        Clause(clause_no="第一条之一", raw="乙内容。", clean="乙内容。", position=1,
               fragment_id="frag:new:1"),
    ])
    items = diff_versions(old, new)
    assert any(i.change_type == ChangeType.SPLIT for i in items)
    back = diff_versions(new, old)
    assert any(i.change_type == ChangeType.MERGE for i in back)


def test_cosmetic_only_change_not_substantive():
    old = _bundle([_cl("第一条", 0, "限值：50毫克。")])
    new = _bundle([Clause(clause_no="第一条", raw="限值 50 毫克", clean="限值 50 毫克",
                          position=0, fragment_id="frag:new:0")])
    items = diff_versions(old, new)
    assert items[0].change_type == ChangeType.COSMETIC
    assert items[0].changed_numbers == []


def test_fingerprint_collision_falls_back_to_text_comparison():
    # 相同指纹被标记碰撞，且文本实际不同 → 必须报替换，真实修改不能消失。
    old = _bundle([_cl("第一条", 0, "原文50毫克。")])
    new = _bundle([Clause(clause_no="第一条", raw="改后60毫克。", clean="改后60毫克。",
                          position=0, fragment_id="frag:new:0")])
    fp = "deadbeef-collision"
    old["clauses"][0].fingerprint = fp
    new["clauses"][0].fingerprint = fp
    items = diff_versions(old, new, fingerprint_collisions={fp})
    assert items[0].change_type == ChangeType.REPLACED
    assert "60" in items[0].changed_numbers


def test_rule_version_2026_0_legacy_reproducible():
    old = _bundle([_cl("第一条", 0, "限值50毫克。")])
    new = _bundle([Clause(clause_no="第一条", raw="限值60毫克。", clean="限值60毫克。",
                          position=0, fragment_id="frag:new:0")])
    legacy = diff_versions(old, new, rule_version="2026.0")
    current = diff_versions(old, new, rule_version="2026.1")
    assert legacy and current
    assert all(i.detail.startswith("2026.0") for i in legacy)
    assert current[0].change_type == ChangeType.REPLACED


def test_replacement_that_moves_position_flags_reorder():
    old = _bundle([_cl("第一条", 0, "甲50。"), _cl("第二条", 1, "乙。")])
    new = _bundle([
        Clause(clause_no="第二条", raw="乙。", clean="乙。", position=0,
               fragment_id="frag:new:0"),
        Clause(clause_no="第一条", raw="甲60。", clean="甲60。", position=1,
               fragment_id="frag:new:1"),
    ])
    items = diff_versions(old, new)
    rep = next(i for i in items if i.change_type == ChangeType.REPLACED)
    assert rep.also_reordered is True
    assert "移动" in rep.detail
