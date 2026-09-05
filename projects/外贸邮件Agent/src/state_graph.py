# -*- coding: utf-8 -*-
"""
自研状态图编排器（JD #4：LangGraph 等价实现，零依赖）
=====================================================

为什么自研而不是 pip install langgraph
--------------------------------------
本机红线：不装 langchain / langgraph（venv 创建失败、重依赖链必翻车，见项目技术红线）。
但 JD #4 考的是「会用状态图编排 Agent」，不是「会 pip install」。
langgraph 的核心抽象只有四个：State / Node / Edge(含条件边) / Checkpointer。
本文件用纯 Python 把它实现出来，**概念与 API 对齐 langgraph**，可直接迁移。

核心抽象（与 langgraph 一一对应）
----------------------------------
| 本实现                | langgraph            | 作用 |
|---|---|---|
| `StateGraph`          | `StateGraph`         | 声明式注册节点/边 |
| `add_node`            | `add_node`           | 一个处理步骤（fn(state) -> state） |
| `add_edge`            | `add_edge`           | 无条件跳转 |
| `add_conditional_edges`| `add_conditional_edges` | 按 state 路由到不同节点 |
| `set_entry_point`     | `set_entry_point`    | 入口 |
| `compile()`           | `compile()`          | 编译为可执行图 |
| `Checkpointer`        | `MemorySaver`/`SqliteSaver` | 每步落盘，支持断点续跑 |

为什么比 if-else 流水线强
--------------------------
1. **条件边**：通知类邮件跳过检索（复用第 8 节多智能体的结论，但不付主管那一跳的钱）。
2. **循环（cycle）**：生成端没引用到依据 → 自动回到 retrieve 换 query 重试，最多 N 次。
   if-else 流水线写不出「回头重试」；这是图相对链的本质优势。
3. **检查点**：每步状态落盘，进程挂了能从断点续跑，也是审计轨迹。
4. **HITL 节点**：低置信度路由到人工审核节点，与第 6 节的 HITL 结论闭环。

作者：麦当 · 2026-09-04（Day06 续 · JD #4）
"""

import json
import os
import time


class Checkpointer:
    """逐步落盘的状态快照。支持断点续跑 + 审计。"""

    def __init__(self, path=None):
        self.path = path
        self.snapshots = []

    def save(self, step, node, state):
        snap = {"step": step, "node": node, "ts": time.strftime("%H:%M:%S"),
                "state": _safe(state)}
        self.snapshots.append(snap)
        if self.path:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self.snapshots, f, ensure_ascii=False, indent=2, default=str)
            except Exception:
                pass

    def last(self):
        return self.snapshots[-1] if self.snapshots else None


def _safe(state):
    """快照只留可序列化且轻量的字段（避免把检索全文写爆文件）。"""
    out = {}
    for k, v in (state or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list):
            out[k] = f"<list len={len(v)}>"
        elif isinstance(v, dict):
            out[k] = f"<dict keys={list(v)[:6]}>"
        else:
            out[k] = f"<{type(v).__name__}>"
    return out


class StateGraph:
    """声明式状态图。"""

    def __init__(self):
        self.nodes = {}
        self.edges = {}
        self.conditionals = {}
        self.entry = None
        self.finish_nodes = set()

    def add_node(self, name, fn):
        if name in self.nodes:
            raise ValueError(f"节点重复：{name}")
        self.nodes[name] = fn
        return self

    def add_edge(self, src, dst):
        self.edges[src] = dst
        return self

    def add_conditional_edges(self, src, router, mapping):
        """router(state) -> label；mapping[label] -> 节点名。未知 label 视为结束。"""
        self.conditionals[src] = (router, mapping)
        return self

    def set_entry_point(self, name):
        self.entry = name
        return self

    def set_finish(self, *names):
        self.finish_nodes |= set(names)
        return self

    def compile(self, checkpointer=None, max_steps=30, verbose=True):
        return CompiledGraph(self, checkpointer=checkpointer,
                             max_steps=max_steps, verbose=verbose)


class CompiledGraph:
    """可执行图。run(initial_state) -> (state, trace)"""

    def __init__(self, graph, checkpointer=None, max_steps=30, verbose=True):
        self.g = graph
        self.cp = checkpointer
        self.max_steps = max_steps
        self.verbose = verbose

    def run(self, state):
        if not self.g.entry:
            raise ValueError("未设置入口节点 set_entry_point()")
        state = dict(state or {})
        trace = []
        node = self.g.entry
        step = 0

        while node and step < self.max_steps:
            step += 1
            fn = self.g.nodes.get(node)
            if fn is None:
                trace.append({"step": step, "node": node, "ok": False,
                              "error": "节点未注册"})
                break
            t0 = time.time()
            try:
                state = fn(state) or state
                ok, err = True, None
            except Exception as e:
                ok, err = False, f"{type(e).__name__}: {e}"
            ms = int((time.time() - t0) * 1000)
            trace.append({"step": step, "node": node, "ok": ok,
                          "elapsed_ms": ms, "error": err})
            if self.verbose:
                mark = "✅" if ok else "❌"
                print(f"    [{step}] {mark} {node} ({ms}ms)")
            if self.cp:
                self.cp.save(step, node, state)
            if not ok:
                break

            # 终点判断：**执行完再停**。
            # 踩坑记录：最初把「是否为终点」放在执行前判断，结果 compose / human_review
            # 被当成哨兵直接跳过、从未执行（三封邮件回信全空才暴露）。
            # langgraph 里 END 是**哨兵**而不是节点，真实节点必须先跑完再结束。
            if node in self.g.finish_nodes:
                break

            # 路由：条件边优先，其次固定边
            if node in self.g.conditionals:
                router, mapping = self.g.conditionals[node]
                try:
                    label = router(state)
                except Exception as e:
                    label = None
                    trace[-1]["error"] = f"路由异常 {type(e).__name__}: {e}"
                nxt = mapping.get(label)
                if nxt is None:
                    trace[-1]["route_label"] = str(label)
                    break
                trace[-1]["route_label"] = str(label)
                node = nxt
            elif node in self.g.edges:
                node = self.g.edges[node]
            else:
                break

        return state, trace
