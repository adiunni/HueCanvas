let state, initialized=false, polling=false, audioStarting=false, pendingWrites=Promise.resolve(), setupOpened=false;
const $=id=>document.getElementById(id);
const RADIOS=['mode','flash_trigger','flash_pattern','pulse_palette','white_response','white_pattern'];
const MUSIC_MODES=['beat_gentle','beat_flash','music_brightness'];

function notice(message){$('notice').textContent=message;$('notice').style.display='block';clearTimeout(notice.timer);notice.timer=setTimeout(()=>$('notice').style.display='none',6000)}
async function api(path,method='GET',body){const options={method};if(body instanceof FormData)options.body=body;else if(body){options.headers={'Content-Type':'application/json'};options.body=JSON.stringify(body)}const response=await fetch('/api'+path,options);const data=await response.json();if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));return data}
function safe(fn){return async(...args)=>{try{await fn(...args)}catch(e){notice(e.message)}}}
function queueWrite(task){const result=pendingWrites.then(task);pendingWrites=result.catch(()=>{});return result}

// ---- Settings: every control names its setting; radios use the setting as their name.
function readSettings(){
  const settings={...state.settings};
  document.querySelectorAll('[data-setting]').forEach(el=>{
    const key=el.dataset.setting;
    settings[key]=el.type==='checkbox'?(el.checked?el.dataset.on:el.dataset.off):typeof state.settings[key]==='number'?Number(el.value):el.value;
  });
  RADIOS.forEach(name=>{const checked=document.querySelector(`input[name="${name}"]:checked`);if(checked)settings[name]=checked.value});
  return settings;
}
function writeSettings(settings){
  document.querySelectorAll('[data-setting]').forEach(el=>{
    const value=settings[el.dataset.setting];
    if(el.type==='checkbox')el.checked=value===el.dataset.on;else el.value=value;
  });
  RADIOS.forEach(name=>document.querySelectorAll(`input[name="${name}"]`).forEach(input=>input.checked=input.value===String(settings[name])));
  updateOutputs();
}
function updateOutputs(){document.querySelectorAll('output[data-for]').forEach(out=>{const input=document.querySelector(`[data-setting="${out.dataset.for}"]`);if(input)out.value=input.value})}
const saveSettings=safe(async()=>{const settings=readSettings();state.settings=settings;await queueWrite(()=>api('/settings','PUT',settings))});

// ---- Show only the options that apply to the current choices.
function updateVisibility(){
  const s=readSettings(),listening=Boolean(state.audio?.running);
  // White floor and pattern only matter when the whites are actually following music.
  const whitesFollow=s.mode==='music_brightness'||(s.white_response==='music'&&(MUSIC_MODES.includes(s.mode)||listening));
  const tests={modes:v=>v.split(' ').includes(s.mode),triggers:v=>v.split(' ').includes(s.flash_trigger),
    palette:v=>s.pulse_palette===v,white:()=>whitesFollow,tempo:v=>s.bpm_source===v};
  document.querySelectorAll('[data-modes],[data-triggers],[data-palette],[data-white],[data-tempo]').forEach(el=>{
    el.hidden=!Object.entries(tests).every(([key,test])=>el.dataset[key]===undefined||test(el.dataset[key]));
  });
  $('modeHelp').textContent={static:'Album colors, set once.',crossfade:'Colors drift slowly through the album palette.',
    rotate:'Each bulb steps through the album palette.',breathe:'Album colors with a slow brightness swell.',
    beat_gentle:'Color bulbs swell with the music. Listening starts when you press Start.',
    beat_flash:'Color bulbs flash sharply with the music. Listening starts when you press Start.',
    music_brightness:'Every dimmable bulb follows the music using brightness only; colors stay as they are.'}[s.mode]||'';
  $('triggerHelp').textContent={bpm:'Pulses land on the detected beat. Until the tempo locks, they follow drum hits.',
    percussion:'Pulses on drum hits, faster during rolls.',
    bands:'Kicks drive the pulses. When the kick drops out, snares and claps take over.',
    frequency:'Pulses when the bass, or the mids while the bass is quiet, crosses a volume level.'}[s.flash_trigger]||'';
  $('whiteHelp').textContent=s.mode==='music_brightness'?'White lamps follow the music with slow brightness changes.'
    :s.white_response==='steady'?'White lamps hold a steady brightness.'
    :MUSIC_MODES.includes(s.mode)?'White lamps change slowly with the music, at most every two seconds.'
    :'White lamps follow the music while HueCanvas is listening; otherwise they stay steady.';
}

