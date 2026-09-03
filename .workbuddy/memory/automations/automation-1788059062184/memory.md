# 自动化任务记忆 · 每日学习计划推送

> automation-1788059062184
> 任务：读取 `WorkBuddy岗位实战学习路线.md` + 实际进度，每天推送当天学习计划

---

## 执行方法（已验证，后续沿用）

1. 读 `WorkBuddy岗位实战学习路线.md`（总纲）+ 最近一份 `DayXX-学习计划-*.md`（格式模板）
2. 读 `.workbuddy/memory/YYYY-MM-DD.md`（**前一天**的工作日志）——**这里才是真实进度**，
   路线总纲的「10 周规划」已严重落后于实际进度，不能照着排
3. 扫 `projects/外贸邮件Agent/` 的 src/ data/ output/，确认代码与数字的真实状态
4. 生成 `DayNN-学习计划-YYYYMMDD.md`，放到学习空间根目录
5. present_files 推送
6. 本文件只写「做了什么 + 下一次从哪接」，不写计划全文

## 进度判断口径

- 目标岗位已从「WorkBuddy 智能工作流工程师(8-9K)」升级为 **AI 应用工程师(15-25K)**，
  排计划以 `AI应用工程师转型路线.md` 的 JD 十项技能为准
- 麦当执行速度是计划的 3-4 倍，**每天都要重新判断，不能沿用昨天的「明日预告」当今天的计划**

---

## 执行记录

### 2026-09-02（Day 4）· 首次执行

- 产出：`Day04-学习计划-20260902.md`
- 对账结论：Day3 计划 5 项全部完成，且超额完成 Day4 混合检索、工作流、业务效果实测、
  飞书第二案例、公网 Web Demo
- 判断：RAG 的「检索 R」已打穿（recall@3=100%，recall@1=75%），
  JD 第 1 条「RAG 全链路」仅剩 **重排序 Rerank** 空白
- 今日主轴定为：**Rerank 精排**（两阶段检索：召回保 recall，精排提 P@1）
- 关键约束：不装 torch/BGE-Reranker（本机 venv 创建失败，重依赖必翻车），
  用 DeepSeek 做交叉编码；候选池取 top-10 保证 recall@3 不退化；评测集 20 条不许改

**下一次接续**：Day5（9/3）= 上下文组装 + 生成 + 忠实度/幻觉评测，补 RAG 的「G」。
排之前先读 `.workbuddy/memory/2026-09-02.md` 拿到 Rerank 实测数字。

---

### 2026-09-03（Day 5）· 第二次执行

- 产出：`Day05-学习计划-20260903.md`
- **重大发现：Day4 计划 5 项全部未执行（0/5）**
  - 硬证据：全空间 `find . -newermt "2026-09-02 10:00"` 返回空；`*rerank*` 搜索为空；
    `eval_retrieval.py` 无 P@1/MRR
  - 归因判断：Day4 是从零开新模块（两个 Reranker + 评测），成本结构与前 4 天「顺着已有代码往下写」
    完全不同，1.5h 装不下 5 项任务
- **改道决定（不按 Day4 原计划顺延）**：扫代码发现更致命问题——
  `workflow.py::_render_draft()` 是**纯 f-string 模板**，全项目 grep 不到任何 LLM 起草路径，
  即 README 宣称的「起草回复调 DeepSeek」实际不存在。**RAG 只有 R，G 是假的**
- 今日主轴改为：**生成闭环**（① 上下文组装带引用编号 → ② 接真生成 → ③ 忠实度/幻觉评测）
  - 排序逻辑：Rerank 是给闭环加分，生成端缺失是闭环本身不成立 → **先补完整性再做优化**
  - Rerank + P@1/MRR 顺延周六，列为欠账第一项
- 计划本身做了校准：**每天最多 4 项、必做 ≤2、其余标注可砍**（吸取 Day4 教训）

**下一次接续**：9/4（Day 6）**不要默认做 Rerank**——
先读 `output/eval_generation.json` 的幻觉归因（检索错 vs 生成编），二选一：
检索错 → Rerank 精排；生成编 → 生成端约束（low temperature / 结构化输出 / 拒答阈值）。
另需先读 `.workbuddy/memory/2026-09-03.md`（若麦当今晚写了）拿实测数字。

---

### 2026-09-03（Day 5）· 执行完成

- 产出：`src/context_builder.py` ✅ + `src/generator.py` ✅ + `src/eval_generation.py` ✅
- RAG 首次真正闭合：检索 → 组装 → 生成 → 评测
- **20条评测结果**：
  | 指标 | 数值 |
  |---|---|
  | 引用准确率 | 100% |
  | 数字可溯源率 | 100% |
  | 实体可溯源率 | 59.8% |
  | 忠实度（LLM judge）| 85.7% |
  | 幻觉率 | 40.1% |
  | 单次成本 | ¥0.0008 |
  | 总成本（20条）| ¥0.015 |
- 幻觉主因是元叙述（"依据现有记录无法确认"），非真正编造
- 实体可溯源率 59.8% 是短板 → 今天做 Rerank 精排
- 修复了 6 个真实 bug（extract_numbers 字段错、_CHUNK_CACHE 缺失、workflow transit 重复等）
- Git: `aa016ab Day5: 生成端闭环完成`

**今日完成欠账**（10:10）：
- ✅ Rerank 精排：`src/reranker.py`（LLMReranker + DeepSeek pairwise scoring）
- ✅ P@1/NDCG 评测：`src/eval_rerank.py` → `output/eval_rerank.json`（20条）
- 精排结果：P@1 75%→95%、NDCG@3 0.824→0.908、R@3 保持 91%
- ✅ README 更新：新增「交叉编码精排」「RAG 生成端闭环」两节
- Git: `a5613d8 Day5 补做：Rerank 精排 + P@1/NDCG + README 更新`

**RAG 完整链路状态**：
- 检索：BM25 baseline → 混合检索（RRF）→ 交叉编码精排（三轮递进）
- 生成：context_builder → generator(LLM+引用标注) → eval_generation(忠实度85.7%)
- 评测指标全集：recall@K / P@1 / NDCG@K / 引用准确率 / 数字可溯源率 / 实体可溯源率 / 忠实度 / 幻觉率
- 全部数字可复现，成本可控（¥0.015/20条生成 + ¥0.01/20条精排）
