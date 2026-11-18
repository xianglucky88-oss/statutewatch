"""法规身份归并（identity）。

归并规则（按优先级）：
1. 文号（doc_number）是强身份键：同机关同文号 → 同一法规对象，URL/下载地址变化
   或重新扫描都不会产生新法规；
2. 无文号时用"规范化标题 + 机关"次强键匹配既有对象；
3. 标题相同但机关不同（同名不同机关）→ 不自动归并，进入 identity_pending 保留依据；
4. 标题与机关任一缺失、无法可靠标识 → 同样进入 identity_pending，等待人工确认。

待确认记录始终保留标题/机关/文号/URL/批次等原始依据，确认前不并入任何法规对象。
"""
from __future__ import annotations

import re

# 标题中的文号后缀、空白与标点差异不参与身份判断。
_TITLE_NOISE_RE = re.compile(r"[\s　，。；：、,.；;:：（）()【】\[\]《》\"'“”‘’\-]+")


def normalize_title(title: str) -> str:
    return _TITLE_NOISE_RE.sub("", str(title or ""))


def candidate_keys(doc) -> dict:
    """派生候选键。strong 命中即可归并；weak 需再核对机关。"""
    keys: dict = {"strong": "", "weak": ""}
    if doc.doc_number and doc.authority:
        keys["strong"] = f"num::{doc.authority}::{doc.doc_number}"
    nt = normalize_title(doc.title)
    if nt and doc.authority:
        keys["weak"] = f"ta::{doc.authority}::{nt}"
    return keys


def _find_pending(con, doc, keys):
    row = con.execute(
        "SELECT pending_id FROM identity_pending "
        "WHERE batch_id=? AND doc_id=? AND status='pending'",
        (doc.batch_id, doc.doc_id),
    ).fetchone()
    if row:
        return row[0], True
    # 同一待确认依据（标题+机关+文号都相同）重复扫描 → 复用。
    row = con.execute(
        "SELECT pending_id FROM identity_pending "
        "WHERE status='pending' AND IFNULL(title,'')=? AND IFNULL(authority,'')=? "
        "AND IFNULL(doc_number,'')=? LIMIT 1",
        (doc.title, doc.authority, doc.doc_number),
    ).fetchone()
    return (row[0], True) if row else ("", False)


def _insert_pending(con, doc, reason: str) -> str:
    import hashlib
    pid = "pend-" + hashlib.sha256(
        f"{doc.batch_id}:{doc.doc_id}:{reason}".encode()
    ).hexdigest()[:12]
    con.execute(
        "INSERT OR IGNORE INTO identity_pending"
        "(pending_id, reason, title, authority, doc_number, urls, batch_id, doc_id, status) "
        "VALUES (?,?,?,?,?,?,?,?, 'pending')",
        (pid, reason, doc.title, doc.authority, doc.doc_number,
         "|".join(doc.urls), doc.batch_id, doc.doc_id),
    )
    return pid


def resolve_identity(con, doc) -> dict:
    """返回 {status: confirmed|pending, regulation_id?, pending_id?, reason?}。"""
    keys = candidate_keys(doc)

    # 规则 1：强键（机关+文号）。
    if keys["strong"]:
        row = con.execute(
            "SELECT regulation_id FROM regulations WHERE strong_key=?",
            (keys["strong"],),
        ).fetchone()
        if row:
            return {"status": "confirmed", "regulation_id": row[0]}

    # 规则 2：次强键（机关+标题）。
    if keys["weak"]:
        row = con.execute(
            "SELECT regulation_id FROM regulations WHERE weak_key=?",
            (keys["weak"],),
        ).fetchone()
        if row:
            return {"status": "confirmed", "regulation_id": row[0]}

    # 规则 3：同名不同机关 —— 标题相同但发布机关不一致，禁止自动归并。
    nt = normalize_title(doc.title)
    if nt:
        clash = con.execute(
            "SELECT regulation_id, authority FROM regulations "
            "WHERE norm_title=? AND IFNULL(authority,'')<>? LIMIT 1",
            (nt, doc.authority),
        ).fetchone()
        if clash:
            pid, _ = _find_pending(con, doc, keys)
            if not pid:
                pid = _insert_pending(
                    con, doc,
                    f"same-title-different-authority: candidate={clash[0]}",
                )
            return {"status": "pending", "pending_id": pid,
                    "reason": "same-title-different-authority"}

    # 规则 4：缺少可靠标识（标题/机关缺失），保留依据待确认。
    if not nt or not doc.authority:
        pid, existed = _find_pending(con, doc, keys)
        if not pid:
            pid = _insert_pending(con, doc, "missing-identity-marker")
        return {"status": "pending", "pending_id": pid,
                "reason": "missing-identity-marker"}

    # 全新法规：生成稳定身份并登记。
    import hashlib
    reg_id = "reg-" + hashlib.sha256(
        (keys["strong"] or keys["weak"]).encode()
    ).hexdigest()[:12]
    con.execute(
        "INSERT OR IGNORE INTO regulations"
        "(regulation_id, title, norm_title, authority, doc_number, strong_key, weak_key) "
        "VALUES (?,?,?,?,?,?,?)",
        (reg_id, doc.title, nt, doc.authority, doc.doc_number,
         keys["strong"], keys["weak"]),
    )
    return {"status": "confirmed", "regulation_id": reg_id}
