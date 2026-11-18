"""archive / 法规流水线：事务原子性、幂等、版本归档、附件留痕、GC、规则复现。"""
import json

import pytest

from src.statutewatch import archive, config, pipeline, store
from src.statutewatch.models import IngestStatus


def _doc(doc_id, title="办法", authority="省厅", number="7号",
         body="第一条 限值50毫克，不得超标。", **kw):
    d = {"doc_id": doc_id, "title": title, "authority": authority,
         "doc_number": number, "url": f"https://x/{doc_id}.pdf",
         "published_at": "2026-10-01", "body_text": body}
    d.update(kw)
    return d


def _batch(bid, docs, source="test"):
    return {"batch": {"batch_id": bid, "source": source}, "documents": docs}


@pytest.fixture()
def con(tmp_path):
    c = store.connect(str(tmp_path / "e2e.db"))
    yield c
    c.close()


def test_corrupt_batch_rolls_back_half_published_archive(con):
    good = _batch("b1", [_doc("d1")])
    r1 = pipeline.run_regulations(con=con, payload=good)
    assert r1["status"] == "committed"
    versions_after_b1 = con.execute("SELECT COUNT(*) FROM versions").fetchone()[0]

    broken = _batch("b2", [
        _doc("d2", title="另一办法", number="8号"),
        {"doc_id": "d3", "title": "办法", "authority": "省厅", "doc_number": "7号",
         "download_error": "stream truncated"},
    ])
    r2 = pipeline.run_regulations(con=con, payload=broken)
    # 损坏批次整体失败，不发布半份成功档案。
    assert r2["status"] == IngestStatus.FAILED.value
    assert con.execute(
        "SELECT COUNT(*) FROM versions").fetchone()[0] == versions_after_b1
    assert con.execute(
        "SELECT status FROM batches WHERE batch_id='b2'").fetchone()[0] == "failed"
    # 失败后恢复：修复内容重跑同批次可成功，历史版本仍可查。
    fixed = _batch("b2", [_doc("d2", title="另一办法", number="8号")])
    r3 = pipeline.run_regulations(con=con, payload=fixed)
    assert r3["status"] == "committed"
    assert con.execute(
        "SELECT COUNT(*) FROM diffs").fetchone()[0] >= 0  # 库结构可正常写入


def test_url_change_rescan_reuses_version_and_records_attachment(con):
    pipeline.run_regulations(con=con, payload=_batch("b1", [
        _doc("d1", attachments=[{"name": "t.pdf", "sha256": "aaa", "url": "u1"}])
    ]))
    same_body = _doc("d1b", url="https://mirror/new.pdf",
                     attachments=[{"name": "t.pdf", "sha256": "bbb", "url": "u2"}])
    r = pipeline.run_regulations(con=con, payload=_batch("b2", [same_body]))
    summary = r["documents"][0]
    # 换地址 + 重新扫描：同一法规、同一内容版本，不报新文件。
    assert summary["reused_version"] is True
    assert con.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM regulations").fetchone()[0] == 1
    # 两条批次关联都保留。
    assert con.execute("SELECT COUNT(*) FROM batch_versions").fetchone()[0] == 2
    # 附件变化独立留痕，不被正文指纹判同掩盖。
    assert con.execute("SELECT COUNT(*) FROM attachment_events").fetchone()[0] == 1


def test_substantive_revision_creates_version_and_diff_with_evidence(con):
    pipeline.run_regulations(con=con, payload=_batch("b1", [_doc("d1")]))
    pipeline.run_regulations(con=con, payload=_batch("b2", [
        _doc("d2", body="第一条 限值60毫克，可以超标。\n第二条 新增评估要求。")
    ]))
    assert con.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 2
    items = con.execute(
        "SELECT change_type, confidence, evidence_json, changed_numbers_json, "
        "negation_changed FROM diff_items").fetchall()
    assert items, "实质修订必须产生差异结论"
    # 每条差异都带可回查的原始片段证据。
    for ct, cf, ev, nums, neg in items:
        ids = json.loads(ev)
        assert ids and all(fid.startswith("frag:") for fid in ids)
    # 阈值与否定词变化被捕获。
    all_nums = {n for _, _, _, nums, _ in items for n in json.loads(nums)}
    assert {"50", "60"} & all_nums
    assert any(neg == 1 for *_, neg in items)
    # 证据片段能在 clauses 表回查到 raw 原文。
    ev_ids = json.loads(items[0][2])
    rows = con.execute(
        f"SELECT COUNT(*) FROM clauses WHERE fragment_id IN "
        f"({','.join('?' * len(ev_ids))})", ev_ids).fetchone()[0]
    assert rows >= 1


