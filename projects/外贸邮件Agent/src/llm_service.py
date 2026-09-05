# -*- coding: utf-8 -*-
"""
LLM 服务层 v2.0（JD #3 补齐：流式 / 多模型统一 / Prompt 版本管理）
==============================================================

三层能力：
1. **流式输出**：SSE 协议原生流式（DeepSeek/OpenAI）+ 兼容层兜底
2. **多模型统一**：统一 `call()` 入口自动选 provider / model / fallback 策略
3. **Prompt 版本管理**：每个 prompt 可注册多个版本（v1/v2），支持灰度回滚与审计

作者：麦当 · 2026-09-04（Day06 续 · JD #3）
"""

import json
import os
import sys
import time
from abc import ABC, abstractmethod
from typing import Callable, Dict, Iterator, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import requests
except Exception:
    requests = None

from llm_fallback import (
    FallbackManager, DeepSeekProvider, OpenAICompatProvider, MockProvider,
    TOKEN_TOTAL, reset_token_total, calc_cost as _calc_cost_v1,
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_DIR = os.path.join(BASE, "prompts")

# ---------------------------------------------------------------------------
# 全局计数器（v2 单独维护，避免污染 v1 的 A/B 口径）
# ---------------------------------------------------------------------------
SERVICE_TOTAL = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}


def reset_service_total():
    SERVICE_TOTAL["prompt_tokens"] = 0
    SERVICE_TOTAL["completion_tokens"] = 0
    SERVICE_TOTAL["calls"] = 0


def _calc_cost_v2(usage):
    """用 DeepSeek 官方价估算（其它 provider 按 token 数乘比例系数，v2 不做精确计费）。"""
    pin = usage.get("prompt_tokens", 0) or 0
    pout = usage.get("completion_tokens", 0) or 0
    return pin * (1.0 / 1_000_000) + pout * (8.0 / 1_000_000)


# ---------------------------------------------------------------------------
# 1) Prompt 版本管理（versioned prompt registry）
# ---------------------------------------------------------------------------
class PromptVersion:
    """一条 prompt 的一个版本。支持字段化占位（{key}）。"""

    def __init__(self, version: str, content: str, author: str = "", changed_at: str = "",
                 note: str = ""):
        self.version = version
        self.content = content
        self.author = author
        self.changed_at = changed_at or time.strftime("%Y-%m-%d %H:%M:%S")
        self.note = note

    def render(self, **kwargs) -> str:
        text = self.content
        for k, v in kwargs.items():
            text = text.replace("{" + k + "}", str(v) if v is not None else "")
        return text


class PromptRegistry:
    """在盘 prompt 的版本管理。支持创建 / 灰度 / 回滚 / 审计。

    用法：
        reg = PromptRegistry(base_dir=prompt_dir)
        reg.register("classify", "v1", "你是分类器...", author="麦当")
        reg.register("classify", "v2", "你是资深业务员，请先共情再判断...", author="麦当")
        prompt = reg.get("classify", version="v2")  # 指定版本
        prompt = reg.get("classify", version="latest")  # 用最新
        reg.set_active("classify", "v2")  # 灰度切换
        history = reg.history("classify")  # 审计日志
    """

    def __init__(self, base_dir: str = None):
        self.base_dir = base_dir
        self._store: Dict[str, Dict[str, PromptVersion]] = {}  # {name: {ver: ver}}
        self._active: Dict[str, str] = {}  # {name: active_ver}
        if base_dir and os.path.isdir(base_dir):
            self._load_from_disk(base_dir)

    # -- 写入 --
    def register(self, name: str, version: str, content: str,
                 author: str = "", note: str = ""):
        if name not in self._store:
            self._store[name] = {}
        self._store[name][version] = PromptVersion(version, content, author, note=note)
        if name not in self._active:
            self._active[name] = version
        self._save_to_disk(name, version, self._store[name][version])
        return self._store[name][version]

    def set_active(self, name: str, version: str):
        if name not in self._store or version not in self._store[name]:
            raise ValueError(f"prompt {name!r} 不存在版本 {version!r}")
        self._active[name] = version

    # -- 读取 --
    def get(self, name: str, version: str = "latest") -> PromptVersion:
        if name not in self._store:
            raise KeyError(f"未注册 prompt: {name}")
        if version == "latest":
            version = self._active.get(name, list(self._store[name])[0])
        return self._store[name][version]

    def history(self, name: str) -> List[PromptVersion]:
        return list((self._store.get(name) or {}).values())

    def active(self, name: str) -> str:
        return self._active.get(name, "?")

    # -- 磁盘持久化（每 prompt 一个 JSON，字段化） --
    def _save_to_disk(self, name: str, version: str, ver: PromptVersion):
        if not self.base_dir:
            return
        try:
            os.makedirs(os.path.join(self.base_dir, name), exist_ok=True)
            path = os.path.join(self.base_dir, name, f"{version}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": ver.version, "content": ver.content,
                           "author": ver.author, "changed_at": ver.changed_at,
                           "note": ver.note, "active": ver.version == self._active.get(name)},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_from_disk(self, base_dir: str):
        try:
            for name in os.listdir(base_dir):
                ndir = os.path.join(base_dir, name)
                if not os.path.isdir(ndir):
                    continue
                self._store[name] = {}
                for fname in os.listdir(ndir):
                    if not fname.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(ndir, fname), encoding="utf-8") as f:
                            d = json.load(f)
                        ver = PromptVersion(d["version"], d["content"], d.get("author", ""),
                                            d.get("changed_at", ""), d.get("note", ""))
                        self._store[name][d["version"]] = ver
                    except Exception:
                        pass
                # active = 标记为 active 的那个；否则取最新写入的
                active = next((v for v in self._store[name].values()
                               if v.version == self._active.get(name)), None)
                if not active:
                    versions = list(self._store[name].values())
                    active = versions[-1] if versions else None
                if active:
                    self._active[name] = active.version
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 2) 多模型统一接口（provider registry + router）
# ---------------------------------------------------------------------------
class ModelSpec:
    """一条模型规格：{provider, model, endpoint?, priority?, cost_per_1m_in?, ...}"""

    def __init__(self, provider: str, model: str, endpoint: str = "",
                 priority: int = 100, cost_in: float = 1.0, cost_out: float = 8.0):
        self.provider = provider
        self.model = model
        self.endpoint = endpoint
        self.priority = priority
        self.cost_in = cost_in
        self.cost_out = cost_out

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


