"""版本归档（archive）。

一个采集批次在**单个事务**内处理，保证"损坏下载或处理中断不能发布半份成功档案"：
- 任一文档 corrupt 或处理异常 → 整批回滚，批次标记 failed，无任何版本/差异落库；
- 批次输入哈希相同的重跑 → 幂等空转；同 batch_id 不同内容 → 报错而不是覆盖；
- 内容指纹相同 → 复用同一版本（URL 变更/重新扫描不产生新版本），仅登记批次关联；
- 附件指纹独立：附件变化单独记录 attachment_events，不掩盖也不冒充正文修改；
- 新版本入库后立即与上一版本做 diff，差异引用的 fragment 证据随结论持久化；
- GC 只清理不被任何版本/差异引用的过期原始件，仍在证据链上的一律保留。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from . import config, fingerprint, identity, normalizer, store
from .diff import diff_versions
from .models import IngestStatus


def process(record):
    """旧流水线兼容入口，保持基线标记。"""
    rec = dict(record)
    rec.setdefault("_stage_archive", "ok")
    return rec


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _input_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _version_id(regulation_id: str, content_fp: str) -> str:
    return "ver-" + hashlib.sha256(
        f"{regulation_id}|{content_fp}".encode()
    ).hexdigest()[:16]


def _diff_id(regulation_id: str, old_id: str, new_id: str, rule_version: str) -> str:
    return "diff-" + hashlib.sha256(
        f"{regulation_id}|{old_id}|{new_id}|{rule_version}".encode()
    ).hexdigest()[:16]


def _mark_batch_failed(con, batch_id: str, payload: dict, reason: str):
    con.execute(
        "INSERT INTO batches(batch_id, status, source, input_hash, rule_version, note, created_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET "
        "status=excluded.status, note=excluded.note",
        (batch_id, IngestStatus.FAILED.value,
         str(payload.get("batch", {}).get("source", "")),
         _input_hash(payload), config.RULE_VERSION, reason, _now()),
    )


def _persist_diff(con, regulation_id: str, old_id: str, new_id: str,
                  rule_version: str, old_bundle: dict, new_bundle: dict,
                  collisions: set) -> dict:
    diff_id = _diff_id(regulation_id, old_id, new_id, rule_version)
    existing = con.execute(
        "SELECT 1 FROM diffs WHERE diff_id=?", (diff_id,)
    ).fetchone()
    if existing:
        return {"diff_id": diff_id, "reused": True, "items": 0}

    items = diff_versions(old_bundle, new_bundle, rule_version, collisions)
    con.execute(
        "INSERT INTO diffs(diff_id, regulation_id, old_version_id, new_version_id, "
        "rule_version, created_at) VALUES (?,?,?,?,?,?)",
        (diff_id, regulation_id, old_id, new_id, rule_version, _now()),
    )
    for seq, it in enumerate(items):
        con.execute(
            "INSERT INTO diff_items(diff_id, item_seq, change_type, confidence, "
            "old_clause_no, new_clause_no, old_position, new_position, old_text, new_text, "
            "detail, changed_numbers_json, changed_units_json, negation_changed, "
            "also_reordered, evidence_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (diff_id, seq, it.change_type.value, it.confidence.value,
             it.old_clause_no, it.new_clause_no, it.old_position, it.new_position,
             it.old_text, it.new_text, it.detail,
             json.dumps(it.changed_numbers, ensure_ascii=False),
             json.dumps(it.changed_units, ensure_ascii=False),
             int(it.negation_changed), int(it.also_reordered),
             json.dumps(it.evidence_fragment_ids, ensure_ascii=False)),
        )
    return {"diff_id": diff_id, "reused": False, "items": len(items)}


def _archive_document(con, doc, norm: dict, rule_version: str) -> dict:
    """归档单个文档，返回该文档的归档摘要。"""
    resolved = identity.resolve_identity(con, doc)
    if resolved["status"] == "pending":
        return {"doc_id": doc.doc_id, "status": "pending_identity",
                "pending_id": resolved["pending_id"], "reason": resolved["reason"]}

    regulation_id = resolved["regulation_id"]
    # 确保 first_seen 登记。
    con.execute(
        "UPDATE regulations SET first_seen_batch=COALESCE(NULLIF(first_seen_batch,''),?), "
        "created_at=COALESCE(NULLIF(created_at,''),?) WHERE regulation_id=?",
        (doc.batch_id, _now(), regulation_id),
    )

    clauses = norm["clauses"]
    clause_fps = []
    for c in clauses:
        fp = fingerprint.clause_fingerprint(c.clean, rule_version)
        c.fingerprint = fp
        clause_fps.append(fp)

    content_fp = fingerprint.document_fingerprint(clause_fps, rule_version)
    attach_fp = fingerprint.attachments_fingerprint(doc.attachments)
    fingerprint.register_fingerprint(
        con, content_fp, f"doc:{doc.doc_id}", content_fp,
        "document", rule_version,
    )
    version_id = _version_id(regulation_id, content_fp)

    prior = store.latest_version(con, regulation_id)
    prior_id = prior[0] if prior else ""
    reused = con.execute(
        "SELECT 1 FROM versions WHERE version_id=?", (version_id,)
    ).fetchone() is not None

    if not reused:
        con.execute(
            "INSERT INTO versions(version_id, regulation_id, content_fingerprint, "
            "attachments_fingerprint, rule_version, published_at, created_at, raw_text, gaps_json, "
            "gap_positions_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (version_id, regulation_id, content_fp, attach_fp, rule_version,
             doc.published_at, _now(), doc.body_text,
             json.dumps([{"locator": g.locator, "reason": g.reason} for g in norm["gaps"]],
                        ensure_ascii=False),
             json.dumps(sorted(norm.get("gap_clause_positions", set())))),
        )
        for c in clauses:
            con.execute(
                "INSERT INTO clauses(version_id, position, clause_no, clean_text, raw_text, "
                "fragment_id, has_ocr_uncertain, fingerprint) VALUES (?,?,?,?,?,?,?,?)",
                (version_id, c.position, c.clause_no, c.clean, c.raw,
                 c.fragment_id, int(c.has_ocr_uncertain), c.fingerprint),
            )
        for idx, a in enumerate(doc.attachments):
            aid = "att-" + hashlib.sha256(
                f"{version_id}|{a.name}|{a.url}|{idx}".encode()
            ).hexdigest()[:12]
            con.execute(
                "INSERT OR IGNORE INTO attachments(attachment_id, version_id, name, url, "
                "sha256, size, fragment_id) VALUES (?,?,?,?,?,?,?)",
                (aid, version_id, a.name, a.url, a.sha256, a.size, a.fragment_id),
            )

    # 条款入库后再登记/检测碰撞：同指纹不同文本才是真碰撞，条文复用不报警。
    collisions: set = set()
    for c in clauses:
        if fingerprint.register_fingerprint(
            con, c.fingerprint, c.fragment_id, c.clean, "clause", rule_version
        ):
            collisions.add(c.fingerprint)

    # 批次↔版本关联（同内容多批次复扫只新增关联，不复制版本）。
    con.execute(
        "INSERT OR IGNORE INTO batch_versions(batch_id, version_id, doc_id, urls_json) "
        "VALUES (?,?,?,?)",
        (doc.batch_id, version_id, doc.doc_id, json.dumps(doc.urls)),
    )

    # 附件变化与上一版本独立对比：不产生正文版本，但必须留痕。
    attachment_event = None
    if prior_id and prior_id != version_id:
        prow = con.execute(
            "SELECT attachments_fingerprint FROM versions WHERE version_id=?",
            (prior_id,),
        ).fetchone()
        if prow and prow[0] != attach_fp:
            event_id = "attev-" + hashlib.sha256(
                f"{regulation_id}|{prior_id}|{version_id}".encode()
            ).hexdigest()[:12]
            con.execute(
                "INSERT OR IGNORE INTO attachment_events(event_id, regulation_id, batch_id, "
                "from_version_id, to_version_id, old_attachments_fingerprint, "
                "new_attachments_fingerprint, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (event_id, regulation_id, doc.batch_id, prior_id, version_id,
                 prow[0], attach_fp, _now()),
            )
            attachment_event = {"event_id": event_id,
                                "from_version": prior_id, "to_version": version_id}

    # 同内容复扫但附件不同：正文版本复用，附件差异仍需留痕（不能被指纹判同掩盖）。
    if reused:
        arow = con.execute(
            "SELECT attachments_fingerprint FROM versions WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if arow and arow[0] != attach_fp:
            event_id = "attev-" + hashlib.sha256(
                f"{regulation_id}|{version_id}|{doc.batch_id}|{attach_fp}".encode()
            ).hexdigest()[:12]
            existed = con.execute(
                "SELECT 1 FROM attachment_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if not existed:
                con.execute(
                    "INSERT INTO attachment_events(event_id, regulation_id, batch_id, "
                    "from_version_id, to_version_id, old_attachments_fingerprint, "
                    "new_attachments_fingerprint, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (event_id, regulation_id, doc.batch_id, version_id, version_id,
                     arow[0], attach_fp, _now()),
                )
                attachment_event = {"event_id": event_id,
                                    "from_version": version_id,
                                    "to_version": version_id,
                                    "note": "正文未变，附件变化独立留痕"}

    # 新版本 → 与上一版本做差异（指纹碰撞集合传入，强制文本复核）。
    diff_summary = None
    if prior_id and prior_id != version_id:
        old_bundle = store.load_version_bundle(con, prior_id)
        new_bundle = store.load_version_bundle(con, version_id)
        diff_summary = _persist_diff(con, regulation_id, prior_id, version_id,
                                     rule_version, old_bundle, new_bundle, collisions)
    return {
        "doc_id": doc.doc_id, "status": "archived",
        "regulation_id": regulation_id, "version_id": version_id,
        "reused_version": reused, "diff": diff_summary,
        "attachment_event": attachment_event,
        "fingerprint_collisions": sorted(collisions),
    }


def archive_batch(con, payload: dict, rule_version: str = config.RULE_VERSION) -> dict:
    """归档整批。成功才提交；任何损坏/异常 → 回滚并标记 failed。"""
    from .canonicalize import parse_documents

    batch_id = str(payload.get("batch", {}).get("batch_id", "unknown-batch"))
    input_hash = _input_hash(payload)

    prior = con.execute(
        "SELECT status, input_hash FROM batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if prior:
        if prior[1] == input_hash and prior[0] == IngestStatus.COMMITTED.value:
            return {"batch_id": batch_id, "idempotent": True, "documents": []}
        if prior[0] == IngestStatus.COMMITTED.value:
            # 已成功批次的输入不可变，防止覆盖历史档案。
            raise ValueError(f"batch_id={batch_id} 已提交且输入内容不同，拒绝覆盖")
        # failed 批次允许用修复后的内容重试（原 failed 记录保留审计痕迹）。
        con.execute("DELETE FROM batches WHERE batch_id=?", (batch_id,))

    docs, _ = parse_documents(payload)
    # 损坏下载：整批拒绝，绝不发布半份档案。
    for d in docs:
        if d.corrupt:
            try:
                _mark_batch_failed(con, batch_id, payload,
                                   f"corrupt document {d.doc_id}: {d.corrupt_reason}")
                con.commit()
            finally:
                pass
            return {"batch_id": batch_id, "status": IngestStatus.FAILED.value,
                    "reason": f"corrupt document {d.doc_id}: {d.corrupt_reason}"}

    try:
        con.execute("BEGIN")
    except sqlite3.OperationalError:
        pass  # 已在事务中（测试复用连接时）
    try:
        con.execute(
            "INSERT INTO batches(batch_id, status, source, input_hash, rule_version, note, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (batch_id, IngestStatus.PENDING.value,
             str(payload.get("batch", {}).get("source", "")),
             input_hash, rule_version, "", _now()),
        )
        summaries = []
        for d in docs:
            norm = normalizer.normalize_document(d)
            summaries.append(_archive_document(con, d, norm, rule_version))
        con.execute(
            "UPDATE batches SET status=? WHERE batch_id=?",
            (IngestStatus.COMMITTED.value, batch_id),
        )
        con.commit()
        return {"batch_id": batch_id, "status": IngestStatus.COMMITTED.value,
                "documents": summaries}
    except Exception as exc:  # 处理中断：回滚，只保留 failed 批次记录。
        con.rollback()
        _mark_batch_failed(con, batch_id, payload, f"interrupted: {exc!r}")
        con.commit()
        raise


def garbage_collect_blobs(con) -> dict:
    """清理过期原始件：仍被附件或差异证据引用的一律保留。

    引用来源（均可回查）：
    - attachments.sha256：仍挂在某个内容版本下的附件原始件；
    - diff_items.evidence_json：差异结论引用的 fragment_id；
    - clauses.fragment_id：条文原始片段（通过 raw_blobs.blob_id 关联登记）。
    """
    referenced: set[str] = set()
    for (sha,) in con.execute("SELECT DISTINCT sha256 FROM attachments WHERE sha256<>''"):
        referenced.add(sha)
    for (ev,) in con.execute("SELECT evidence_json FROM diff_items"):
        referenced.update(json.loads(ev))
    for (fid,) in con.execute("SELECT DISTINCT fragment_id FROM clauses"):
        referenced.add(fid)
    removed, kept = [], []
    for blob_id, in con.execute("SELECT blob_id FROM raw_blobs WHERE status='expired'"):
        if blob_id in referenced:
            kept.append(blob_id)
        else:
            con.execute("DELETE FROM raw_blobs WHERE blob_id=?", (blob_id,))
            removed.append(blob_id)
    con.commit()
    return {"removed": removed, "retained_referenced": kept}
