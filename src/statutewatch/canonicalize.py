"""字段规整与输入映射（canonicalize）。

两条入口：
- process(record)：旧流水线兼容入口，行为与基线保持一致（src/name/value 规整）；
- parse_documents(payload)：法规输入 → RegulationDocument 列表，并做损坏检测。

损坏检测只识别"整份不可用"的下载（download_error/内容哈希校验失败/空正文标记），
OCR 局部不确定属于正常文档，由 normalizer 保留标记，绝不在这里擅自修正。
"""
from __future__ import annotations

from .models import Attachment, Gap, RegulationDocument
from . import config


def process(record):
    """旧流水线（generic records）入口，保持基线行为。"""
    rec = dict(record)
    rec["src"] = str(rec.get("src", "")).strip().lower()
    rec["name"] = str(rec.get("name", "")).strip()
    rec["value"] = int(rec.get("value", 0))
    return rec


def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _as_urls(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def parse_document(raw: dict, batch_id: str) -> RegulationDocument:
    """单条原始映射为 RegulationDocument；字段别名在此消化，后续模块只见规范结构。"""
    doc_id = str(_first(raw, "doc_id", "id"))
    urls = _as_urls(_first(raw, "urls", "url", "download_url", "source_url", default=[]))
    attachments = [
        Attachment(
            name=str(a.get("name", "")),
            url=str(a.get("url", "")),
            sha256=str(a.get("sha256", "")),
            size=a.get("size"),
            fragment_id=str(a.get("fragment_id", "")),
        )
        for a in raw.get("attachments", [])
    ]
    gaps = [
        Gap(locator=str(g.get("locator", "")), reason=str(g.get("reason", "")))
        for g in raw.get("gaps", [])
    ]
    corrupt = bool(raw.get("corrupt")) or bool(raw.get("download_error"))
    corrupt_reason = str(raw.get("corrupt_reason") or raw.get("download_error") or "")
    # 空正文且没有显式合法标记 → 视为损坏下载（无正文可发布）。
    body = str(raw.get("body_text", raw.get("body", "")))
    if not body.strip() and not corrupt:
        corrupt, corrupt_reason = True, "empty body"

    return RegulationDocument(
        batch_id=batch_id,
        doc_id=doc_id,
        title=str(_first(raw, "title", "name")).strip(),
        authority=str(_first(raw, "authority", "issuer", "agency")).strip(),
        doc_number=str(_first(raw, "doc_number", "number", "no")).strip(),
        urls=urls,
        published_at=str(_first(raw, "published_at", "publish_date", "date")),
        body_text=body,
        gaps=gaps,
        attachments=attachments,
        source_locator=str(raw.get("source_locator", "")),
        corrupt=corrupt,
        corrupt_reason=corrupt_reason,
        raw_blob_id=str(raw.get("raw_blob_id", "")),
        rule_version=str(raw.get("rule_version") or config.RULE_VERSION),
    )


def parse_documents(payload: dict) -> tuple[list[RegulationDocument], dict]:
    """解析整份法规 payload，返回 (文档列表, 批次元数据)。"""
    batch = payload.get("batch", {}) if isinstance(payload, dict) else {}
    batch_id = str(batch.get("batch_id", "unknown-batch"))
    docs = [parse_document(raw, batch_id) for raw in payload.get("documents", [])]
    return docs, batch