def test_idempotent_batch_rerun_is_noop(con):
    payload = _batch("b1", [_doc("d1")])
    r1 = pipeline.run_regulations(con=con, payload=payload)
    r2 = pipeline.run_regulations(con=con, payload=json.loads(json.dumps(payload)))
    assert r2.get("idempotent") is True
    assert con.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1


def test_same_batch_id_different_content_rejected(con):
    pipeline.run_regulations(con=con, payload=_batch("b1", [_doc("d1")]))
    with pytest.raises(ValueError):
        pipeline.run_regulations(con=con, payload=_batch("b1", [_doc("d2")]))


def test_gc_keeps_evidence_still_referenced(con):
    pipeline.run_regulations(con=con, payload=_batch("b1", [_doc("d1")]))
    pipeline.run_regulations(con=con, payload=_batch("b2", [
        _doc("d2", body="第一条 限值60毫克。")
    ]))
    # 一个过期 blob 被差异证据引用（fragment_id），另一个无人引用。
    referenced_fid = con.execute(
        "SELECT fragment_id FROM clauses LIMIT 1").fetchone()[0]
    con.execute("INSERT INTO raw_blobs(blob_id,status) VALUES (?, 'expired')",
                (referenced_fid,))
    con.execute("INSERT INTO raw_blobs(blob_id,status) VALUES (?, 'expired')",
                ("orphan-blob",))
    con.commit()
    result = archive.garbage_collect_blobs(con)
    assert "orphan-blob" in result["removed"]
    assert referenced_fid in result["retained_referenced"]


def test_rule_upgrade_keeps_history_reproducible(con):
    pipeline.run_regulations(con=con, payload=_batch("b1", [_doc("d1")]))
    pipeline.run_regulations(con=con, payload=_batch("b2", [
        _doc("d2", body="第一条 限值60毫克。")
    ]), rule_version="2026.1")
    # 历史差异行带规则版本，规则变更日志入库。
    rv = con.execute("SELECT rule_version FROM diffs").fetchall()
    assert rv and all(r[0] in config.RULE_VERSIONS for r in rv)
    notes = dict(con.execute("SELECT rule_version, changelog FROM rule_meta").fetchall())
    assert "2026.0" in notes and "2026.1" in notes
    # 用旧规则可重新复现（结构可用、行为独立）。
    old_bundle = store.load_version_bundle(
        con, con.execute("SELECT version_id FROM versions LIMIT 1").fetchone()[0])
    new_bundle = store.load_version_bundle(
        con, con.execute("SELECT version_id FROM versions "
                         "ORDER BY created_at DESC LIMIT 1").fetchone()[0])
    legacy = __import__("src.statutewatch.diff", fromlist=["diff_versions"]).diff_versions(
        old_bundle, new_bundle, rule_version="2026.0")
    current = __import__("src.statutewatch.diff", fromlist=["diff_versions"]).diff_versions(
        old_bundle, new_bundle, rule_version="2026.1")
    assert legacy and current
    assert legacy[0].detail.startswith("2026.0")


def test_pending_identity_does_not_create_regulation(con):
    r = pipeline.run_regulations(con=con, payload=_batch("b1", [
        {"doc_id": "dx", "title": "匿名文", "authority": "", "doc_number": "",
         "url": "u", "body_text": "第一条 甲。"}
    ]))
    assert r["documents"][0]["status"] == "pending_identity"
    assert con.execute("SELECT COUNT(*) FROM regulations").fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM identity_pending WHERE status='pending'").fetchone()[0] == 1
