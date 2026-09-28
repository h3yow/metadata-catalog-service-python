# 本地元数据目录服务（FastAPI）

一个本地即可运行的元数据目录（metadata catalog）：登记表与字段、管理结构演进、
记录表级/字段级血缘、做下游影响分析，并支持标签、术语库、归属、权限与并发安全。

- 详细协议与判定规则见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)。

## 能力概览

- **元数据登记与结构演进**
  - 同一表可多次提交版本，版本不可变、可回放；
  - 新增字段必须带明确稳定的 `default_value`（历史行按此补全）；
  - 删字段 / 改名字必须显式声明，拒绝原因可区分：
    `ILLEGAL_RENAME` / `IMPLICIT_DROP` / `TYPE_INCOMPATIBLE` /
    `MISSING_DEFAULT` / `RENAME_CONFLICT`；
  - 字段有跨版本稳定 id，改名不断血缘；历史血缘结论不随后续更新改变。
- **血缘与影响分析**
  - 表级 + 字段级边；确定性边键保证重复提交/重启不重边、不漏边；
  - 成环被显式拒绝并给出环（`CYCLE_DETECTED`），影响分析遇环也会标记而非死循环；
  - 下游清单稳定排序，附逐跳解释路径。
- **标签 / 术语 / 归属 / 权限 / 并发**
  - 仅 owner 或 `admin`/`steward` 可写；术语发布需特权角色；
  - `404 NOT_FOUND` 与 `403 FORBIDDEN` 严格区分；
  - RLock + fcntl 文件锁 + 原子写 + 乐观锁，多线程/多进程并发不丢更新、不写一半；
  - `Idempotency-Key` 批量登记：同 key 只执行一次，失败整批回滚。

## 目录结构

```
main.py                  # 应用入口（uvicorn / create_app）
app/
  models.py              # 领域模型与请求/响应 schema
  errors.py              # 错误码与异常
  storage.py             # 内存 + JSON 文件存储（锁、事务、原子写）
  evolve.py              # 结构演进兼容性判定
  lineage.py             # 边键、环检测、下游闭包
  permissions.py         # 身份识别与授权
  service.py             # 服务编排（事务、幂等、解释依据）
  routers.py             # HTTP 路由与统一错误响应
tests/                   # pytest：演进/成环/重复提交/越权/并发/跨进程/HTTP
docs/PROTOCOL.md         # 协议与判定规则
```

## 本地运行

需要 Python 3.14+（用 [uv](https://docs.astral.sh/uv/)）：

```bash
uv sync
uv run python main.py
# 或：uv run uvicorn main:app --reload
```

默认把数据持久化到当前目录 `catalog_data.json`（配 `catalog_data.json.lock`）。
环境变量：

- `CATALOG_DB=/path/to/file.json`：指定持久化文件；
- `CATALOG_MEM=1`：纯内存模式（不写盘，便于快速试验）。

打开交互文档：<http://127.0.0.1:8000/docs>。

## 本地验证

### 1. 运行测试

```bash
uv run pytest
```

重点覆盖（单测日志会打印每次操作的输入与判定依据）：

- `tests/test_evolution.py`：默认值、非法改名、隐式删列、类型不兼容、回放稳定性；
- `tests/test_lineage.py`：重复提交幂等、重启恢复、成环拦截、自环、影响解释；
- `tests/test_authz_and_concurrency.py`：越权 vs 不存在、乐观并发、
  多线程并发重复登记同一批元数据（不重复、不丢失、原因可区分）；
- `tests/test_multiprocess.py`：多进程（真实服务实例）并发，同 key 执行一次、
  不同 key 仅一胜且失败可区分；
- `tests/test_api_http.py`：HTTP 状态码/错误码端到端。

### 2. 手动 curl 走查

```bash
uv run python main.py &             # 启动服务
B=http://127.0.0.1:8000
H='-H X-User:alice -H Content-Type:application/json'

# 登记表并提交 v1
curl $H -XPOST $B/tables -d '{"id":"ods_user","name":"用户表","owner":"alice"}'
curl $H -XPOST $B/tables/ods_user/versions -d '{
  "fields":[{"name":"id","type":"integer"},
            {"name":"name","type":"string"}]}'

# 新增字段必须给默认值；改名字必须显式声明 -> v2
curl $H -XPOST $B/tables/ods_user/versions -d '{
  "fields":[{"name":"id","type":"integer"},
            {"name":"full_name","type":"string"},
            {"name":"age","type":"integer","default_value":"0"}],
  "renames":[{"from_name":"name","to_name":"full_name","type":"string"}],
  "expected_version":1}'

# 回放所有版本
curl $H $B/tables/ods_user/replay

# 登记下游与血缘、查影响
curl $H -XPOST $B/tables -d '{"id":"dwd_user","name":"明细","owner":"alice"}'
curl $H -XPOST $B/tables/dwd_user/versions -d '{"fields":[{"name":"id","type":"integer"}]}'
curl $H -XPOST $B/lineage -d '{"upstream_table":"ods_user","downstream_table":"dwd_user"}'
curl $H $B/tables/ods_user/impact

# 成环会被拒绝
curl $H -XPOST $B/lineage -d '{"upstream_table":"dwd_user","downstream_table":"ods_user"}'

# 越权（bob，403）与不存在（404）可区分
curl -H X-User:bob $B/tables/ods_user
curl -H X-User:bob -XPOST $B/tables/ghost/tags -d '{"add":["x"]}'
```

### 3. 幂等批量登记

```bash
curl $H -H 'Idempotency-Key: batch-001' -XPOST $B/batch -d '[
  {"op":"register_table","body":{"id":"t_a","name":"A","owner":"alice"}},
  {"op":"submit_version","body":{"table_id":"t_a","fields":[{"name":"id","type":"integer"}]}}
]'
# 用同一个 key 再发一次：返回 replayed=true，不会重复建表/建边
```
