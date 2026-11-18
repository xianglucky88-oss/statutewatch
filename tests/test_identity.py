"""identity：法规对象归并、URL 变化不产生新对象、同名不同机关/缺标识待确认。"""
import pytest

from src.statutewatch import identity, store
from src.statutewatch.models import RegulationDocument


@pytest.fixture()
def con(tmp_path):
    c = store.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def _doc(doc_id, title, authority, number, urls=None, batch="b1"):
    return RegulationDocument(
        batch_id=batch, doc_id=doc_id, title=title, authority=authority,
        doc_number=number, urls=urls or [], published_at="", body_text="第一条 甲。",
    )


def test_same_doc_number_merges_across_url_change(con):
    d1 = _doc("d1", "排放办法", "省厅", "7号", ["https://old/a.pdf"])
    r1 = identity.resolve_identity(con, d1)
    assert r1["status"] == "confirmed"
    d2 = _doc("d2", "排放办法", "省厅", "7号", ["https://new/mirror.pdf"], batch="b2")
    r2 = identity.resolve_identity(con, d2)
    assert r2["status"] == "confirmed"
    assert r2["regulation_id"] == r1["regulation_id"]  # 换地址/复扫不产生新法规
    assert con.execute("SELECT COUNT(*) FROM regulations").fetchone()[0] == 1


def test_same_title_different_authority_goes_pending(con):
    identity.resolve_identity(con, _doc("d1", "市容规定", "甲市局", "甲3号"))
    r = identity.resolve_identity(con, _doc("d2", "市容规定", "乙市局", "乙9号"))
    assert r["status"] == "pending"
    assert r["reason"] == "same-title-different-authority"
    row = con.execute(
        "SELECT reason, title, authority, doc_number, urls FROM identity_pending"
    ).fetchone()
    assert row[0].startswith("same-title-different-authority")
    assert row[2] == "乙市局"  # 待确认依据保留
    assert con.execute("SELECT COUNT(*) FROM regulations").fetchone()[0] == 1


def test_missing_identity_marker_goes_pending(con):
    r = identity.resolve_identity(con, _doc("d3", "", "", "", ["https://x/a.pdf"]))
    assert r["status"] == "pending"
    assert r["reason"] == "missing-identity-marker"
    # URL 等原始依据仍在。
    assert "https://x/a.pdf" in con.execute(
        "SELECT urls FROM identity_pending").fetchone()[0]


def test_rescan_of_pending_does_not_duplicate(con):
    d = _doc("d4", "市容规定", "乙市局", "乙9号", batch="b1")
    identity.resolve_identity(con, _doc("d1", "市容规定", "甲市局", "甲3号"))
    r1 = identity.resolve_identity(con, d)
    r2 = identity.resolve_identity(con,
        _doc("d4", "市容规定", "乙市局", "乙9号", batch="b2"))
    assert r1["pending_id"] == r2["pending_id"]
    assert con.execute(
        "SELECT COUNT(*) FROM identity_pending WHERE status='pending'").fetchone()[0] == 1


def test_weak_key_title_authority_merges_without_number(con):
    r1 = identity.resolve_identity(con, _doc("d5", "无文号办法", "县局", ""))
    r2 = identity.resolve_identity(con,
        _doc("d6", "无文号办法", "县局", "", batch="b2"))
    assert r1["regulation_id"] == r2["regulation_id"]


def test_normalize_title_ignores_layout_differences():
    assert identity.normalize_title(" 排放　办法（试行） ") == \
        identity.normalize_title("排放办法试行")
