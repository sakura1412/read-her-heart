/** Render the README workflow; verified with mermaid 11.17.2 and Playwright 1.62.1/Chromium.
 * Run: node scripts/render-workflow.mjs (NODE_PATH may point to temporary dependencies).
 * These are documentation build tools, not runtime dependencies of the Skill.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const mermaidDir = path.dirname(require.resolve('mermaid/package.json'));
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const readme = await fs.readFile(path.join(root, 'README.md'), 'utf8');
const source = readme.match(/```mermaid\s*\n([\s\S]*?)\n```/)?.[1];
if (!source) throw new Error('README workflow definition is missing');
const server = http.createServer(async (req, res) => {
  const relative = decodeURIComponent(new URL(req.url, 'http://localhost').pathname).slice(1);
  if (!relative) { res.setHeader('Content-Type','text/html; charset=utf-8'); res.end('<!doctype html><meta charset="utf-8"><style>body{margin:0}</style>'); return; }
  const target = path.resolve(mermaidDir, relative);
  if (!target.startsWith(mermaidDir + path.sep)) { res.writeHead(403).end(); return; }
  try { res.setHeader('Content-Type','text/javascript; charset=utf-8'); res.end(await fs.readFile(target)); }
  catch { res.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
let browser;
try {
  browser = await chromium.launch({headless:true});
  const page = await browser.newPage({deviceScaleFactor:1});
  const origin = `http://127.0.0.1:${server.address().port}`;
  await page.route('**/*', route => route.request().url().startsWith(origin+'/') ? route.continue() : route.abort());
  await page.goto(origin);
  const result = await page.evaluate(async source => {
    const {default:mermaid} = await import('/dist/mermaid.esm.min.mjs');
    mermaid.initialize({startOnLoad:false,securityLevel:'strict',theme:'base',htmlLabels:false,
      fontFamily:'Arial Unicode MS, PingFang SC, Noto Sans CJK SC, sans-serif',
      themeVariables:{primaryColor:'#ffffff',primaryTextColor:'#202c26',primaryBorderColor:'#c9d3cf',lineColor:'#404d47',edgeLabelBackground:'#eaf2f0',fontSize:'24px'},
      flowchart:{htmlLabels:false,useMaxWidth:false,curve:'linear',nodeSpacing:50,rankSpacing:72,padding:22,wrappingWidth:480},
    });
    const original = await mermaid.mermaidAPI.getDiagramFromText(source);
    const vertices = JSON.parse(JSON.stringify([...original.db.getVertices().values()]));
    const edges = JSON.parse(JSON.stringify(original.db.getEdges()));
    const groups = [
      {id:'intake',direction:'LR',ids:'BT BM BO BE S P W U M'},
      {id:'analysis',direction:'TD',ids:'A B C D E F G H RV PM OP RC I J V K R Z'},
      {id:'mandatory',direction:'TD',ids:'X Q Y RF RB'},
      {id:'delivery',direction:'TD',ids:'N PT PQ PW L'},
    ].map(g=>({...g,ids:g.ids.split(' ')}));
    if (new Set(groups.flatMap(g=>g.ids)).size !== vertices.length || groups.flatMap(g=>g.ids).length !== vertices.length) throw new Error('Layout groups must cover every node exactly once');
    const groupOf = Object.fromEntries(groups.flatMap(g=>g.ids.map(id=>[id,g.id])));
    if (vertices.some(v=>!groupOf[v.id])) throw new Error('Unassigned workflow node');
    const entry = new Set('BT BM BO BE S P W M X Q N PT PQ L'.split(' '));
    const branch = new Set('U RB PW PM OP RC V K R Z'.split(' '));
    const purple = new Set('A:H D:H E:H F:H RB:Y U:M PW:L R:A R:D R:E R:F R:G R:H R:Z Z:RV'.split(' '));
    const norm = value => value.replace(/<br\s*\/?\s*>/gi,'').replace(/\s/g,'');
    let checkedNodes=0, checkedEdges=0;
    for (const group of groups) {
      const localNodes=vertices.filter(v=>group.ids.includes(v.id));
      const localEdges=edges.filter(e=>group.ids.includes(e.start)&&group.ids.includes(e.end));
      const input=`flowchart ${group.direction}\n`+localNodes.map(v=>`${v.id}${v.type==='diamond'?'{':'['}${JSON.stringify(v.text)}${v.type==='diamond'?'}':']'}`).join('\n')+'\n'+localEdges.map(e=>`${e.start} ${e.stroke==='dotted'?'-.->':'-->'}${e.text?'|'+e.text+'|':''} ${e.end}`).join('\n');
      const {svg}=await mermaid.render('workflow-'+group.id,input);
      document.body.innerHTML=svg;
      const graphic=document.querySelector('svg');
      const [, ,w,h]=graphic.getAttribute('viewBox').split(/\s+/).map(Number);
      graphic.setAttribute('width',w); graphic.setAttribute('height',h); graphic.style.maxWidth='none';
      const origin=graphic.getBoundingClientRect();
      group.nodes={};
      for (const node of graphic.querySelectorAll('g.node')) {
        const id=node.id.match(/flowchart-(.+)-\d+$/)?.[1];
        const expected=localNodes.find(v=>v.id===id);
        if (!expected || norm(node.textContent)!==norm(expected.text)) throw new Error(`Node text changed: ${id}`);
        const b=node.getBoundingClientRect(); group.nodes[id]={x:b.x-origin.x,y:b.y-origin.y,w:b.width,h:b.height};
        const stroke=branch.has(id)?'#7b6bdd':entry.has(id)?'#92765a':'#c9d3cf';
        for (const shape of node.querySelectorAll(':scope > rect, :scope > polygon, :scope > path')) {
          shape.style.fill='#ffffff';shape.style.stroke=shape.tagName==='polygon'&&!branch.has(id)?'#404d47':stroke;shape.style.strokeWidth='2px';
          if(shape.tagName==='rect'){shape.setAttribute('rx','14');shape.setAttribute('ry','14');}
        }
        node.querySelectorAll('text').forEach(t=>t.style.fill=branch.has(id)?'#6359c9':'#202c26');
        if(expected.type!=='diamond')node.querySelectorAll('text > tspan:not(:first-child)').forEach(t=>{t.style.fontSize='21px';t.style.fill=branch.has(id)?'#6359c9':'#56645e';});
        checkedNodes++;
      }
      const marker=graphic.querySelector('marker[id$="-pointEnd"]');
      const pm=marker.cloneNode(true);pm.id=group.id+'-purple';pm.querySelectorAll('path').forEach(p=>{p.style.fill='#6359c9';p.style.stroke='#6359c9';});graphic.prepend(pm);
      for(const edge of graphic.querySelectorAll('path.flowchart-link')){
        const match=edge.id.match(/L_([^_]+)_([^_]+)_(\d+)/);if(!match)throw new Error('Unidentified edge');
        const candidates=localEdges.filter(e=>e.start===match[1]&&e.end===match[2]);
        const label=graphic.querySelector(`.edgeLabel [data-id="${match[0]}"]`);
        const expected=candidates.find(e=>norm(e.text||'')===norm(label?.textContent||''));if(!expected)throw new Error(`Unexpected edge: ${edge.id} ${label?.textContent}`);
        localEdges.splice(localEdges.indexOf(expected),1);
        if(norm(label?.textContent||'')!==norm(expected.text||''))throw new Error(`Edge label changed: ${edge.id}`);
        edge.style.strokeWidth='2.5px';
        if(purple.has(match[1]+':'+match[2])){edge.style.stroke='#6359c9';edge.setAttribute('marker-end',`url(#${pm.id})`);label?.querySelectorAll('text').forEach(t=>t.style.fill='#6359c9');}
        checkedEdges++;
      }
      graphic.querySelectorAll('.edgeLabel rect').forEach(r=>r.style.fill=group.id==='intake'?'#f8f9f3':'#eaf2f0');
      graphic.querySelectorAll('marker').forEach(m=>{m.setAttribute('markerWidth','16');m.setAttribute('markerHeight','16');});
      if(graphic.querySelector('foreignObject'))throw new Error('SVG must use portable text labels');
      group.graphic=graphic;group.w=w;group.h=h;
    }
    const [intake,analysis,mandatory,delivery]=groups;
    const rightWidth=Math.max(mandatory.w,delivery.w);
    const width=Math.ceil(Math.max(intake.w+160,analysis.w+rightWidth+400));
    intake.x=(width-intake.w)/2;intake.y=160;
    analysis.x=80;analysis.y=intake.y+intake.h+230;
    mandatory.x=width-rightWidth-80+(rightWidth-mandatory.w)/2;mandatory.y=analysis.y;
    delivery.x=width-rightWidth-80+(rightWidth-delivery.w)/2;
    delivery.y=Math.max(mandatory.y+mandatory.h+290,analysis.y+analysis.h-delivery.h);
    const height=Math.ceil(Math.max(analysis.y+analysis.h,delivery.y+delivery.h)+85);
    const ns='http://www.w3.org/2000/svg';const outer=document.createElementNS(ns,'svg');
    outer.setAttribute('xmlns',ns);outer.setAttribute('width',width);outer.setAttribute('height',height);outer.setAttribute('viewBox',`0 0 ${width} ${height}`);outer.setAttribute('role','img');outer.setAttribute('aria-labelledby','workflow-title');
    outer.innerHTML=`<title id="workflow-title">Read Her Heart 十阶段产品工作流</title><defs><marker id="cross-arrow" viewBox="0 0 12 12" refX="11" refY="6" markerWidth="8" markerHeight="8" orient="auto"><path d="M1 1 11 6 1 11Z" fill="#404d47"/></marker></defs><rect width="100%" height="100%" fill="#f8f9f3"/><text x="65" y="95" fill="#202c26" font-size="48" font-family="Arial Unicode MS, PingFang SC, sans-serif">Read Her Heart　十阶段产品工作流</text>`;
    for(const group of groups){
      if(group.id!=='intake'){const panel=document.createElementNS(ns,'rect');for(const [key,value]of Object.entries({x:group.x-30,y:group.y-30,width:group.w+60,height:group.h+60,rx:25,fill:'#eaf2f0',stroke:'#32827a','stroke-width':2.4}))panel.setAttribute(key,value);outer.append(panel);}
      group.graphic.setAttribute('x',group.x);group.graphic.setAttribute('y',group.y);outer.append(group.graphic);
    }
    const box=id=>{const g=groups.find(g=>g.ids.includes(id)),b=g.nodes[id];return{x:g.x+b.x,y:g.y+b.y,w:b.w,h:b.h};};
    const escape=t=>t.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
    const crosses=edges.filter(e=>groupOf[e.start]!==groupOf[e.end]);
    for(const edge of crosses){
      const a=box(edge.start),b=box(edge.end);let points,labelX,labelY;
      if(edge.start==='M'){
        const lane=intake.y+intake.h+(edge.end==='A'?110:165);
        points=[[a.x+a.w/2,a.y+a.h],[a.x+a.w/2,lane],[b.x+b.w/2,lane],[b.x+b.w/2,b.y]];labelX=b.x+b.w/2+24;labelY=lane+35;
      }else if(edge.start==='RF'){
        const lane=width-25;
        points=[[a.x+a.w,a.y+24],[lane,a.y+24],[lane,delivery.y-95],[b.x+b.w/2,delivery.y-95],[b.x+b.w/2,b.y]];labelX=delivery.x+delivery.w/2;labelY=delivery.y-160;
      }else if(edge.start==='I'){
        const lane=analysis.x+analysis.w+100;
        points=[[a.x+a.w,a.y+a.h/2],[lane,a.y+a.h/2],[lane,b.y+b.h/2],[b.x,b.y+b.h/2]];labelX=lane+10;labelY=b.y+b.h/2-50;
      }else throw new Error('New cross-panel connection needs layout');
      const link=document.createElementNS(ns,'g');link.setAttribute('data-from',edge.start);link.setAttribute('data-to',edge.end);
      const lines=(edge.text||'').split(/<br\s*\/?\s*>/i);
      link.innerHTML=`<path d="${points.map((p,i)=>(i?'L':'M')+p.join(',')).join(' ')}" fill="none" stroke="#404d47" stroke-width="2.5" stroke-linejoin="round" marker-end="url(#cross-arrow)"/><text x="${labelX}" y="${labelY}" font-family="Arial Unicode MS, PingFang SC, sans-serif" font-size="19" fill="#404d47">${lines.map((line,i)=>`<tspan x="${labelX}" dy="${i?27:0}">${escape(line)}</tspan>`).join('')}</text>`;
      outer.append(link);checkedEdges++;
    }
    if(checkedNodes!==vertices.length||checkedEdges!==edges.length)throw new Error('Workflow content verification failed');
    return{svg:new XMLSerializer().serializeToString(outer),width,height,nodes:checkedNodes,edges:checkedEdges};
  },source);
  const out=path.join(root,'docs');await fs.mkdir(out,{recursive:true});
  await fs.writeFile(path.join(out,'product-workflow.svg'),result.svg+'\n');
  const width=3200,height=Math.ceil(width*result.height/result.width);
  await page.setViewportSize({width,height});
  await page.setContent(`<style>html,body{margin:0;background:#f8f9f3}body>svg{display:block;width:100%;height:auto}</style>${result.svg}`);
  await page.screenshot({path:path.join(out,'product-workflow.png'),fullPage:true});
  console.log(JSON.stringify({nodes:result.nodes,edges:result.edges,svgSize:[result.width,result.height],pngSize:[width,height]}));
}finally{await browser?.close();await new Promise(resolve=>server.close(resolve));}
