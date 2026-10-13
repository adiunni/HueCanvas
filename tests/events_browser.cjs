// Dashboard-handler integration with a small DOM, not a physical browser render.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const page=fs.readFileSync('app/static/index.html','utf8');
const elements=new Map();
function element(id='') {
  if(id&&elements.has(id))return elements.get(id);
  const node={id,type:'',value:'',checked:false,style:{},children:[],handlers:{},files:[],
    append(...children){this.children.push(...children);},replaceChildren(...children){this.children=children;},
    addEventListener(name,handler){this.handlers[name]=handler;},pause(){this.paused=true;}};
  if(id){
    const tag=page.match(new RegExp('<[^>]+id="'+id+'"[^>]*>'))?.[0];
    assert.ok(tag,'Actual HTML contains '+id);
    node.type=tag.match(/type="([^"]+)"/)?.[1]||'';
    node.value=tag.match(/value="([^"]*)"/)?.[1]||'';
    node.checked=/\schecked/.test(tag);elements.set(id,node);
  }
  return node;
}
const cue={id:'candidate',track_id:'manual',type:'FINAL_IMPACT',position:25,source:'offline',confidence:.9,review:'pending',
  effect:'CINEMATIC_IMPACT',enabled:true,envelope:{attack:.25,hold:.6,decay:5,intensity:.65,pre_dim:0}};
const options={enabled:false,output:'rest',reduced_motion:true,ambient_level:.3,confidence:.7,output_latency_ms:0,
  live_enabled:false,live_trigger:false,beat_align:false,live_delay_ms:0,follow_music:false};
const data={options,transport:{track_id:'manual',position:0,playing:false,rate:1},events:[cue],live_candidates:[],analysis:null,processing_ms:0};
const requests=[];
async function api(path,method='GET',body) {
  requests.push({path,method,body:body&&JSON.parse(JSON.stringify(body))});
  if(path==='/events/cue'){data.events=[{...body,id:body.id||'new'}];return structuredClone(data.events[0]);}
  if(path==='/events/options'){data.options=body;return {};}
  if(path==='/events/transport'){data.transport=body;return {};}
  if(path==='/events/preview')return {palette:['#ff0000','#0000ff'],duration:5,samples:Array(101).fill({level:.6})};
  if(path==='/events/audition')return {ok:true};
  return structuredClone(data);
}
const timers=[];
const context=vm.createContext({document:{hidden:false,getElementById:element,createElement:()=>element()},api,
  safe:fn=>fn,notice:()=>{},queueWrite:fn=>fn(),pendingWrites:Promise.resolve(),setInterval:fn=>timers.push(fn),
  requestAnimationFrame:()=>1,cancelAnimationFrame:()=>{},performance:{now:()=>1000},FormData,URL});
vm.runInContext(fs.readFileSync('app/static/events.js','utf8'),context);
(async()=>{
  assert.equal(requests.length,0,'Opening app starts no analysis, capture, playback or hardware');
  element('eventsPanel').open=true;await element('eventsPanel').handlers.toggle();
  assert.equal(element('eventReduced').checked,true);
  assert.equal(element('eventList').children.length,1);
  await element('eventList').children[0].children[1].onclick(); // Edit candidate
  assert.equal(element('cuePosition').value,25);
  element('cuePosition').value='25.125';element('cueEnabled').checked=false;
  await element('cueSave').onclick();
  const saved=requests.find(r=>r.path==='/events/cue').body;
  assert.equal(saved.id,'candidate');assert.equal(saved.position,25.125);assert.equal(saved.enabled,false);
  await element('eventList').children[0].children[2].onclick(); // Accept edited candidate
  assert.equal(data.events[0].review,'accepted');assert.equal(data.events[0].enabled,true);
  element('eventPosition').value='24';element('eventRate').value='1.5';
  await element('eventPlay').onclick();
  assert.equal(data.transport.position,24);assert.equal(data.transport.rate,1.5);assert.equal(data.transport.playing,true);
  await element('eventPause').onclick();assert.equal(data.transport.playing,false);
  element('eventPosition').value='23';await element('eventSeek').onclick();assert.equal(data.transport.seek,true);
  await element('cueNew').onclick();assert.equal(element('cuePosition').value,5);
  await element('cuePreview').onclick();assert.equal(element('eventPreview').children.length,4);
  assert.ok(!requests.some(r=>/audio\/start|control\/start|audition/.test(r.path)));
  await element('cueAudition').onclick();assert.ok(requests.some(r=>r.path==='/events/audition'));
  console.log('Event editor, acceptance, transport and preview handlers passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
