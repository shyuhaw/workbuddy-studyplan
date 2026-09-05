# -*- coding: utf-8 -*-
"""
工具注册器（Agent 的工具层）
============================
把「外贸邮件处理流水线」的 7 个环节，包装成模型看得懂、调得动的 Function Calling 工具。

为什么要这一层
--------------
在 Day01-05 里，处理顺序是写死在 workflow.py::run_pipeline() 里的：

    _step_retrieve() → _step_draft()

那是**流水线**，不是 Agent —— 顺序由人决定，模型只是其中一个被调用的函数。

Function Calling 把控制权交出去：
    模型看工具清单 → 自己决定调哪个、按什么顺序、调几次 → 调 finish 结束
    真正执行代码的仍然是本地 Python（模型只出决策，本地出执行）。

本文件的职责边界
----------------
- 只做「能力暴露」：定义 JSON Schema + 绑定真实函数 + 统一执行入口
- 不做「决策」：调哪个工具由 agent_loop.py 里的模型决定
- 不做「状态机」：业务流程状态仍在 workflow.py

关键设计
--------
1. **description 写得带边界**：模型选错工具 90% 的原因不是模型笨，是描述没说清
   「什么时候不该用它」。每个工具的 description 都显式写了「何时不用」。

2. **会话态 ToolSession**：retrieve 的结果要传给 rerank / build_context，
   不可能全塞进消息历史（烧 token）。所以中间结果存在 session 里，
   工具之间靠「id 引用」传递 —— 这和真实 Agent 的做法一致。

3. **错误不抛异常，返回结构化错误字符串**：模型要能「看见」错误并自我修正，
   直接 raise 会让整个循环崩掉。

4. **幂等 + 零重复烧钱**：同一 (工具名, 参数) 二次调用直接返回缓存结果。

作者：麦当
日期：2026-09-04（Day06）
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from classifier import score_email
except Exception:
    score_email = None

try:
    from extractor import extract_fields as _extract_fields_rule
except Exception:
    _extract_fields_rule = None

try:
    from vector_retriever import build_hybrid
except Exception:
    build_hybrid = None

try:
    from context_builder import build_context, EMPTY_CONTEXT
except Exception:
    build_context = None
    EMPTY_CONTEXT = "（无相关历史记录）"

try:
    from generator import AnswerGenerator
except Exception:
    AnswerGenerator = None

try:
    from reranker import LLMReranker
except Exception:
    LLMReranker = None


# ----------------------------------------------------------------------
# JSON Schema（OpenAI / DeepSeek 通用格式）
# ----------------------------------------------------------------------
# 说明：description 的写法是这一层的核心资产。
#       「什么时候用它」+「什么时候别用它」缺一不可。
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "classify_email",
            "description": (
                "判断一封外贸邮件属于哪一类：询盘 inquiry / 订单 order / 投诉 complaint / 通知 notification。"
                "【何时用】收到新邮件、需要决定后续走什么处理路径时，第一步就调它。"
                "【何时不用】已经分过类的邮件不要重复调用；"
                "它只给类别和建议置信度，不会告诉你客户名、产品、数量等具体字段（那是 extract_fields 的事）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "邮件主题"},
                    "body": {"type": "string", "description": "邮件正文"},
                },
                "required": ["subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_fields",
            "description": (
                "从当前邮件中抽取结构化字段：客户名 customer / 产品 product / 数量 quantity / 目标价 price / 期望交期 deadline。"
                "【何时用】需要知道这封邮件的具体业务要素时；也是构造检索 query 的前置步骤。"
                "【何时不用】它只抽当前邮件里写明的字段，查不到历史成交价和过往往来 —— "
                "那属于检索范围，请用 retrieve_history。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_history",
            "description": (
                "在客户历史往来语料库中做混合检索（BM25 关键词 + 向量语义 + 意图自适应调权），"
                "返回最相关的历史记录片段。"
                "【何时用】需要引用客观事实性信息时必调 —— 历史成交价、该客户过往规格、类似询盘的报价。"
                "不检索就回答，等于让模型编造价格，这是必须避免的。"
                "【何时不用】纯礼貌性回复、内部流程判断、或问题不涉及任何历史数据时不要调；"
                "同一 query 已检索过也不要重复调（会浪费成本）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索词。建议用「客户名 + 产品名」或具体问题，比整封邮件原文更有效",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回条数，默认 3。需要更全的候选时用 5-10（配合 rerank_history 精排）",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rerank_history",
            "description": (
                "对上一次 retrieve_history 的结果做交叉编码精排，把最相关的记录排到第一位。"
                "实测能把 P@1 从 75% 提升到 95%。"
                "【何时用】检索结果条数较多（>=4 条）、或检索词是语义类问法（不含明确产品关键词）时，"
                "精排收益最大。"
                "【何时不用】只有 1-3 条结果、或检索已经明显命中时不要调 —— "
                "精排要额外调 LLM，有成本和耗时，属于优化项不是必需项。"
                "【前置】必须先调用过 retrieve_history。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "原始问题，用于判断相关性"},
                    "top_k": {"type": "integer", "description": "精排后保留的条数，默认 3"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_context",
            "description": (
                "把检索到的历史片段组装成带 [1][2][3] 编号的上下文，并返回编号到原文 id 的映射。"
                "【何时用】准备生成答案之前必调 —— 没有编号，答案就无法标注引用，忠实度也就无从验证。"
                "【何时不用】不打算生成事实性答案时不必调。"
                "【前置】必须先调用过 retrieve_history（可选先调 rerank_history）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "当前要回答的问题"},
                    "hit_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要组装的历史记录 id 列表；留空则使用上一次检索的全部结果",
                    },
                    "max_chars": {"type": "integer", "description": "上下文字符预算，默认 2000"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_answer",
            "description": (
                "基于已组装好的带编号上下文，调用 LLM 生成答案，强制每句话标注引用编号，"
                "并校验引用是否真实存在。"
                "【何时用】上下文已就绪、需要产出给客户的答复内容时。"
                "【何时不用】上下文还没组装（必须先 build_context）；"
                "或问题在上下文里根本没有依据 —— 那种情况直接调 finish 说明「依据现有记录无法确认」，"
                "不要硬生成。"
                "【前置】必须先调用过 build_context。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要回答的问题"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "结束任务，提交最终答案。**这是唯一表示「我做完了」的方式，最后必须调用它。**"
                "【何时用】已经有足够信息回答时（无论答案是正面答复还是「依据现有记录无法确认」）。"
                "【何时不用】还没拿到事实依据就不要调 —— 先去检索。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {"type": "string", "description": "最终回复客户的正文"},
                    "cited_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "答案引用到的历史记录 id 列表（如 C07、C12）；无依据时留空数组",
                    },
                },
                "required": ["answer"],
            },
        },
    },
]

TOOL_NAMES = [t["function"]["name"] for t in TOOLS]

# ----------------------------------------------------------------------
# 跨轮记忆工具（仅在 memory 启用时挂上，见 make_tools）
# ----------------------------------------------------------------------
MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": (
                "把一条关于客户/线索的事实存入**跨轮工作记忆**，供后续同客户邮件召回。"
                "【存什么】确认过的合同规格、约定价格、客户偏好（语言/联系人/时区）、"
                "未决问题/索赔进度、本次处理结论。"
                "【边界】记忆是 Agent 的工作笔记，**可能过时或出错**；涉及价格/规格等权威事实，"
                "正式答复前仍必须 retrieve_history 校验，不要拿记忆当权威依据。"
                "【何时用】处理完一封邮件、确认了值得跨轮复用的信息后调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer": {"type": "string", "description": "客户名（与邮件客户一致）"},
                    "fact_type": {
                        "type": "string",
                        "description": "事实类型：contract_spec / price / preference / open_issue / contact / outcome",
                    },
                    "fact": {"type": "string", "description": "事实内容，一句话说清"},
                },
                "required": ["customer", "fact_type", "fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": (
                "召回某客户的跨轮历史记忆，处理新邮件前先查一遍以保证连贯。"
                "【何时用】开始处理一封邮件、尤其涉及「续单/跟进/反复出现的问题」时，"
                "先用本工具拉取该客户的历史上下文。"
                "【边界】召回结果仅供参考，不是权威事实；与本次邮件冲突时以检索到的权威记录为准。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer": {"type": "string", "description": "客户名"},
                    "query": {
                        "type": "string",
                        "description": "可选，用于筛选相关记忆的关键词（如 PO 号、产品名）；留空则返回该客户全部记忆",
                    },
                },
                "required": ["customer"],
            },
        },
    },
]

MEMORY_TOOL_NAMES = [t["function"]["name"] for t in MEMORY_TOOLS]


# ----------------------------------------------------------------------
# 会话态：工具的中间结果存放处
# ----------------------------------------------------------------------
class ToolSession:
    """一次 Agent 运行的共享状态。

    设计要点：中间结果不进消息历史（省 token），工具之间靠 id 引用传递。
    """

    def __init__(self, mail, retriever=None, reranker=None, generator=None, memory=None):
        self.mail = mail or {}
        self.retriever = retriever
        self.reranker = reranker
        self.generator = generator
        self.memory = memory  # 跨轮记忆存储（MemoryStore 实例）；None = 不启用记忆

        self.last_hits = []       # 上一次检索/精排的结果（含完整 text）
        self.last_context = None  # build_context 的完整返回
        self.last_answer = None   # generate_answer 的原始返回

        # 观测
        self.trace = []           # 每步 {tool, args, ok, elapsed_ms, result_len}
        self._cache = {}          # 幂等缓存：(tool, args_key) -> 结果字符串
        self.llm_cost = 0.0

    # -- 懒加载重型资源（建库 / 起 LLM 客户端都要时间，用到才建） --
    def _get_retriever(self):
        if self.retriever is None:
            if build_hybrid is None:
                raise RuntimeError("vector_retriever 不可用")
            self.retriever, _ = build_hybrid()
        return self.retriever

    def _get_reranker(self):
        if self.reranker is None and LLMReranker is not None:
            try:
                self.reranker = LLMReranker(verbose=False)
            except Exception:
                self.reranker = None
        return self.reranker

    def _get_generator(self):
        if self.generator is None and AnswerGenerator is not None:
            try:
                self.generator = AnswerGenerator()
            except Exception:
                self.generator = None
        return self.generator


def _compact_hits(hits, preview=60):
    """检索结果的紧凑表示：给模型看的，不能塞全文（烧 token）。"""
    out = []
    for h in hits:
        text = (h.get("text") or "").replace("\n", " ")
        out.append({
            "id": h.get("id"),
            "customer": h.get("customer"),
            "score": round(float(h.get("score", 0)), 4),
            "preview": text[:preview] + ("..." if len(text) > preview else ""),
        })
    return out


# ----------------------------------------------------------------------
# 工具实现（签名 = JSON Schema 里的参数，session 通过闭包绑定）
# ----------------------------------------------------------------------
def _t_classify(session, subject, body):
    if score_email is None:
        return {"error": "classifier 模块不可用"}
    cat, conf, scores, hits = score_email(subject, body)
    return {
        "category": cat,
        "confidence": round(float(conf), 3),
        "scores": {k: round(float(v), 2) for k, v in scores.items()},
        "matched_keywords": hits[:8],
    }


def _t_extract(session):
    if _extract_fields_rule is None:
        return {"error": "extractor 模块不可用"}
    raw = _extract_fields_rule(session.mail)
    return {
        k: {"value": v.get("value"), "confidence": round(float(v.get("confidence", 0)), 2)}
        for k, v in raw.items()
    }


def _t_retrieve(session, query, top_k=3):
    r = session._get_retriever()
    hits = r.search(query, top_k=int(top_k or 3))
    session.last_hits = hits
    return {"query": query, "count": len(hits), "hits": _compact_hits(hits)}


def _t_rerank(session, query, top_k=3):
    if not session.last_hits:
        return {"error": "尚未调用 retrieve_history，没有可精排的候选"}
    rr = session._get_reranker()
    if rr is None:
        return {"error": "reranker 不可用（无 API Key 或模块缺失），请直接用上一次检索结果"}
    ranked = rr.rerank(query, session.last_hits, top_k=int(top_k or 3))
    session.last_hits = ranked
    # 注意：reranker 用的是 rerank_score 排序，原 score 字段仍是混合检索分（不一定降序）。
    # 这里必须回传 rerank_score，否则模型会看到"顺序和分数对不上"而困惑。
    return {
        "query": query,
        "count": len(ranked),
        "order_note": "以下按相关性从高到低排列（rerank_score 为精排分，非原始检索分）",
        "reranked": [
            {
                "id": h.get("id"),
                "rerank_score": round(float(h.get("rerank_score", 0)), 2),
                "reason": (h.get("rerank_reason") or "")[:40],
            }
            for h in ranked
        ],
    }


def _t_build_context(session, query, hit_ids=None, max_chars=2000):
    if build_context is None:
        return {"error": "context_builder 模块不可用"}
    hits = session.last_hits
    if hit_ids:
        wanted = set(hit_ids)
        selected = [h for h in hits if h.get("id") in wanted]
        # 允许引用未在 last_hits 里的 id（例如模型手写 id）→ 明确报错而不是静默忽略
        missing = wanted - {h.get("id") for h in selected}
        if missing:
            return {"error": f"以下 id 不在上一次检索结果中：{sorted(missing)}，"
                             f"可用 id：{[h.get('id') for h in hits]}"}
        hits = selected
    if not hits:
        return {"error": "没有可组装的历史片段，请先调用 retrieve_history"}
    bundle = build_context(query, hits, max_chars=int(max_chars or 2000))
    session.last_context = bundle
    return {
        "context": bundle["context"],
        "id_map": bundle["id_map"],
        "kept": [h.get("id") for h in bundle["kept"]],
        "chars": bundle["chars"],
        "est_tokens": bundle["est_tokens"],
        "truncated": bundle["truncated"],
    }


def _t_generate(session, query):
    gen = session._get_generator()
    bundle = session.last_context
    if not bundle:
        return {"error": "尚未调用 build_context，没有可依据的上下文"}
    if gen is None:
        return {"error": "generator 不可用（无 API Key 或模块缺失）"}
    res = gen.generate(query, bundle["context"], bundle["id_map"])
    session.last_answer = res
    if res.get("est_cost"):
        session.llm_cost += float(res["est_cost"])
    return {
        "answer": res.get("answer"),
        "cited_ids": res.get("cited_ids", []),
        "mode": res.get("mode"),
        "invalid_citations": res.get("invalid_citations", []),
        "elapsed_sec": res.get("elapsed_sec"),
    }


def _t_finish(session, answer, cited_ids=None):
    return {"finished": True, "answer": answer, "cited_ids": list(cited_ids or [])}


# -- 跨轮记忆工具实现（依赖 session.memory） --
def _t_remember(session, customer, fact_type, fact):
    if session.memory is None:
        return {"error": "记忆存储未启用（AgentLoop.run 未传入 memory_store）"}
    entry = session.memory.remember(customer, fact_type, fact,
                                    mail_id=session.mail.get("id"))
    return {
        "ok": True,
        "stored_id": entry["id"],
        "customer": customer,
        "fact_type": fact_type,
        "total_for_customer": len(session.memory.get_customer(customer)),
    }


def _t_recall(session, customer, query=None):
    if session.memory is None:
        return {"error": "记忆存储未启用（AgentLoop.run 未传入 memory_store）"}
    rows = session.memory.recall(customer=customer, query=query, limit=5)
    if not rows:
        return {"customer": customer, "count": 0,
                "memories": [], "note": "该客户暂无跨轮记忆"}
    return {
        "customer": customer,
        "count": len(rows),
        "memories": [
            {"id": r["id"], "fact_type": r["fact_type"],
             "fact": r["fact"], "source": r["mail_id"], "ts": r["ts"]}
            for r in rows
        ],
    }


# ----------------------------------------------------------------------
# 统一执行入口
# ----------------------------------------------------------------------
def make_tools(session, memory=None):
    """返回 (TOOLS, IMPL)。IMPL 中的函数已绑定 session。

    当传入 memory（MemoryStore 实例）时，工具清单额外挂上跨轮记忆工具
    remember_fact / recall_memory，且 ToolSession.memory 指向它。
    不传（默认）= 原 7 工具，行为与 A/B 评测完全一致，零破坏。
    """
    if memory is not None:
        session.memory = memory

    impl = {
        "classify_email": lambda **kw: _t_classify(session, **kw),
        "extract_fields": lambda **kw: _t_extract(session, **kw),
        "retrieve_history": lambda **kw: _t_retrieve(session, **kw),
        "rerank_history": lambda **kw: _t_rerank(session, **kw),
        "build_context": lambda **kw: _t_build_context(session, **kw),
        "generate_answer": lambda **kw: _t_generate(session, **kw),
        "finish": lambda **kw: _t_finish(session, **kw),
    }
    spec = TOOLS
    if memory is not None:
        impl["remember_fact"] = lambda **kw: _t_remember(session, **kw)
        impl["recall_memory"] = lambda **kw: _t_recall(session, **kw)
        spec = TOOLS + MEMORY_TOOLS
    return spec, impl


def execute_tool(name, arguments, impl, session=None):
    """执行单个工具，返回 JSON 字符串（给模型回填用）。

    设计要点：
    - **错误不抛异常**：返回结构化错误字符串，让模型看得见、能自我修正
    - **幂等**：同参数二次调用返回缓存，不重复烧钱
    """
    fn = impl.get(name)
    if fn is None:
        return json.dumps({"error": f"未知工具：{name}。可用工具：{', '.join(impl)}"},
                          ensure_ascii=False)

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception as e:
            return json.dumps({"error": f"参数不是合法 JSON：{e}"}, ensure_ascii=False)
    arguments = arguments or {}

    # 幂等：finish 不缓存（它是终止动作，且模型可能想改答案重提）
    cache_key = None
    if session is not None and name != "finish":
        cache_key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
        if cache_key in session._cache:
            session.trace.append({
                "tool": name, "args": arguments, "ok": True,
                "elapsed_ms": 0, "result_len": 0, "cached": True,
            })
            return session._cache[cache_key]

    t0 = time.time()
    try:
        result = fn(**arguments)
        ok = True
    except TypeError as e:
        result = {"error": f"参数不符合 {name} 的签名：{e}"}
        ok = False
    except Exception as e:
        result = {"error": f"{name} 执行异常：{type(e).__name__}: {e}"}
        ok = False
    elapsed_ms = int((time.time() - t0) * 1000)

    out = json.dumps(result, ensure_ascii=False, default=str)

    if session is not None:
        session.trace.append({
            "tool": name, "args": arguments, "ok": ok,
            "elapsed_ms": elapsed_ms, "result_len": len(out), "cached": False,
        })
        if cache_key is not None and ok:
            session._cache[cache_key] = out
    return out


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _demo_mail():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "data", "e2e_emails.json")
    with open(path, encoding="utf-8") as f:
        emails = json.load(f)
    return next((e for e in emails if e.get("expected", {}).get("customer")), emails[0])


def main():
    ap = argparse.ArgumentParser(description="工具注册器：Agent 的能力清单")
    ap.add_argument("--list", action="store_true", help="打印工具清单（人读）")
    ap.add_argument("--demo", action="store_true", help="对 1 封真实邮件手动按序调用全部工具")
    args = ap.parse_args()

    if args.list or not (args.demo):
        print(f"共 {len(TOOLS)} 个工具：\n")
        for i, t in enumerate(TOOLS, 1):
            f = t["function"]
            req = f["parameters"].get("required", [])
            params = ", ".join(
                f"{k}{'*' if k in req else ''}" for k in f["parameters"].get("properties", {})
            ) or "无参数"
            print(f"{i}. {f['name']}({params})")
            print(f"   {f['description'][:110]}...")
            print()
        return

    if args.demo:
        mail = _demo_mail()
        print(f"演示邮件：{mail.get('id')} | {mail.get('subject')}\n")
        session = ToolSession(mail)
        _, impl = make_tools(session)

        steps = [
            ("classify_email", {"subject": mail.get("subject", ""), "body": mail.get("body", "")}),
            ("extract_fields", {}),
            ("retrieve_history", {"query": "Global Import Ltd. LED Panel Light", "top_k": 3}),
            ("rerank_history", {"query": "Global Import Ltd. LED Panel Light 历史报价", "top_k": 3}),
            ("build_context", {"query": "该客户的历史成交价是多少", "max_chars": 2000}),
            ("generate_answer", {"query": "该客户的历史成交价是多少"}),
            ("finish", {"answer": "（演示用占位答案）", "cited_ids": []}),
        ]
        for name, arg in steps:
            out = execute_tool(name, arg, impl, session)
            data = json.loads(out)
            print(f"▶ {name}({json.dumps(arg, ensure_ascii=False)[:70]})")
            brief = json.dumps(data, ensure_ascii=False)
            print(f"  ← {brief[:300]}{'...' if len(brief) > 300 else ''}\n")

        print("—" * 60)
        print(f"工具调用 {len(session.trace)} 次 | 累计耗时 "
              f"{sum(t['elapsed_ms'] for t in session.trace)}ms | LLM 成本 ¥{session.llm_cost:.6f}")


if __name__ == "__main__":
    main()
