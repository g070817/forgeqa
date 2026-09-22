"""forgeqa.report — 自包含 HTML 报告 + JUnit XML。

报告不只是「绿了还是红了」。它必须回答三个问题：
1. 挂在哪个步骤、期望什么、实际什么；
2. 这是缺陷、环境、脚本还是数据问题（根因四分类，先给出初判再人工确认）；
3. 用例和数据是否可复现（env / seed / 每次请求的请求响应全留档）。

HTML 单文件、无外部依赖、截图 base64 内嵌，可直接归档给不装环境的同事。
"""
from __future__ import annotations

import base64
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree as ET

from .runner import CaseResult, SuiteResult

# --------------------------------------------------------------------------- #
# 根因分类
# --------------------------------------------------------------------------- #
CATEGORY_RULES: list[tuple[str, str, str]] = [
    # (分类, 匹配模式, 判定说明)
    ("环境", r"timeout|timed out|connection|refused|无法连接|超时|DNS|SSL|certificate|未安装|未启动|browser",
     "网络/服务/依赖环境问题，非产品缺陷"),
    ("数据", r"no such table|no such column|唯一约束|外键|not null|SQL 查询失败|SQL 执行失败|造数|去重|unique|foreign key",
     "测试数据或库结构问题，检查造数与建表"),
    ("脚本", r"用例 YAML|断言格式|无法识别|语法无法解析|缺少|格式错误|未知 UI 动作|找不到元素|JSONPath|变量.*未定义|引擎层异常",
     "用例/脚本自身问题，修正用例后即可复跑"),
    ("缺陷", r"状态码|期望|实际|数据快照|业务不变量|与预期不符|返回与预期",
     "被测系统行为与预期不符，需人工确认后提缺陷"),
]


def classify(result: CaseResult) -> dict[str, str]:
    """根因初判。这是 AI/规则辅助，不是终审结论。"""
    if result.status in ("PASSED", "SKIPPED"):
        return {"category": "-", "reason": ""}
    text = " ".join(filter(None, [result.error or "", result.hint or ""] + [
        c.get("message", "") for c in result.failed_checks[:5]]))
    for cat, pattern, reason in CATEGORY_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return {"category": cat, "reason": reason}
    return {"category": "缺陷" if result.status == "FAILED" else "环境",
            "reason": "未命中规则，按状态默认归类，需人工复核"}


