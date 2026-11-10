import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../mixmatch/static/charts.js',import.meta.url),'utf8');
const {currentInspection,referenceDistance,comparableSeries,latestReference}=await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('current work is independent of the previous reference measurement',()=>{
 const s={active:true,phase:'Effects: what does nothing or harms',section:null};
 assert.match(currentInspection(s),/^Effect checks/);
 assert.equal(currentInspection({...s,current_focus:{channel:'Piano',device:'EQ Eight',start_bar:99,end_bar:115}}),'Piano · EQ Eight · bars 99–115');
 assert.equal(currentInspection({active:true,section:'Kick, bars 99–115'}),'Kick, bars 99–115');
 assert.equal(currentInspection({...s,active:false,workflow:{status:'paused'}}),'Run paused');
 assert.equal(currentInspection({...s,workflow:{status:'finalizing'}}),'Recording the After preview');
});

test('master loudness and dynamics cannot change the tone/stereo score',()=>{
 const balance={tonal:2.4,width:0.49,masking:8.09};
 const full={...balance,loudness:1.77,dynamics:0.38,clipping:2};
 assert.equal(referenceDistance(balance),referenceDistance(full));
 assert.ok(Math.abs(referenceDistance(full)-2.89)<1e-9);
 assert.ok(Math.abs(referenceDistance({...full,tonal:3.4})-3.89)<1e-9);
 assert.equal(referenceDistance({masking:8}),null);
 const groups=[{phase:'channels',run:1,scoring:'balance',section:'Kick, bars 99–115',values:[10.98],components:[balance]},
  {phase:'final',run:1,scoring:'full',section:'Main, bars 99–115',values:[15.13],components:[full]}];
 assert.deepEqual(comparableSeries(groups).map(s=>s.points[0].reference.toFixed(2)),['2.89','2.89']);
 assert.equal(latestReference([...groups,{phase:'effects',components:[{tonal:99}]}]).parts,full);
});
