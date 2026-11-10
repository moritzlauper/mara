import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../mixmatch/static/charts.js',import.meta.url),'utf8');
const {analysisFeedback,emptyMeasurements}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

test('waiting gives actionable feedback without fabricated progress or ETA',()=>{
 const s={active:true,workflow:{status:'analysing',started_at:100,activity:{text:'Waiting for the audio measurements to arrive',started:120}}};
 const a=analysisFeedback(s,155);
 assert.equal(a.progress,null);assert.equal(a.elapsed,'55s');assert.match(a.hint,/audio engine/);
 assert.match(analysisFeedback(s,125).hint,/started playback/);
});
test('only actual counters produce progress and the most recent activity wins',()=>{
 const s={active:true,workflow:{status:'analysing',activity:{text:'Listening to the whole song',started:200,current:0,total:100}},activity:{text:'old message',started:100}};
 assert.equal(analysisFeedback(s,202).progress,0);
 assert.match(analysisFeedback(s,202).title,/Scanning/);
 s.workflow.activity.current=25;assert.equal(analysisFeedback(s,210).progress,25);
});
test('review countdown is replaced by an explicit hold, not a second mandatory start',()=>{
 const s={active:true,workflow:{status:'reviewing',review:{deadline:130}}};
 assert.equal(analysisFeedback(s,110).remaining,20);
 s.workflow.review.held=true;assert.equal(analysisFeedback(s,140).remaining,null);
 assert.match(analysisFeedback(s,140).hint,/on hold/);
});
test('empty history explains why Before recording has no comparisons yet',()=>{
 const s={active:true,workflow:{activity:{text:'Recording before preview · 16 bars'}}};
 assert.match(emptyMeasurements(s).text,/first reference comparison follows automatically/);
 assert.match(emptyMeasurements({active:true,workflow:{status:'mixing'}}).text,/complete measurement/);
});
