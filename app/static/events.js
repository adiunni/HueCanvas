/* Musical cues extend the existing dashboard; no capture or hardware starts here. */
(() => {
  const el = id => document.getElementById(id);
  let snapshot = null, selected = null, listSignature = '', busy = false, playerURL = null;
  let previewFrame = 0, playerSyncAt = 0;
  const optionFields = {
    enabled:'eventEnabled', output:'eventOutput', reduced_motion:'eventReduced', ambient_level:'eventAmbient',
    confidence:'eventConfidence', output_latency_ms:'eventLatency', live_enabled:'eventLive',
    live_trigger:'eventLiveTrigger', beat_align:'eventBeat', live_delay_ms:'eventLiveDelay', follow_music:'eventFollow'
  };
  const track = () => el('eventTrack').value.trim() || 'manual';
  function optionsFromForm() {
    return Object.fromEntries(Object.entries(optionFields).map(([key,id]) => {
      const input=el(id);
      return [key,input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value];
    }));
  }
  function writeOptions(options) {
    for(const [key,id] of Object.entries(optionFields)) {
      const input=el(id);
      if(input.type==='checkbox')input.checked=options[key];else input.value=options[key];
    }
  }
  function edit(cue) {
    selected=cue;
    el('cueType').value=cue?.type||'FINAL_IMPACT';
    el('cuePosition').value=cue?.position??5;
    el('cueEffect').value=cue?.effect||'CINEMATIC_IMPACT';
    el('cueEnabled').checked=cue?.enabled??true;
    for(const [name,id,defaultValue] of [['attack','cueAttack',.25],['hold','cueHold',.6],['decay','cueDecay',5],['intensity','cueIntensity',.65],['pre_dim','cuePreDim',0]])el(id).value=cue?.envelope?.[name]??defaultValue;
    el('cueEditing').textContent=cue?`Editing ${cue.source} cue · ${cue.review}${cue.user_edited?' · timing/override protected':''}`:'New manual final-impact cue.';
  }
  function cueFromForm() {
    const cue={...(selected||{}),track_id:track(),type:el('cueType').value,position:Number(el('cuePosition').value),
      effect:el('cueEffect').value,enabled:el('cueEnabled').checked,
      envelope:{attack:Number(el('cueAttack').value),hold:Number(el('cueHold').value),decay:Number(el('cueDecay').value),intensity:Number(el('cueIntensity').value),pre_dim:Number(el('cuePreDim').value)}};
    if(!selected)Object.assign(cue,{source:'manual',review:'accepted'});
    return cue;
  }
  function button(text,action) {
    const b=document.createElement('button');b.type='button';b.textContent=text;b.onclick=safe(action);return b;
  }
  function renderList(root,cues,live=false) {
    root.replaceChildren();
    if(!cues.length){const p=document.createElement('p');p.textContent=live?'No live candidates. Enable detection, then use Listen in Audio setup.':'No cues yet. Add a manual cue or analyze a WAV.';root.append(p);return;}
    for(const cue of cues) {
      const row=document.createElement('div');row.className='event-row';
      const text=document.createElement('span');
      text.textContent=`${cue.position.toFixed(2)} s · ${cue.type} · ${cue.confidence==null?'manual':Math.round(cue.confidence*100)+'%'} · ${cue.review}${!cue.enabled?' · disabled':''}${live?' · detected '+Math.round(cue.detection_delay*1000)+' ms later':''}`;
      row.append(text,button('Edit',()=>edit(cue)));
      for(const review of ['accepted','rejected'])row.append(button(review==='accepted'?'Accept':'Reject',async()=>{
        await api('/events/cue','PUT',{...cue,review,enabled:review==='accepted'});await load();
      }));
      root.append(row);
    }
  }
  async function load(full=false) {
    const data=await api('/events?track_id='+encodeURIComponent(track()));snapshot=data;
    if(data.options.follow_music&&data.transport.track_id!==track()){
      el('eventTrack').value=data.transport.track_id;edit(null);return load(full);
    }
    if(full)writeOptions(data.options);
    const signature=JSON.stringify([data.events,data.live_candidates]);
    if(signature!==listSignature){renderList(el('eventList'),data.events);renderList(el('eventLiveList'),data.live_candidates,true);listSignature=signature;}
    const t=data.transport,analysis=data.analysis;
    el('eventStatus').textContent=`${t.playing?'Running':'Paused'} cue clock · ${t.track_id} · ${t.position.toFixed(2)} s · ×${t.rate}`+
      (analysis?` · WAV ${analysis.duration.toFixed(1)} s · ${data.events.length} cues`:' · No analysis cached')+
      ` · live worker ${data.processing_ms.toFixed(1)} ms`+(data.error?' · '+data.error:'');
  }
  async function clockUpdate(playing,seek=false,position=null,rate=null) {
    const loopStart=el('eventLoopStart').value,loopEnd=el('eventLoopEnd').value;
    await queueWrite(()=>api('/events/transport','PUT',{track_id:track(),position:position??Number(el('eventPosition').value),
      playing,rate:rate??Number(el('eventRate').value),seek,
      loop_start:loopStart===''?null:Number(loopStart),loop_end:loopEnd===''?null:Number(loopEnd)}));
    el('eventFollow').checked=false;
  }
  for(const id of Object.values(optionFields))el(id).onchange=safe(async()=>{
    await queueWrite(()=>api('/events/options','PUT',optionsFromForm()));await load();
  });
  el('eventLoad').onclick=safe(async()=>{
    await clockUpdate(false,true);await load(true);edit(null);
  });
  el('eventCurrent').onclick=safe(async()=>{
    const current=await api('/state');
    if(!current.music_track?.id)throw Error('No music track has been identified yet. Enter a stable track identifier.');
    el('eventTrack').value=current.music_track.id;
    await clockUpdate(false,true);await load(true);edit(null);
  });
  el('eventAnalyze').onclick=safe(async()=>{
    const file=el('eventFile').files[0];if(!file)throw Error('Choose an authorized WAV file first.');
    const form=new FormData();form.append('file',file);el('eventAnalyze').disabled=true;
    el('eventStatus').textContent='Analyzing section changes and the ending…';
    try{const result=await api('/events/analyze?track_id='+encodeURIComponent(track()),'POST',form);await load();notice(result.cached?'Loaded cached analysis; edits preserved.':'Analysis complete. Review candidates before accepting.');}
    finally{el('eventAnalyze').disabled=false;}
  });
  el('eventFile').onchange=()=>{
    const file=el('eventFile').files[0];if(!file)return;
    const player=el('eventPlayer');player.pause();
    if(playerURL)URL.revokeObjectURL(playerURL);
    playerURL=URL.createObjectURL(file);player.src=playerURL;player.hidden=false;
  };
  const player=el('eventPlayer');
  async function syncPlayer(seek=false) {
    if(!playerURL)return;
    const loopEnd=Number(el('eventLoopEnd').value),loopStart=Number(el('eventLoopStart').value);
    if(el('eventLoopEnd').value!==''&&loopEnd>loopStart&&player.currentTime>=loopEnd){player.currentTime=loopStart;return;}
    await clockUpdate(!player.paused&&!player.ended,seek,player.currentTime,player.playbackRate);
  }
  for(const event of ['play','pause','ratechange','seeked','ended'])player.addEventListener(event,safe(()=>syncPlayer(event==='seeked')));
  player.addEventListener('timeupdate',safe(async()=>{if(performance.now()-playerSyncAt<500)return;playerSyncAt=performance.now();await syncPlayer();}));
  el('eventPlay').onclick=safe(async()=>{await clockUpdate(true);await load();});
  el('eventPause').onclick=safe(async()=>{const current=await api('/events');el('eventPosition').value=current.transport.position.toFixed(3);await clockUpdate(false);await load();});
  el('eventSeek').onclick=safe(async()=>{
    if(playerURL){player.currentTime=Number(el('eventPosition').value);player.playbackRate=Number(el('eventRate').value);}
    await clockUpdate(snapshot?.transport.playing||false,true);await load();
  });
  el('cueNew').onclick=()=>edit(null);
  el('cueMark').onclick=safe(async()=>{
    const position=playerURL?player.currentTime:(await api('/events')).transport.position;
    el('cuePosition').value=position.toFixed(3);
  });
  el('cueSave').onclick=safe(async()=>{const cue=await api('/events/cue','PUT',cueFromForm());edit(cue);await load();notice('Cue saved; reanalysis will preserve your edits.');});
  el('cueAudition').onclick=safe(async()=>{await pendingWrites;await api('/events/audition','POST',cueFromForm());notice('Effect audition started. Restore returns the original bulb state.');});
  el('cuePreview').onclick=safe(async()=>{
    const preview=await api('/events/preview','POST',cueFromForm()),root=el('eventPreview');root.replaceChildren();cancelAnimationFrame(previewFrame);
    const lights=preview.palette.slice(0,2).concat(['#ffe1a8','#ffe1a8']).map(color=>{const orb=document.createElement('span');orb.style.background=color;root.append(orb);return orb;});
    const start=performance.now();
    function draw(now){const seconds=(now-start)/1000,index=Math.min(100,Math.floor(seconds/preview.duration*100));lights.forEach((orb,i)=>orb.style.opacity=i<2?preview.samples[index].level:preview.samples[index].white_level);if(seconds<preview.duration)previewFrame=requestAnimationFrame(draw);}
    previewFrame=requestAnimationFrame(draw);
  });
  el('eventsPanel').addEventListener('toggle',safe(async()=>{if(el('eventsPanel').open)await load(true);}));
  setInterval(async()=>{if(busy||document.hidden||!el('eventsPanel').open)return;busy=true;try{await load();}catch(error){el('eventStatus').textContent=error.message;}finally{busy=false;}},1000);
})();
