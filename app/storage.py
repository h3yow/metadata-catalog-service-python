"""存储层：内存 + 单 JSON 文件持久化，带文件锁与原子写。

并发模型
--------
- 进程内：所有变更在一把可重入锁 (RLock) 下完成；
- 跨进程：文件后端用 fcntl 排他锁串行化“读入-修改-落盘”整个事务；
  进入排他区时先重新从磁盘加载，避免覆盖其他进程的更新（防丢失更新）；
- 落盘：同目录临时文件 + os.replace 原子替换，不会写到一半。
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

FORMAT_VERSION = 1


class FileLock:
    """跨进程排他文件锁（Unix fcntl）；path 为 None 时退化为空操作。

    锁文件创建后持久保留、绝不删除。注意 fcntl 锁绑定「打开文件描述」，
    因此每个线程必须各自 open 得到独立句柄，句柄保存在 thread-local 中，
    避免共享同一 FileLock 实例时多线程互相覆盖句柄。
    """

    def __init__(self, path: str | None):
        self.path = path
        self._local = threading.local()

    def __enter__(self) -> "FileLock":
        if self.path is not None:
            lock_path = f"{self.path}.lock"
            Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
            fh = open(lock_path, "a+")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            self._local.fh = fh
        return self

    def __exit__(self, *exc) -> None:
        fh = getattr(self._local, "fh", None)
        if fh is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()
            self._local.fh = None


def empty_data() -> dict:
    return {
        "format": FORMAT_VERSION,
        "seq": 0,              # 全局单调递增序号（边序、判定时序）
        "tables": {},          # table_id -> TableDef
        "versions": {},        # table_id -> [TableVersionRecord ...]
        "edges": {},           # edge_key -> LineageEdge
        "terms": {},           # term_id -> Term
        "idempotency": {},     # idempotency_key -> 已完成的事务结果
    }


class CatalogStore:
    def __init__(self, path: str | None = None):
        self.path = path
        self._rlock = threading.RLock()
        self._file_lock = FileLock(path)
        self._data: dict = empty_data()
        # _data 始终是“最新已提交基线”；事务在其深拷贝上修改，成功后才整体替换。
        # 内存模式下它就是权威状态；文件模式下进入事务时会先从磁盘重读覆盖它。

    # ---------------- 持久化 ----------------

    def load(self) -> None:
        """启动时加载；文件不存在则保持空数据。"""
        if self.path is None or not Path(self.path).exists():
            return
        with open(self.path, encoding="utf-8") as fh:
            raw = json.load(fh)
        data = empty_data()
        data.update({k: raw.get(k, data[k]) for k in data})
        self._data = data

    def _reload_locked(self) -> None:
        """排他区内重新读盘，合并到当前内存状态。"""
        if self.path is None or not Path(self.path).exists():
            return
        with open(self.path, encoding="utf-8") as fh:
            raw = json.load(fh)
        data = empty_data()
        data.update({k: raw.get(k, data[k]) for k in data})
        self._data = data

    def persist(self) -> None:
        """原子写当前基线。"""
        self.persist_to(self._data)

    def persist_to(self, data: dict) -> None:
        """原子写给定数据：先写临时文件再 replace，保证不会出现写一半的文件。"""
        if self.path is None:
            return
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    # ---------------- 事务边界 ----------------

    @contextlib.contextmanager
    def transaction(self) -> Iterator[dict]:
        """排他事务：跨进程文件锁 + 进程内锁，进入时重新读盘。

        ``self._data`` 始终是“最新已提交基线”，事务期间不被改动；
        变更只发生在它的深拷贝 ``working`` 上：
        - 正常退出：把 working 发布为新基线并原子落盘；
        - 异常：working 被丢弃，基线保持不变（也不会覆盖并发期间他人的提交）。

        后进入的事务在拿到锁之后才 reload/深拷贝，因此必然看到前一个已提交事务
        的结果，不会丢失更新。
        """
        with self._file_lock, self._rlock:
            self._reload_locked()
            working = copy.deepcopy(self._data)
            try:
                yield working
            except BaseException:
                raise
            else:
                self.persist_to(working)
                self._data = working

    @contextlib.contextmanager
    def read(self) -> Iterator[dict]:
        """只读一致快照：加锁重读后返回深拷贝，与进行中的事务互不共享可变状态。"""
        with self._file_lock, self._rlock:
            self._reload_locked()
            yield copy.deepcopy(self._data)

    @staticmethod
    def next_seq(data: dict) -> int:
        data["seq"] += 1
        return data["seq"]