// ---- Tempo readout and transport.
function formatBpm(bpm){return Number.isInteger(bpm)?String(bpm):bpm.toFixed(1)}
function updateTempo(){
  const audio=state.audio||{},s=state.settings,manual=s.bpm_source==='manual';
  let value='—',text;
  if(manual){value=formatBpm(Number(s.bpm));text='Manual tempo.'}
  else if(!audio.running)text=MUSIC_MODES.includes(s.mode)?'Listening starts when you press Start.':'Not listening.';
  else if(audio.warming_up)text='Listening… the first tempo arrives in about 6 seconds.';
  else if(!audio.bpm)text=s.flash_trigger==='bpm'?'Finding the beat… pulses follow drum hits meanwhile.':'Finding the beat…';
  else{value=formatBpm(audio.bpm);text=(audio.tempo_locked?'Locked':'Settling')+(audio.tempo_scale!==1?` · ${audio.tempo_scale>1?'×2':'½'} applied for this song`:'')+'. Wrong by double or half? Use ½ or ×2.'}
  $('tempoValue').textContent=value;$('tempoState').textContent=text;
  $('tempoHalf').disabled=$('tempoDouble').disabled=manual||!audio.bpm;
  $('tempoPill').hidden=!audio.running;
  $('tempoPill').textContent=audio.running?`♪ ${manual?formatBpm(Number(s.bpm)):audio.bpm?formatBpm(audio.bpm):'…'} BPM`:'';
}
function updateSelection(){
  const enabled=state.lights.filter(light=>light.enabled).length;
  $('selectionHelp').hidden=enabled>0;
  $('selectionHelp').textContent='No bulbs are enabled yet. Enable them under Your lights, then press Start.';
  $('start').disabled=!enabled;
  $('enableAll').hidden=state.lights.length!==4;$('enableAll').disabled=enabled===4;
}
function updateAudioStatus(){
  const audio=state.audio;if(!audio)return;
  $('hitStatus').textContent=audio.running?`Input ${audio.input_db??'—'} dBFS · ${audio.percussion_hit_count||0} drum hits in the last 3 s${audio.last_percussion?' · last: '+(audio.last_percussion.family||'drum'):''}${audio.percussion_pattern?.roll_active?' · roll '+audio.percussion_pattern.roll_rate+'/s':''}${audio.drop_active?' · drop':''}`:'';
  $('audioStop').disabled=!audio.running;$('audioListen').disabled=audioStarting;
  $('audioListen').textContent=audio.running?'Restart listening':'Listen now';
  $('audioLevel').value=audio.rms||0;
  $('audioStatus').textContent=!audio.running?audio.error||'Not listening. No audio is recorded to disk.'
    :audio.capture_state==='processing_error'||audio.capture_state==='stalled'?audio.error||'Audio capture stalled. Restart listening.'
    :audio.capture_state==='no_signal'?`Input is arriving but silent (${audio.input_db} dBFS). Play music and check routing above.`
    :`Listening to ${audio.device}.`;
}
const STATUS={running:'Running',stopped:'Stopped',paused:'Paused',starting:'Starting',waiting_audio:'Waiting for music'};

// ---- Palette and lights.
function renderPalette(){const root=$('palette');root.replaceChildren();state.palette.forEach((color,i)=>{const label=document.createElement('label'),input=document.createElement('input'),text=document.createElement('span');input.type='color';input.value=color;text.textContent=color;input.onchange=safe(async()=>{state.palette[i]=input.value;await api('/palette','PUT',{colors:state.palette});text.textContent=input.value});label.append(input,text);root.append(label)})}
const roles=['','COLOR_LEFT','COLOR_RIGHT','WHITE_LEFT','WHITE_RIGHT'];
const ROLE_NAMES={'':'Unassigned',COLOR_LEFT:'Color · left',COLOR_RIGHT:'Color · right',WHITE_LEFT:'White · left',WHITE_RIGHT:'White · right'};
function renderLights(){const root=$('lights');root.replaceChildren();state.lights.forEach(light=>{const card=document.createElement('div');card.className='bulb';card.dataset.id=light.id;const orb=document.createElement('div');orb.className='orb';const name=document.createElement('input');name.type='text';name.value=light.name;name.setAttribute('aria-label','Bulb name');const capability=document.createElement('small');capability.textContent=(light.color?'Color':'White')+(light.dimming?' · dimmable':'')+(light.temperature?' · temperature':'');const enabled=document.createElement('input');enabled.type='checkbox';enabled.checked=light.enabled;const label=document.createElement('label');label.append(enabled,document.createTextNode(' Use this bulb'));const select=document.createElement('select');select.setAttribute('aria-label','Bulb role');roles.forEach(role=>{const option=document.createElement('option');option.value=role;option.textContent=ROLE_NAMES[role];option.disabled=role.startsWith('COLOR')&&!light.color;select.append(option)});select.value=light.role||'';const save=safe(async()=>{const settings={name:name.value,enabled:enabled.checked,role:select.value||null};await queueWrite(()=>api('/lights/'+light.id,'PUT',settings));await refresh(false)});name.onchange=save;enabled.onchange=save;select.onchange=save;const identify=document.createElement('button');identify.textContent='Identify';identify.onclick=safe(async()=>{await api('/lights/'+light.id+'/identify','POST');notice('Identify command sent'+(state.mock?' (simulated)':''))});card.append(orb,name,capability,label,select,identify);root.append(card)});updateOrbs()}
function updateOrbs(){document.querySelectorAll('.bulb').forEach(card=>{const light=state.lights.find(l=>l.id===card.dataset.id);if(!light)return;const i=light.role==='COLOR_RIGHT'?1:0;card.style.setProperty('--glow',light.color?(light.preview_color||state.palette[i%state.palette.length]):'#ffe1a8');card.style.setProperty('--power',light.on?Math.max(.15,light.brightness/100):.1)})}

