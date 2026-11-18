"""canonicalize / loader：旧输入兼容、字段别名映射、损坏检测。"""
import pytest

from src.statutewatch import canonicalize, loader
from src.statutewatch.models import IngestStatus


def test_legacy_process_keeps_baseline_behavior():
    out = canonicalize.process({"src": " Alpha ", "name": " X ", "value": "7"})
    assert out == {"src": "alpha", "name": "X", "value": 7}


def test_field_aliases_are_mapped_to_document():
    raw = {
        "id": "x1", "name": "办法", "issuer": "市局", "number": "9号",
        "download_url": "https://x/a.pdf", "publish_date": "2026-10-01",
        "body": "第一条 甲。",
        "attachments": [{"name": "a.pdf", "sha256": "z"}],
        "gaps": [{"locator": "p3", "reason": "missing"}],
    }
    doc = canonicalize.parse_document(raw, "b1")
    assert doc.doc_id == "x1" and doc.title == "办法"
    assert doc.authority == "市局" and doc.doc_number == "9号"
    assert doc.urls == ["https://x/a.pdf"]
    assert doc.attachments[0].sha256 == "z"
    assert doc.gaps[0].locator == "p3"
    assert doc.corrupt is False


def test_corrupt_and_empty_body_detected():
    d1 = canonicalize.parse_document(
        {"doc_id": "c1", "download_error": "truncated"}, "b1")
    assert d1.corrupt is True and "truncated" in d1.corrupt_reason
    d2 = canonicalize.parse_document(
        {"doc_id": "c2", "body_text": "   "}, "b1")
    assert d2.corrupt is True and d2.corrupt_reason == "empty body"
    # OCR 局部不确定不是损坏文档。
    d3 = canonicalize.parse_document(
        {"doc_id": "c3", "body_text": "第一条 4??毫克。"}, "b1")
    assert d3.corrupt is False


def test_parse_documents_returns_batch_metadata():
    payload = {"batch": {"batch_id": "b9"}, "documents": [
        {"doc_id": "d", "body_text": "第一条 甲。"}]}
    docs, batch = canonicalize.parse_documents(payload)
    assert batch["batch_id"] == "b9" and docs[0].batch_id == "b9"


def test_legacy_loader_reads_list_fixture():
    data = loader.load()
    assert isinstance(data, list) and data[0]["id"] == "r-001"


def test_regulation_loader_reads_batch_fixture():
    payload = loader.load_regulations()
    assert payload["batch"]["batch_id"] == "b-2026-10"
    assert len(payload["documents"]) >= 3


def test_legacy_pipeline_still_works(tmp_path):
    from src.statutewatch import pipeline, store
    con = store.connect(str(tmp_path / "legacy.db"))
    try:
        r1 = pipeline.run(con=con)
        r2 = pipeline.run(con=con)  # 幂等
        assert r1["fed"] == 4 and r2["rows"] == r1["rows"] == 3
        # 旧表与法规新表共存互不干扰。
        assert con.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 3
    finally:
        con.close()
