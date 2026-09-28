"""血缘与影响分析：边管理、成环检测、下游闭包、解释路径。

判定方式
--------
- 边按「上游表/字段 + 下游表/字段 + transform」生成确定性 key，重复提交天然幂等，
  服务重启后从持久化数据恢复，不会产生重复边。
- 表级边：两端 field_id 均为 None；字段级边：两端都带 field_id。
- 成环检测在**表级 DAG** 上做（同一张表内部的字段边不影响表级环）。
  使用 DFS 三色标记，若加入某条边会成环，则拒绝登记并给出环上的表清单。
  已存在的环（理论上不会有，因为登记时拦截）在影响分析中也会被显式收集并标记，
  绝不死循环或悄悄丢掉。
- 影响分析：从指定表出发做 DFS 得到全部可达下游；下游清单按表 id 字典序稳定输出；
  每个下游都给出一条解释路径（沿边如何传播），并单独列出检测到的环。
"""

from __future__ import annotations

from .errors import CyclicLineageError


def edge_key(
    upstream_table: str,
    upstream_field_id: int | None,
    downstream_table: str,
    downstream_field_id: int | None,
    transform: str,
) -> str:
    u = "*" if upstream_field_id is None else f"f{upstream_field_id}"
    d = "*" if downstream_field_id is None else f"f{downstream_field_id}"
    return f"{upstream_table}.{u}->{downstream_table}.{d}#{transform}"


def _table_adjacency(edges: dict) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {}
    for e in edges.values():
        adj.setdefault(e["upstream_table"], set()).add(e["downstream_table"])
        adj.setdefault(e["downstream_table"], set())
    return adj


def detect_cycle(edges: dict, new_upstream: str, new_downstream: str) -> list[str]:
    """模拟加入 new_upstream -> new_downstream 后是否成环。

    成环则返回环上的表 id 列表（new_upstream 开头、new_upstream 结尾），否则 []。
    """
    if new_upstream == new_downstream:
        return [new_upstream, new_upstream]
    adj = _table_adjacency(edges)
    adj.setdefault(new_upstream, set()).add(new_downstream)
    adj.setdefault(new_downstream, set())

    # 只需判断从 new_downstream 能否沿边回到 new_upstream；
    # 找到后环为 [new_upstream, ...path..., new_upstream]。
    def dfs(node: str, trail: list[str]) -> list[str] | None:
        for nxt in sorted(adj.get(node, ())):
            if nxt == new_upstream:
                return [new_upstream, *trail, new_upstream]
            if nxt not in trail:
                found = dfs(nxt, [*trail, nxt])
                if found is not None:
                    return found
        return None

    return dfs(new_downstream, [new_downstream]) or []


def ensure_acyclic(edges: dict, new_upstream: str, new_downstream: str) -> None:
    cycle = detect_cycle(edges, new_upstream, new_downstream)
    if cycle:
        raise CyclicLineageError(
            "lineage edge would create a cycle: " + " -> ".join(cycle),
            details={"cycle": cycle},
        )


def downstream_closure(
    edges: dict,
    start_table: str,
) -> tuple[list[str], dict[str, list[list[str]]], list[list[str]]]:
    """从 start_table 求全部可达下游。

    返回：
      downstream : 稳定字典序排序的下游表 id（不含起点）
      paths      : 每个下游 -> 一条解释路径（表 id 序列，起点为 start_table）
      cycles     : 遍历时发现的环（每个环为闭合表 id 序列）；有环也不丢结果
    """
    adj = _table_adjacency(edges)

    paths: dict[str, list[str]] = {}
    cycles: list[list[str]] = []
    color: dict[str, int] = {}  # 0/1/2 白/灰/黑

    def visit(node: str, trail: list[str]) -> None:
        color[node] = 1
        for nxt in sorted(adj.get(node, ())):
            c = color.get(nxt, 0)
            if c == 0:
                if nxt != start_table and nxt not in paths:
                    paths[nxt] = [*trail, nxt]
                visit(nxt, [*trail, nxt])
            elif c == 1:
                # 回边 -> 环，从当前路径中 nxt 第一次出现处切出闭合环
                idx = trail.index(nxt)
                cycle = trail[idx:] + [nxt]
                if cycle not in cycles:
                    cycles.append(cycle)
        color[node] = 2

    adj.setdefault(start_table, set())
    visit(start_table, [start_table])

    downstream = sorted(t for t in paths if t != start_table)
    explanations = {t: paths[t] for t in downstream}
    return downstream, explanations, cycles
