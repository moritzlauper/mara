import {renderCharts,analysisFeedback} from '/app/charts.js';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const PHASES = {master:'Master comparison',channels:'Channels, groups & returns',character:'Dynamics, width & key channels',effects:'Effect checks',final:'Final master check',preview:'Record After & restore Live'};
const PHASE_PURPOSE={character:'Thorough mix: compression and width per channel, then the important channels.',master:'Compare the full mix with your reference.',channels:'Improve individual channels, groups and returns in the mix.',effects:'Test effects one at a time · keep useful effects, bypass those that do not help.',final:'Check the finished mix as a whole.',preview:'Record the result and restore normal Live playback.'};
const STATUS = {idle:'Ready',preparing:'Preparing',analysing:'Analysing',reviewing:'Reviewing roles',ready:'Ready to mix',mixing:'Mixing',pausing:'Pausing safely',paused:'Paused',interrupted:'Interrupted',finalizing:'Preparing your result',repairing:'Repairing previews',finished:'Finished',preview_failed:'Preview needs attention',failed:'Needs attention',restored:'Original restored'};
let state, projectId, workflowKey, stage='project', dirty=false, rolesDirty=false, rolesRevision, roles={}, rolesProject, pending=false, pausedRequested=false;
let selected='before', baseAudio='', audioId='', hiddenAudio=false, changeTab='kept', reviewHash='', restoreToken;
const player=$('player'); player.volume=.5;
const monitorPlayer=$('monitor-player');monitorPlayer.volume=1;
const referencePlayer=$('reference-player');referencePlayer.volume=.5;
function protectPlayback(audio,other){audio.onplay=()=>{other.pause();if(audio!==referencePlayer)referencePlayer.pause();if(state?.output?.silent===false){audio.pause();api('/api/output',{silent:true,current:true}).then(r=>{state.output=r;audio.play().catch(()=>{});}).catch(e=>message(e.message,true));}};}
protectPlayback(monitorPlayer,player);protectPlayback(player,monitorPlayer);protectPlayback(referencePlayer,player);referencePlayer.addEventListener('play',()=>monitorPlayer.pause());
async function api(path, body, method='POST') {
 const response=await fetch(path,{method:body === undefined && method==='POST'?'GET':method,headers:body !== undefined && method!=='PUT'?{'Content-Type':'application/json'}:{},body:body === undefined?undefined:method==='PUT'?body:JSON.stringify(body)});
 const data=await response.json(); if(!response.ok || data.error) throw new Error(data.error || `Request failed (${response.status})`); return data;
}
function message(text,error=false){$('message').textContent=text;$('message').hidden=!text;$('message').classList.toggle('error',error);}
function show(next){stage=next;if(next==='mix' || next==='result')$(next==='mix'?'mix-change-review':'result-change-review').appendChild($('change-review'));document.querySelectorAll('[data-panel]').forEach(el=>el.hidden=el.dataset.panel!==next);document.querySelectorAll('nav [data-stage]').forEach(el=>{if(el.dataset.stage===next)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current');});}
async function action(fn){if(pending)return;pending=true;try{message('');await fn();await refresh();}catch(e){message(e.message,true);}finally{pending=false;}}
document.querySelectorAll('[data-stage]').forEach(button=>button.onclick=()=>show(button.dataset.stage));
let theme=localStorage.getItem('mara-theme') || 'auto';
function applyTheme(){const hour=new Date().getHours();document.documentElement.dataset.theme=theme==='auto'?(hour>=7 && hour<19?'light':'dark'):theme;$('theme').textContent=`Theme: ${theme}`;}
async function openSettings(){try{const r=await api('/api/preferences');$('workspace-path').value=r.workspace;$('reference-library-path').value=r.reference_library;$('settings-message').textContent='';if(!$('settings-dialog').open)$('settings-dialog').showModal();}catch(e){message(e.message,true);}}
$('settings').onclick=openSettings;$('close-settings').onclick=()=>$('settings-dialog').close();
$('save-settings').onclick=async()=>{try{await api('/api/preferences',{reference_library:$('reference-library-path').value});$('settings-message').textContent='Saved. The library is available under References.';await refresh();}catch(e){$('settings-message').textContent=e.message;}};
$('connection-settings').onclick=()=>{$('settings-dialog').close();show('project');document.querySelectorAll('[data-panel="project"] details.setup').forEach(d=>d.open=true);};
document.addEventListener('keydown',e=>{if((e.metaKey || e.ctrlKey) && e.key===','){e.preventDefault();openSettings();}});
$('theme').onclick=()=>{theme=['auto','light','dark'][(['auto','light','dark'].indexOf(theme)+1)%3];localStorage.setItem('mara-theme',theme);applyTheme();};applyTheme();
$('header-progress').onclick=()=>action(async()=>{if(state?.following_live===false)await api('/api/songs/follow-live',{});show(state?.workflow?.stage || 'mix');});
$('project-switch').onchange=()=>action(async()=>{if($('project-switch').value==='__live__')await api('/api/songs/follow-live',{});else await api('/api/songs/switch',{slug:$('project-switch').value});});
async function changeSilent(silent){await action(async()=>{if(!silent){monitorPlayer.pause();player.pause();referencePlayer.pause();}state.output=await api('/api/output',{silent,equal:$('current-equal').checked});updateCurrentMatch(state.output.match);$('monitor-context').textContent=silent?'Live is muted.':'Listening to Current.';});}

$('unmute-current').onclick=()=>{monitorPlayer.pause();player.pause();$('monitor-preview').hidden=true;changeSilent(!(state?.output?.silent ?? true));};
function closeBefore(){monitorPlayer.pause();$('monitor-preview').hidden=true;$('monitor-before').textContent='Before';}
$('monitor-before').onclick=()=>action(async()=>{if(!$('monitor-preview').hidden){closeBefore();return;}const p=state?.previews?.before;if(!p)return;player.pause();state.output=await api('/api/output',{silent:true,equal:$('current-equal').checked});updateCurrentMatch(state.output.match);const src=state.output.match?.ready?state.output.match.before_url:`/api/preview-audio?which=before&id=${encodeURIComponent(p.id)}`;if(monitorPlayer.dataset.source!==src){monitorPlayer.src=src;monitorPlayer.dataset.source=src;}$('monitor-preview').hidden=false;await monitorPlayer.play().catch(()=>{});});
function updateCurrentMatch(match){$('current-equal-status').textContent=!$('current-equal').checked?'Original listening levels.':match?.ready?`Matched listening level · ${match.target_lufs.toFixed(1)} LUFS. Current uses the latest passage measurement (approximate).`:match?.reason || 'Matching will use the next available Current measurement.';}
$('current-equal').onchange=()=>action(async()=>{const playing=!monitorPlayer.paused,position=monitorPlayer.currentTime;state.output=await api('/api/output',{silent:state.output?.silent!==false,equal:$('current-equal').checked});updateCurrentMatch(state.output.match);if(!$('monitor-preview').hidden && state.previews?.before){const p=state.previews.before;const src=state.output.match?.ready?state.output.match.before_url:`/api/preview-audio?which=before&id=${encodeURIComponent(p.id)}`;monitorPlayer.src=src;monitorPlayer.dataset.source=src;monitorPlayer.volume=1;monitorPlayer.onloadedmetadata=()=>{monitorPlayer.currentTime=Math.min(position,monitorPlayer.duration-.1);};if(playing)await monitorPlayer.play();}});
$('description').oninput=$('mode').onchange=()=>{dirty=true;$('save-project').disabled=false;};
$('save-project').onclick=()=>action(async()=>{const body={description:$('description').value};if(!state?.active)body.mode=$('mode').value;const r=await api('/api/project',body);dirty=false;if(state?.active)message(r.applies==='next_step'?'Goal saved. MARA uses it at the next planning step.':'Goal saved. This running version picks it up on resume or on the next run.');else{show('analysis');if(canAnalyse())await api('/api/analyse',runBody({reference_mode:'discover'}));}});
let plannerSettings, plannerDrafts={}, editingProvider;
function plannerBody(){return {provider:$('planner-provider').value,model:$('planner-model').value==='__custom__'?$('planner-custom').value.trim():$('planner-model').value,base_url:$('planner-url').value.trim(),key:$('api-key').value.trim()};}
function modelOptions(models, selected){const ids=[...new Set([...models,...(selected?[selected]:[])])];$('planner-model').innerHTML=ids.map(id=>`<option value="${esc(id)}">${esc(id==='claude-haiku-5-5'?'Claude Haiku 5.5 (default)':id)}</option>`).join('')+'<option value="__custom__">Enter model ID…</option>';$('planner-model').value=selected || ids[0] || '__custom__';$('planner-custom-field').hidden=$('planner-model').value!=='__custom__';}
function renderPlannerProvider(){if(!plannerSettings)return;const provider=$('planner-provider').value,profile=plannerDrafts[provider] || plannerSettings.profiles[provider==='auto'?'anthropic':provider] || {};editingProvider=provider;const usesModel=provider!=='rules',usesKey=['auto','anthropic','gemini','compatible'].includes(provider);$('planner-model-field').hidden=!usesModel;modelOptions(plannerSettings.suggested_models[provider==='auto'?'anthropic':provider] || [],profile.model);$('planner-custom-field').hidden=!usesModel || $('planner-model').value!=='__custom__';$('planner-custom').value=profile.model || '';$('planner-url-field').hidden=provider!=='compatible';$('planner-url').value=profile.base_url || '';$('planner-key-field').hidden=!usesKey;$('api-key').value=profile.key || '';$('api-key-label').textContent=(provider==='auto'?'Anthropic fallback':plannerSettings.providers[provider] || '')+' API key';$('planner-key-status').textContent=plannerSettings.configured[provider==='auto'?'anthropic':provider]?'A key is configured. Leave blank to keep it.':'Keys are saved in macOS Keychain or Windows Credential Manager.';$('load-models').hidden=!usesModel;$('planner-help').textContent=provider==='auto'?'Uses Claude Code when installed and on PATH. With an Anthropic API key, switches to the selected API model for the rest of the run if Claude Code reports a usage or rate limit; other errors do not trigger this switch. Without Claude Code, uses the API directly if a key is configured, otherwise built-in rules.':provider==='claude-code'?'Uses your local Claude Code login and the selected model for every mixing phase.':provider==='codex'?'Uses your local ChatGPT login through Codex CLI. It runs headlessly in an empty read-only folder, without tools or access to your Live project.':provider==='compatible'?'For hosted or local APIs supporting Chat Completions with JSON output. Use the API base URL, usually ending in /v1.':provider==='rules'?'Uses built-in mixing rules without an AI model.':'Uses the selected model for every mixing phase.';updatePlannerCost();}
function updatePlannerCost(){if(!plannerSettings)return;const body=plannerBody(),provider=body.provider,channels=Math.max(1,state?.open_set?.total || 30);let text='',basis='';if(provider==='rules')text='Estimated cost per mix: $0 · built-in rules';else if(provider==='claude-code')text='Uses your Claude Code plan · subscription limits and extra usage depend on your account.';else if(provider==='codex')text='Uses your ChatGPT plan through Codex CLI · subscription limits and extra usage depend on your account.';else{const rates=plannerSettings.pricing?.[provider==='auto'?'anthropic':provider],price=rates?.models?.[body.model];if(price){const lowCalls=channels+4,highCalls=channels*2+16,low=lowCalls*(15000*price[0]+1000*price[1])/1e6,high=highCalls*(50000*price[0]+4000*price[1])/1e6;text=`Estimated ${provider==='auto'?'cost if the entire mix uses the Anthropic API':'API cost per mix'}: about $${low.toFixed(2)}–$${high.toFixed(2)} USD`;basis=`Planning estimate for ${channels} channels, ${lowCalls}–${highCalls} AI calls. `;if(provider==='auto')basis+='API charges apply only to calls made through the API, separately from your Claude Code plan. If switching partway through a mix, only the remaining calls use the API. ';basis+=`<a href="${esc(rates.source)}" target="_blank" rel="noopener">Prices checked ${esc(plannerSettings.pricing.checked)}</a>. `;basis+='Actual usage can exceed this range.';}else text='Estimated cost per mix: price unavailable for this model/provider.';}$('planner-cost').textContent=text;$('planner-cost-basis').innerHTML=basis;$('planner-cost-basis').title='Assumes 15k–50k input and 1k–4k output tokens per call, including reasoning. Standard API rates, no cache discounts. Excludes retries and tax. Haiku estimate assumes prompts below 100k tokens.';}
function setupPlanner(settings){if(!settings?.providers){$('planner-status').textContent='Planner settings are loading…';return;}plannerSettings=settings;plannerDrafts={};$('planner-provider').innerHTML=Object.entries(settings.providers).map(([id,label])=>`<option value="${esc(id)}">${esc(label)}</option>`).join('');$('planner-provider').value=settings.provider;renderPlannerProvider();$('planner-status').textContent='Selected planner: '+settings.providers[settings.provider];}
$('planner-provider').onchange=()=>{if(editingProvider){const body=plannerBody();plannerDrafts[editingProvider]={model:body.model,base_url:body.base_url};}$('api-key').value='';renderPlannerProvider();};
$('planner-model').onchange=()=>{$('planner-custom-field').hidden=$('planner-model').value!=='__custom__';updatePlannerCost();};
$('planner-custom').oninput=updatePlannerCost;
$('load-models').onclick=()=>action(async()=>{const provider=$('planner-provider').value,body=plannerBody();$('load-models').disabled=true;try{const r=await api('/api/setup/models',body);if($('planner-provider').value===provider){modelOptions(r.models,body.model);message(r.models.length?'Models loaded.':'No models listed. Enter a model ID.');}}finally{$('load-models').disabled=false;}});
$('save-planner').onclick=()=>action(async()=>{const r=await api('/api/setup/planner',plannerBody());$('api-key').value='';setupPlanner(r.settings);message('Planner saved. Applies on the next start or resume.');});
$('set-taps').onclick=()=>action(async()=>{followingStage=null;await api('/api/taps',{});message('Setting up measurement taps in Live …');});
let removingMara=false,cleanedProject;
$('remove-mara').onclick=()=>action(async()=>{const project=state.project_id;removingMara=true;$('remove-mara').disabled=true;try{await api('/api/taps/remove',{project_id:project});cleanedProject=project;message('MARA measurement devices removed. Your mix and saved previews are kept.');}finally{removingMara=false;}});
$('install-bridge').onclick=()=>action(async()=>{const r=await api('/api/setup/install',{library:$('library-path').value});message(r.message);});
$('browse-references').onclick=()=>{$('reference-library').open=true;$('reference-search').focus();};
$('reference-search').oninput=()=>renderReferences(state);
$('change-references').onclick=()=>action(async()=>{await api('/api/stop',{});pausedRequested=true;message('Stopping at the next safe checkpoint. Reference selection becomes available when the run has stopped.');});
$('pick-files').onclick=()=>$('reference-files').click();
async function upload(files){
 if(state?.active || state?.running){message('Reference files can be added when the current run has stopped.',true);return;}
 if(pending){message('An import or another action is still running. Try the drop again when it finishes.');return;}
 if(!files.length){message('Drop audio files directly from Finder or File Explorer, or choose Add audio files.',true);return;}
 if(files.some(f=>!f.size || !/\.(wav|aif|aiff|flac|mp3|m4a|aac)$/i.test(f.name))){message('Choose audio files (WAV, AIFF, FLAC, MP3, M4A or AAC). Folders and empty files cannot be imported.',true);return;}
 await action(async()=>{const warnings=[];for(const [i,file] of files.entries()){message(`Importing ${file.name} · ${i+1} of ${files.length}…`);const r=await api('/api/references/'+encodeURIComponent(file.name),file,'PUT');if(r.warning)warnings.push(r.warning);}message(`Added ${files.length} reference ${files.length===1?'file':'files'}. ${warnings.join(' ')}`);});
}
$('reference-files').onchange=e=>{upload([...e.target.files]);e.target.value='';};
const fileDrag=e=>[...(e.dataTransfer?.types || [])].includes('Files');
for(const name of ['dragenter','dragover'])document.addEventListener(name,e=>{if(!fileDrag(e))return;e.preventDefault();e.dataTransfer.dropEffect=stage==='references' && !state?.active?'copy':'none';$('drop').classList.toggle('over',stage==='references' && !state?.active);});
document.addEventListener('dragleave',e=>{if(!e.relatedTarget)$('drop').classList.remove('over');});
document.addEventListener('drop',e=>{if(!fileDrag(e))return;e.preventDefault();$('drop').classList.remove('over');if(stage==='references')upload([...e.dataTransfer.files]);else message('Open References to add audio files.');});
// One mix depth, shown the same way in References, Settings and Mix.
let mixDepth='thorough';try{mixDepth=localStorage.getItem('mara-depth') || 'thorough';}catch{}
const DEPTHS={thorough:['Thorough','about 25–30 min','Also compression and width per channel, and the planner on the important channels.'],fast:['Fast','about 15–20 min','Tone, balance, collisions and loudness: calculated, then measured.']};
let depthLocked=false;
function renderDepth(){for(const id of ['depth-refs','depth-settings','depth-mix'])markup(id,Object.entries(DEPTHS).map(([k,[title,time,what]])=>
 `<button type="button" class="depth-option${mixDepth===k?' chosen':''}" data-depth="${k}" aria-pressed="${mixDepth===k}" ${depthLocked && id!=='depth-settings'?'disabled':''}><strong>${title}</strong><em>${time}</em>${id==='depth-mix'?'':`<small>${what}</small>`}</button>`).join(''));}
document.addEventListener('click',e=>{const b=e.target.closest('[data-depth]');if(!b || b.disabled)return;mixDepth=b.dataset.depth;try{localStorage.setItem('mara-depth',mixDepth);}catch{}renderDepth();});
function runBody(extra={}){return {reference_mode:'selected',silent:true,mode:$('mode').value,depth:mixDepth,...extra};}
function canAnalyse(){const s=state || {},live=s.open_set;return !s.active && !s.running && live && live.taps>0 && s.following_live!==false && !s.understanding && s.workflow?.status!=='ready';}
$('discover').onclick=()=>action(async()=>{await api('/api/analyse',runBody({reference_mode:'discover'}));show('analysis');});
$('go-references').onclick=()=>show('references');
// After a reference search the song is analysed already: continue to the instruments without a new run.
$('analyse').onclick=()=>action(async()=>{if(state?.workflow?.status==='ready'){show('analysis');return;}await api('/api/analyse',runBody({auto_mix:true}));show('analysis');});
$('start-top').onclick=()=>$('start').click();
$('start').onclick=()=>action(async()=>{if(rolesDirty)await saveRoles();await api('/api/run',runBody({continue:!!state.checkpoint}));show('mix');});
let startOverArmed=0;
$('start-over').onclick=()=>{if(Date.now()-startOverArmed>4000){startOverArmed=Date.now();$('start-over').textContent='Click again to start over';setTimeout(()=>{if(Date.now()-startOverArmed>=4000)$('start-over').textContent='Start over';},4100);return;}
 startOverArmed=0;$('start-over').textContent='Start over';action(async()=>{const r=await api('/api/start-over',{});pausedRequested=false;message(r.stopping?'Stopping at the next safe step, then starting over. The changes MARA kept stay in Live; Restore original in Result resets them.':'Started over. MARA analyses the song again.');show('analysis');});};
$('resume').onclick=()=>action(async()=>{await api('/api/run',runBody({continue:true}));pausedRequested=false;show('mix');});
$('pause').onclick=()=>action(async()=>{await api('/api/stop',{});pausedRequested=true;message('Pausing at the next safe checkpoint. Any current test will be restored first.');});
$('repair').onclick=()=>action(async()=>{await api('/api/preview/repair',{});message('Recording missing previews. The mix stays unchanged.');});
$('new-run').onclick=()=>$('new-run-dialog').showModal();
$('cancel-new-run').onclick=()=>$('new-run-dialog').close();
$('confirm-new-run').onclick=()=>{$('new-run-dialog').close();$('new-run-final-dialog').showModal();};
$('cancel-new-run-final').onclick=()=>$('new-run-final-dialog').close();
$('new-run-final').onclick=()=>{$('new-run-final-dialog').close();show('project');message('Review your goal and references before starting another run.');};
function markup(id,html){const el=$(id);if(el.dataset.render!==html){el.innerHTML=html;el.dataset.render=html;}}
function phaseOf(phase=''){phase=phase.toLowerCase();return phase.startsWith('character') || phase.startsWith('dynamics') || phase.startsWith('width')?'character':phase.startsWith('final')?'final':phase.startsWith('channel')?'channels':phase.startsWith('effect')?'effects':phase.startsWith('master')?'master':'setup';}
// A reference as one compact card: its own play button and seek bar, one small action, sources behind «⋯».
function refCard({key,src,title,sub='',why='',badge='',action='',links=[]}){
 const play=src?`<button class="play" data-audition="${esc(key)}" data-src="${esc(src)}" data-title="${esc(title)}" aria-label="Play ${esc(title)}">▶</button>`
  :'<span class="play none" title="No preview available">♪</span>';
 const more=links.length?`<details class="more"><summary aria-label="Where to get ${esc(title)}" title="Sources">⋯</summary><div class="menu">${links.map(([k,u])=>`<button data-open="${esc(u)}">${esc(k)}</button>`).join('')}</div></details>`:'';
 return `<div class="ref-card" data-card="${esc(key)}">${play}<div class="ref-main"><div class="ref-head"><strong>${esc(title)}</strong>${sub?`<span class="muted">${esc(sub)}</span>`:''}${badge?`<span class="pill">${esc(badge)}</span>`:''}</div>`
  +(why?`<p class="why">${esc(why)}</p>`:'')+`<div class="seek" data-seek="${esc(key)}" hidden><i></i><span class="time"></span></div></div><div class="ref-actions">${action}${more}</div></div>`;
}
const bare=name=>String(name).replace(/\.(wav|aiff?|flac|mp3|m4a|aac|ogg)$/i,'');
function renderReferences(s){
 markup('reference-list',(s.reference_files || []).map(name=>refCard({key:'n:'+name,src:'/api/reference-audio?name='+encodeURIComponent(name),title:bare(name),badge:'selected',
   action:`<button class="mini" data-remove="${esc(name)}">Remove</button>`})).join('')
  +(s.project_references || []).map(r=>refCard({key:'l:'+(r.file || r.track),title:bare(r.file?.split(/[\\/]/).pop() || r.track || r),sub:'reference clip in Live',badge:'in Live'})).join('')
  || ((s.reference_candidates || []).some(r=>!r.hidden)?'':'<p class="muted">Nothing selected yet. Add one or two of the suggestions below.</p>'));
 const q=$('reference-search').value.toLowerCase();
 const chosen=new Set((s.reference_files || []).map(n=>n.toLowerCase()));
 markup('candidates',(s.reference_candidates || []).filter(r=>!r.hidden && !chosen.has(r.name.toLowerCase()) && r.name.toLowerCase().includes(q)).map(r=>refCard({key:'c:'+r.id,
   src:'/api/reference-audio?id='+encodeURIComponent(r.id),title:bare(r.name),badge:'on your computer',action:`<button class="mini primary" data-reference="${esc(r.id)}">Add</button><button class="mini" data-hide-reference="${esc(r.id)}" title="Remove from this list (the file stays on your computer)">Remove</button>`})).join(''));
 // A track already on the computer (selected or in the library) is not suggested for purchase again.
 const plain=t=>String(t || '').toLowerCase().replace(/[^a-z0-9]/g,''),owned=[...(s.reference_files || []),...(s.reference_candidates || []).map(r=>r.name)].map(plain);
 const ownedTitle=t=>{const k=plain(String(t).split(' – ').pop());return k.length>=5 && owned.some(o=>o.includes(k));};
 markup('suggestions',(s.to_get || []).filter(r=>!ownedTitle(r.title || r.name)).map(r=>{const links=r.links || {},[artist,title]=String(r.title || r.name).includes(' – ')?String(r.title).split(' – ',2):['',r.title || r.name];
   const pages=Object.entries(links).filter(([k])=>!['Preview','Download'].includes(k));
   // One direct click per card: Add when the file can be fetched, else the shop (or free page) that sells the full file.
   const direct=r.free?(links.Download?null:pages.find(([k])=>k!=='Spotify')):['Bandcamp','Beatport','Apple Music'].map(k=>[k,links[k]]).find(([,u])=>u);
   const action=r.free && links.Download?`<button class="mini primary" data-use="${esc(links.Download)}" data-use-name="${esc(r.title)}">Add</button>`
    :direct?`<button class="mini primary" data-open="${esc(direct[1])}" title="${r.free?'Download it free':'Buy the full file'} on ${esc(direct[0])}">${r.free?'Get free':esc(direct[0])} ↗</button>`:'';
   return refCard({key:'p:'+(links.Preview || r.title),src:links.Preview || '',title,sub:artist,why:r.why,badge:r.free?'free':'buy',
    action,links:pages.filter(p=>!direct || p[0]!==direct[0])});}).join('')
  +((s.to_get || []).length?`<p class="muted small">Bought or downloaded one (WAV, FLAC, MP3 or ZIP)? MARA picks it up from Downloads. <button class="mini" data-import-downloads>Import now</button></p>`:''));
 syncPlayButtons();
}
// One player for every reference: the row that plays shows Pause, a second click pauses, the dock below has seek.
function renderUnderstanding(s,w,status,active){
 const u=s.understanding,discovering=w.purpose==='references' && active,failed=w.purpose==='references' && ['failed','interrupted'].includes(status);
 $('discover').textContent=u?'Analyse again':'Analyse my mix';$('discover').disabled=active;
 $('understanding-title').textContent=discovering?'MARA is listening to your song':u?'Your song':'Not analysed yet';
 let html='';
 if(discovering){const p=w.analysis_progress;let frac=null;
  if(p){let part=0;if(p.by_position && w.activity?.total)part=Math.min(1,(w.activity.current || 0)/w.activity.total);else if(p.started && p.estimate)part=Math.min(.95,(Date.now()/1000-p.started)/p.estimate);
   frac=Math.min(1,p.base+p.weight*part);}
  html=(p?`<p class="summary">Step ${p.step} of ${p.steps} · ${esc(p.label)}</p>`:`<p class="summary">${esc(w.activity?.text || 'Listening to your song')} …</p>`)
   +(frac===null?'<div class="progress indeterminate"><div style="width:30%"></div></div>':`<div class="progress" role="progressbar" aria-valuenow="${Math.round(frac*100)}"><div style="width:${(frac*100).toFixed(1)}%"></div></div><p class="muted small">${Math.round(frac*100)}% · ${esc(w.activity?.text || '')}</p>`)
   +'<p class="muted">One pass through the song, the instruments, a style estimate, reference suggestions and a recording of your original. You can already pick references in the next step.</p>';}
 else if(failed)html=`<p class="summary">The analysis stopped${w.error?': '+esc(w.error):'.'}</p><p class="muted">Start it again with «Analyse my mix».</p>`;
 if(u && !discovering){const fam=(u.families || []).filter(f=>f.family!=='groups'),c=u.comparison;
  const facts=[['Key',u.key],['Tempo',u.tempo?u.tempo+' BPM':null],['Style',u.style || (u.sounds_like || []).slice(0,3).join(', ')],
   ['Loudest part',u.loudest?`bars ${u.loudest.from}–${u.loudest.to} · ${u.loudest.lufs.toFixed(1)} LUFS`:null],
   ['Instruments',fam.map(f=>`${f.family} ${f.count}`).join(' · ')],
   ['Against the references',c?`${c.loudness_lu>0?'+':''}${c.loudness_lu} LU · PLR ${c.plr_db} vs ${c.reference_plr_db} dB`:null]].filter(([,v])=>v);
  html+=`<p class="summary">${esc(u.text || '')}</p><div class="facts">${facts.map(([k,v])=>`<div class="fact"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('')}</div>`
   +(s.previews?.before?refCard({key:'before',src:`/api/preview-audio?which=before&id=${encodeURIComponent(s.previews.before.id)}`,title:'Your original',sub:'loudest part, as MARA recorded it'}):'');}
 else if(!discovering && !failed)html='<p class="summary">MARA has not listened to this song yet. «Analyse my mix» plays it once with Live muted, describes what it hears and suggests references.</p>';
 markup('understanding',html);
 $('go-references').textContent=discovering?'Choose references now →':'Choose references →';
 $('suggestions-block').hidden=!(s.to_get || []).length;
}
function syncPlayButtons(){const key=referencePlayer.dataset.key,on=!referencePlayer.paused;
 document.querySelectorAll('[data-audition]').forEach(b=>{const mine=b.dataset.audition===key;b.textContent=mine && on?'❚❚':'▶';b.classList.toggle('playing',mine && on);b.setAttribute('aria-pressed',String(mine && on));});
 document.querySelectorAll('[data-seek]').forEach(el=>el.hidden=el.dataset.seek!==key);updateSeek();}
const clock=t=>isFinite(t)?`${Math.floor(t/60)}:${String(Math.floor(t%60)).padStart(2,'0')}`:'0:00';
function updateSeek(){const el=document.querySelector(`[data-seek="${CSS.escape(referencePlayer.dataset.key || '')}"]`);if(!el)return;const d=referencePlayer.duration,t=referencePlayer.currentTime;
 el.querySelector('i').style.width=(d?100*t/d:0)+'%';el.querySelector('.time').textContent=`${clock(t)} / ${clock(d)}`;}
function audition(key,src,label){if(referencePlayer.dataset.key===key){if(referencePlayer.paused)referencePlayer.play().catch(()=>{});else referencePlayer.pause();return;}
 referencePlayer.dataset.key=key;referencePlayer.src=src;$('reference-playing').textContent=label;referencePlayer.play().catch(()=>message('Press Play to listen.'));syncPlayButtons();}
for(const ev of ['play','pause','ended','emptied','loadedmetadata'])referencePlayer.addEventListener(ev,syncPlayButtons);
referencePlayer.addEventListener('timeupdate',updateSeek);
$('reference-close').onclick=()=>{referencePlayer.pause();referencePlayer.removeAttribute('src');referencePlayer.load();delete referencePlayer.dataset.key;syncPlayButtons();};
document.addEventListener('click',e=>{const seek=e.target.closest('[data-seek]');if(seek && referencePlayer.duration){const r=seek.getBoundingClientRect();referencePlayer.currentTime=referencePlayer.duration*Math.min(1,Math.max(0,(e.clientX-r.left)/r.width));}
 const b=e.target.closest('button');if(b?.dataset.audition)audition(b.dataset.audition,b.dataset.src,b.dataset.title);if(b?.closest('details.more'))b.closest('details.more').open=false;});
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;
 if(b.dataset.open)action(()=>api('/api/open',{url:b.dataset.open}));
 if(b.dataset.hideReference)action(()=>api('/api/references/hide',{id:b.dataset.hideReference,hidden:true}));
 if(b.dataset.use)action(async()=>{const r=await api('/api/references/use',{url:b.dataset.use,name:b.dataset.useName});message('Added to your references: '+r.saved);});
 if(b.dataset.importDownloads!==undefined)action(async()=>{const r=await api('/api/references/import',{});message(r.added?.length?'Imported: '+r.added.join(', '):'Nothing new in Downloads from the last hour.');});
});
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.listenReference || b.dataset.listenName){const c=(state.reference_candidates || []).find(c=>c.id===b.dataset.listenReference);audition(b.dataset.playKey,'/api/reference-audio?'+(c?'id='+encodeURIComponent(c.id):'name='+encodeURIComponent(b.dataset.listenName)),c?.name || b.dataset.listenName);}if(b.dataset.remove)action(()=>api('/api/references/remove',{name:b.dataset.remove}));if(b.dataset.reference)action(()=>api('/api/references/choose',{id:b.dataset.reference}));});
async function loadRoles(s){
 if(rolesDirty || (rolesRevision===s.roles_revision && rolesProject===s.project_id))return;
 const expected=s.project_id;const data=await api('/api/roles');if(state?.project_id!==expected || rolesDirty)return;roles=data;rolesRevision=s.roles_revision;rolesProject=expected;
 const options=s.role_groups || {'Roles':['kick','snare','clap','hats','cymbals','percussion','drums_bus','bass','sub','lead_vocal','piano','keys','strings','lead_synth','pad','bus','ambience','other']};
 markup('roles',Object.entries(data).map(([name,r],index)=>`<div class="role"><span>${esc(name)}<small class="muted" style="display:block">${esc(r.source==='user'?'Your assignment':r.note || r.evidence || 'Automatic estimate · review if needed')}</small></span><select data-role="${index}" aria-label="Role for ${esc(name)}">${Object.entries(options).map(([g,rs])=>`<optgroup label="${esc(g)}">${rs.map(v=>`<option ${v===r.role?'selected':''}>${esc(v)}</option>`).join('')}</optgroup>`).join('')}</select><input type="number" data-priority="${index}" value="${r.priority}" min="1" max="5" aria-label="Priority for ${esc(name)}"></div>`).join('') || '<p class="muted">Instrument roles appear after analysis.</p>');
 $('save-roles').disabled=true;
}
$('roles').onchange=()=>{rolesDirty=true;$('save-roles').disabled=false;};
async function saveRoles(){const updated=structuredClone(roles);Object.entries(roles).forEach(([name,r],i)=>{const role=document.querySelector(`[data-role="${i}"]`).value,priority=+document.querySelector(`[data-priority="${i}"]`).value;if(role!==r.role || priority!==r.priority)updated[name]={...r,role,priority,source:'user'};});await api('/api/roles',updated);rolesDirty=false;rolesRevision=undefined;message(state?.workflow?.status==='reviewing'?'Roles saved. This mix will use your corrections.':'Roles saved. The next run uses your corrections.');}
$('save-roles').onclick=()=>action(saveRoles);
$('hold-review').onclick=()=>action(()=>api('/api/analysis/review',{run_id:state.workflow.id,action:'hold'}));
$('continue-review').onclick=()=>action(async()=>{if(rolesDirty)await saveRoles();await api('/api/analysis/review',{run_id:state.workflow.id,action:'continue'});});
function loadAudio(url,play=false,position=0){baseAudio=url;player.src=url+($('equal').checked && state.loudness_match?.ready?'&equal=1&match='+encodeURIComponent(state.loudness_match.version):'');player.onloadedmetadata=()=>{if(position)player.currentTime=Math.min(position,Math.max(0,player.duration-.1));};if(play)player.play().catch(()=>message('Press Play to listen.'));}
function choose(which,play=true){const p=state?.previews?.[which];if(!p)return;const position=player.currentTime || 0;selected=which;audioId=p.id;hiddenAudio=false;$('blind-label').textContent='';loadAudio(`/api/preview-audio?which=${which}&id=${encodeURIComponent(p.id)}`,play,position);renderAudio(state);}
$('ab-before').onclick=()=>choose('before');$('ab-after').onclick=()=>choose('after');
$('equal').onchange=()=>{if(baseAudio)loadAudio(baseAudio,!player.paused,player.currentTime);renderAudio(state);};
$('blind').onclick=()=>action(async()=>{const r=await api('/api/preview/blind',{});hiddenAudio=true;loadAudio(r.url,true,player.currentTime);$('blind-label').textContent='Hidden version · repeats are possible.';});
async function reveal(verdict){const r=await api('/api/preview/reveal',verdict?{verdict}:{});selected=r.which;hiddenAudio=false;audioId=state.previews[selected].id;const v=r.votes;$('blind-label').textContent=`You are hearing ${selected==='before'?'Before':'After'}.`+(v?` Blind votes: After preferred in ${v.after_preferred} of ${v.total}`+(v.total>=5?` (p = ${v.p_value.toFixed(2)} against guessing).`:'.'):'');renderAudio(state);}
$('reveal').onclick=()=>action(()=>reveal());
$('vote-better').onclick=()=>action(()=>reveal('better'));$('vote-worse').onclick=()=>action(()=>reveal('worse'));
document.addEventListener('keydown',e=>{if(e.key.toLowerCase()==='b' && !e.repeat && !e.metaKey && !e.ctrlKey && !e.altKey && !e.target.closest('input,textarea,select,[contenteditable]') && stage==='result' && !$('blind').disabled){e.preventDefault();$('blind').click();}});
function renderAudio(s){const versions=s.previews || {},both=versions.before && versions.after;for(const w of ['before','after']){$('ab-'+w).disabled=!versions[w];$('ab-'+w).setAttribute('aria-pressed',String(!hiddenAudio && selected===w));}$('equal').disabled=!s.loudness_match?.ready;$('blind').disabled=!both;$('reveal').disabled=!hiddenAudio;$('vote-better').disabled=!hiddenAudio;$('vote-worse').disabled=!hiddenAudio;
 if(!versions[selected] && versions.before)selected='before';if(versions[selected] && !hiddenAudio && audioId!==versions[selected].id)choose(selected,false);
 const p=versions[selected];$('audio-context').textContent=p?`${hiddenAudio?'Blind preview':selected==='before'?'Before':'After'} · ${p.bars} bars from bar ${p.start_bar}. `+(both?(s.loudness_match?.ready && $('equal').checked?'Matched listening level · original dynamics preserved.':'Original listening levels.'):'After is available when the run finishes.'):'No audio preview yet. Analyse your mix to record the original.';
 player.hidden=!p;
}
function renderLatestChanges(s){
 const grouped=new Map();
 for(const item of s.review?.kept || []){if(!Number.isFinite(item.at))continue;const key=item.track+'|'+item.device;if(!grouped.has(key))grouped.set(key,{channel:item.channel,device:item.device,at:0,items:[]});const row=grouped.get(key);row.at=Math.max(row.at,item.at);row.items.push(item);}
 const latest=[...grouped.values()].sort((a,b)=>b.at-a.at).slice(0,3);
 const describe=item=>{if(item.kind==='added'){const settings=item.settings || [],gains=settings.filter(p=>/Gain/.test(p.param));const bands=gains.map(g=>{const frequency=settings.find(p=>p.param===g.param.replace('Gain','Frequency'));return frequency?`${frequency.value}: ${g.value}`:`${g.param}: ${g.value}`;});return 'Added'+(bands.length?' · '+bands.join(' · '):' device');}if(item.kind==='removed')return 'Removed device';if(item.param==='Device On')return Number(item.raw_after)===0?'Bypassed':'Enabled';return `${item.param}: ${item.before} → ${item.after}`;};
 const changedAt=at=>new Intl.DateTimeFormat(undefined,{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}).format(new Date(at*1000));
 markup('latest-changes',`<div class="latest-changes-heading"><h2>Latest changes</h2><span class="muted">Kept in the mix</span></div>${latest.length?`<ul>${latest.map(row=>{const descriptions=row.items.map(describe),time=changedAt(row.at);return `<li><strong>${esc(row.channel)} <span>· ${esc(row.device.replace(/#1$/,''))}</span></strong><span class="latest-change-detail">${esc(descriptions.slice(0,3).join(' · '))}${descriptions.length>3?` · +${descriptions.length-3} settings`:''}<time class="latest-change-time" datetime="${new Date(row.at*1000).toISOString()}">Changed ${esc(time)}</time></span></li>`;}).join('')}</ul>`:'<p class="muted">No kept changes recorded yet.</p>'}`);
}
function renderChanges(s){const r=s.review || {},list=changeTab==='kept'?r.kept || []:r.rejected || [];
 $('change-count').textContent=r.ready?`${(r.kept || []).length} ${r.partial?'verified changes so far':'changes'} across ${r.channels || 0} channels`:'No saved result yet';
 $('legacy-note').hidden=!r.legacy;$('legacy-note').textContent='This earlier run has saved settings but no detailed decision journal. Values marked “Live value” are the stored parameter values.';
 const channels=[...new Set((r.kept || []).map(x=>x.channel))],select=$('channel-filter'),old=select.value;
 const optionHtml='<option value="">All channels</option>'+channels.map(c=>`<option>${esc(c)}</option>`).join('');if(select.dataset.render!==optionHtml){select.innerHTML=optionHtml;select.dataset.render=optionHtml;select.value=channels.includes(old)?old:'';}
 select.hidden=changeTab==='rejected';
 const hash=JSON.stringify([list,select.value,changeTab]);if(hash===reviewHash)return;reviewHash=hash;
 if(changeTab==='rejected'){markup('changes',list.map(r=>`<details class="change"><summary>Reverted · ${esc(r.section || 'Mix test')}<small>${esc(r.verification || 'Not kept in the final mix')}</small></summary><div class="change-body">${(r.parameters || []).map(p=>`<p>${esc(p.track)} · ${esc(p.device)} · ${esc(p.param)}: ${esc(p.old_display || p.old_value)} → ${esc(p.new_display || p.new_value)}<br>${esc(p.reason)}</p>`).join('')}</div></details>`).join('') || '<p class="muted">No reverted attempts recorded for this run.</p>');return;}
 const groups=new Map();for(const item of list){if(select.value && item.channel!==select.value)continue;const key=item.channel+' · '+item.device;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(item);}
 markup('changes',[...groups].map(([name,items])=>`<details class="change"><summary>${esc(name)}<small>${items.length} ${items.length===1?'change':'changes'} · ${esc(items.map(x=>x.kind==='added'?`Added device · ${(x.settings || []).length} recorded settings`:x.param).join(', '))}</small></summary><div class="change-body"><div class="diff heading"><span>Parameter</span><span>Before</span><span>After</span></div>${items.map(i=>`<div class="diff"><span>${esc(i.param)}</span><span>${esc(i.before)}</span><span>${esc(i.after)}</span></div>${(i.settings || []).map(p=>`<div class="diff"><span>${esc(p.param)}</span><span>New device</span><span>${esc(p.value)}</span></div>`).join('')}<dl class="proof"><dt>Why</dt><dd>${esc(i.reason)}</dd><dt>Check</dt><dd>${esc(i.verification || 'Saved in the final snapshot. No detailed verification recorded.')}${i.section?'<br>'+esc(i.section):''}</dd></dl>`).join('')}</div></details>`).join('') || `<p class="muted">${r.ready?'No net parameter changes in this selection.':'Changes appear when an end snapshot is saved.'}</p>`);
}
$('kept').onclick=()=>{changeTab='kept';$('kept').setAttribute('aria-pressed','true');$('rejected').setAttribute('aria-pressed','false');renderChanges(state);};$('rejected').onclick=()=>{changeTab='rejected';$('kept').setAttribute('aria-pressed','false');$('rejected').setAttribute('aria-pressed','true');renderChanges(state);};$('channel-filter').onchange=()=>renderChanges(state);
$('restore').onclick=()=>action(async()=>{const r=await api('/api/restore/plan',{});restoreToken=r.token;$('restore-description').textContent=`Restore ${r.count} parameter settings to the original snapshot?`;$('restore-dialog').showModal();});
$('cancel-restore').onclick=()=>$('restore-dialog').close();
$('confirm-restore').onclick=()=>{$('restore-dialog').close();$('restore-final-dialog').showModal();};
$('cancel-restore-final').onclick=()=>$('restore-final-dialog').close();
$('restore-final').onclick=()=>action(async()=>{await api('/api/restore',{token:restoreToken});$('restore-final-dialog').close();message('Original settings restored in Live.');});
function timeLeft(progress,active){if(!active)return '';const minutes=progress?.eta_minutes;if(!minutes)return 'estimating time…';return minutes>=60?`about ${Math.floor(minutes/60)} h ${minutes%60} min left`:`about ${minutes} min left`;}
// Internal work units are revised while MARA discovers how many channels, effects
// and review rounds the project needs.  They are useful for the ETA, but an old
// checkpoint can otherwise make the percentage look ahead of the visible phase.
// Never let the public bar enter a later milestone before that milestone starts.
const PHASE_PROGRESS_CAP={master:44,channels:64,character:74,effects:84,final:94,preview:99};
function shownProgress(percent,phase,status){
 if(status==='finished')return 100;
 const cap=PHASE_PROGRESS_CAP[phase];
 return cap===undefined?percent:Math.min(percent,cap);
}
function progressIsCapped(percent,phase,status){return shownProgress(percent,phase,status)!==percent;}

function isConnectedProject(s){return !!s.open_set && s.following_live!==false;}
function render(s){const w=s.workflow || {},status=w.status || 'idle',live=s.open_set,active=s.active || s.running;
 const projects=`<option value="__live__">${esc(s.engine?.track || s.open_set?.name || s.track || 'Open Live project')} (current)</option>`+(s.tracks || []).map(t=>`<option value="${esc(t.slug)}">${esc(t.name)}</option>`).join('');if($('project-switch').dataset.render!==projects){$('project-switch').innerHTML=projects;$('project-switch').dataset.render=projects;}$('project-switch').value=s.following_live?'__live__':s.project_id;$('project-switch').disabled=false;$('project-switch').title='Choose a project · the current run continues in its own project';
 const analysis=analysisFeedback(s);
 $('role-fields').disabled=active && !(analysis.review && analysis.held);
 $('start').hidden=false;

 $('project-name').textContent=s.track || 'No project yet';$('connection').textContent=live?'Live connected':'Waiting for Live';$('connection').classList.toggle('connected',!!live);$('status').textContent=pausedRequested && active?'Pausing safely':STATUS[status] || status;
 $('footer-project').textContent=live?.path || s.track || 'Open your set in Ableton Live';$('footer-state').textContent=active?s.activity?.text || w.activity?.text || s.phase || 'Working …':'Changes are saved with this project';
 if(!dirty){$('description').value=s.project?.description || '';$('mode').value=s.project?.mode || 'auto';}
 markup('readiness',live?`<div class="fact"><span>Live project</span><strong>${esc(live.name || s.track)}</strong></div><div class="fact"><span>Measurement taps</span><span>${live.taps} of ${live.total} channels</span></div>`:'<p>Waiting for Ableton Live. Open your set; MARA connects automatically.</p>');
 $('set-taps').hidden=!!live && live.taps>=live.total;$('set-taps').disabled=active || !live;
 if(live && live.taps >= live.total && $('message').textContent === 'Setting up measurement taps in Live …')message('');
 for(const id of ['install-bridge','discover','mode'])$(id).disabled=active;
 $('save-project').disabled=!s.track || (!dirty && active);$('save-project').textContent=active?'Save goal':'Save & analyse my mix →';$('save-status').textContent=active?(s.workflow?.live_goal_updates?'Updates guide the next planning step.':'Saved updates apply when this run resumes or the next run starts.'):'Your description guides every run.';
 const refs=(s.reference_files?.length || 0)+(s.project_references?.length || 0),taps=live && live.taps>0,connected=!!live && s.following_live!==false;
 $('analyse').disabled=active || !connected || !taps || !refs;$('analyse').textContent=status==='ready'?'Continue to the instruments →':'Analyse & mix →';
 renderUnderstanding(s,w,status,active);
 $('start').disabled=active || !connected || !taps || !refs;$('start-top').disabled=$('start').disabled;$('start-top-hint').textContent=$('start-hint').textContent;depthLocked=active;renderDepth();$('depth-mix-label').textContent=active?`Mix depth · this run: ${DEPTHS[mixDepth][0]}. Change it for the next start or resume.`:'Mix depth for the next start or resume';
 $('start-hint').textContent=active?'':!connected?'Waiting for Ableton Live.':!taps?'Set the measurement taps first (Project).':!refs?'Choose at least one reference.':
  (status==='ready' || s.understanding)?'Starts with the saved analysis.':'MARA analyses the song first, then mixes.';
 $('references-context').textContent=active && w.purpose!=='references'?'You can change the references at any time. The running mix uses the new selection from its next phase.':'Choose references for the mix. Listen before adding a file to your selection.';
 $('change-references').hidden=true;
 renderReferences(s);renderAudio(s);renderChanges(s);renderLatestChanges(s);renderCharts(s);updatePlannerCost();
 $('unmute-current').textContent=s.output?.silent===false?'Mute Current':'Unmute Current';
 $('unmute-current').disabled=!connected || w.preview_recording || ['finalizing','repairing'].includes(status);
 $('monitor-before').textContent=$('monitor-preview').hidden?'Before':'Close Before';
 $('monitor-before').disabled=!connected || !s.previews?.before || ['finalizing','repairing'].includes(status);
 $('monitor-context').textContent=s.output?.silent===false?'Listening to Current · live working passage, including changes being tested.':!monitorPlayer.paused?'Listening to Before · saved original recording. Live is muted.':'Live is muted. Unmute Current to hear what MARA is doing.';

 $('analysis-title').textContent=status==='ready' || status==='reviewing' || w.analysis_complete?'Your mix is ready to work on.':status==='analysing'?'MARA is understanding your mix.':'Understand first. Then improve.';
 markup('analysis-info',`<div class="fact"><span>Original preview</span><span>${s.previews?.before?'Recorded · ready to hear':'Pending'}</span></div><div class="fact"><span>References</span><span>${refs} selected</span></div><p>${esc(s.song || s.style || 'Instrument roles and arrangement details appear after analysis.')}</p>`);
 $('analysis-status').hidden=!analysis.running && !w.error;
 $('analysis-status-title').textContent=analysis.title;$('analysis-progress').textContent=w.error || analysis.detail;
 $('analysis-elapsed').textContent=analysis.running?`${analysis.elapsed} elapsed`:'';
 $('analysis-guidance').textContent=analysis.hint;$('analysis-next').textContent=analysis.review?'':analysis.next;
 $('analysis-meter').hidden=analysis.review || !analysis.running;
 const ap=analysis.progress;$('analysis-meter').classList.toggle('indeterminate',ap===null);$('analysis-bar').style.width=ap===null?'30%':ap+'%';
 if(ap===null)$('analysis-meter').removeAttribute('aria-valuenow');else $('analysis-meter').setAttribute('aria-valuenow',ap);
 $('analysis-review-actions').hidden=!analysis.review;$('hold-review').hidden=analysis.held;
 $('continue-review').textContent=rolesDirty?'Save roles & continue →':'Continue now →';
 const phase=analysis.running?'setup':status==='finalizing'?'preview':phaseOf(s.phase || w.phase || '');
 // Keep the complete workflow visible: the six milestones are a stable map of
 // the run, even when the optional detailed character pass is skipped.
 const shown=Object.entries(PHASES),index=shown.findIndex(([k])=>k===phase);
 const phaseLabel=index>=0?`Phase ${index+1} of ${shown.length} · ${PHASES[phase]}`:'Preparing your mix';
 $('mix-stage').textContent=phaseLabel;$('mix-stage-purpose').textContent=PHASE_PURPOSE[phase] || analysis.hint;
 markup('mix-phases',shown.map(([key,name],i)=>{const complete=status==='finished' || i<index,current=status!=='finished' && key===phase,label=complete?'Done':current?(status==='paused'?'Paused':status==='pausing'?'Pausing':status==='interrupted' || status==='failed'?'Needs attention':'In progress'):'Upcoming';return `<li class="phase-item ${complete?'complete':current?'current':'upcoming'}"${current?' aria-current="step"':''}><span class="phase-mark" aria-hidden="true">${complete?'✓':i+1}</span><span class="phase-name">${esc(name)}</span><span class="phase-state">${label}</span></li>`;}).join(''));

 $('mix-title').textContent=status==='paused'?'Your run is paused.':status==='failed' || status==='interrupted'?'This run needs attention.':status==='finalizing'?'Preparing your result.':'MARA is working on your mix.';
 $('mix-description').textContent=w.error || s.activity?.text || w.activity?.text || s.phase || 'Your progress is saved at each safe checkpoint.';
 const p=s.progress || w.progress,rawPercent=s.display_progress?.percent ?? 0,percent=shownProgress(rawPercent,phase,status),headerPhase=phaseOf(s.engine?.phase || w.phase || phase),rawHeaderPercent=s.engine?.active?(s.engine.display_progress?.percent ?? rawPercent):rawPercent,hp=shownProgress(rawHeaderPercent,headerPhase,status),finalising=headerPhase==='final',headerCapped=progressIsCapped(rawHeaderPercent,headerPhase,status),capped=progressIsCapped(rawPercent,phase,status);$('header-progress-bar').style.width=hp+'%';const headerEta=headerCapped?'':finalising?'final validation in progress':timeLeft(s.engine?.active?s.engine.display_progress:s.display_progress,s.engine?.active || active);$('header-progress-label').textContent=hp+'%'+(headerEta?' · '+headerEta:'');$('header-progress').style.visibility=s.engine?.active || active || s.checkpoint || status==='finished'?'visible':'hidden';$('header-progress').setAttribute('aria-label',`${hp}% complete. Show mix progress.`);$('progress-bar').style.width=percent+'%';$('progress-bar').parentElement.setAttribute('aria-valuenow',percent);const eta=capped?'':finalising?'final validation in progress':timeLeft(s.display_progress,active);$('progress-label').textContent=p?`${percent}%${eta?' · '+eta:''} · ${phaseLabel}`:'Waiting for the next step';
 const headerState=s.engine?.active && s.following_live===false?{...s.engine,active:true}:s,headerAnalysis=analysisFeedback(headerState);
 $('header-progress-bar').parentElement.hidden=headerAnalysis.review;
 $('header-progress-bar').parentElement.classList.toggle('indeterminate',headerAnalysis.running && headerAnalysis.progress===null);
 if(headerAnalysis.running){$('header-progress-bar').style.width=headerAnalysis.progress===null?'30%':headerAnalysis.progress+'%';$('header-progress-label').textContent=headerAnalysis.review?headerAnalysis.detail:`${headerAnalysis.counter || headerAnalysis.title} · ${headerAnalysis.elapsed}`;$('header-progress').setAttribute('aria-label',headerAnalysis.detail+'. Show analysis progress.');}
 $('progress-bar').parentElement.classList.toggle('indeterminate',analysis.running && analysis.progress===null);
 if(analysis.running){$('progress-bar').style.width=ap===null?'30%':ap+'%';if(ap===null)$('progress-bar').parentElement.removeAttribute('aria-valuenow');else $('progress-bar').parentElement.setAttribute('aria-valuenow',ap);$('progress-label').textContent=analysis.detail+' · '+analysis.elapsed+' elapsed';}
 $('pause').hidden=!active;$('pause').disabled=pausedRequested;$('resume').hidden=active || !s.checkpoint || status==='ready';if(!active)pausedRequested=false;
 markup('events',(s.events || []).slice(-25).map(e=>`<div class="event">${esc(e.text || e.message || e)}</div>`).join(''));$('log').textContent=(s.log || []).join('\n');
 const done=status==='finished',repair=status==='preview_failed';$('result-title').textContent=done?'Your mix run is finished.':status==='restored'?'Original restored in Live.':repair?'Mix complete. Preview needs attention.':active?'Changes so far.':'Your saved mix result.';
 $('result-description').textContent=done?'Before and After are ready to hear. The MARA mix is already in Live.'+(w.error?' '+w.error:''):status==='restored'?'The original settings are back in Live. You can still listen to the saved comparison.':repair?'The mix is saved. Repair the missing audio before comparing.':w.error || 'This run is not marked complete. Review its status in the Mix stage.';
 const quality=w.quality_check,hasAssessment=quality?.schema===1;
 $('quality-check').hidden=!w.mix_complete;
 markup('quality-check',`<h2>Mix assessment</h2>${hasAssessment?`<p><strong>${esc(quality.summary)}</strong></p><p>${esc(quality.benefit_text)}</p><details><summary>What was checked</summary><dl class="remaining-list">${(quality.checks || []).map(c=>`<div><dt>${esc(c.name)}</dt><dd>${esc(c.text)}</dd></div>`).join('')}</dl><p class="muted">${esc(quality.basis)} · ${quality.samples} measurements. ${quality.other_passages_checked || 0} other passages checked during changes.</p></details><p class="muted">Automatic measurements do not establish audible improvement or artistic quality. Reference differences can be intentional.</p>`:'<p class="muted">This saved run has no comparable final assessment. The next complete run checks automatically.</p>'}`);
 $('repair-box').hidden=!repair && status!=='repairing';$('repair-description').textContent=w.error || 'Record the missing preview without repeating the mixing pass.';$('repair').disabled=active;
 $('restore').disabled=active || !s.review?.ready || s.review?.partial || !connected || status==='restored';$('new-run').disabled=active;
 if(connected && live.taps===0)cleanedProject=undefined;
 const cleaned=connected && (live.taps===0 || cleanedProject===s.project_id);
 $('cleanup').hidden=!w.mix_complete;
 $('remove-mara').disabled=removingMara || active || !!s.engine?.active || !connected || cleaned;
 $('remove-mara').textContent=removingMara?'Removing MARA taps…':cleaned?'MARA taps removed':'Remove all MARA taps';
 $('cleanup-status').textContent=cleaned?'Your mix processing and saved previews are kept. Set taps again before another run.':!connected?'Open this result’s project in Live to remove its MARA measurement devices.':'Removes measurement devices from Live. Keeps your mix processing and saved previews.';
 markup('measurement-list',(s.measurement_groups || []).map(g=>{const values=g.values || [],start=values[0],last=values.at(-1);return `<article class="measurement"><h3>${esc(PHASES[g.phase] || g.phase)} · ${esc(g.section)}</h3><p class="muted">${esc(g.scoring)} · this passage only</p><div class="metric">Start ${start?.toFixed(2) ?? '—'} → Last ${values.length>1?last.toFixed(2):'—'}</div><p class="muted">${esc(g.verification || 'No verified change recorded for this measurement.')}</p></article>`;}).join('') || '<p class="muted">No comparable measurements recorded yet.</p>');
 const resultLabels={loudness:'Loudness',dynamics:'Dynamics',width:'Stereo width',low_side:'Low-end stereo',clipping:'Peak level',tonal:'Tone balance',masking:'Masking'};
 markup('remaining','<dl class="remaining-list">'+Object.entries(resultLabels).map(([key,name])=>`<div><dt>${name}</dt><dd>${esc((s.explain?.[key] || 'Awaiting measurement').replace(/bass -([\d.]+) dB more stereo/,'bass $1 dB less stereo'))}</dd></div>`).join('')+'</dl>');
}
let refreshing=false,followingStage;
async function refresh(){if(refreshing)return;refreshing=true;try{const s=await api('/api/state');state=s;
 if(projectId!==s.project_id){projectId=s.project_id;dirty=false;rolesDirty=false;rolesRevision=undefined;workflowKey=undefined;reviewHash='';audioId='';baseAudio='';hiddenAudio=false;referencePlayer.pause();$('reference-player-box').hidden=true;player.pause();player.removeAttribute('src');player.load();monitorPlayer.pause();monitorPlayer.removeAttribute('src');monitorPlayer.dataset.source='';monitorPlayer.load();$('monitor-preview').hidden=true;}
 const key=[s.project_id,s.workflow?.id,s.workflow?.status].join('|');if(key!==workflowKey){const previous=workflowKey;workflowKey=key;const next=s.workflow?.stage || 'project';if(!previous)show(document.body.dataset.demoStage || 'project');else if(stage===followingStage)show(next);followingStage=next;}
 render(s);await loadRoles(s);
 }catch(e){$('connection').textContent='Connection interrupted';$('connection').classList.remove('connected');message(e.message,true);}finally{refreshing=false;}}
api('/api/setup').then(r=>{$('library-path').value=r.library;setupPlanner(r.planner_settings);}).catch(e=>message(e.message,true));show('project');refresh();setInterval(refresh,1500);
