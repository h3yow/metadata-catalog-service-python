# Metadata Catalog Service（元数据目录服务）

一套可以在本地直接跑起来的 FastAPI 服务，用来登记**数据表 / 字段 / 血缘 / 标签 / 术语 / 归属**，
并在不同入口（HTTP、多线程并发、进程重启、历史版本回放）下给出**一致、稳定、可解释**的
结构演进与上下游影响结论。

- 存储：SQLite（WAL），单连接 + 进程锁 + `BEGIN IMMEDIATE`，写操作串行化；
- 版本：只追加（append-only）事件日志 + 字段状态按版本追加，历史永不被覆盖；
- 血缘：表级 / 字段级两类边，字段以稳定身份 `fid` 穿越改名，边有冻结快照；
- 权限：404（不存在）/ 401（未认证）/ 403（越权）三种结果严格可区分；
- 并发：归属转移带 `expected_revision` 乐观锁，重复登记天然幂等。

---

## 1. 本地运行

```bash
uv sync                       # 安装依赖（fastapi / pytest / httpx）
.venv/bin/python main.py      # 或：.venv/bin/uvicorn main:app --reload --port 8000
```

- 默认数据库文件：`data/catalog.db`（首次自动建表）；
- 可用环境变量 `METADATA_DB_PATH` 覆盖路径；
- 健康检查：`GET /health` 返回当前事件序列 `event_seq`。

跑测试（含结构演进、成环、重复提交、越权、并发）：

```bash
.venv/bin/python -m pytest -q -s       # -s 可看到每个用例打印的输入与判定依据日志
```

### 认证约定（本地用）

所有写接口通过请求头表达调用方，便于在 HTTP 层清楚地区分权限结果：

| Header | 含义 |
| --- | --- |
| `X-Principal-User` | 调用方用户；缺省为 `anonymous`（写操作 -> 401） |
| `X-Principal-Roles` | 逗号分隔角色，支持 `admin`、`steward` |

所有业务错误统一信封：

```json
{"error": {"code": "STABLE_CODE", "message": "human readable", "details": {...}}}
```

---

## 2. 登记协议

### 2.1 登记 / 演进一张表

`POST /api/v1/tables`

```json
{
  "name": "orders",
  "owner": "alice",
  "description": "订单主表",
  "expected_revision": 1,
  "fields": [
    {"name": "id",     "data_type": "int64", "nullable": false},
    {"name": "amount", "data_type": "int64", "nullable": true,
     "default_semantic": "zero_value"}
  ],
  "renamed_fields": {"old_name": "new_name"},
  "dropped_fields":  ["legacy_col"]
}
```

响应：

```json
{"version": { ... 完整的新版本快照 ... }, "deduped": false}
```

- `owner` 仅首次登记必填；
- `expected_revision`：乐观锁，必须等于当前 revision，否则 `409 REVISION_MISMATCH`；
- `deduped=true` 表示这是一次**完全相同的重复提交**，不产生新版本（幂等）。

字段：

| 字段 | 说明 |
| --- | --- |
| `data_type` | `int32/int64/float32/float64/string/boolean/date/timestamp/bytes/json` |
| `nullable` | 默认 `true` |
| `default_semantic` | 新字段必填，见 §3.3 |
| `default_value` | 当 `default_semantic=explicit_value` 时必填 |

查询：

- `GET /api/v1/tables/{name}` —— 当前版本
- `GET /api/v1/tables/{name}/versions` —— 全部版本（可逐个回放）
- `GET /api/v1/tables/{name}/versions/{v}` —— 指定历史版本快照

### 2.2 登记血缘边

`POST /api/v1/lineage/edges`

```json
{
  "upstream":   {"table": "ods_orders", "field": "amount"},
  "downstream": {"table": "dwd_orders", "field": "amount"},
  "transformation": "cast(amount as bigint)",
  "expected_upstream_version": 2,
  "expected_downstream_version": 1,
  "allow_cycle": false
}
```

- `field` 省略即为**表级**边；填写则为**字段级**边（`''` 与名字在内部区分）；
- 端点必须存在于当前版本，否则 `404 TABLE_NOT_FOUND / FIELD_NOT_FOUND`；
- 给定 `expected_*_version` 但与当前版本不一致时 `409 FIELD_VERSION_MISMATCH`；
- 重复边（按当前字段名 **或** 稳定 `fid` 判定）返回同一条边且 `deduped=true`，
  服务重启后同样不产生重复边、也不漏边。

### 2.3 影响分析 / 历史回放

