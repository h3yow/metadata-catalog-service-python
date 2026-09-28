"""真正的跨进程并发：多个服务实例（不同进程）登记同一批元数据。

- 同一 Idempotency-Key：恰好一个进程执行，其余返回首次结果；
- 不同 key：一个成功，其余 DUPLICATE_ENTRY；
- 最终边数为 1，表版本不丢失。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "tests" / "mproc_worker.py"


def _spawn(db_path: Path, worker: int, key: str) -> str:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, str(WORKER), str(db_path), str(worker), key],
        capture_output=True, text=True, cwd=str(ROOT), env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_multiprocess_same_key_runs_once(tmp_path: Path):
    import concurrent.futures as cf

    db = tmp_path / "mp.json"
    with cf.ProcessPoolExecutor(max_workers=5) as ex:
        outs = list(ex.map(_spawn, [db] * 5, range(5), ["shared"] * 5))
    committed = [o for o in outs if o.endswith("replayed=False")]
    replayed = [o for o in outs if o.endswith("replayed=True")]
    assert len(committed) == 1, outs
    assert len(replayed) == 4, outs

    # 最终状态：两表各 1 版本、恰 1 条边
    from app.service import CatalogService
    from app.storage import CatalogStore

    svc = CatalogService(CatalogStore(str(db)))
    assert svc.get_table(__import__("app.models", fromlist=["Actor"]).Actor(user="x"), "p1")["current_version"] == 1
    assert svc.lineage_graph(__import__("app.models", fromlist=["Actor"]).Actor(user="x"))["edge_count"] == 1


def test_multiprocess_distinct_keys_one_wins(tmp_path: Path):
    import concurrent.futures as cf

    db = tmp_path / "mp2.json"
    with cf.ProcessPoolExecutor(max_workers=5) as ex:
        outs = list(ex.map(_spawn, [db] * 5, range(5), [f"key-{i}" for i in range(5)]))
    ok = [o for o in outs if ":OK:" in o]
    dup = [o for o in outs if o.endswith("DUPLICATE_ENTRY")]
    assert len(ok) == 1, outs
    assert len(dup) == 4, outs
