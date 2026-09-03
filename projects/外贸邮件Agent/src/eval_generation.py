#!/usr/bin/env python3
"""
RAG 生成端评测脚本（任务③ Day5）

指标:
  - 数字可溯源率 (num_traceable_rate)   = 答案里所有数字能在引用的 chunks 里找到  / 答案数字总数
  - 实体可溯源率 (entity_traceable_rate) = 客户名/产品名这些实体可在 chunks 里找到  / 总实体数
  - 引用准确率 (citation_precision)     = 有效引用 / 总引用编号
  - 幻觉率 (hallucination_rate)         = 含未引用断言的条目 / 总条目
  - 忠实度 (faithfulness)               = 每条断言是否能在 chunks 里验证

用法:
  # 强制模板路径（¥0）
  python src/eval_generation.py --bm25 --no-llm

  # 真实 LLM 路径
  python src/eval_generation.py --max-rows 20 --llm

  # 只看单条详细输出
  python src/eval_generation.py --max-rows 1 --llm --verbose
"""

import argparse
import sys
import io
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm_fallback import FallbackManager
from workflow import WorkflowCase
from vector_retriever import build_hybrid
from context_builder import extract_citations, map_citations

# 共享 retriever：build_hybrid 返回 (HybridRetriever, all_chunks)，评测全程复用同一实例
_RETRIEVER, _CHUNKS_REF = build_hybrid()


# ---------------------------------------------------------------------------
# 测试数据
# ---------------------------------------------------------------------------
TEST_QUERIES = [
    {
        "id": "TEST-Q01",
        "type": "product_spec",
        "query": "哪些客户问过 LED Downlight？",
        "expects": ["C04", "C06"],
        "answer_should_have_numbers": True,
        "expected_entities": ["Global Import Ltd.", "Nordic Trading", "LED Downlight"],
    },
    {
        "id": "TEST-Q02",
        "type": "number_amount",
        "query": "报过 USD 12.80 的是哪家？",
        "expects": ["C01"],
        "answer_should_have_numbers": True,
        "expected_entities": ["Lucky Star Ltd."],
    },
    {
        "id": "TEST-Q03",
        "type": "semantic",
        "query": "有没有客户投诉过质量问题？",
        "expects": ["C04", "C08"],
        "answer_should_have_numbers": False,
        "expected_entities": [],
    },
    {
        "id": "TEST-Q04",
        "type": "product_spec",
        "query": "谁采购过 Solar Street Light？",
        "expects": ["C07", "C09"],
        "answer_should_have_numbers": False,
        "expected_entities": ["Nordic Trading", "Solar Street Light"],
    },
]

# 每个 query 期望 answer 里出现的具体数字（用于验证数字可溯源）
EXPECTED_NUMBERS = {
    "TEST-Q01": {"USD 12.80", "3000", "USD 12.00"},
    "TEST-Q02": {"USD 12.80", "1500"},
    "TEST-Q03": set(),  # 语义检索，不强制数字
    "TEST-Q04": set(),
}


# ---------------------------------------------------------------------------
# 评测函数
# ---------------------------------------------------------------------------

def _tokenize_entities(text: str) -> list[str]:
    """提取中英文实体（客户名/产品名/公司名）。"""
    # 英文大写开头 + 后续词
    en = re.findall(r'[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+', text)
    # 中文实体（连续中文字符，长度 ≥ 3）
    zh = re.findall(r'[\u4e00-\u9fff]{3,}', text)
    return list(dict.fromkeys(en + zh))


def _extract_numbers(text: str) -> set[str]:
    """提取金额和数量类数字。"""
    nums = re.findall(r'(?:USD\s*)?\$?\d+(?:,\d{3})*(?:\.\d+)?(?:\s*/\s*pc|pcs|件|台|个)?', text, re.IGNORECASE)
    return set(n.strip() for n in nums if len(n) > 2)