class ProviderRegistry:
    """多 provider / 多模型注册表。支持按优先级 + 健康度选择。"""

    def __init__(self):
        self._specs: Dict[str, ModelSpec] = {}   # key = provider/model 去重
        self._providers: Dict[str, object] = {}  # key = provider name → instance
        self._failures: Dict[str, int] = {}       # 每 provider 连续失败计数
        self._max_consecutive_fail = 3

    def register(self, provider_name: str, provider_obj, specs: List[ModelSpec]):
        self._providers[provider_name] = provider_obj
        for s in specs:
            s.provider = provider_name
            key = f"{provider_name}/{s.model}"
            self._specs[key] = s

    def list_models(self, provider: str = None) -> List[ModelSpec]:
        out = [s for s in self._specs.values()]
        if provider:
            out = [s for s in out if s.provider == provider]
        return sorted(out, key=lambda s: s.priority)

    def resolve(self, prefer: str = None, prefer_provider: str = None) -> Optional[ModelSpec]:
        """按优先级选择可用模型：跳过连续失败 ≥ N 的 provider。"""
        candidates = self.list_models(provider=prefer_provider)
        if prefer:
            candidates = [s for s in candidates if prefer in s.model]
        for s in candidates:
            key = s.provider
            if (self._failures.get(key, 0) or 0) < self._max_consecutive_fail:
                return s
        # 全部熔断 → 退回到最高优先级（不计健康度）
        return candidates[0] if candidates else None

    def mark_fail(self, provider: str):
        self._failures[provider] = (self._failures.get(provider, 0) or 0) + 1

    def mark_ok(self, provider: str):
        self._failures[provider] = 0


