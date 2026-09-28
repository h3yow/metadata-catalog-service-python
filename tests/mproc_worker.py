"""跨进程并发 worker：对同一 JSON 文件并发提交同一批元数据。"""
import sys
from app.service import CatalogService
from app.storage import CatalogStore
from app.models import Actor, BatchItem

db_path, worker_id, key = sys.argv[1], sys.argv[2], sys.argv[3]
svc = CatalogService(CatalogStore(db_path))
actor = Actor(user="alice")
items = [
    BatchItem(op="register_table", body={"id": "p1", "name": "p1", "owner": "alice"}),
    BatchItem(op="submit_version", body={"table_id": "p1", "fields": [{"name": "id", "type": "integer"}]}),
    BatchItem(op="register_table", body={"id": "p2", "name": "p2", "owner": "alice"}),
    BatchItem(op="submit_version", body={"table_id": "p2", "fields": [{"name": "id", "type": "integer"}]}),
    BatchItem(op="add_lineage", body={"upstream_table": "p1", "downstream_table": "p2"}),
]
try:
    r = svc.run_batch(actor, key, items)
    print(f"worker{worker_id}:OK:replayed={r['replayed']}")
except Exception as exc:
    print(f"worker{worker_id}:{getattr(exc, 'code', type(exc).__name__)}")