// ---- Music artwork events.
let musicEvents=null,lastMusicRevision=-1;
function renderMusicUpdate(update){if(update.revision<lastMusicRevision)return;lastMusicRevision=update.revision;const track=update.track;$('musicStatus').textContent=update.error||(track?`${track.artist} — ${track.track}${track.album?' · '+track.album:''}`:'');if(update.artwork!==refresh.lastArtwork){$('cover').style.backgroundImage=update.artwork?`url("${update.artwork}")`:'';$('cover').replaceChildren();if(!update.artwork){const span=document.createElement('span');span.textContent=track?'Loading Apple Music artwork…':'Drop an album into your evening';$('cover').append(span)}refresh.lastArtwork=update.artwork}if(update.palette&&JSON.stringify(update.palette)!==refresh.lastPalette){state.palette=update.palette;renderPalette();refresh.lastPalette=JSON.stringify(update.palette)}}

async function refresh(full=false){
  state=await api('/state');
  $('status').textContent=(state.mock?'Simulation':'Live Hue')+' · '+(STATUS[state.status]||state.status);
  $('connectionInfo').textContent=state.error||state.playback_message||(state.streaming?'Entertainment streaming active':state.streaming_key_configured?'REST + Entertainment ready':'REST ready · add HUE_CLIENT_KEY for beat effects')+(state.music_enabled?' · Music sync enabled':'');
  if(state.error||state.playback_message)$('status').title=state.error||state.playback_message;
  $('bridge_id').style.display=state.bridge?.verify_tls?'':'none';
  if(state.bridge){if(!$('ip').value)$('ip').value=state.bridge.ip;if(!$('bridge_id').value)$('bridge_id').value=state.bridge.id}
  const lightIds=state.lights.map(light=>light.id).join('|'),lightsChanged=lightIds!==refresh.lastLightIds;refresh.lastLightIds=lightIds;
  if(!initialized||full){writeSettings(state.settings);renderPalette();renderLights();initialized=true}else if(lightsChanged)renderLights();else updateOrbs();
  updateSelection();updateVisibility();updateTempo();updateAudioStatus();
  renderMusicUpdate({revision:state.music_revision||0,track:state.music_track,artwork:state.artwork,palette:state.palette,error:state.music_error});
  if(state.music_enabled&&!musicEvents){musicEvents=new EventSource('/api/music/events');musicEvents.onmessage=event=>{try{renderMusicUpdate(JSON.parse(event.data))}catch(e){notice('Music update unavailable: '+e.message)}}}
}

// ---- Control wiring.
document.querySelectorAll('[data-setting],input[type=radio]').forEach(el=>{
  el.addEventListener('input',()=>{updateOutputs();updateVisibility()});
  el.addEventListener('change',()=>{
    // Drum triggers start with drum stems on, so notes do not pulse.
    if(el.name==='flash_trigger'&&['percussion','bands'].includes(el.value))document.querySelector('[data-setting="drum_isolation"]').checked=true;
    if(el.dataset.setting==='palette_size')return;
    updateVisibility();saveSettings().then(updateTempo);
  });
});
$('palette_size').addEventListener('change',safe(async()=>{await saveSettings();await refresh();notice('Palette: '+$('palette_size').value+' colors')}));
$('upload').onchange=safe(async()=>{if(!$('upload').files[0])return;const form=new FormData();form.append('file',$('upload').files[0]);await api('/artwork?count='+$('palette_size').value,'POST',form);await refresh(true);notice('Album palette applied')});
document.querySelectorAll('[data-action]').forEach(button=>button.onclick=safe(async()=>{
  await pendingWrites;const action=button.dataset.action;
  if(action==='start'&&MUSIC_MODES.includes(state.settings.mode)&&!state.audio?.running)$('status').textContent='Starting · listening to BlackHole…';
  try{await api('/control/'+action,'POST')}
  catch(e){if(action==='start'&&/BlackHole|audio input/i.test(e.message))$('setupPanel').open=true;throw e}
  await refresh(action==='refresh'||action==='restore');
}));
async function scaleTempo(factor){await api('/audio/tempo','POST',{factor});await refresh()}
$('tempoHalf').onclick=safe(()=>scaleTempo(.5));$('tempoDouble').onclick=safe(()=>scaleTempo(2));
$('enableAll').onclick=safe(async()=>{const button=$('enableAll');button.disabled=true;try{for(const light of state.lights){await queueWrite(()=>api('/lights/'+light.id,'PUT',{name:light.name,enabled:true,role:light.role}))}await refresh(true);notice('All four bulbs enabled. Press Start.')}finally{updateSelection()}});
$('spectrumPanel').addEventListener('toggle',()=>window.dispatchEvent(new Event('resize')));

