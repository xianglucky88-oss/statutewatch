"""薄编排层。

两条互不干扰的流水线：
- run()：旧通用记录链 canonicalize→archive→fingerprint→keyer→store，行为与基线一致；
- run_regulations()：法规批次链 canonicalize→normalizer→identity→fingerprint→
  archive(含 diff)，单事务归档，业务判断全部在业务模块内，编排层不做判断。
"""
from __future__ import annotations

import importlib

from . import loader as _loader
from . import store as _store
from .archive import archive_batch
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ['canonicalize', 'archive', 'fingerprint']


def run(con=None):
    own = con is None
    con = con or _store.connect()
    try:
        chain = [importlib.import_module("." + n, __package__).process
                 for n in CHAIN_NAMES]
        fed = 0
        for raw in _loader.load():
            rec = raw
            for fn in chain:
                rec = fn(rec)
            _store.upsert(con, Record(key=key_of(rec), data=rec))
            fed += 1
        _store.commit(con)
        return {"fed": fed, "rows": _store.count(con)}
    finally:
        if own:
            con.close()


def run_regulations(con=None, payload=None, rule_version=None):
    """法规批次流水线。payload 为 None 时从默认 fixture 读取（离线）。"""
    from . import config
    own = con is None
    con = con or _store.connect()
    try:
        payload = payload if payload is not None else _loader.load_regulations()
        return archive_batch(con, payload, rule_version or config.RULE_VERSION)
    finally:
        if own:
            con.close()
