"""内容指纹（fingerprint）。

定位：指纹只用于"快速判同/对齐辅助"，不能替代实质差异比较。
- 条款指纹基于清理后文本；文档指纹 = 各条款指纹的有序聚合；
- 指纹相同且文本相同 → 同一条文在不同版本/批次复用（正常，不算碰撞）；
- 指纹相同但文本不同 → 真碰撞，在 fingerprint_index 标记 collision=1，
  下游对碰撞条文退回逐条文本比对，绝不凭指纹直接下结论；
- 指纹不同 → 必须进入逐条款 diff，真实修改不得因附件变化、规则升级或碰撞而消失。
"""
from __future__ import annotations

import hashlib

from . import config


def process(record):
    """旧流水线兼容入口，保持基线标记。"""
    rec = dict(record)
    rec.setdefault("_stage_fingerprint", "naive")
    return rec


def clause_fingerprint(clean_text: str, rule_version: str = config.RULE_VERSION) -> str:
    return hashlib.sha256(
        f"{rule_version}|clause|{clean_text}".encode("utf-8")
    ).hexdigest()


def document_fingerprint(clause_fps: list[str],
                         rule_version: str = config.RULE_VERSION) -> str:
    # 有序聚合：条款重排会改变文档指纹，重排本身由 diff 识别而非静默忽略。
    digest = hashlib.sha256()
    digest.update(f"{rule_version}|document".encode("utf-8"))
    for fp in clause_fps:
        digest.update(fp.encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def attachments_fingerprint(attachments) -> str:
    """附件指纹独立计算：附件变化单独记录，不覆盖正文版本判断。"""
    digest = hashlib.sha256()
    for a in sorted(attachments, key=lambda x: (x.name, x.url, x.sha256)):
        digest.update(f"{a.name}|{a.url}|{a.sha256}|{a.size}".encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def register_fingerprint(con, fp: str, fragment_id: str, clean_text: str,
                         kind: str, rule_version: str) -> bool:
    """登记指纹并返回是否为真碰撞（同一指纹对应不同清理文本）。

    同指纹同文本只是条文在新版本中的正常复用，不标记碰撞。
    文档级指纹不参与碰撞判定（碰撞只对条款指纹有意义）。
    """
    con.execute(
        "INSERT INTO fingerprint_index(fingerprint, fragment_id, kind, rule_version, collision) "
        "VALUES (?,?,?,?,0) ON CONFLICT(fingerprint, fragment_id) DO NOTHING",
        (fp, fragment_id, kind, rule_version),
    )
    if kind != "clause":
        return False
    texts = {r[0] for r in con.execute(
        "SELECT DISTINCT c.clean_text FROM fingerprint_index fi "
        "JOIN clauses c ON c.fragment_id=fi.fragment_id WHERE fi.fingerprint=?",
        (fp,),
    )}
    # clauses 中尚未登记当前片段时，用入参文本补上再判碰撞。
    texts.add(clean_text)
    collision = len(texts) > 1
    if collision:
        con.execute(
            "UPDATE fingerprint_index SET collision=1 WHERE fingerprint=?", (fp,),
        )
    return collision


def is_collision(con, fp: str) -> bool:
    row = con.execute(
        "SELECT COALESCE(MAX(collision),0) FROM fingerprint_index WHERE fingerprint=?",
        (fp,),
    ).fetchone()
    return bool(row and row[0])