# --------------------------------------------------------------------------- #
# JUnit XML（CI 门禁标准格式）
# --------------------------------------------------------------------------- #
def write_junit(suite: SuiteResult, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    root = ET.Element("testsuites", {
        "name": "forgeqa", "tests": str(suite.total), "failures": str(suite.failed),
        "errors": str(suite.errors), "skipped": str(suite.skipped),
        "time": f"{suite.duration_ms / 1000:.3f}",
    })
    ts = ET.SubElement(root, "testsuite", {
        "name": f"forgeqa[{suite.env}]", "tests": str(suite.total),
        "failures": str(suite.failed), "errors": str(suite.errors),
        "skipped": str(suite.skipped), "time": f"{suite.duration_ms / 1000:.3f}",
        "timestamp": suite.started_at,
    })
    ET.SubElement(ts, "properties")
    props = ts.find("properties")
    assert props is not None
    for k, v in (("env", suite.env), ("base_url", suite.base_url), ("seed", str(suite.seed))):
        ET.SubElement(props, "property", {"name": k, "value": v})

    for c in suite.cases:
        tc = ET.SubElement(ts, "testcase", {
            "classname": f"{c.layer}.{c.priority}", "name": f"{c.id} {c.title}",
            "time": f"{c.ms / 1000:.3f}",
        })
        ET.SubElement(tc, "system-out").text = _case_text(c)
        if c.status == "SKIPPED":
            ET.SubElement(tc, "skipped", {"message": c.error or "skipped"})
        elif c.status in ("FAILED", "ERROR"):
            tag = "failure" if c.status == "FAILED" else "error"
            node = ET.SubElement(tc, tag, {
                "message": (c.error or "")[:500],
                "type": classify(c)["category"],
            })
            node.text = _case_text(c)
        if c.flaky:
            ET.SubElement(tc, "system-err").text = f"FLAKY: 第 {c.attempts} 次重试后才通过"
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(p, encoding="utf-8", xml_declaration=True)
    return p


def _case_text(c: CaseResult) -> str:
    lines = [f"# {c.id} {c.title} [{c.status}] {c.priority} tags={c.tags}"]
    if c.error:
        lines.append(f"ERROR: {c.error}")
    if c.hint:
        lines.append(f"HINT: {c.hint}")
    for s in c.steps:
        lines.append(f"  - [{s.status}] {s.name} ({s.kind}, {s.ms:.0f}ms)")
        for ck in s.checks:
            mark = "✓" if ck.get("passed") else "✗"
            lines.append(f"      {mark} {ck.get('target')} {ck.get('op')} "
                         f"expected={ck.get('expected')!r} actual={ck.get('actual')!r}")
        if s.detail.get("sql"):
            lines.append(f"      SQL: {s.detail['sql'].get('text')} params={s.detail['sql'].get('params')}")
        if s.detail.get("response"):
            lines.append(f"      HTTP {s.detail['response'].get('status')} {s.detail['response'].get('url')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTML 报告
# --------------------------------------------------------------------------- #
CSS = """
:root{
  --bg:#f5f6f8; --panel:#ffffff; --line:#e3e6ea; --text:#1f2328; --muted:#6b7280;
  --pass:#15803d; --fail:#dc2626; --error:#b45309; --skip:#6b7280; --flaky:#7c3aed;
  --accent:#1d4ed8; --chip:#eef2f7;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 64px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:15px;margin:28px 0 12px;padding-left:9px;border-left:3px solid var(--accent)}
.sub{color:var(--muted);font-size:13px;margin-bottom:18px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(124px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card .k{font-size:12px;color:var(--muted)}
.card .v{font-size:24px;font-weight:650;letter-spacing:-.5px}
.v.pass{color:var(--pass)}.v.fail{color:var(--fail)}.v.error{color:var(--error)}
.v.skip{color:var(--skip)}.v.flaky{color:var(--flaky)}
.bar{height:8px;border-radius:99px;background:#e5e7eb;overflow:hidden;display:flex;margin-top:14px}
.bar i{display:block;height:100%}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:20px 0 10px}
button{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 11px;
  font-size:13px;cursor:pointer;color:var(--text)}
button.on{background:#e8efff;border-color:#b9cdfb;color:var(--accent);font-weight:600}
input[type=search]{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:6px 10px;font-size:13px;min-width:210px;color:var(--text)}
.case{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-bottom:9px;overflow:hidden}
.case.fail{border-left:4px solid var(--fail)}
.case.err{border-left:4px solid var(--error)}
.case.pass{border-left:4px solid var(--pass)}
.case.skip{border-left:4px solid var(--skip)}
.head{display:flex;gap:10px;align-items:center;padding:11px 14px;cursor:pointer}
.head:hover{background:#fafbfc}
.badge{font-size:11px;font-weight:700;padding:2px 7px;border-radius:5px;letter-spacing:.3px}
.b-pass{background:#e7f6ec;color:var(--pass)}
.b-fail{background:#fdeaea;color:var(--fail)}
.b-error{background:#fdf3e3;color:var(--error)}
.b-skip{background:#f0f1f3;color:var(--skip)}
.b-flaky{background:#f2ecfe;color:var(--flaky)}
.id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted)}
.title{font-weight:600;flex:1;min-width:120px}
.meta{font-size:12px;color:var(--muted);white-space:nowrap}
.pri{font-size:11px;padding:1px 6px;border-radius:4px;background:var(--chip);color:#475569}
.body{padding:0 14px 14px;display:none;border-top:1px solid var(--line)}
.case.open .body{display:block}
.step{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:10px 0}
.step .st{display:flex;gap:8px;align-items:center;font-weight:600}
.step .st .ms{margin-left:auto;font-weight:400;font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted);font-weight:600}
td.mono,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;word-break:break-all}
.ok{color:var(--pass)}.no{color:var(--fail)}
pre{background:#f7f8fa;border:1px solid var(--line);border-radius:7px;padding:9px 11px;
  overflow:auto;max-height:320px;font-size:12px;margin:7px 0 0;white-space:pre-wrap;word-break:break-all}
.note{background:#fff8ec;border:1px solid #f2ddb5;border-radius:8px;padding:10px 12px;margin-top:10px;font-size:13px}
.note.err{background:#fdf1f1;border-color:#f3c8c8}
.tag{font-size:11px;background:var(--chip);border-radius:99px;padding:2px 8px;color:#475569;margin-left:5px}
img.shot{max-width:100%;border:1px solid var(--line);border-radius:8px;margin-top:8px}
.sec{font-size:12px;color:var(--muted);margin-top:10px;text-transform:uppercase;letter-spacing:.4px}
.empty{color:var(--muted);padding:18px;text-align:center}
dialog{border:none;border-radius:10px;padding:0;max-width:92vw}
dialog img{display:block;max-width:90vw;max-height:85vh}
"""

JS = r"""
const DATA = __DATA__;
const F = {status:'all', pri:'all', q:''};
function el(t,c,h){const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e;}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));}
function val(v){return typeof v==='object'&&v!==null?JSON.stringify(v):v;}
function badge(st){
  const map={PASSED:['b-pass','通过'],FAILED:['b-fail','失败'],ERROR:['b-error','异常'],SKIPPED:['b-skip','跳过']};
  const [c,t]=map[st]||['b-skip',st];
  return '<span class="badge '+c+'">'+t+'</span>';
}
function catBadge(cat){
  if(!cat||cat==='-')return '';
  const color={'缺陷':'b-fail','环境':'b-error','脚本':'b-flaky','数据':'b-error'}[cat]||'b-skip';
  return '<span class="badge '+color+'">'+cat+'</span>';
}
function renderSummary(){
  const s=DATA.summary, rate=(s.pass_rate*100).toFixed(1);
  document.getElementById('cards').innerHTML=[
    ['用例总数',s.total,''],['通过',s.passed,'pass'],['失败',s.failed,'fail'],
    ['异常',s.errors,'error'],['跳过',s.skipped,'skip'],['flaky',s.flaky,'flaky'],
    ['通过率',rate+'%','pass']
  ].map(([k,v,cls])=>'<div class="card"><div class="k">'+k+'</div><div class="v '+cls+'">'+v+'</div></div>').join('');
  const t=s.total||1;
  document.getElementById('bar').innerHTML=
    '<i style="background:var(--pass);width:'+(s.passed/t*100)+'%"></i>'+
    '<i style="background:var(--fail);width:'+(s.failed/t*100)+'%"></i>'+
    '<i style="background:var(--error);width:'+(s.errors/t*100)+'%"></i>'+
    '<i style="background:var(--skip);width:'+(s.skipped/t*100)+'%"></i>';
}
function checksTable(checks){
  if(!checks||!checks.length)return '';
  let h='<table><tr><th>断言目标</th><th>算子</th><th>期望</th><th>实际</th><th>结果</th></tr>';
  for(const c of checks){
    h+='<tr><td class="mono">'+esc(c.target)+'</td><td>'+esc(c.op)+'</td>'+
       '<td class="mono">'+esc(val(c.expected))+'</td><td class="mono">'+esc(val(c.actual))+'</td>'+
       '<td class="'+(c.passed?'ok':'no')+'">'+(c.passed?'通过':'失败')+'</td></tr>';
    if(!c.passed&&c.message)h+='<tr><td colspan="5" class="mono no">'+esc(c.message)+'</td></tr>';
  }
  return h+'</table>';
}
function imgs(artifacts){
  if(!artifacts||!artifacts.length)return '';
  return artifacts.filter(a=>/\.png$/i.test(a)).map(a=>
    DATA.images[a]?('<div class="sec">失败截图</div><img class="shot" loading="lazy" src="data:image/png;base64,'+DATA.images[a]+'">'):''
  ).join('');
}
function stepHtml(s){
  let h='<div class="step"><div class="st">'+badge(s.status)+'<span>'+esc(s.name)+'</span>'+
    '<span style="font-weight:400;color:var(--muted);font-size:12px">'+esc(s.kind)+'</span>'+
    '<span class="ms">'+s.ms.toFixed(0)+' ms</span></div>';
  if(s.detail&&s.detail.request){
    const r=s.detail.request,resp=s.detail.response||{};
    h+='<div class="sec">请求</div><pre>'+esc(r.method+' '+r.url)+'\n'+
       (r.json?esc('body: '+JSON.stringify(r.json)):'')+'</pre>';
    h+='<div class="sec">响应 '+esc(resp.status||'')+' ('+(resp.elapsed_ms||0)+' ms)</div><pre>'+esc(val(resp.body))+'</pre>';
  }
  if(s.detail&&s.detail.sql){
    const q=s.detail.sql;
    h+='<div class="sec">SQL</div><pre>'+esc(q.text)+'\nparams: '+esc(val(q.params))+'</pre>';
  }
  if(s.detail&&s.detail.rows){
    h+='<div class="sec">结果集（'+s.detail.row_count+' 行）</div><pre>'+esc(val(s.detail.rows))+'</pre>';
  }
  if(s.detail&&s.detail.ui_actions){
    h+='<div class="sec">UI 动作</div><pre>'+esc(s.detail.ui_actions.map(a=>a.action+' → '+a.detail+(a.target?' ('+a.target+')':'')).join('\n'))+'</pre>';
  }
  if(s.detail&&s.detail.dom_digest){
    h+='<div class="sec">失败现场 DOM 摘要</div><pre>'+esc(s.detail.dom_digest)+'</pre>';
  }
  if(s.detail&&s.detail.baseline){
    const b=s.detail.baseline;
    h+='<div class="sec">快照回归</div><pre>'+esc(JSON.stringify(b,null,1))+'</pre>';
  }
  h+=checksTable(s.checks);
  if(s.error)h+='<div class="note err">'+esc(s.error)+'</div>';
  if(s.hint)h+='<div class="note">修复建议：'+esc(s.hint)+'</div>';
  h+=imgs(s.artifacts);
  return h+'</div>';
}
function caseHtml(c){
  const cls=c.status.toLowerCase(), cat=c.run_category||'-';
  let h='<div class="case '+cls+'" data-status="'+c.status+'" data-pri="'+c.priority+'" '+
        'data-q="'+esc((c.id+' '+c.title+' '+c.tags.join(' ')).toLowerCase())+'">'+
    '<div class="head">'+badge(c.status)+catBadge(cat)+
    (c.flaky?'<span class="badge b-flaky">flaky</span>':'')+
    '<span class="id">'+esc(c.id)+'</span><span class="title">'+esc(c.title)+'</span>'+
    '<span class="pri">'+esc(c.priority)+'</span>'+
    (c.tags||[]).map(t=>'<span class="tag">'+esc(t)+'</span>').join('')+
    '<span class="meta">'+c.ms.toFixed(0)+' ms</span></div><div class="body">';
  if(c.run_reason)h+='<div class="note">根因初判：<b>'+esc(cat)+'</b> — '+esc(c.run_reason)+'</div>';
  if(c.data_snapshot&&Object.keys(c.data_snapshot).length)
    h+='<div class="sec">本次造数（seed='+(DATA.seed==null?'随机':DATA.seed)+'）</div><pre>'+
       esc(JSON.stringify(c.data_snapshot,null,1))+'</pre>';
  for(const s of c.steps)h+=stepHtml(s);
  if(c.error&&!c.steps.some(s=>s.error))h+='<div class="note err">'+esc(c.error)+'</div>';
  return h+'</div></div>';
}
function render(){
  const list=DATA.cases.filter(c=>
    (F.status==='all'||c.status===F.status)&&
    (F.pri==='all'||c.priority===F.pri)&&
    (!F.q||(c.id+' '+c.title+' '+c.tags.join(' ')).toLowerCase().includes(F.q)));
  const box=document.getElementById('list');
  box.innerHTML=list.length?list.map(caseHtml).join(''):'<div class="empty">没有匹配的用例</div>';
  box.querySelectorAll('.head').forEach(h=>h.onclick=()=>h.parentElement.classList.toggle('open'));
  document.getElementById('shown').textContent=list.length;
}
function bind(){
  document.querySelectorAll('[data-f]').forEach(b=>b.onclick=()=>{
    const [k,v]=b.dataset.f.split(':');
    F[k]=(F[k]===v&&k==='status')?'all':v;
    document.querySelectorAll('[data-f^="'+k+':"]').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');render();
  });
  document.getElementById('q').oninput=e=>{F.q=e.target.value.trim().toLowerCase();render();};
  document.getElementById('expand').onclick=()=>{
    const all=document.querySelectorAll('.case');
    const open=document.querySelectorAll('.case.open').length>all.length/2;
    all.forEach(c=>c.classList.toggle('open',!open));};
}
renderSummary();bind();render();
"""


def _inline_images(suite: SuiteResult, *, max_bytes: int = 400_000) -> dict[str, str]:
    """把失败截图 base64 内嵌，报告单文件即可归档。"""
    out: dict[str, str] = {}
    seen: set[str] = set()
    for c in suite.cases:
        for s in c.steps:
            for a in s.artifacts:
                if a in seen or not a.lower().endswith(".png"):
                    continue
                seen.add(a)
                p = Path(a)
                try:
                    if p.exists() and p.stat().st_size <= max_bytes:
                        out[a] = base64.b64encode(p.read_bytes()).decode()
                except OSError:
                    continue
    return out


def render_html(suite: SuiteResult, *, title: str = "ForgeQA 回归报告") -> str:
    payload = suite.to_dict()
    for c, src in zip(payload["cases"], suite.cases):
        c["run_category"] = classify(src)["category"]
        c["run_reason"] = classify(src)["reason"]
    payload["images"] = _inline_images(suite)
    payload["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    data_json = json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")
    summary = payload["summary"]
    head = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>{html.escape(title)}</h1>
<div class="sub">环境 <b>{html.escape(suite.env)}</b> ｜ 站点 {html.escape(suite.base_url or '-')}
 ｜ 开始 {html.escape(suite.started_at)} ｜ 耗时 {suite.duration_ms / 1000:.1f}s
 ｜ 数据 seed <b>{suite.seed if suite.seed is not None else '随机'}</b>
 ｜ 生成于 {payload['generated_at']}</div>
<div class="cards" id="cards"></div><div class="bar" id="bar"></div>
<div class="toolbar">
  <button class="on" data-f="status:all">全部</button>
  <button data-f="status:FAILED">仅失败</button>
  <button data-f="status:ERROR">仅异常</button>
  <button data-f="status:PASSED">仅通过</button>
  <button data-f="status:SKIPPED">仅跳过</button>
  <span style="width:10px"></span>
  <button class="on" data-f="pri:all">全部优先级</button>
  <button data-f="pri:P0">P0</button><button data-f="pri:P1">P1</button><button data-f="pri:P2">P2</button>
  <input type="search" id="q" placeholder="搜索用例 ID / 标题 / 标签">
  <button id="expand">展开/收起全部</button>
  <span class="meta">显示 <b id="shown">0</b> / {summary['total']} 条</span>
</div>
<div id="list"></div>
</div><script>{JS.replace('__DATA__', data_json)}</script></body></html>"""
    return head


def write_html(suite: SuiteResult, path: str | Path, *, title: str = "ForgeQA 回归报告") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_html(suite, title=title), encoding="utf-8")
    return p


def write_json(suite: SuiteResult, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = suite.to_dict()
    for c, src in zip(payload["cases"], suite.cases):
        c["run_category"] = classify(src)["category"]
        c["run_reason"] = classify(src)["reason"]
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def console_summary(suite: SuiteResult) -> str:
    """终端摘要：一眼看清结果 + 该优先看哪几条。"""
    s = suite.to_dict()["summary"]
    lines = [
        "",
        "═" * 64,
        f"  ForgeQA 回归结果   env={suite.env}   seed={suite.seed if suite.seed is not None else '随机'}",
        "═" * 64,
        f"  总计 {s['total']}   通过 {s['passed']}   失败 {s['failed']}   "
        f"异常 {s['errors']}   跳过 {s['skipped']}   flaky {s['flaky']}   "
        f"通过率 {s['pass_rate'] * 100:.1f}%",
        f"  耗时 {suite.duration_ms / 1000:.1f}s",
        "─" * 64,
    ]
    bad = [c for c in suite.cases if c.status in ("FAILED", "ERROR")]
    if bad:
        lines.append("  需要关注：")
        for c in bad[:20]:
            cls = classify(c)
            lines.append(f"   ✗ [{c.priority}] {c.id} {c.title}")
            lines.append(f"       [{cls['category']}] {(c.error or '').splitlines()[0][:100]}")
            failed = c.failed_checks[:1]
            if failed:
                f = failed[0]
                lines.append(f"       {f.get('target')} {f.get('op')} 期望={f.get('expected')!r} "
                             f"实际={f.get('actual')!r}")
        if len(bad) > 20:
            lines.append(f"   … 另有 {len(bad) - 20} 条，详见报告")
    flaky = [c for c in suite.cases if c.flaky]
    if flaky:
        lines.append(f"  ⚠ flaky 用例 {len(flaky)} 条（重试后才通过，需要修稳定性）：")
        for c in flaky[:5]:
            lines.append(f"     {c.id} {c.title}（第 {c.attempts} 次才通过）")
    lines.append("═" * 64)
    return "\n".join(lines)