# ---------------------------------------------------------------------------
# 3) 流式与非流式调用（统一外层）
# ---------------------------------------------------------------------------
class LLMService:
    """把「多模型 + 流式 + Prompt 版本 + 降级 + 成本统计」整合成一套调用入口。

    典型用法：
        svc = LLMService(prompt_dir="prompts/", fallback_cfg=None)
        svc.provider_registry.register(...)
        # 非流式
        msg, usage = svc.call(prompt_name="classify", prompt_version="v2", kwargs={},
                              prefer="deepseek", temperature=0)
        # 流式（原生 SSE）
        for chunk in svc.stream(prompt_name="classify", kwargs={}):
            print(chunk["delta"], end="", flush=True)
        # 成本
        print(svc.total_cost_yuan)
    """

    def __init__(self, prompt_dir: str = None, fallback_cfg=None):
        self.registry = ProviderRegistry()
        self.prompt_reg = PromptRegistry(base_dir=prompt_dir)
        self.fallback_cfg = fallback_cfg
        self._summary_log: List[dict] = []  # 审计轨迹
        self._total_cost_yuan = 0.0
        self._setup_builtin_providers()

    def _setup_builtin_providers(self):
        """自动注册 DeepSeek（config 里取 key）与 Mock（无 key 兜底）。"""
        cfg = {}
        if self.fallback_cfg:
            cfg = self.fallback_cfg
        elif os.path.isfile(os.path.join(BASE, "config", "llm_config.json")):
            with open(os.path.join(BASE, "config", "llm_config.json"),
                      encoding="utf-8") as f:
                cfg = json.load(f)
        ds = cfg.get("deepseek", {}) or {}
        key = ds.get("api_key") or os.environ.get("DEEPSEEK_API_KEY")
        if key and DeepSeekProvider is not None:
            p = DeepSeekProvider(key)
            self.registry.register("deepseek", p, [
                ModelSpec("deepseek", "deepseek-chat", priority=10),
                ModelSpec("deepseek", "deepseek-coder", priority=5),
            ])
        if MockProvider is not None:
            self.registry.register("mock", MockProvider(), [
                ModelSpec("mock", "mock-classifier", priority=0),
            ])

    # ---- 调用层 ----
    def call(self, prompt_name: str, prompt_version: str = "latest",
             kwargs: dict = None, prefer: str = None,
             prefer_provider: str = None, temperature: float = 0,
             max_retries: int = 3) -> tuple:
        """非流式调用：返回 (message_dict, usage_dict, cost_yuan)。"""
        system = self.prompt_reg.get(prompt_name, version=prompt_version).render(
            **(kwargs or {}))
        last_err = None
        for attempt in range(max_retries):
            model = self.registry.resolve(prefer=prefer, prefer_provider=prefer_provider)
            if model is None:
                raise RuntimeError("没有可用模型：registry 为空")
            provider = self.registry._providers[model.provider]
            user_msg = kwargs.get("user", "") if kwargs else ""
            try:
                if hasattr(provider, "chat"):
                    msg, usage = provider.chat(messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_msg},
                    ], tools=None, temperature=temperature)
                elif hasattr(provider, "raw_call"):
                    raw = provider.raw_call(system, user_msg)
                    msg, usage = {"content": raw, "role": "assistant"}, {"prompt_tokens": 0, "completion_tokens": 0}
                elif hasattr(provider, "call"):
                    cat, reason, conf = provider.call(system, user_msg)
                    msg = {"content": json.dumps({"category": cat, "reason": reason,
                                                  "confidence": conf}, ensure_ascii=False),
                           "role": "assistant"}
                    usage = {"prompt_tokens": 0, "completion_tokens": 0}
                else:
                    raise RuntimeError(f"provider {model.provider!r} 无 chat / raw_call / call")
                self.registry.mark_ok(model.provider)
                cost = _calc_cost_v2(usage)
                self._total_cost_yuan += cost
                self._audit("call", prompt_name, model.label, usage, cost)
                return msg, usage, cost
            except Exception as e:
                last_err = e
                self.registry.mark_fail(model.provider)
                time.sleep(1 * (2 ** attempt))
        raise last_err

    def stream(self, prompt_name: str, prompt_version: str = "latest",
               kwargs: dict = None, prefer: str = None,
               prefer_provider: str = None, temperature: float = 0,
               max_retries: int = 3) -> Iterator[dict]:
        """SSE 原生流式调用：yield 每个 chunk，格式与 OpenAI/DeepSeek SSE 一致。

        chunk 格式：
            {"delta": "文本片段", "finish_reason": null}  # 每块
            {"delta": "", "finish_reason": "stop", "usage": {...}}  # 最后一块

        当前实现：直接调 DeepSeek/OpenAI 的 stream=True 接口，逐行解析 SSE。
        兼容层（fallback）：若 provider 不支持流式，退回到非流式后逐字符 yield。
        """
        system = self.prompt_reg.get(prompt_name, version=prompt_version).render(
            **(kwargs or {}))
        user_msg = kwargs.get("user", "") if kwargs else ""

        last_err = None
        for attempt in range(max_retries):
            model = self.registry.resolve(prefer=prefer, prefer_provider=prefer_provider)
            if model is None:
                raise RuntimeError("没有可用模型")
            provider = self.registry._providers[model.provider]

            # 尝试原生流式（provider 有 stream() 方法）
            if hasattr(provider, "stream"):
                try:
                    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
                    for chunk in provider.stream(system, user_msg, temperature):
                        yield chunk
                        # 汇总 usage（如果有）
                        if "usage" in chunk and chunk["usage"]:
                            u = chunk["usage"]
                            total_usage["prompt_tokens"] += u.get("prompt_tokens", 0) or 0
                            total_usage["completion_tokens"] += u.get("completion_tokens", 0) or 0
                    self.registry.mark_ok(model.provider)
                    cost = _calc_cost_v2(total_usage)
                    self._total_cost_yuan += cost
                    self._audit("stream", prompt_name, model.label, total_usage, cost)
                    return
                except Exception as e:
                    last_err = e
                    self.registry.mark_fail(model.provider)
                    time.sleep(1 * (2 ** attempt))
                    continue

            # 兼容层：非流式后逐字符 yield
            try:
                msg, usage, cost = self.call(prompt_name, prompt_version, kwargs,
                                             prefer=prefer, prefer_provider=prefer_provider,
                                             temperature=temperature, max_retries=1)
                content = msg.get("content") or ""
                for i in range(0, len(content), 1):
                    yield {"delta": content[i:i+1], "finish_reason": None,
                           "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
                yield {"delta": "", "finish_reason": "stop", "usage": usage}
                self._total_cost_yuan += cost
                self._audit("stream_fallback", prompt_name, model.label, usage, cost)
                return
            except Exception as e:
                last_err = e
                self.registry.mark_fail(model.provider)
                time.sleep(1 * (2 ** attempt))

        raise last_err or RuntimeError("流式调用失败")

    # ---- 审计 ----
    def _audit(self, op, prompt_name, model_label, usage, cost):
        entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "op": op, "prompt": prompt_name,
                 "model": model_label, "tokens": usage,
                 "cost_yuan": round(cost, 6)}
        self._summary_log.append(entry)

    def summary(self) -> dict:
        """本次 session 的服务层使用汇总（供审计）。"""
        return {
            "total_calls": len(self._summary_log),
            "total_cost_yuan": round(self._total_cost_yuan, 6),
            "models_used": list({e["model"] for e in self._summary_log}),
            "prompts_used": list({e["prompt"] for e in self._summary_log}),
            "log": self._summary_log[-20:],  # 最近 20 条
        }


