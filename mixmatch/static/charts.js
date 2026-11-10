const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names={master:'Master comparison',channels:'Channel balance',effects:'Effect checks',final:'Final master check'};
const labels={tonal:'Tone balance',masking:'Masking',loudness:'Loudness',dynamics:'Punch',width:'Stereo width',low_side:'Low-end stereo',clipping:'Peak level'};
const scoring={full:'Full mix',balance:'Channel balance',local:'Local mix check'};
let selection='',project='',latestState;
// Keep the displayed reference metric identical in channel and master phases.
// Loudness/dynamics enter the optimizer only during master checks; they are shown separately.
export function referenceDistance(parts){
 if(!Number.isFinite(parts?.tonal))return null;
 return ['tonal','width','low_side'].reduce((sum,key)=>sum+(Number.isFinite(parts[key])?parts[key]:0),0);
}
export function comparableSeries(groups){
 const result=new Map();
 groups.forEach((g,ordinal)=>{
  const bars=g.section?.match(/bars ([\d.]+)[–-]([\d.]+)/),passage=bars?`${bars[1]}–${bars[2]}`:g.section;
  // Scoring, actual passage and run must all agree. Unknown passages stay separate.
  const key=JSON.stringify([g.run,g.scoring,g.phase,passage || ordinal]);
  if(!result.has(key))result.set(key,{key,phase:g.phase,scoring:g.scoring,passage,points:[],last:ordinal});
  const row=result.get(key);row.last=ordinal;
  (g.values || []).forEach((value,i)=>{const parts=g.components?.[i];row.points.push({value,section:g.section,kind:i?'Kept change':'Measurement',
   reference:referenceDistance(parts),
   masking:parts?.masking ?? null});});
 });return [...result.values()];
}
export function latestReference(groups){
 for(const group of [...groups].reverse()){
  if(group.scoring==='local' || group.phase==='effects')continue;
  const parts=[...(group.components || [])].reverse().find(p=>Number.isFinite(p?.tonal));
  if(parts)return {parts,section:group.section,phase:group.phase};
 }
 return null;
}
export function currentInspection(s){
 const status=s.workflow?.status;
 if(status==='finalizing' || status==='repairing')return 'Recording the After preview';
 if(!s.active && !s.running)return status==='paused' || status==='interrupted'?'Run paused':status==='finished'?'Run finished':'No check running';
 const focus=s.current_focus;
 if(focus?.channel){const bars=Number.isFinite(focus.start_bar) && Number.isFinite(focus.end_bar)?` · bars ${focus.start_bar}–${focus.end_bar}`:'';return `${focus.channel}${focus.device?' · '+focus.device:''}${bars}`;}
 // Older workers do not publish the current effect. Never label the last completed one as current.
 if(s.section)return s.section;
 const phase=(s.phase || '').toLowerCase();
 return phase.startsWith('effect')?'Effect checks · listening with effects on and off':phase.startsWith('final')?'Final master check':phase.startsWith('channel')?'Channel balance':phase.startsWith('master')?'Master comparison':s.activity?.text || 'Preparing the mix';
}
function plot(series){
 const points=series.points;if(!points.length)return '<p class="muted">Waiting for a measurement.</p>';
 const detailed=points.some(p=>p.reference!==null || p.masking!==null),lines=detailed?[{key:'reference',name:'Tone & stereo distance',cls:'reference'},{key:'masking',name:'Masking',cls:'masking'}]:[{key:'value',name:'Section score',cls:'reference'}];
 const values=points.flatMap(p=>lines.map(l=>p[l.key])).filter(Number.isFinite),max=Math.max(1,...values)*1.15;
 const w=600,h=192,left=36,right=12,top=14,bottom=30,x=i=>left+i*(w-left-right)/Math.max(1,points.length-1),y=v=>top+(max-v)/max*(h-top-bottom);
 const grid=[0,.5,1].map(f=>`<line x1="${left}" y1="${y(max*f)}" x2="${w-right}" y2="${y(max*f)}" class="chart-grid"/><text x="${left-7}" y="${y(max*f)+4}" text-anchor="end">${(max*f).toFixed(1)}</text>`).join('');
 const curves=lines.map(line=>{let segments=[],segment=[];points.forEach((p,i)=>{if(Number.isFinite(p[line.key]))segment.push([i,p]);else if(segment.length){segments.push(segment);segment=[];}});if(segment.length)segments.push(segment);return segments.map(segment=>`${segment.length>1?`<polyline class="chart-curve ${line.cls}" points="${segment.map(([i,p])=>`${x(i)},${y(p[line.key])}`).join(' ')}"/>`:''}${segment.map(([i,p])=>`<circle class="chart-dot ${line.cls}" tabindex="0" cx="${x(i)}" cy="${y(p[line.key])}" r="3"><title>${esc(p.section)} · ${p.kind}: ${line.name} ${p[line.key].toFixed(2)}</title></circle>`).join('')}`).join('');}).join('');
 return `<svg class="measure-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${points.length} comparable measurements in ${esc(series.passage)}. Tone and stereo distance and masking are separate curves.">${grid}${curves}<text x="${left}" y="${h-6}">First measurement</text><text x="${w-right}" y="${h-6}" text-anchor="end">${points.length>1?`${points.length} measurements`:'First point · more measurements will follow'}</text></svg><div class="chart-legend">${lines.filter(l=>points.some(p=>Number.isFinite(p[l.key]))).map(l=>`<span><i class="${l.cls}"></i>${l.name}</span>`).join('')}</div>`;
}
function write(id,html){const el=document.getElementById(id);if(el && el.dataset.render!==html){el.innerHTML=html;el.dataset.render=html;}}
export function renderCharts(s){
 latestState=s;if(project!==s.project_id){selection='';project=s.project_id;}
 const groups=s.measurement_groups || [],latest=groups.at(-1),parts=s.parts || {},phase=(s.phase || '').toLowerCase(),series=comparableSeries(groups);
 const preferred=series.filter(g=>g.scoring==='balance').sort((a,b)=>b.points.length-a.points.length)[0] || series.at(-1);
 const current=series.find(g=>g.key===selection) || preferred;
 const comparison=latestReference(groups),referenceParts=comparison?.parts || ((!phase.startsWith('effect') && latest?.scoring!=='local')?parts:{}),hasReference=Number.isFinite(referenceParts.tonal);
 const ref=referenceDistance(referenceParts) ?? 0;
 const inspection=currentInspection(s),referenceSource=comparison?.section || 'Latest measured passage';
 const verdict=!hasReference?'Comparison pending':ref<1?'Very close':ref<2.5?'Close':ref<4?'Audible differences':'Large differences';
 write('mix-closeness',`<h2 class="window-title">Tone & stereo balance</h2><div class="closeness-word">${verdict}</div><div class="score-value" title="${esc(referenceSource)}">${hasReference?ref.toFixed(2)+' points · latest comparison':'No reference comparison in this passage'}</div><div class="reference-scale" role="img" aria-label="${esc(verdict)}"><i style="left:${Math.min(100,ref/6*100)}%" ${!hasReference?'hidden':''}></i></div><div class="scale-labels"><span>Like the reference</span><span>Large differences</span></div><p class="comparison-scope">Compared with the reference · same criteria in every phase.</p><p class="current-inspection"><strong>${s.active || s.running?'Now checking':'Status'}</strong> · ${esc(inspection)}</p><div class="masking-summary"><span>Simultaneous masking</span><strong>${Number.isFinite(referenceParts.masking)?(referenceParts.masking<1?'Small':referenceParts.masking<2.5?'Noticeable':'Strong')+' · '+referenceParts.masking.toFixed(2):'Pending'}</strong></div><dl class="reference-levels">${['loudness','dynamics'].map(key=>`<div><dt>${key==='loudness'?'Loudness':'Dynamics'}</dt><dd>${esc(s.explain?.[key] || 'Not measured yet')}</dd></div>`).join('')}</dl>`);
 write('mix-curve',`<h2 class="window-title">Mix history</h2>${current?`<select id="measurement-select" class="measure-select" aria-label="Choose comparable measurement history">${series.map(g=>`<option value="${esc(g.key)}" ${g.key===current.key?'selected':''}>${esc(scoring[g.scoring])} · ${esc(g.passage?.includes('–')?'bars '+g.passage:g.passage)} · ${g.points.length} measurements</option>`).join('')}</select>${plot(current)}<p class="muted">Same passage and scoring throughout this curve. Lower values mean less measured difference; small fluctuations are normal.</p>`:'<p class="muted">Measurements appear here as MARA listens.</p>'}`);
 const select=document.getElementById('measurement-select');if(select)select.onchange=()=>{selection=select.value;renderCharts(latestState);};
 write('mix-metrics',`<h2 class="window-title">Measurements</h2><table class="metric-table"><tbody>${Object.entries(labels).map(([key,name])=>`<tr><td>${name}</td><td>${Number.isFinite(parts[key])?parts[key].toFixed(2):'—'}</td><td>${esc(s.explain?.[key] || 'Not measured in this check')}</td></tr>`).join('')}</tbody></table><p class="muted">Numbers are score contributions. Loudness and punch are excluded during channel balance checks.</p>`);
 write('mix-history',`<p class="muted">Use the Mix history selector to inspect earlier phases and passages. Each uses its own measurement scale.</p>`);
 const decisions=[...new Map((s.events || []).filter(e=>['kept','undone','action'].includes(e.kind)).map(e=>[e.text,e])).values()].slice(-4);
 write('mix-decisions',`<h2 class="window-title">Latest decisions</h2>${decisions.map(e=>`<div class="decision ${e.kind}">${esc(e.text)}</div>`).join('') || '<p class="muted">No changes tested yet.</p>'}`);
}
