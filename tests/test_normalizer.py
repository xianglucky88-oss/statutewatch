"""normalizer：版式噪声清理必须保留数字/单位/否定词/条文边界；OCR 不修正。"""
from src.statutewatch import normalizer as nz
from src.statutewatch.models import Gap, RegulationDocument


def test_clean_layout_strips_noise_but_keeps_substance():
    raw = "第一条　排放限值为 50 毫克/立方米，\t不得超标。\n\n\n第二条 第 3 页 监测一次。"
    clean = nz.clean_layout(raw)
    assert "50" in clean and "毫克" in clean and "不得" in clean
    assert "　" not in clean and "\t" not in clean
    assert clean.count("\n\n") <= 1
    # 页脚被移除，但正文数字保留。
    assert "第 3 页" not in clean or "3" in clean


def test_line_numbers_removed_but_clause_numbers_kept():
    raw = "12 第一条 限值100毫克。\n13 第二条 不得超过2倍。"
    clean = nz.clean_layout(raw)
    assert "第一条" in clean and "第二条" in clean
    assert "100" in clean and "2倍" in clean and "不得" in clean
    assert not clean.startswith("12")


def test_ocr_uncertainty_is_kept_verbatim():
    raw = "第一条 限值为4??毫克，疑似[?]处不得猜补。"
    assert nz.has_ocr_uncertain(raw)
    clauses = nz.split_clauses(raw, "d1")
    assert clauses[0].has_ocr_uncertain is True
    # 原始片段保留 ??，clean 也不得"修正"它。
    assert "4??毫克" in clauses[0].raw and "4??毫克" in clauses[0].clean
    assert "[?]" in clauses[0].raw


def test_split_clauses_keeps_boundaries_and_fragments():
    body = "总则部分无条号。\n第一条 甲。\n第二条 乙。\n（1）子项丙。"
    clauses = nz.split_clauses(body, "d9")
    nos = [c.clause_no for c in clauses]
    assert nos[0] == "preamble"
    assert any(n.startswith("第一条") for n in nos)
    assert any(n.startswith("第二条") for n in nos)
    assert any(n.startswith("（1）") for n in nos)
    assert [c.position for c in clauses] == list(range(len(clauses)))
    # raw 与 clean 双份，fragment_id 稳定可回查。
    assert all(c.raw and c.clean and c.fragment_id for c in clauses)
    assert clauses[1].fragment_id == "frag:d9:clause:1"


def test_embedded_gap_marker_becomes_gap_and_neighbor_flag():
    body = "第一条 甲。\n第二条 乙。\n【缺页：原件第3页缺失】\n第三条 丙。"
    doc = RegulationDocument(
        batch_id="b", doc_id="dg", title="t", authority="a", doc_number="n",
        urls=[], published_at="", body_text=body,
    )
    norm = nz.normalize_document(doc)
    assert norm["gaps"], "内嵌缺页标记必须转成 Gap 证据"
    # 缺页标记本身不伪装成正常条文（已剥离为 gap-marker 并在规范结果中过滤）。
    assert all("【缺页" not in c.clean for c in norm["clauses"])
    assert [c.clause_no for c in norm["clauses"]] == ["第一条", "第二条", "第三条"]
    # 相邻条文位置被标记，供 diff 对删除/替换降级。
    assert norm["gap_clause_positions"]  # 非空


def test_substantive_token_extraction():
    text = "罚款5万元，不得超过3倍，面积100平方米，30日内整改。"
    nums = nz.extract_numbers(text)
    assert "5" in nums and "3" in nums and "100" in nums and "30" in nums
    units = nz.extract_units(text)
    assert "万元" in units and "倍" in units and "平方米" in units and "日" in units
    assert "不得" in nz.extract_negations(text)
    sub = nz.substantive_changes("限值50毫克，不得超标", "限值60毫克，可以超标")
    assert sub["negation_changed"] is True
    assert sub["changed_numbers"]  # 50/60 变化被捕获


def test_punctuation_only_equal_detects_cosmetic():
    assert nz.punctuation_only_equal("第一条：限值 50 毫克。", "第一条 限值50毫克")
    assert not nz.punctuation_only_equal("限值50毫克", "限值60毫克")


def test_explicit_gap_objects_are_preserved():
    doc = RegulationDocument(
        batch_id="b", doc_id="de", title="t", authority="a", doc_number="n",
        urls=[], published_at="", body_text="第一条 甲。",
        gaps=[Gap(locator="附件页2", reason="parse-failed")],
    )
    norm = nz.normalize_document(doc)
    assert any(g.locator == "附件页2" for g in norm["gaps"])