// ---- Setup: bridge, routing, input.
$('discover').onclick=safe(async()=>{const bridges=await api('/bridges');if(!bridges.length)return notice('No bridges found; enter the IP manually');$('ip').value=bridges[0].internalipaddress;$('bridge_id').value=bridges[0].id;notice('Bridge found; press its physical button, then Pair')});
$('pair').onclick=safe(async()=>{await api('/pair','POST',{ip:$('ip').value,bridge_id:$('bridge_id').value});await refresh(true);notice('Paired successfully')});
async function loadAudioDevices(){const result=await api('/audio/devices'),select=$('audioDevice');select.replaceChildren();result.devices.forEach(device=>{const option=document.createElement('option');option.value=device.id;option.textContent=device.name;select.append(option)});const loopback=result.devices.find(device=>device.loopback);if(loopback)select.value=String(loopback.id);if(!result.devices.length){const option=document.createElement('option');option.value='';option.textContent='No audio inputs available';select.append(option)}return result.devices}
async function checkRouting(){const route=await api('/audio/routing'),select=$('audioOutput'),old=select.value;select.replaceChildren();(route.candidates||[]).forEach(device=>{const option=document.createElement('option');option.value=device.id;option.textContent=device.name;select.append(option)});if([...select.options].some(o=>o.value===old))select.value=old;select.hidden=!route.available;$('audioRoute').hidden=!route.available;$('audioRoute').disabled=!route.candidates?.length||route.routed;$('routingStatus').textContent=!route.available?route.message||'Set your music output to a loopback device to capture system audio.':route.routed?`Mac sound output: ${route.output} · includes BlackHole.`:route.message;return route}
$('audioRoute').onclick=safe(async()=>{await api('/audio/routing','POST',{device_id:Number($('audioOutput').value)});await checkRouting();notice('Mac sound now uses the Multi-Output Device.')});
$('audioRoutingRefresh').onclick=safe(checkRouting);
$('audioDevices').onclick=safe(async()=>{await loadAudioDevices();await checkRouting()});
$('audioListen').onclick=safe(async()=>{if(audioStarting)return;audioStarting=true;$('audioListen').disabled=true;try{const id=$('audioDevice').value;await api('/audio/start','POST',{device_id:id===''?null:Number(id)});await refresh();await checkRouting();notice('Listening. No audio is recorded.')}finally{audioStarting=false;updateAudioStatus()}});
$('audioStop').onclick=safe(async()=>{await api('/audio/stop','POST');await refresh()});
$('music').onclick=safe(async()=>{const track=await api('/music');notice(track.available?`${track.artist} — ${track.track} · ${track.album}`:track.reason)});

// Open Setup once on load when something needs attention.
async function initialSetupCheck(){
  const [devices,route]=await Promise.all([loadAudioDevices().catch(()=>[]),checkRouting().catch(()=>null)]);
  const problems=[];
  if(!devices.some(device=>device.loopback))problems.push('no BlackHole input');
  if(route&&route.available&&!route.routed)problems.push('Mac sound is not routed through BlackHole');
  if(state&&!state.mock&&!state.lights.length)problems.push('no bridge paired');
  $('setupSummary').textContent=problems.length?'Needs attention: '+problems.join(', '):'Audio input, bridge and Apple Music';
  if(problems.length&&!setupOpened){$('setupPanel').open=true;setupOpened=true}
}
safe(async()=>{await refresh(true);await initialSetupCheck()})();
setInterval(async()=>{if(polling||document.hidden)return;polling=true;try{await refresh()}catch(e){$('status').textContent='Disconnected'}finally{polling=false}},1500);
setInterval(()=>checkRouting().catch(()=>{}),5000);