# ---------------------------------------------------------------------------
# 演示
# ---------------------------------------------------------------------------
def main():
    """运行演示模式。"""
    import argparse
    ap = argparse.ArgumentParser(description="LLM 服务层 v2 demo")
    ap.add_argument("--demo", default="prompt", choices=["prompt", "multi", "stream", "all"])
    args = ap.parse_args()

    svc = LLMService(prompt_dir=PROMPT_DIR, fallback_cfg=None)

    if args.demo in ("prompt", "all"):
        print("\n===== Prompt 版本管理 =====")
        svc.prompt_reg.register("classify", "v1",
                                "你是分类器。给定邮件返回 JSON {\"category\":\"...\"}。",
                                author="麦当", note="初始版")
        svc.prompt_reg.register("classify", "v2",
                                ("你是资深业务员。请判断邮件意图并给出分类与置信度，"
                                 "JSON {\"category\":\"...\",\"confidence\":0.XX}。"),
                                author="麦当", note="加业务视角")
        svc.prompt_reg.set_active("classify", "v2")
        print("当前 active: v", svc.prompt_reg.active("classify"), sep="")
        print("v1 内容:", svc.prompt_reg.get("classify", "v1").content[:40])
        print("v2 内容:", svc.prompt_reg.get("classify", "v2").content[:40])

    if args.demo in ("multi", "all"):
        print("\n===== 多模型统一 =====")
        model = svc.registry.resolve()
        print("当前 resolve 到的模型:", model.label if model else "无")
        for m in svc.registry.list_models():
            print("  ", m.label, "priority=", m.priority)
        print("服务层总计 cost: ¥", round(svc.summary()["total_cost_yuan"], 6))

    if args.demo in ("stream", "all"):
        print("\n===== 流式调用（原生 SSE / 兼容层） =====")
        svc.prompt_reg.register("demo", "v1", "请用英文简短回复：{greeting}。",
                                author="麦当")
        chunks = []
        for c in svc.stream("demo", kwargs={"greeting": "Hello from the team"}, temperature=0):
            chunks.append(c)
            if c.get("delta"):
                print(c["delta"], end="", flush=True)
        print()
        last = chunks[-1]
        print("流式总 tokens:", last.get("usage", {}).get("completion_tokens", 0))
        print("结束原因:", last.get("finish_reason"))

    print("\n===== 审计汇总 =====")
    print(json.dumps(svc.summary(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