def evaluate_citation(answer: str, id_map: dict) -> dict:
    """引用准确率 + 幻觉检测。"""
    cited_nums = extract_citations(answer)
    valid_ids, invalid_nums = map_citations(cited_nums, id_map)

    citation_precision = len(valid_ids) / len(cited_nums) if cited_nums else 1.0

    # 幻觉：找出所有断言性句子，检查是否都有引用
    sentences = re.split(r'[。.!！\n]', answer)
    claims = [s.strip() for s in sentences if len(s.strip()) > 10]
    total_claims = len(claims)
    if total_claims == 0:
        return {
            "citation_precision": 1.0,
            "hallucination_rate": 0.0,
            "invalid_citations": invalid_nums,
            "valid_citations": valid_ids,
        }

    # 简单启发：有引用编号的句子算"有依据"，否则算"可能幻觉"
    claimed_with_ref = 0
    for claim in claims:
        if re.search(r'\[\d+\]', claim):
            claimed_with_ref += 1
        elif not re.search(r'(可以确认|不确定|无法确认|需要)', claim, re.IGNORECASE):
            pass  # 中性句不算

    ungrounded = total_claims - claimed_with_ref
    hallucination_rate = ungrounded / total_claims if total_claims > 0 else 0.0

    return {
        "citation_precision": round(citation_precision, 3),
        "hallucination_rate": round(hallucination_rate, 3),
        "invalid_citations": invalid_nums,
        "valid_citations": valid_ids,
    }


def evaluate_factfulness(answer: str, chunks: list[dict], id_map: dict) -> dict:
    """忠实度：每条陈述能否在 chunks 里找到依据。"""
    if not chunks:
        return {"faithfulness": 0.0, "claims_verified": 0, "claims_total": 0}

    # 收集所有 chunk 文本用于匹配
    all_text = " ".join(c["text"] for c in chunks)

    sentences = re.split(r'[。.!！\n]', answer)
    claims = [s.strip() for s in sentences if len(s.strip()) > 8]
    total_claims = len(claims)
    if total_claims == 0:
        return {"faithfulness": 1.0, "claims_verified": 0, "claims_total": 0}

    verified = 0
    for claim in claims:
        # 跳过模糊表述
        if re.search(r'(可以确认|不确定|无法确认|建议|可能需要)', claim, re.IGNORECASE):
            continue
        # 简单匹配：claim 的关键字在 chunks 中出现
        keywords = re.findall(r'[\w\u4e00-\u9fff]{3,}', claim)
        if any(kw in all_text for kw in keywords):
            verified += 1

    faithfulness = verified / total_claims if total_claims > 0 else 1.0
    return {
        "faithfulness": round(faithfulness, 3),
        "claims_verified": verified,
        "claims_total": total_claims,
    }


def evaluate_traceability(answer: str, chunks: list[dict], id_map: dict) -> dict:
    """数字可溯源率 + 实体可溯源率。"""
    all_text = " ".join(c["text"] for c in chunks)

    # 数字
    answer_nums = _extract_numbers(answer)
    traceable_nums = sum(1 for n in answer_nums if n in all_text)
    num_traceable_rate = traceable_nums / len(answer_nums) if answer_nums else 1.0

    # 实体
    answer_entities = _tokenize_entities(answer)
    traceable_entities = sum(1 for e in answer_entities if e in all_text)
    entity_traceable_rate = traceable_entities / len(answer_entities) if answer_entities else 1.0

    return {
        "num_traceable_rate": round(num_traceable_rate, 3),
        "entity_traceable_rate": round(entity_traceable_rate, 3),
        "answer_numbers": sorted(answer_nums),
        "answer_entities": answer_entities[:10],  # 限制输出
    }


# ---------------------------------------------------------------------------
# 主评测流程
# ---------------------------------------------------------------------------

