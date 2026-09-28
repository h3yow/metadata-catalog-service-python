# 元数据目录服务：登记协议与判定规则

本地 FastAPI 服务，用于登记数据表、字段、表级/字段级血缘、标签、术语与归属。
所有结论都带 `reasons` / `explanations` 字段，保证稳定、可解释。

## 1. 身份与权限协议

通过请求头传递调用方（本地简化方案，不做密码学鉴权）：

| Header | 含义 |
|---|---|
| `X-User` | 用户 id，缺省 `anonymous` |
| `X-Roles` | 逗号分隔角色，如 `admin,steward` |

角色：`admin`（最高）、`steward`（数据管理员）、`viewer`/其他（普通用户）。

授权矩阵：

| 操作 | 允许的人 |
|---|---|
| 登记表 / 提交版本 / 加血缘（下游方）/ 加标签 / 转移归属 | 该表 **负责人 owner**，或 `admin`/`steward` |
| 发布术语 | 仅 `admin`/`steward` |
| 查询表/版本/血缘/影响/术语 | 任何已识别用户 |

**不存在与越权严格区分**：所有写接口先判定资源是否存在（不存在 → `404 NOT_FOUND`），
再判定权限（无权 → `403 FORBIDDEN`）。不会因为资源不存在而泄露成 403，
也不会因为无权而返回 404。

## 2. 错误码（可区分的失败原因）

统一响应体：

```json
{ "error": { "code": "ILLEGAL_RENAME", "message": "...", "details": { } } }
```

| HTTP | code | 触发条件 |
|---|---|---|
| 404 | `NOT_FOUND` | 表 / 版本 / 字段 / 术语不存在 |
| 403 | `FORBIDDEN` | 非负责人且无 `admin`/`steward` 角色；非特权角色发布术语 |
| 409 | `DUPLICATE_ENTRY` | 重复登记表 / 重复术语 / 重复字段名 |
| 409 | `VERSION_CONFLICT` | `expected_version` 与服务端当前版本不一致（乐观锁失败） |
| 409 | `CYCLE_DETECTED` | 新增血缘会在表级图中成环；`details.cycle` 给出环 |
| 422 | `ILLEGAL_RENAME` | 旧字段消失、同类型新字段出现，但未用 `renames` 声明 |
| 422 | `IMPLICIT_DROP` | 旧字段消失且未用 `drops` 显式声明 |
| 422 | `TYPE_INCOMPATIBLE` | 同名字段类型变化，或借改名修改类型 |
| 422 | `MISSING_DEFAULT` | 新增字段未提供非空 `default_value` |
| 422 | `RENAME_CONFLICT` | 改名源/目标重复、目标不在 fields、源同时被保留 |
| 422 | `VALIDATION_ERROR` | 字段为空、字段名重复、drop 与 fields/rename 冲突等 |

## 3. 结构演进协议（兼容与演进规则）

### 3.1 版本模型

- 一张表首次 `POST /tables/{id}/versions` 产生不可变 **v1**，字段分配稳定整数 id（从 1 起）。
- 之后每次提交产生 v2、v3…… 每个版本记录都完整保存当时的字段清单、`renames`、
  `drops`、`change_note`、提交人、提交时间，**只追加、永不修改**。
- 提交体含 `expected_version`（乐观锁）：必须等于服务端当前版本号，否则
  `VERSION_CONFLICT`。并发演进只有一个成功，其余得到明确冲突原因，可重试。

### 3.2 提交体语义

```json
{
  "fields": [ {"name": "id", "type": "integer"},
              {"name": "age", "type": "integer", "default_value": "0"} ],
  "renames": [ {"from_name": "name", "to_name": "full_name", "type": "string"} ],
  "drops":  [ {"name": "tmp_col"} ],
  "change_note": "why",
  "expected_version": 2
}
```

`fields` 是「期望的最终字段集合」。判定顺序（决定拒绝原因时结构问题优先）：

1. **批次内静态校验**：fields 非空且字段名唯一；renames 的源、目标各自唯一；
   rename 目标必须出现在 fields；源不能同时被原样保留；drop 的字段不能同时
   出现在 fields 或 rename 源。
2. **引用存在性与改名类型**：drop/rename 的源必须是上一版本存在的字段；
   改名时类型必须与旧类型一致且与新字段声明一致——**不允许借改名改类型**。
3. **静默改名 / 静默删列**：旧字段消失且既不在 `drops` 也不在 `renames` 时，
   - 若新集合里存在一个同类型“孤列”，判 `ILLEGAL_RENAME`（`details` 给出
     `from` 与 `to_guess`），提示必须显式声明改名；
   - 否则判 `IMPLICIT_DROP`，必须用 `drops` 显式删除。
4. **类型兼容**：同名保留字段类型必须与旧类型完全一致，否则 `TYPE_INCOMPATIBLE`。
5. **新增字段默认值**：真正新增的字段必须给非空 `default_value`，否则
   `MISSING_DEFAULT`。该默认值即“新增字段的明确稳定默认语义”：历史行在该列
   上以此字面量补全，语义随版本永久记录在该字段的 `default_value` 中。

> 设计取舍：类型系统采用“相等即兼容”的保守策略，不做隐式加宽（如 int→bigint），
> 任何类型变化都必须显式处理，避免静默的下游破坏。

### 3.3 稳定字段 id 与历史血缘不变性

