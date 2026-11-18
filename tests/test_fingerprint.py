"""fingerprint：确定性、复用不报警、真碰撞（同指纹不同文本）显式标记。"""
from src.statutewatch import fingerprint as fp
from src.statutewatch import store


def test_fingerprint_stable_and_text_sensitive():
    a = fp.clause_fingerprint("限值50毫克")
    b = fp.clause_fingerprint("限值50毫克")
    c = fp.clause_fingerprint("限值60毫克")
    assert a == b and a != c


def test_rule_version_participates_in_fingerprint():
    assert fp.clause_fingerprint("甲", "2026.0") != fp.clause_fingerprint("甲", "2026.1")


def test_document_fingerprint_detects_reorder():
    f1 = fp.document_fingerprint(["a", "b", "c"])
    f2 = fp.document_fingerprint(["a", "c", "b"])
    assert f1 != f2  # 重排改变文档指纹，但由 diff 识别而非静默忽略


def test_reuse_is_not_collision_but_real_collision_flags(tmp_path):
    con = store.connect(str(tmp_path / "fp.db"))
    try:
        h = fp.clause_fingerprint("同一条文")
        # 先入库两条 clauses 记录模拟两个版本的同一文本。
        con.execute(
            "INSERT INTO clauses(version_id,position,clause_no,clean_text,raw_text,"
            "fragment_id,has_ocr_uncertain,fingerprint) "
            "VALUES ('v1',0,'第一条',?,?,?,0,?)",
            ("同一条文", "同一条文", "frag:v1:0", h),
        )
        con.execute(
            "INSERT INTO clauses(version_id,position,clause_no,clean_text,raw_text,"
            "fragment_id,has_ocr_uncertain,fingerprint) "
            "VALUES ('v2',0,'第一条',?,?,?,0,?)",
            ("同一条文", "同一条文", "frag:v2:0", h),
        )
        collision = fp.register_fingerprint(con, h, "frag:v2:0", "同一条文",
                                            "clause", "2026.1")
        assert collision is False  # 同指纹同文本 = 正常复用

        # 人为制造真碰撞：不同 clean 文本挂同一指纹。
        con.execute(
            "INSERT INTO clauses(version_id,position,clause_no,clean_text,raw_text,"
            "fragment_id,has_ocr_uncertain,fingerprint) "
            "VALUES ('v3',0,'第一条',?,?,?,0,?)",
            ("不同文本", "不同文本", "frag:v3:0", h),
        )
        collision = fp.register_fingerprint(con, h, "frag:v3:0", "不同文本",
                                            "clause", "2026.1")
        assert collision is True
        assert fp.is_collision(con, h) is True
    finally:
        con.close()


def test_attachment_fingerprint_independent():
    from src.statutewatch.models import Attachment
    a1 = [Attachment(name="t.pdf", url="u1", sha256="x")]
    a2 = [Attachment(name="t.pdf", url="u2", sha256="y")]
    assert fp.attachments_fingerprint(a1) != fp.attachments_fingerprint(a2)
