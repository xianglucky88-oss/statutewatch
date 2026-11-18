"""领域数据结构。

四类核心对象在数据模型上严格分离：
- 法规对象 Regulation：同一个机关发布的同一份法规，URL/下载地址变化不改变身份；
- 内容版本 Version：一次实质内容快照，内容指纹相同则归并为同一版本；
- 采集批次 Batch：一次扫描/下载动作，同一内容可在多个批次重复出现；
- 附件 Attachment：随版本出现的附属文件，附件变化不等于正文修改。
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ChangeType(str, Enum):
    REORDER = "reorder"        # 条款内容未变，仅位置/编号重排
    SPLIT = "split"            # 一条旧条款拆成多条新条款
    MERGE = "merge"            # 多条旧条款并入一条新条款
    ADDED = "added"            # 新增条款
    REMOVED = "removed"        # 删除（无缺页/解析失败证据时才确定）
    REPLACED = "replaced"      # 同一条款内容被替换
    COSMETIC = "cosmetic"      # 仅版式/空白/标点噪声，无实质变化


class Confidence(str, Enum):
    HIGH = "high"              # 完整对齐，无 OCR/缺页干扰
    MEDIUM = "medium"          # 对齐可用，但涉及 OCR 不确定文本或弱匹配
    LOW = "low"                # 附近有缺页/局部解析失败，结论不能确定


class IngestStatus(str, Enum):
    PENDING = "pending"
    COMMITTED = "committed"
    FAILED = "failed"


@dataclass
class Record:
    """通用记录（兼容旧输入的最小结构）。"""
    key: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Gap:
    """缺页 / 局部解析失败的描述。携带该信息的删除只能报 low 可信度。"""
    locator: str
    reason: str = ""


@dataclass
class Clause:
    """一条条文。raw 保留原始片段，clean 是清理版式噪声后的文本。"""
    clause_no: str
    raw: str
    clean: str
    position: int
    fragment_id: str
    has_ocr_uncertain: bool = False


@dataclass
class Attachment:
    name: str
    url: str = ""
    sha256: str = ""
    size: int | None = None
    fragment_id: str = ""


@dataclass
class RegulationDocument:
    """一份采集到的法规文档（批次中的输入单元）。"""
    batch_id: str
    doc_id: str
    title: str
    authority: str
    doc_number: str
    urls: list[str]
    published_at: str
    body_text: str
    gaps: list[Gap] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    source_locator: str = ""
    corrupt: bool = False
    corrupt_reason: str = ""
    raw_blob_id: str = ""
    rule_version: str = ""


@dataclass
class DiffItem:
    """单条差异结论，evidence_fragment_ids 保证可回查原始片段。"""
    change_type: ChangeType
    confidence: Confidence
    old_clause_no: str = ""
    new_clause_no: str = ""
    old_position: int | None = None
    new_position: int | None = None
    old_text: str = ""
    new_text: str = ""
    detail: str = ""
    also_reordered: bool = False
    changed_numbers: list = field(default_factory=list)
    changed_units: list = field(default_factory=list)
    negation_changed: bool = False
    evidence_fragment_ids: list = field(default_factory=list)