- 保留字段、改名字段**沿用原字段 id**；新增字段分配 `max(existing_id)+1`。
- 血缘按字段 **id** 记录，因此字段改名不会让已有血缘断裂。
- 每个版本不可变、可独立回放：`GET /tables/{id}/replay` 与
  `GET /tables/{id}/versions/{n}` 永远返回当时的结构。
- 历史血缘结论只引用登记时刻的版本数据；后续演进不改变历史版本内容，
  因此“历史血缘结论不会因为后来的更新而改变”。

## 4. 血缘与影响分析

### 4.1 登记

`POST /lineage`

```json
{ "upstream_table": "ods", "upstream_field_id": 2,
  "downstream_table": "dwd", "downstream_field_id": 2,
  "transform": "direct" }
```

- 表级边：两端 `field_id` 均为 `null`；字段级边：两端都必须给 id（不成对 →
  `VALIDATION_ERROR`），且字段必须在对应表当前版本中存在。
- 需要下游表的 owner/admin/steward 权限。
- **确定性边键**：`{up_table}.{f|*}->{down_table}.{f|*}#{transform}`。
  重复提交命中同键时返回 `status="duplicate"` 且不新增边——天然幂等；
  服务重启后从持久化文件恢复，键不变，因此重复提交/重启都不产生重复边或漏边。
- 每条边带全局单调 `seq` 与创建时间/创建人。

### 4.2 成环判定

- 在**表级 DAG** 上检测：加入新边前，从下游点 DFS，若能沿现有边回到上游点则
  拒绝登记（`409 CYCLE_DETECTED`），`details.cycle` 形如
  `["c","a","b","c"]`；自环（a→a）同样拒绝。
- 被拒的边不落库。因为登记时强约束，正常图无环；影响分析遍历仍做三色标记，
  万一存在环也会把环显式收集到 `cycles` 字段而**不是死循环或悄悄丢掉**。

### 4.3 影响分析

`GET /tables/{id}/impact` 返回：

- `downstream_tables`：从该表沿表级边可达的全部下游表，**按表 id 字典序稳定排序**；
- `explanations`：每个下游一条传播路径（表 id 序列），解释“为什么它受影响”；
- `cycles`：遍历时发现的环（正常为空）；
- `field_impacts`：字段级影响，key 为上游当前版本字段 id，value 为直接下游字段；
- `reasons`：判定口径说明。

同一张图从不同入口查询时，共享子图上的结论一致（同一组可达边），且都可解释。

## 5. 标签、归属、术语

- `POST /tables/{id}/tags`，body `{"add": [...], "remove": [...]}`：
  owner/admin/steward 可操作；幂等（重复 add/remove 不产生重复标签），返回实际动作。
- `POST /tables/{id}/owner`，body `{"new_owner": "..."}`：
  owner/admin/steward 可转移；转移后旧 owner 立即失去写权限，新 owner 获得。
- `POST /terms`：仅 admin/steward；可链接表与字段（链接对象必须存在）。
  术语 id 由名字规范化得到，重复发布 → `DUPLICATE_ENTRY`。

## 6. 批量登记与幂等

`POST /batch`，请求头 `Idempotency-Key: <string>`，body 为操作数组：

```json
[ {"op": "register_table", "body": {...}},
  {"op": "submit_version", "body": {"table_id": "...", ...}},
  {"op": "add_lineage", "body": {...}} ]
```

- 整批在**一个事务**内执行，任何一项失败则整批回滚（不留下半批表/边）。
- 同一 `Idempotency-Key`：首次执行并缓存结果；之后无论重复多少次都返回首次结果，
  标记 `replayed=true`，绝不重复执行（不重复建表/建边）。
- 不同 key 提交同一批数据：恰有一批成功，其余在首个冲突项处失败，错误码可区分
  （如 `DUPLICATE_ENTRY`、`CYCLE_DETECTED`），且失败批整批回滚。

## 7. 并发与持久化保证

- 进程内：所有写事务由一把 `RLock` 串行化；
- 跨进程：每个 DB 文件配一把 `fcntl` 排他文件锁，事务范围为
  “加锁 → 从磁盘重读最新状态 → 在深拷贝上修改 → 原子落盘 → 释放”；
- 事务基线在成功提交前不会被修改，失败事务直接丢弃工作副本，因此并发回滚
  不会覆盖他人已提交状态（无丢失更新、无写一半）；
- 落盘用“同目录临时文件 + `os.replace` + fsync”，崩溃也不会出现半截 JSON；
- 乐观锁 `expected_version` 提供应用层冲突检测，二者结合保证最终状态一致。

## 8. 端点一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/tables` | 登记表 |
| POST | `/tables/{id}/versions` | 提交新版本（演进） |
| GET | `/tables/{id}` | 表与当前字段 |
| GET | `/tables/{id}/versions/{n}` | 指定历史版本 |
| GET | `/tables/{id}/replay` | 回放全部版本 |
| POST | `/lineage` | 登记表级/字段级血缘 |
| GET | `/lineage` | 全部边（按 seq 排序） |
| GET | `/tables/{id}/impact` | 下游影响分析 |
| POST | `/tables/{id}/tags` | 增删标签 |
| POST | `/tables/{id}/owner` | 转移归属 |
| POST | `/terms` / GET | 发布 / 列出术语 |
| POST | `/batch` | 幂等批量登记（需 `Idempotency-Key`） |

数据类型枚举：`string | integer | bigint | double | boolean | date | timestamp`。
