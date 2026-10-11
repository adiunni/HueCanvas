// These weights are a visual experiment, independent of Hue lighting settings.
function blendSpectrumColors(colors, levels) {
  const total = levels.reduce((sum, value) => sum + Math.max(0, value), 0);
  if (!total) return {hex: '#282c34', weights: [0, 0, 0]};
  const weights = levels.map(value => Math.max(0, value) / total);
  const rgb = [0, 1, 2].map(channel => Math.round(colors.reduce((sum, color, i) =>
    sum + parseInt(color.slice(1 + channel * 2, 3 + channel * 2), 16) * weights[i], 0)));
  return {hex: '#' + rgb.map(value => value.toString(16).padStart(2, '0')).join(''), weights};
}

(() => {
  if (typeof document === 'undefined') return;
  const element = id => document.getElementById(id);
  const canvas = element('spectrumCanvas');
  const context = canvas.getContext('2d');
  if (!context) return;
  const keys = ['Bass', 'Mids', 'Treble'];
  const labels = ['bass', 'mids', 'treble'];
  let latest = null, timer = null, controller = null, fetching = false, suspended = false;
  let lastPalette = '', lastStatus = '', lastSummaryAt = 0;

  function hz(value) {
    return value >= 1000 ? `${Number((value / 1000).toFixed(1))}k` : String(Math.round(value));
  }

  function colors() {
    if (element('spectrumColors').value === 'album' && typeof state !== 'undefined' && state?.palette?.length) {
      const palette = state.palette;
      const signature = JSON.stringify(palette);
      if (signature !== lastPalette) {
        // Spread three selections across whatever palette size is available.
        keys.forEach((key, i) => element('spectrum' + key).value = palette[Math.round(i * (palette.length - 1) / 2)]);
        lastPalette = signature;
      }
    }
    return keys.map(key => element('spectrum' + key).value);
  }

  function draw() {
    const bounds = canvas.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const width = bounds.width, height = bounds.height;
    const physicalWidth = Math.round(width * ratio), physicalHeight = Math.round(height * ratio);
    if (canvas.width !== physicalWidth || canvas.height !== physicalHeight) {
      canvas.width = physicalWidth; canvas.height = physicalHeight;
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const left = 44, right = width - 20, top = 22, bottom = height - 34;
    const maximum = latest?.max_frequency || 20000;
    const floor = latest?.floor_db ?? -90;
    const x = frequency => left + Math.log(frequency / 20) / Math.log(maximum / 20) * (right - left);
    const y = db => bottom - Math.max(0, Math.min(1, (db - floor) / -floor)) * (bottom - top);
    const palette = colors();
    context.font = '11px system-ui, sans-serif';
    context.textBaseline = 'middle';
    for (const db of [0, -30, -60, -90]) {
      context.strokeStyle = '#2a2e37'; context.lineWidth = 1;
      context.beginPath(); context.moveTo(left, y(db)); context.lineTo(right, y(db)); context.stroke();
      context.fillStyle = '#a3a6b0'; context.textAlign = 'right'; context.fillText(String(db), left - 9, y(db));
    }
    context.textAlign = 'left'; context.fillText('dBFS', 8, 10);
    const ticks = width < 500 ? [20, 100, 1000, maximum] : [20, 50, 100, 250, 500, 1000, 2000, 5000, 10000, maximum];
    let lastTick = -Infinity;
    for (const frequency of ticks) {
      if (frequency > maximum || x(frequency) - lastTick < 32) continue;
      lastTick = x(frequency); context.textAlign = 'center';
      context.fillStyle = '#a3a6b0'; context.fillText(hz(frequency), lastTick, height - 14);
    }
    if (latest?.ready) {
      latest.frequencies.forEach((frequency, i) => {
        const band = frequency < 250 ? 0 : frequency < 2000 ? 1 : 2;
        const start = x(latest.edges[i]), end = x(latest.edges[i + 1]);
        context.fillStyle = palette[band];
        context.fillRect(start + 1, y(latest.levels_db[i]), Math.max(1, end - start - 2), bottom - y(latest.levels_db[i]));
      });
    }
    for (const frequency of [250, 2000]) {
      if (frequency >= maximum) continue;
      context.strokeStyle = '#666b76'; context.setLineDash([3, 5]);
      context.beginPath(); context.moveTo(x(frequency), top); context.lineTo(x(frequency), bottom); context.stroke();
      context.setLineDash([]);
    }
    if (!latest?.ready || latest.silent) {
      context.textAlign = 'center'; context.fillStyle = '#a3a6b0';
      context.fillText(latest?.silent ? 'Waiting for music…' : 'Start listening to see your music', (left + right) / 2, (top + bottom) / 2);
    }
  }

  function render(force = false) {
    const palette = colors();
    const levels = latest?.ready && !latest.silent ? latest.bands.map(band => band.rms) : [0, 0, 0];
    const blend = blendSpectrumColors(palette, levels);
    const swatch = element('spectrumColor');
    swatch.style.backgroundColor = blend.hex;
    // Lower volume dims the preview without changing its frequency weighting.
    swatch.style.opacity = latest?.ready && !latest.silent ? Math.min(1, Math.max(.2, Math.sqrt(latest.rms / .1))) : 1;
    const messages = {
      idle: 'Press Listen to BlackHole, or Start on a music look, to see the live spectrum.',
      waiting_input: 'Listening; waiting for audio frames from the selected input…',
      warming_up: 'Collecting the first short audio window…',
      stalled: 'Audio frames stopped arriving. Restart listening and check routing below.',
      error: latest?.error || 'Audio processing failed. Restart listening.',
      silent: 'Input is arriving but is silent. Play music and check the output routing below.',
      live: `Live spectrum · ${latest?.device || 'audio input'} · ${latest?.window_ms || '—'} ms window`
    };
    const message = messages[latest?.state] || 'Spectrum disconnected. Check that HueCanvas is running.';
    const statusChanged = message !== lastStatus;
    if (statusChanged) {
      element('spectrumStatus').textContent = message; lastStatus = message;
    }
    element('spectrumListen').disabled = Boolean(latest?.running) || typeof audioStarting !== 'undefined' && audioStarting;
    element('spectrumListen').textContent = latest?.running ? 'Listening' : 'Start listening';
    // Limit text updates for readability while bars and the swatch stay live.
    const now = performance.now();
    if (force || statusChanged || now - lastSummaryAt >= 500) {
      lastSummaryAt = now;
      keys.forEach((key, i) => {
        const band = latest?.ready ? latest.bands[i] : null;
        element('spectrum' + key + 'Level').textContent = band ? `${band.db.toFixed(1)} dBFS` : '— dBFS';
        element('spectrum' + key + 'Meter').value = band?.db ?? -90;
        if (band) element('spectrum' + key).parentElement.firstChild.textContent = `${band.label} · ${hz(band.low)}–${hz(band.high)} Hz`;
      });
      element('spectrumPeak').textContent = latest?.ready && !latest.silent ? `Peak: ${hz(latest.peak_frequency)} Hz · ${latest.peak_db.toFixed(1)} dBFS` : 'Peak: —';
      element('spectrumBlend').textContent = levels.some(value => value > 0) ? `${blend.hex.toUpperCase()} · ${blend.weights.map((weight, i) => `${Math.round(weight * 100)}% ${labels[i]}`).join(' / ')}` : 'Waiting for sound';
      const rhythm = latest?.percussion;
      const family = {snare:'Snare-like attack',kick:'Kick-like attack',hat:'High percussion attack'};
      element('spectrumRhythm').textContent = !latest?.ready || !rhythm ? 'Drum pattern detection starts when listening.' : rhythm.roll_active ? `Drum roll · ${rhythm.roll_rate} attacks / second · ${family[rhythm.last_family] || 'repeated percussion'} · comparing the preceding ${rhythm.comparison_seconds} seconds` : `${family[rhythm.last_family] || 'Listening for drum patterns'} · comparing the preceding ${rhythm.comparison_seconds} seconds`;
      if (rhythm?.drop_active) element('spectrumRhythm').textContent += ' · Drop energy rise detected';
      if (rhythm?.lead_band && typeof state !== 'undefined' && state?.settings?.flash_trigger === 'bands') element('spectrumRhythm').textContent += rhythm.lead_band === 'bass' ? ' · Lights following bass changes' : ' · Lights following mid changes (bass dropped out)';
      const gate = latest?.threshold_gate;
      const selected = typeof state !== 'undefined' && state?.settings?.flash_trigger === 'frequency';
      element('spectrumGate').textContent = !selected ? 'Choose Levels under Pulses follow to use these band thresholds.' : !gate || !latest?.ready ? 'Frequency trigger: waiting for fresh audio.' : `Frequency trigger · bass ${gate.levels_db.bass.toFixed(1)} dBFS (trigger > ${gate.bass_threshold_db}) · mids ${gate.levels_db.mids.toFixed(1)} dBFS (trigger > ${gate.mids_threshold_db}) · ${gate.active_band === 'mids' ? 'mids selected: bass < ' + gate.bass_fallback_db + ' dBFS' : 'bass priority: mids blocked until bass < ' + gate.bass_fallback_db + ' dBFS'}`;
      canvas.setAttribute('aria-label', latest?.ready && !latest.silent ? `Music spectrum; strongest frequency ${Math.round(latest.peak_frequency)} Hertz. ${latest.bands.map(band => `${band.label} ${band.db.toFixed(1)} dBFS`).join(', ')}` : message);
    }
    draw();
  }

  async function poll() {
    if (document.hidden || suspended || fetching) return;
    fetching = true; const started = performance.now();
    controller = new AbortController();
    const timeout = setTimeout(() => controller?.abort(), 2000);
    try {
      const response = await fetch('/api/audio/spectrum', {signal: controller.signal, cache: 'no-store'});
      if (!response.ok) throw Error('Spectrum request failed');
      latest = await response.json(); render();
    } catch (error) {
      latest = null; render(true);
    } finally {
      clearTimeout(timeout); controller = null; fetching = false;
      // One request at a time, ten refreshes/second when capturing, one when idle.
      if (!document.hidden && !suspended) timer = setTimeout(poll, Math.max(0, (latest?.running ? 100 : 1000) - (performance.now() - started)));
    }
  }

  element('spectrumListen').onclick = () => element('audioListen').click();
  element('spectrumColors').onchange = () => {lastPalette = ''; render(true);};
  keys.forEach(key => element('spectrum' + key).oninput = () => {
    element('spectrumColors').value = 'custom'; render(true);
  });
  new ResizeObserver(draw).observe(canvas);
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (document.hidden) {controller?.abort(); latest = null; draw();}
    else poll();
  });
  window.addEventListener('pagehide', () => {suspended = true; clearTimeout(timer); controller?.abort();});
  window.addEventListener('pageshow', () => {suspended = false; poll();});
  poll();
})();
