# -*- coding: utf-8 -*-
"""
外贸邮件 Agent · 公网静态交互 Demo 构建器
========================================
把 20 封真实 e2e 邮件的全链路结果（分类→提取→决策→RAG→起草）预跑出来，
内嵌进一个纯静态 HTML，部署到任意静态托管（CloudStudio）即可公网访问。
零后端、零 API key 暴露 —— HR/面试官点开即能交互浏览。

用法：  python src/build_static_demo.py
产出：  dist_demo/index.html

作者：麦当 · 2026-09-01
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo_server as D  # 触发一次 Agent + 混合检索器加载

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E2E = os.path.join(BASE, "data", "e2e_emails.json")
OUT_DIR = os.path.join(BASE, "dist_demo")
OUT = os.path.join(OUT_DIR, "index.html")

STYLE = """
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --border:#30363d;
    --txt:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --accent2:#3fb950;
    --warn:#d29922; --danger:#f85149; --pill:#21262d;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
       background:var(--bg);color:var(--txt);line-height:1.6}
  header{padding:22px 28px;border-bottom:1px solid var(--border);background:linear-gradient(180deg,#11161f,#0d1117)}
  header h1{margin:0;font-size:20px;letter-spacing:.3px}
  header p{margin:6px 0 0;color:var(--muted);font-size:13px}
  .wrap{display:grid;grid-template-columns:minmax(340px,38%) 1fr;gap:20px;padding:20px 28px;align-items:start}
  @media(max-width:900px){.wrap{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px}
  .card h2{font-size:14px;margin:0 0 12px;color:var(--accent);display:flex;align-items:center;gap:8px}
  .maillist{max-height:72vh;overflow:auto;border:1px solid var(--border);border-radius:8px}
  .mailitem{padding:10px 12px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .15s}
  .mailitem:last-child{border-bottom:none}
  .mailitem:hover{background:var(--panel2)}
  .mailitem.active{background:var(--panel2);border-left:3px solid var(--accent);padding-left:9px}
  .mi-id{font-size:11px;color:var(--accent);font-weight:700}
  .mi-sub{font-size:13px;margin:2px 0;font-weight:500;line-height:1.4}
  .mi-meta{font-size:11px;color:var(--muted);margin-top:3px}
  .meta{margin-top:10px;font-size:12px;color:var(--muted)}
  .steps{display:flex;flex-direction:column;gap:14px}
  .step{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 16px;
        opacity:0;transform:translateY(8px);animation:fade .35s forwards}
  @keyframes fade{to{opacity:1;transform:none}}
  .step .h{display:flex;align-items:center;gap:10px;font-weight:600;font-size:14px;margin-bottom:8px}
  .step .num{width:24px;height:24px;border-radius:50%;background:var(--accent);color:#06122a;
             display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex:none}
  .badge{font-size:11px;padding:2px 9px;border-radius:20px;background:var(--pill);border:1px solid var(--border);color:var(--muted)}
  .badge.rule{color:var(--accent2)}
  .badge.llm{color:var(--warn)}
  .kv{display:grid;grid-template-columns:90px 1fr;gap:6px 12px;font-size:13px}
  .kv .k{color:var(--muted)}
  .kv .v{font-weight:500}
  .v.empty{color:var(--danger);font-weight:400}
  .reason{font-size:12.5px;color:var(--muted);margin:3px 0 0 0;padding-left:14px;position:relative}
  .reason:before{content:"›";position:absolute;left:2px;color:var(--accent)}
  .rag-hit{border-left:3px solid var(--accent);background:var(--panel2);padding:8px 10px;border-radius:6px;margin-top:8px;font-size:12.5px}
  .rag-hit .id{color:var(--accent);font-weight:600;margin-right:6px}
  .rag-hit .sc{float:right;color:var(--muted);font-size:11px}
  .draft{white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;
         background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:12px;color:#c9d1d9}
  .empty-state{color:var(--muted);text-align:center;padding:60px 20px;font-size:14px}
  .pri-高{color:var(--danger);font-weight:700}
  .pri-中{color:var(--warn);font-weight:700}
  .pri-低{color:var(--accent2);font-weight:700}
  .pill-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
  .pill{background:var(--pill);border:1px solid var(--border);border-radius:20px;padding:3px 11px;font-size:12px}
  .pill.risk{border-color:var(--danger);color:#ffb4ae}
  .pill.act{border-color:var(--accent);color:#aacbff}
"""

# ---- HTML 模板（__DATA__ 由构建时注入）----
TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>外贸邮件智能处理 Agent · 全链路交互演示（公网版）</title>
<style>{style}</style>
</head>
<body>
<header>
  <h1>📧 外贸邮件智能处理 Agent · 全链路交互演示（公网版）</h1>
  <p>20 封真实测试邮件的完整全链路结果（分类 → 提取 → 决策 → RAG 检索 → 起草回复），点击左侧任意邮件查看。本地「粘贴实时跑」版见 README 启动命令。</p>
</header>

<div class="wrap">
  <div class="card">
    <h2>📨 测试邮件（20 封真实样本）</h2>
    <div class="maillist" id="maillist"></div>
    <div class="meta" id="meta"></div>
  </div>
  <div>
    <div class="steps" id="steps">
      <div class="empty-state" id="empty">← 点击左侧任意一封邮件，查看它的完整全链路处理结果</div>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const FIELD_CN = {{customer:"客户", product:"产品", quantity:"数量", price:"价格", deadline:"截止日"}};
const DATA = {data};

function esc(s){{ return String(s==null?'':s).replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c])); }}
function step(n,title,badge,html){{
  return `<div class="step"><div class="h"><span class="num">${{n}}</span>${{title}} ${{badge||''}}</div>${{html}}</div>`;
}}
function valCell(v){{ return v? `<span class="v">${{esc(v)}}</span>` : `<span class="v empty">未提供 / 未识别</span>`; }}

function render(d){{
  const j=d.result, raw=d.raw;
  const c=j.classification, ex=j.extraction, dd=j.decision, rag=j.rag;
  let h='';
  h+=step('原','原始邮件（输入）','',`<div class="draft">${{esc(raw)}}</div>`);
  const cbadge = c.need_llm ? '<span class="badge llm">需 LLM 兜底</span>'
                            : (c.source==='规则层'?'<span class="badge rule">规则层</span>':'');
  h+=step(1,'分类结果',cbadge,`
    <div class="kv">
      <div class="k">预测分类</div><div class="v"><b style="font-size:15px">${{c.category_cn}}</b></div>
      <div class="k">规则层预判</div><div class="v">${{c.rule_category_cn}}（置信度 ${{c.confidence}}）</div>
      <div class="k">最终来源</div><div class="v">${{c.source}}</div>
    </div>`);
  let fld='';
  for(const k of ['customer','product','quantity','price','deadline']){{
    const v=ex.fields[k]?.value||'';
    fld+=`<div class="k">${{FIELD_CN[k]}}</div>${{valCell(v)}}`;
  }}
  h+=step(2,'关键信息提取','<span class="badge rule">规则层+LLM</span>',`<div class="kv">${{fld}}</div>`);
  const prBadge=`<span class="badge" style="border-color:var(--border)">优先级 <span class="pri-${{dd.priority}}">${{dd.priority}}</span></span>`;
  let reasons=(dd.reasons||[]).map(r=>`<p class="reason">${{esc(r)}}</p>`).join('');
  let pills='';
  (dd.actions||[]).forEach(a=>pills+=`<span class="pill act">👉 ${{esc(a)}}</span>`);
  (dd.risks||[]).forEach(r=>pills+=`<span class="pill risk">⚠ ${{esc(r)}}</span>`);
  if(!dd.risks||!dd.risks.length) pills+=`<span class="pill">✓ 无显性风险</span>`;
  h+=step(3,'决策建议',prBadge,`<div>${{reasons}}</div><div class="pill-row">${{pills}}</div>`);
  let hits=(rag.hits||[]).map(hh=>`
    <div class="rag-hit"><span class="sc">融合分 ${{hh.score}}</span>
      <span class="id">${{esc(hh.id)}}</span>${{esc(hh.customer||'')}}
      <div style="color:var(--muted);margin-top:3px">${{esc(hh.text)}}</div>
    </div>`).join('');
  if(!hits) hits='<div class="rag-hit">无相关历史记录</div>';
  h+=step(4,'RAG 检索历史','<span class="badge rule">BM25+向量</span>',`
    <div class="kv"><div class="k">检索式</div><div class="v">${{esc(rag.query||'')}}</div></div>${{hits}}`);
  h+=step(5,'起草回复','',`<div class="draft">${{esc(j.draft||'')}}</div>`);
  $('#steps').innerHTML=h;
}}

// 左侧邮件列表
const maillist=$('#maillist');
DATA.forEach((d,i)=>{{
  const el=document.createElement('div');
  el.className='mailitem'+(i===0?' active':'');
  el.innerHTML=`<div class="mi-id">${{d.id}}</div>
    <div class="mi-sub">${{esc(d.subject)}}</div>
    <div class="mi-meta">[${{d.category}}] ${{d.note?('陷阱: '+esc(d.note)):''}}</div>`;
  el.onclick=()=>{{
    document.querySelectorAll('.mailitem').forEach(x=>x.classList.remove('active'));
    el.classList.add('active');
    select(d);
  }};
  maillist.appendChild(el);
}});

function select(d){{
  $('#empty')?.remove();
  render(d);
  $('#meta').textContent=`${{d.id}} · 耗时 ${{d.result.elapsed}}s · 真实 LLM 调用 ${{d.result.llm_calls}} 次 · 检索器 混合(BM25+向量)`;
}}

// 默认选中第一封
if(DATA.length) select(DATA[0]);
</script>
</body>
</html>
"""


def main():
    emails = json.load(open(E2E, encoding="utf-8"))
    agent = D._AGENT
    results = []
    t_total = time.time()
    for i, e in enumerate(emails, 1):
        text = f"From: {e.get('from','')}\nSubject: {e.get('subject','')}\n\n{e.get('body','')}"
        before = agent.fm.call_count + agent.em.call_count
        r = D.run_pipeline(text)
        after = agent.fm.call_count + agent.em.call_count
        r["llm_calls"] = after - before
        results.append({
            "id": e["id"],
            "subject": e.get("subject", ""),
            "from": e.get("from", ""),
            "category": e.get("category", ""),
            "note": e.get("note", ""),
            "raw": text,
            "result": r,
        })
        print(f"  [{i}/{len(emails)}] {e['id']} -> {r['classification']['category_cn']} | "
              f"抽取 {sum(1 for k,v in r['extraction']['fields'].items() if v.get('value'))}/5 | "
              f"RAG {len(r['rag']['hits'])}条 | {r['elapsed']}s | LLM {r['llm_calls']}次")

    os.makedirs(OUT_DIR, exist_ok=True)
    data_json = json.dumps(results, ensure_ascii=False)
    data_json = data_json.replace("</", "<\\/")  # 防 </script> 截断
    html = TEMPLATE.format(style=STYLE, data=data_json)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(html)

    print(f"\n[OK] 静态 Demo 已生成：{OUT}")
    print(f"     总耗时 {round(time.time()-t_total,1)}s · {len(results)} 封真实全链路结果内嵌完成")


if __name__ == "__main__":
    main()
