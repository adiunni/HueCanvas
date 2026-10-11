const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync('app/static/spectrum.js', 'utf8');
const core = vm.createContext({});
vm.runInContext(script, core);
const blend = (colors, levels) => JSON.parse(JSON.stringify(vm.runInContext(`blendSpectrumColors(${JSON.stringify(colors)}, ${JSON.stringify(levels)})`, core)));
assert.equal(blend(['#ff0000', '#00ff00', '#0000ff'], [1, 0, 0]).hex, '#ff0000');
assert.equal(blend(['#ff0000', '#00ff00', '#0000ff'], [1, 1, 0]).hex, '#808000');
assert.equal(blend(['#ff0000', '#00ff00', '#0000ff'], [0, 0, 0]).hex, '#282c34');
assert.deepEqual(blend(['#ff0000', '#00ff00', '#0000ff'], [.1, .2, .7]).weights, [.1, .2, .7]);

(async () => {
  const elements = new Map(), handlers = {}, timers = new Map();
  let timerId = 0, bars = 0, requests = 0, listenClicks = 0;
  const context2d = new Proxy({}, {get(target, key) {
    if (key === 'fillRect') return () => {bars++;};
    return target[key] || (() => {});
  }});
  const element = id => {
    if (!elements.has(id)) elements.set(id, {value: '', textContent: '', style: {}, parentElement: {firstChild: {textContent: ''}},
      setAttribute(key, value) {this[key] = value;}, getContext() {return context2d;},
      getBoundingClientRect() {return {width: 900, height: 260};}, click() {listenClicks++;}});
    return elements.get(id);
  };
  element('spectrumColors').value = 'album';
  const doc = {hidden: false, getElementById: element, addEventListener(event, fn) {handlers[event] = fn;}};
  let data = {running: true, ready: true, state: 'live', device: 'BlackHole 2ch', window_ms: 171,
    frequencies: [100, 1000, 4000], edges: [20, 250, 2000, 20000], levels_db: [-10, -90, -90],
    max_frequency: 20000, floor_db: -90, rms: .1, peak_frequency: 100, peak_db: -10,
    bands: [{label: 'Bass', low:20, high:250, rms:.1, db:-20}, {label:'Midrange',low:250,high:2000,rms:0,db:-90}, {label:'Treble',low:2000,high:20000,rms:0,db:-90}]};
  const app = vm.createContext({document: doc, window: {devicePixelRatio: 2, addEventListener(event, fn) {handlers[event] = fn;}},
    performance: {now: () => 1000}, state: {palette: ['#ff0000', '#00ff00', '#0000ff']}, audioStarting: false,
    ResizeObserver: class {observe() {}}, AbortController,
    setTimeout(fn, delay) {timers.set(++timerId, {fn, delay}); return timerId;}, clearTimeout(id) {timers.delete(id);},
    async fetch(url, options) {requests++; assert.equal(url, '/api/audio/spectrum'); assert.equal(options.cache, 'no-store'); return {ok: true, async json() {return data;}};}});
  vm.runInContext(script, app);
  const settle = () => new Promise(resolve => setImmediate(resolve));
  await settle();
  assert.equal(element('spectrumColor').style.backgroundColor, '#ff0000');
  assert.ok(element('spectrumStatus').textContent.includes('Live spectrum'));
  assert.ok(element('spectrumCanvas')['aria-label'].includes('100 Hertz'));
  assert.ok(element('spectrumBlend').textContent.includes('100% bass'));
  assert.equal(bars, 3);
  data.threshold_gate = {levels_db:{bass:-14,mids:-10,treble:-40},bass_threshold_db:-15,bass_fallback_db:-70,mids_threshold_db:-20,active_band:'bass'};
  vm.runInContext("state.settings = {flash_trigger: 'frequency'}", app);
  element('spectrumColors').onchange();
  assert.ok(element('spectrumGate').textContent.includes('bass priority'));
  data.threshold_gate.active_band = 'mids'; data.threshold_gate.levels_db.bass = -80;
  element('spectrumColors').onchange();
  assert.ok(element('spectrumGate').textContent.includes('mids selected'));

  assert.equal(requests, 1);
  assert.equal(timers.size, 1);
  const scheduled = [...timers.values()][0];
  assert.equal(scheduled.delay, 100);
  element('spectrumBass').value = '#ffffff'; element('spectrumBass').oninput();
  assert.equal(element('spectrumColors').value, 'custom');
  assert.equal(element('spectrumColor').style.backgroundColor, '#ffffff');
  data = {running: false, ready: false, state: 'idle'};
  timers.clear(); scheduled.fn(); await settle();
  assert.equal(element('spectrumColor').style.backgroundColor, '#282c34');
  assert.equal(element('spectrumPeak').textContent, 'Peak: —');
  assert.equal(element('spectrumListen').disabled, false);
  element('spectrumListen').onclick(); assert.equal(listenClicks, 1);
  doc.hidden = true; handlers.visibilitychange();
  assert.equal(timers.size, 0);
  doc.hidden = false; handlers.visibilitychange(); await settle();
  assert.equal(requests, 3);
  console.log('Spectrum blend, live rendering, custom swatches, stop and tab visibility passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