- `GET /api/v1/lineage/downstream/{table}?field=&as_of_seq=&include_stale=`
  - 表级查询会同时沿该表的表级边和所有字段身份展开；
  - 字段级查询只沿字段身份链；
  - `as_of_seq`：只使用该事件序号之前已登记的边与当时的字段状态，**历史结论不会随后续更新改变**；
  - `include_stale`：默认隐藏指向已删除字段的分支，置 true 时保留并可解释；
  - 每个下游节点带 `distance` 与 `via_edges`（到达它的完整解释路径），输出按
    `(distance, table, field)` 全序排序，结论确定。
- `GET /api/v1/lineage/replay/{seq}`：重放不可变事件日志，返回该时点的图状态。

### 2.4 归属 / 标签 / 术语

| 接口 | 权限 |
| --- | --- |
| `POST /api/v1/tables/{name}/owner`（body：`new_owner`,`expected_revision`） | owner / admin，且乐观锁 |
| `POST /api/v1/tables/{name}/grants`（body：`grantee`） | owner / admin，给某用户授予该表 steward |
| `PUT /api/v1/tables/{name}/tags` | owner / 被授权人 / admin |
| `DELETE /api/v1/tables/{name}/tags/{key}` | 同上 |
| `GET /api/v1/tables/{name}/tags` | 任意（读） |
| `POST /api/v1/glossary/terms` | 仅 `steward` / `admin` 角色；重复发布版本号 +1 |
| `GET /api/v1/glossary/terms/{term}` | 任意（读） |

---

## 3. 兼容性与结构演进规则

所有"不合法演进"都会被**拒绝**并给出**稳定、可区分的错误码**，绝不当作普通覆盖静默处理：

| 场景 | 判定 | 错误码（HTTP） |
| --- | --- | --- |
| 新表缺 owner / 字段缺名字 / 显式默认值缺值 | 必填信息缺失 | `MISSING_REQUIRED_INFO` (422) |
| 新字段未声明默认语义 | 见 §3.3 | `MISSING_DEFAULT_SEMANTIC` (422) |
| 旧版本里存在、新版本消失，但既不在 `dropped_fields` 也不在 `renamed_fields` | 疑似静默删字段 | `ILLEGAL_DROP` (422) |
| 显式删除一个当前不存在的字段 | 删除目标非法 | `ILLEGAL_DROP` (422) |
| 改名源不存在 / 目标为空 / 目标与现存字段撞名 / 两个字段改成同名 / 字段既声明改名又当普通字段提交 | 非法改名 | `ILLEGAL_RENAME` (422)（源缺失为 `FIELD_NOT_FOUND` 404） |
| 类型非拓宽方向变化（如 `int64->int32`、`string->int64`） | 类型不兼容 | `INCOMPATIBLE_TYPE` (422) |
| `nullable=true` 收紧为 `false` | 约束不兼容 | `INCOMPATIBLE_TYPE` (422) |
| 并发演进基于旧 revision 提交 | 乐观锁冲突 | `REVISION_MISMATCH` (409) |
| 与当前版本完全相同的提交 | 幂等，不拒绝，`deduped=true` | — |

### 3.1 类型兼容矩阵（只允许拓宽）

```
int32 -> int64 -> float64        float32 -> float64
date  -> timestamp
string / boolean / bytes / json  仅允许保持自身
```

不在上述链上的变化一律 `INCOMPATIBLE_TYPE`，`details` 中给出字段名、旧类型、新类型。

### 3.2 改名与删字段

- 改名是**一等操作**：必须经 `renamed_fields` 显式声明；
- 字段身份 `fid` 在改名后保持不变：血缘边、影响链连续，不会因改名断链或被当成"删旧增新"；
- `fields` 采用"完整快照"语义：当前所有存活字段都必须出现在新快照里，
  任何未交代的消失都会被判为 `ILLEGAL_DROP`。

### 3.3 新增字段的默认语义（稳定、可回放）

新增字段必须显式声明 `default_semantic`，取值之一：

`null_default` / `zero_value` / `empty_string` / `false_value` /
`current_timestamp` / `explicit_value`（后者必须同时给 `default_value`）。

这样历史版本回放时，每个"当时新增的列"对旧数据如何补值都有明确、稳定的含义。

### 3.4 版本回放

- 每次被接受的变更集产生且仅产生一个新版本，`tables.revision == 当前版本号`；
- `fields` 表按版本追加：旧行以 `ended_in_version`/`ended_reason` 封存，新行携带同一 `fid` 打开；
- 任意历史版本都可以通过版本接口或事件序列原样重建，血缘快照（登记时的表/字段名与版本）冻结，
  **历史血缘结论不会因为后来的改名、删字段、加边而改变**。

---

## 4. 血缘与影响分析的判定方式

1. **去重**：边在 `(上游表,上游字段,下游表,下游字段)` 与稳定 `fid` 两个维度唯一；
   并发与重启下依靠唯一约束 + 写事务保证"不重复、不漏边"。
