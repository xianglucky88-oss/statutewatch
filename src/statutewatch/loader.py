"""离线摄取入口（不联网）。

- load()：旧输入 fixtures/input.json（通用记录列表），保持基线行为；
- load_regulations()：法规批次 fixtures/regulations.json（含 batch/documents 结构）。
两个入口都只读本地文件，路径以参数传入，兼容旧输入。
"""
from __future__ import annotations

import json

from .config import FIXTURES, REGULATIONS_FIXTURE


def load(path=None):
    with open(path or FIXTURES, encoding="utf-8") as f:
        return json.load(f)


def load_regulations(path=None) -> dict:
    with open(path or REGULATIONS_FIXTURE, encoding="utf-8") as f:
        return json.load(f)
