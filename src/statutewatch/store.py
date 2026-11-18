"""本地 sqlite3 持久化。

表结构对应四类对象与证据链：
- regulations        法规对象（身份归并结果，含待确认隔离表 identity_pending）
- versions           内容版本（内容指纹相同即同一版本，URL 变化不产生新版本）
- clauses            条文版本（raw/clean 双份，差异结论回查 raw）
- attachments        附件（独立于正文版本）
- batches            采集批次（状态机 pending/committed/failed，事务边界）
- batch_versions     批次↔版本多对多（同内容在多批次复扫只关联、不复制）
- fingerprint_index  指纹登记与碰撞标记
- diffs / diff_items 差异结论与逐条证据
- raw_blobs          原始下载物登记（GC 引用计数，证据仍被引用时不清理）
- records            旧流水线兼容表
"""
from __future__ import annotations

import json
import sqlite3

from .config import DB_PATH, RULE_VERSION, RULE_CHANGELOG

SCHEMA = """
CREATE TABLE IF NOT EXISTS records(
    key TEXT PRIMARY KEY, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS regulations(
    regulation_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    norm_title TEXT NOT NULL,
    authority TEXT NOT NULL,
    doc_number TEXT NOT NULL DEFAULT '',
    strong_key TEXT NOT NULL DEFAULT '',
    weak_key TEXT NOT NULL DEFAULT '',
    first_seen_batch TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS identity_pending(
    pending_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    authority TEXT NOT NULL DEFAULT '',
    doc_number TEXT NOT NULL DEFAULT '',
    urls TEXT NOT NULL DEFAULT '',
    batch_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS versions(
    version_id TEXT PRIMARY KEY,
    regulation_id TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    attachments_fingerprint TEXT NOT NULL DEFAULT '',
    rule_version TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT '',
    gaps_json TEXT NOT NULL DEFAULT '[]',
    gap_positions_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS clauses(
    version_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    clause_no TEXT NOT NULL,
    clean_text TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    fragment_id TEXT NOT NULL,
    has_ocr_uncertain INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(version_id, position)
);
CREATE TABLE IF NOT EXISTS attachments(
    attachment_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    size INTEGER,
    fragment_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS batches(
    batch_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    source TEXT NOT NULL DEFAULT '',
    input_hash TEXT NOT NULL DEFAULT '',
    rule_version TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS batch_versions(
    batch_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    urls_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(batch_id, version_id, doc_id)
);
CREATE TABLE IF NOT EXISTS fingerprint_index(
    fingerprint TEXT NOT NULL,
    fragment_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    collision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(fingerprint, fragment_id)
);
CREATE TABLE IF NOT EXISTS diffs(
    diff_id TEXT PRIMARY KEY,
    regulation_id TEXT NOT NULL,
    old_version_id TEXT NOT NULL,
    new_version_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS diff_items(
    diff_id TEXT NOT NULL,
    item_seq INTEGER NOT NULL,
    change_type TEXT NOT NULL,
    confidence TEXT NOT NULL,
    old_clause_no TEXT NOT NULL DEFAULT '',
    new_clause_no TEXT NOT NULL DEFAULT '',
    old_position INTEGER,
    new_position INTEGER,
    old_text TEXT NOT NULL DEFAULT '',
    new_text TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    changed_numbers_json TEXT NOT NULL DEFAULT '[]',
    changed_units_json TEXT NOT NULL DEFAULT '[]',
    negation_changed INTEGER NOT NULL DEFAULT 0,
    also_reordered INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(diff_id, item_seq)
);
CREATE TABLE IF NOT EXISTS raw_blobs(
    blob_id TEXT PRIMARY KEY,
    path TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    size INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS attachment_events(
    event_id TEXT PRIMARY KEY,
    regulation_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    from_version_id TEXT NOT NULL,
    to_version_id TEXT NOT NULL,
    old_attachments_fingerprint TEXT NOT NULL DEFAULT '',
    new_attachments_fingerprint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_meta(
    rule_version TEXT PRIMARY KEY,
    changelog TEXT NOT NULL
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(db_path or DB_PATH)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    for rv, note in RULE_CHANGELOG.items():
        con.execute(
            "INSERT OR IGNORE INTO rule_meta(rule_version, changelog) VALUES(?,?)",
            (rv, note),
        )
    con.commit()
    return con


# ---- 旧流水线兼容 API -----------------------------------------------------
def upsert(con, record):
    con.execute(
        "INSERT INTO records(key,data) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
        (record.key, json.dumps(record.data, ensure_ascii=False)),
    )


def commit(con):
    con.commit()


def count(con):
    return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]


# ---- 法规流水线读取辅助 ---------------------------------------------------
def latest_version(con, regulation_id: str):
    return con.execute(
        "SELECT version_id FROM versions WHERE regulation_id=? "
        "ORDER BY created_at, version_id LIMIT 1",
        (regulation_id,),
    ).fetchone()


def load_version_bundle(con, version_id: str) -> dict | None:
    """组装 diff 所需的版本结构（条款 + 缺页信息）。"""
    vrow = con.execute(
        "SELECT version_id, gaps_json, raw_text, "
        "COALESCE(gap_positions_json,'[]') FROM versions WHERE version_id=?",
        (version_id,),
    ).fetchone()
    if not vrow:
        return None
    from .models import Clause
    clauses = []
    for pos, cno, clean, raw, fid, ocr, fp in con.execute(
        "SELECT position, clause_no, clean_text, raw_text, fragment_id, "
        "has_ocr_uncertain, fingerprint FROM clauses WHERE version_id=? ORDER BY position",
        (version_id,),
    ):
        c = Clause(clause_no=cno, raw=raw, clean=clean, position=pos,
                   fragment_id=fid, has_ocr_uncertain=bool(ocr))
        c.fingerprint = fp
        clauses.append(c)
    gaps = json.loads(vrow[1])
    return {
        "version_id": vrow[0],
        "clauses": clauses,
        "gaps": gaps,
        "gap_locators": {g.get("locator", "") for g in gaps},
        "gap_positions": set(json.loads(vrow[3])),
        "raw_text": vrow[2],
    }
