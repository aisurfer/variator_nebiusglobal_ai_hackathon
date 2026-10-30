"""One-file HTML report: weight sliders, ranking, RAG bands, tunnel maps, trade-off axes.

The page gets the decision matrix and a sample of scenarios as JSON. The browser
recalculates the weighted ranking when the user moves a slider, so a weight change
needs no server call and no LLM call. Plotly comes from cdnjs (TODO-1: final UI).
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from variator.mcda import tensor_stats
from variator.rag import RagReport, TunnelMap
from variator.simulation import TensorRun

# 2.35.1 and 2.35.2 have no files on cdnjs (404); 2.35.0 is the newest 2.35 build there.
PLOTLY_URL = "https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.35.0/plotly.min.js"
MAX_SCENARIOS = 400


def report_payload(
    run: TensorRun,
    *,
    title: str,
    rag: RagReport | None = None,
    tunnels: Sequence[TunnelMap] = (),
    notes: Sequence[str] = (),
) -> dict[str, Any]:
    stats = tensor_stats(run)
    step = max(1, run.n_scenarios // MAX_SCENARIOS)
    sample = run.values[::step][:MAX_SCENARIOS]
    return {
        "title": title,
        "options": run.option_names,
        "criteria": [
            {
                "name": c.name,
                "unit": c.unit,
                "higher": c.higher_is_better,
                "weight": c.weight,
                "source": c.source,
            }
            for c in run.criteria
        ],
        "mean": np.round(stats["mean"], 6).tolist(),
        "cvar": np.round(stats["cvar"], 6).tolist(),
        "p5": np.round(stats["p5"], 6).tolist(),
        "p95": np.round(stats["p95"], 6).tolist(),
        "sample": np.round(sample, 6).tolist(),
        "sample_bad": run.infeasible_mask()[::step][:MAX_SCENARIOS].astype(int).tolist(),
        "feasible": np.round(run.feasible_share(), 4).tolist(),
        "n_scenarios": run.n_scenarios,
        "rag": rag.model_dump(mode="json") if rag else None,
        "tunnels": [t.model_dump(mode="json") for t in tunnels],
        "notes": list(notes) + list(run.notes),
    }


def write_report(path: str | Path, payload: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    page = _PAGE.replace("__TITLE__", html.escape(payload["title"])).replace("__DATA__", data)
    target.write_text(page.replace("__PLOTLY__", PLOTLY_URL), encoding="utf-8")
    return target


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<script src="__PLOTLY__"></script>
<style>
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e3e2dc;--g:#2e8b57;--a:#e0a100;--r:#c8372d;--accent:#3b5bdb}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;--card:#20201e;--line:#34332f;--accent:#7c95ff}}
:root[data-theme=dark]{--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;--card:#20201e;--line:#34332f;--accent:#7c95ff}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:8px 0 4px}h2{font-size:17px;margin:0 0 10px}
.muted{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0;overflow-x:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px}
label{display:block;font-size:13px}input[type=range]{width:100%}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}.bar{display:flex;height:12px;border-radius:3px;overflow:hidden;min-width:120px}
.bar span{display:block}.top{font-weight:600}
</style></head><body><main>
<h1>__TITLE__</h1><div class="muted" id="meta"></div>
<div class="card"><h2>Weights (your preference)</h2><div class="grid" id="sliders"></div>
<label>Risk aversion: <b id="rav">0.25</b><input type="range" id="ra" min="0" max="1" step="0.05" value="0.25"></label>
<div class="muted">The ranking changes in your browser. No new simulation is necessary.</div></div>
<div class="card"><h2>Ranking</h2><table id="rank"></table></div>
<div class="card"><h2>RAG bands (share of scenarios)</h2><div id="rag"></div></div>
<div class="card"><h2>Trade-off axes</h2><div id="par" style="height:360px"></div></div>
<div id="tunnels"></div>
<div class="card"><h2>Notes</h2><ul id="notes" class="muted"></ul></div>
</main><script>
const D=__DATA__;const K=D.criteria.length,O=D.options.length;
const fmt=x=>Math.abs(x)>=1e4?x.toExponential(2):(+x.toPrecision(4)).toString();
document.getElementById('meta').textContent=`${D.n_scenarios} scenarios, ${O} options, ${K} criteria`;
const w=D.criteria.map(c=>c.weight);
const sl=document.getElementById('sliders');
D.criteria.forEach((c,k)=>{const l=document.createElement('label');
l.innerHTML=`${c.name} (${c.higher?'higher':'lower'} is better${c.unit?', '+c.unit:''}): <b id="wv${k}">${w[k]}</b>`;
const i=document.createElement('input');i.type='range';i.min=0;i.max=5;i.step=0.1;i.value=w[k];
i.oninput=()=>{w[k]=+i.value;document.getElementById('wv'+k).textContent=i.value;draw()};l.appendChild(i);sl.appendChild(l)});
const ra=document.getElementById('ra');ra.oninput=()=>{document.getElementById('rav').textContent=ra.value;draw()};
function norm(M){const out=M.map(r=>r.slice());for(let k=0;k<K;k++){const col=M.map(r=>r[k]);const lo=Math.min(...col),hi=Math.max(...col);
for(let j=0;j<M.length;j++){let v=hi>lo?(M[j][k]-lo)/(hi-lo):1;if(!D.criteria[k].higher&&hi>lo)v=1-v;out[j][k]=v}}return out}
function draw(){const a=+ra.value,tw=w.reduce((s,x)=>s+x,0)||1,wn=w.map(x=>x/tw);
const M=D.mean.map((r,j)=>r.map((m,k)=>(1-a)*m+a*D.cvar[j][k]));const N=norm(M);
const score=N.map(r=>r.reduce((s,v,k)=>s+v*wn[k],0));const best=new Array(O).fill(0);
let counted=0;D.sample.forEach((S,s)=>{const bad=D.sample_bad[s]||[];const n=norm(S);
const sc=n.map((r,j)=>bad[j]?-Infinity:r.reduce((t,v,k)=>t+v*wn[k],0));const m=Math.max(...sc);if(m===-Infinity)return;counted++;
const t=sc.filter(x=>x===m).length;sc.forEach((x,j)=>{if(x===m)best[j]+=1/t})});
const F=D.feasible||D.options.map(()=>1);
const order=[...Array(O).keys()].sort((x,y)=>score[y]*F[y]-score[x]*F[x]);  // rank rule: score x feasible share
let h='<tr><th>Option</th><th>Score</th><th>Feasible</th><th>P(best)</th>'+D.criteria.map(c=>`<th>${c.name} mean [p5, p95]</th>`).join('')+'</tr>';
order.forEach((j,i)=>{h+=`<tr class="${i?'':'top'}"><td>${i+1}. ${D.options[j]}</td><td>${score[j].toFixed(3)}</td><td>${(F[j]*100).toFixed(0)}%</td><td>${(best[j]/Math.max(counted,1)*100).toFixed(1)}%</td>`+
D.criteria.map((c,k)=>`<td>${fmt(D.mean[j][k])} [${fmt(D.p5[j][k])}, ${fmt(D.p95[j][k])}]</td>`).join('')+'</tr>'});
document.getElementById('rank').innerHTML=h;
Plotly.react('par',[{type:'parcoords',line:{color:score,colorscale:'Viridis',showscale:true},
dimensions:D.criteria.map((c,k)=>({label:c.name+(c.higher?' (max)':' (min)'),values:M.map(r=>r[k])}))}],
{margin:{l:60,r:40,t:30,b:20},paper_bgcolor:'rgba(0,0,0,0)',font:{color:getComputedStyle(document.body).color}},{responsive:true})}
function bar(s){return `<div class="bar"><span style="width:${s.green*100}%;background:var(--g)"></span><span style="width:${s.amber*100}%;background:var(--a)"></span><span style="width:${s.red*100}%;background:var(--r)"></span></div>`}
if(D.rag){let h='<table><tr><th>Option</th><th>Overall</th>'+D.criteria.map(c=>`<th>${c.name}</th>`).join('')+'</tr>';
D.rag.options.forEach(o=>{h+=`<tr><td>${o.option}</td><td>${bar(o.overall)}<span class="muted">${(o.overall.red*100).toFixed(0)}% red</span></td>`+D.criteria.map(c=>`<td>${bar(o.criteria[c.name])}</td>`).join('')+'</tr>'});
document.getElementById('rag').innerHTML=h+'</table><div class="muted">Green, amber, red. Limits: '+Object.entries(D.rag.limits).map(([k,v])=>`${k}: green ${fmt(v.green)}, red ${fmt(v.red)}`).join('; ')+'</div>'}
else document.getElementById('rag').textContent='No RAG data. Call the rag tool first.';
const tw=document.getElementById('tunnels');
D.tunnels.forEach((t,i)=>{const c=document.createElement('div');c.className='card';
c.innerHTML=`<h2>Tunnel: ${t.x} x ${t.y}${t.criterion?' ('+t.criterion+')':''}</h2><div class="muted">Colour: share of green scenarios. Text: best option per cell.</div><div class="grid" id="tg${i}"></div>`;tw.appendChild(c);
t.options.forEach((o,j)=>{const d=document.createElement('div');d.style.height='300px';document.getElementById('tg'+i).appendChild(d);
Plotly.newPlot(d,[{type:'heatmap',x:t.x_values,y:t.y_values,z:t.green[o],zmin:0,zmax:1,colorscale:[[0,'#c8372d'],[0.5,'#e0a100'],[1,'#2e8b57']],
text:t.best,hovertemplate:t.x+'=%{x}<br>'+t.y+'=%{y}<br>green=%{z:.0%}<br>best=%{text}<extra></extra>'}],
{title:{text:o,font:{size:13}},margin:{l:50,r:10,t:30,b:40},xaxis:{title:t.x},yaxis:{title:t.y},paper_bgcolor:'rgba(0,0,0,0)',font:{color:getComputedStyle(document.body).color}},{responsive:true})})});
document.getElementById('notes').innerHTML=(D.notes.length?D.notes:['No notes.']).map(n=>`<li>${n.replace(/</g,'&lt;')}</li>`).join('');
draw();
</script></body></html>
"""