def run_eval(query_id: str, query: str, use_llm: bool, verbose: bool = False) -> dict:
    """运行单条评测，返回指标 dict。"""
    # 构建 workflow（复用共享 BM25+向量 retriever）
    mail_result = {
        "id": query_id,
        "category": "inquiry",
        "priority": "普通",
        "subject": query,
        "fields": {
            "customer": "",
            "product": "",
            "description": query,
        }
    }

    case = WorkflowCase(mail_result, retriever=_RETRIEVER, use_llm_draft=use_llm)
    case.run_pipeline()

    summary = case.summary()
    bundle = case.context_bundle or {}
    chunks = case.retrieved or []
    answer = case.draft or ""

    # 评测指标
    citation = evaluate_citation(answer, bundle.get("id_map", {}))
    traceability = evaluate_traceability(answer, chunks, bundle.get("id_map", {}))
    faithfulness = evaluate_factfulness(answer, chunks, bundle.get("id_map", {}))

    result = {
        "query_id": query_id,
        "query": query,
        "gen_mode": summary.get("gen_mode", "unknown"),
        "answer": answer[:500],  # 截断防溢出
        "cited_ids": summary.get("cited_ids", []),
        "chunks_count": len(chunks),
        "citation_precision": citation["citation_precision"],
        "hallucination_rate": citation["hallucination_rate"],
        "num_traceable_rate": traceability["num_traceable_rate"],
        "entity_traceable_rate": traceability["entity_traceable_rate"],
        "faithfulness": faithfulness["faithfulness"],
        "invalid_citations": citation["invalid_citations"],
        "valid_citations": citation["valid_citations"],
        "answer_numbers": traceability["answer_numbers"],
        "gen_cost_yuan": summary.get("gen_cost_yuan") or 0,
    }

    if verbose:
        print(f"\n{'='*60}")
        print(f"【{query_id}】{query}")
        print(f"生成模式: {result['gen_mode']}")
        print(f"引用数: {len(result['cited_ids'])} | 答案字符: {len(answer)}")
        print(f"引用准确率: {result['citation_precision']} | 幻觉率: {result['hallucination_rate']}")
        print(f"数字可溯源: {result['num_traceable_rate']} | 实体可溯源: {result['entity_traceable_rate']}")
        print(f"忠实度: {result['faithfulness']}")
        if result['invalid_citations']:
            print(f"⚠️ 无效引用: {result['invalid_citations']}")
        print(f"答案: {answer[:200]}...")

    return result


def aggregate_results(results: list[dict]) -> dict:
    """汇总所有指标的均值。"""
    n = len(results)
    if n == 0:
        return {}

    return {
        "总条目": n,
        "平均引用准确率": round(sum(r["citation_precision"] for r in results) / n, 3),
        "平均幻觉率": round(sum(r["hallucination_rate"] for r in results) / n, 3),
        "平均数字可溯源率": round(sum(r["num_traceable_rate"] for r in results) / n, 3),
        "平均实体可溯源率": round(sum(r["entity_traceable_rate"] for r in results) / n, 3),
        "平均忠实度": round(sum(r["faithfulness"] for r in results) / n, 3),
        "总生成成本(元)": round(sum(r["gen_cost_yuan"] for r in results), 4),
        "LLM 生成条目": sum(1 for r in results if r["gen_mode"] == "llm"),
        "模板降级条目": sum(1 for r in results if r["gen_mode"] == "template_fallback"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="RAG 生成端评测")
    parser.add_argument("--max-rows", type=int, default=20, help="最多评测多少条（默认 20）")
    parser.add_argument("--llm", action="store_true", help="使用 LLM 生成（非 ¥0）")
    parser.add_argument("--no-llm", dest="no_llm", action="store_true", help="强制模板路径（¥0）")
    parser.add_argument("--verbose", action="store_true", help="打印每条详情")
    parser.add_argument("--output", type=str, default=None, help="结果保存路径")
    args = parser.parse_args()

    use_llm = args.llm and not args.no_llm
    print(f"\n📊 RAG 生成端评测 | LLM={'开' if use_llm else '关（模板路径）'} | 最多 {args.max_rows} 条\n")

    results = []
    queries = TEST_QUERIES[:args.max_rows]

    for q in queries:
        r = run_eval(q["id"], q["query"], use_llm, verbose=args.verbose)
        results.append(r)

    # 汇总
    agg = aggregate_results(results)
    print(f"\n{'='*60}")
    print("📈 汇总指标")
    print(f"{'='*60}")
    for k, v in agg.items():
        print(f"  {k}: {v}")

    # 保存结果
    output_path = args.output or ROOT / "output" / "eval_generation.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"meta": {"llm": use_llm, "total": len(results), "timestamp": "auto"},
                   "results": results, "aggregate": agg},
                  f, ensure_ascii=False, indent=2)
    print(f"\n💾 结果已保存: {output_path}")

    # 关键判断
    if agg.get("平均幻觉率", 0) > 0.3:
        print("\n⚠️  幻觉率偏高（>30%），需优化生成端约束（降 temperature / 加拒答阈值）")
    if agg.get("平均数字可溯源率", 1) < 0.8:
        print("\n⚠️  数字可溯源率偏低（<80%），检查上下文组装或引用编号注入")
    print()


if __name__ == "__main__":
    main()