2. **成环检测（登记前）**：若新边会使"下游 -> 上游"可达，则
   - 默认拒绝：`409 CYCLE_DETECTED`，`details.cycle` 给出闭合的 `表#字段` 路径；
   - `allow_cycle=true` 时允许登记，但该边标记 `is_cyclic=1`；
   - 遍历使用带 visited 集合的 BFS，**永不死循环**，影响报告的 `cycles` 列出基本环。
3. **影响范围**：从根节点 BFS，表级查询扇出到 `t:<表>` 与该表所有 `f:<fid>`；
   字段级查询只从该 `fid` 出发。首次到达某节点的边路径即其 `via_edges` 解释。
4. **改名连续性**：遍历以 `fid` 为 key，输出时再映射回（当时/当前的）字段名；
   冻结在边上的名字保证每条结论都能解释"登记那一刻看到的是什么"。
5. **失效字段**：实时查询中，指向已不存在字段身份的边默认不进入下游清单，
   但环检测仍考虑全图；`include_stale=true` 可查看这些分支。
6. **稳定性**：节点排序、环的规范化（从最小节点旋转）均为全序，重复查询结果字节级一致。

---

## 5. 并发与一致性

- 所有写操作在 `app.db.write_tx()` 中运行：同一进程内串行（`threading.RLock`），
  最外层 `BEGIN IMMEDIATE`，服务内组合调用使用 SAVEPOINT，失败只回滚自己那一层，
  不会出现"写了一半"；
- 重复登记表 / 重复登记边：唯一约束 + 先查重，16/20 个线程同时提交时只有一个创建，
  其余全部得到 `deduped=true`（见 `tests/test_concurrency.py`）；
- 归属转移：`expected_revision` 乐观锁。多个授权方并发转移时恰好一个成功，
  其余 `409 REVISION_MISMATCH`；原 owner 在失去归属后再操作得到 `403 FORBIDDEN`；
- 标签写为 keyed upsert，并发写入互不覆盖，最终键集合完整一致。

---

## 6. HTTP 接口与错误码速查

```
POST   /api/v1/tables                         登记 / 演进
GET    /api/v1/tables/{name}                  当前表
GET    /api/v1/tables/{name}/versions         版本列表
GET    /api/v1/tables/{name}/versions/{v}     历史版本
POST   /api/v1/lineage/edges                  登记血缘边
GET    /api/v1/lineage/downstream/{table}     下游影响（?field&as_of_seq&include_stale）
GET    /api/v1/lineage/replay/{seq}           事件日志回放
POST   /api/v1/tables/{name}/owner            转移归属
POST   /api/v1/tables/{name}/grants           授予 steward
PUT    /api/v1/tables/{name}/tags             加/改标签
DELETE /api/v1/tables/{name}/tags/{key}       删标签
GET    /api/v1/tables/{name}/tags             查标签
POST   /api/v1/glossary/terms                 发布/更新术语（steward/admin）
GET    /api/v1/glossary/terms/{term}          查术语
GET    /health
```

错误码：`TABLE_NOT_FOUND / FIELD_NOT_FOUND / VERSION_NOT_FOUND / GLOSSARY_TERM_NOT_FOUND`(404)、
`UNAUTHENTICATED`(401)、`FORBIDDEN`(403)、`REVISION_MISMATCH / CYCLE_DETECTED /
FIELD_VERSION_MISMATCH`(409)、`MISSING_REQUIRED_INFO / MISSING_DEFAULT_SEMANTIC /
ILLEGAL_RENAME / ILLEGAL_DROP / INCOMPATIBLE_TYPE / EMPTY_CHANGESET`(422)、
`INVALID_REQUEST`(400)。

---

## 7. 代码结构与测试

```
app/
  errors.py     稳定错误码 + CatalogError（code/status/details）
  models.py     请求/响应模型
  db.py         SQLite schema、连接、串行化/可嵌套写事务
  catalog.py    登记、结构演进规则、版本回放
  lineage.py    边登记、成环检测、影响分析、事件回放
  governance.py 归属、授权、grants、标签、术语
  api.py        FastAPI 路由、认证头、错误信封
main.py         create_app() + uvicorn
tests/          31 个用例（演进/血缘/治理/并发/HTTP）
```

测试关注点（每个用例都打印输入 CASE 与判定依据 JUDGEMENT 日志）：

- `tests/test_catalog.py`：默认语义、非法改名、类型不兼容、静默删字段、乐观锁、版本回放；
- `tests/test_lineage.py`：重复提交、重启不重不漏、成环拒绝/显式标记、确定性、改名连续、历史回放；
- `tests/test_governance.py`：404/401/403 区分、授权、标签、术语角色、归属锁；
- `tests/test_concurrency.py`：16 路重复登记、20 路重复边、并发归属转移、24 个并发标签；
- `tests/test_api.py`：HTTP 端到端错误信封与完整演进-血缘流程。
