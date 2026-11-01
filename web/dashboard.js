'use strict';
const names = {'wled-log':'WLED log','distance-test':'Distance test',overview:'Overview',radar:'Radar',light:'Lux sensor',lighting:'WLED light',birdcast:'BirdCast',recordings:'Recordings','config-directionality':'Directionality','config-schedule':'Recording schedule','config-lighting':'WLED hardware','logs-sensors':'Sensor histories'};
const sectionMap = {light:'Input',radar:'Input',recordings:'Input',birdcast:'Input',lighting:'Output','distance-test':'Output','config-directionality':'Configuration','config-schedule':'Configuration','config-lighting':'Configuration','wled-log':'Logs','logs-sensors':'Logs'};
const itemNames = {light:'Lux sensor',radar:'Radar',recordings:'Recordings',birdcast:'BirdCast',lighting:'WLED light','distance-test':'Distance test',overview:'Station overview','config-directionality':'Directionality','config-schedule':'Recording schedule','config-lighting':'WLED hardware','wled-log':'WLED log','logs-sensors':'Sensor histories'};
const titles = {'wled-log':'WLED command log','distance-test':'Distance sensor · LED test',overview:'Station overview',radar:'Presence & movement',light:'Luminance',lighting:'WLED strip lighting',birdcast:'Migration outlook',recordings:'Sound library','config-directionality':'Directionality configuration','config-schedule':'Audio collection schedule','config-lighting':'WLED hardware settings','logs-sensors':'Sensor history logs'};
const descriptions = {'wled-log':'USB traffic observed by this server · Latest 2,000 events · Cleared on server restart.','distance-test':'Live radar readings and single-LED output, refreshed every half second. The test continues when you leave this page; select Off to stop it.',overview:'Your sensors, migration data, and sound — in one place.',radar:'Live LD2410C presence & activity estimator, radial distance gates (0–6.75 m), and raw UART diagnostics.',light:'Measure the light around your station with the VEML7700.',lighting:'Control your 3.2 ft strip through the SP803E WLED-ESP32 USB controller.',birdcast:'Fetch the latest migration snapshot for a US state.',recordings:'Capture a moment. Listen again. Manage your saved recordings.','config-directionality':'Configure sensor directionality and orientation.','config-schedule':'Automate microphone sampling every two minutes across all clients.','config-lighting':'Hardware parameters, serial connection details, and output bounds for WLED.','logs-sensors':'Historical readings collected from presence, light, and migration sensors.'};
let sources = {}, recordings = [], regions = [], region = 'US-CA', loadingLibrary = true, libraryError = '', cacheError = '', stationMode = 'normal';
let histories = {};
let lighting = {};
const historyRanges = {radar:'1h',light:'1h',birdcast:'1d',microphone:'1d'};
const rangeOptions = [['30m','Half hour'],['1h','Hour'],['6h','6 hours'],['12h','12 hours'],['1d','Day'],['7d','Week'],['all','All time']];
const historyInfo = {}, historyErrors = {}, historyRequests = {}, historyKeys = {};
let historyTimer, historyGeneration = 0, selectedAnimal = '';
const commonNames = {'Bubo virginianus':'Great Horned Owl'}, nameRequests = new Set();
const scoreExplanation = '<p class="timestamp">Score scale: raw model score (logit), with no fixed minimum or maximum. Scores can be negative; they are not out of 10 or 100. Higher scores mean stronger model support, but the scale varies between species. Scores are not confidence percentages. Each new five-second recording uses one analysis window; older ten-second recordings use the maximum from two windows.</p>';
const animalLabel = name => commonNames[name] ? `${commonNames[name]} · ${name}` : `${name} · Common name unavailable`;
async function loadAnimalNames(names) {
  const missing = [...new Set(names)].filter(name=>typeof name==='string' && /^[A-Z][a-z]+ /.test(name) && !Object.hasOwn(commonNames,name) && !nameRequests.has(name)).slice(0,30);
  if (!missing.length) return;
  missing.forEach(name=>nameRequests.add(name));
  try {
    const data = await api(`/v1/animal-names?${missing.map(name=>`name=${encodeURIComponent(name)}`).join('&')}`);
    Object.assign(commonNames,data.names);
    visibleRecordings().forEach(updateAnimalResults);
    updateAnimalSelector();
    if (view()==='recordings') updateHistoryPlot('microphone');
  } catch { /* Scientific names remain available offline. */ }
  finally { missing.forEach(name=>nameRequests.delete(name)); }
}
function updateAnimalSelector() {
  const selector = content.querySelector('[data-history-metric="microphone"]');
  if (selector?.options) [...selector.options].forEach(option=>option.textContent=animalLabel(option.value));
}
const historyMetrics = {radar:'presence',light:'lux',birdcast:'birds_in_flight'};
const graphMetrics = {
  radar:[['presence','Presence state'],['presence_state','Derived presence state'],['range_estimate_cm','Smoothed range estimate (cm)'],['detection_distance_cm','Detection distance (cm)'],['moving_distance_cm','Moving distance (cm)'],['stationary_distance_cm','Stationary distance (cm)'],['moving_energy_score','Moving energy score'],['stationary_energy_score','Stationary energy score'],['activity_level','Activity level (0–1)'],['presence_confidence','Presence confidence heuristic (0–1)']],
  light:[['lux','Luminance (lux)'],['ambient_counts','Ambient light (raw counts)'],['white_counts','White channel (raw counts)']],
  birdcast:[['birds_in_flight','Birds in flight'],['total_birds_crossed','Birds crossed'],['speed_mph','Speed (mph)'],['altitude_ft','Altitude (ft)']]
};
let radarLoadedAt = performance.now(), radarExpiryTimer;
let radarPollTimer, radarPollGeneration = 0;
let collection = {enabled:false,recording:false}, collectionTimer, collectionGeneration = 0;
let analysisTimer, analysisPollGeneration = 0;
const analysisRequests = new Set();
const deletingRecordings = new Set();
const pending = new Set();
const content = document.querySelector('#content');
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const time = value => { if (!value) return 'No reading yet'; const date = new Date(value); return Number.isNaN(date.getTime()) ? escapeHtml(value) : escapeHtml(date.toLocaleString()); };
const number = value => value == null ? '—' : escapeHtml(typeof value === 'number' ? value.toLocaleString(undefined,{maximumFractionDigits:2}) : value);
const view = () => Object.hasOwn(names, location.hash.slice(1)) ? location.hash.slice(1) : location.pathname === '/distance-test' ? 'distance-test' : 'overview';
function notice(message, error = false) { const box = document.querySelector('#notice'); box.textContent = message; box.classList.toggle('error',error); box.hidden = !message; }
async function api(path, options = {}) {
  const controller = new AbortController();
  // Reads recover quickly; actions allow the server's capture/scraper deadline.
  const timeout = setTimeout(()=>controller.abort(), options.method ? 60000 : 10000);
  try {
    const response = await fetch(path, {...options, signal:controller.signal, cache:'no-store'});
    let data;
    try { data = await response.json(); } catch (error) {
      if (controller.signal.aborted) throw error;
      throw new Error('The station returned an unreadable response.');
    }
    if (!response.ok) {
      const error = new Error(data.error?.message || data.analysis?.error || `Request failed (${response.status}).`);
      error.data = data;
      throw error;
    }
    return data;
  } catch (error) {
    if (controller.signal.aborted) throw new Error('The station took too long to respond. Check its connection. Actions may still finish; refresh before trying again.');
    if (error instanceof TypeError) throw new Error('Cannot reach the station. Check your network connection.');
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}
function details(data, label = 'View complete data') { return `<details class="details"><summary>${escapeHtml(label)}</summary><pre>${escapeHtml(JSON.stringify(data,null,2))}</pre></details>`; }
function action(source, label) { return `<button class="button" data-action="${source}" ${pending.has(source) ? 'disabled aria-busy="true"' : ''}>${pending.has(source) ? (source === 'microphone' ? 'Recording 5 seconds…' : 'Requesting…') : label}</button>`; }
function currentEntry(source) {
  const entry = sources[source];
  if (source !== 'radar' || entry?.http_status !== 200) return entry;
  const data = entry.result;
  const age = (data.age_seconds ?? 0) + (performance.now() - radarLoadedAt) / 1000;
  if (age <= data.stale_after_seconds) return entry;
  const {presence,moving_distance_cm,stationary_distance_cm,detection_distance_cm,raw_moving_distance_cm,raw_stationary_distance_cm,raw_detection_distance_cm,moving_energy_score,stationary_energy_score,range_estimate_cm,range_trend,activity_level,presence_confidence,gate_profile,window_summary,transition_reason,...status} = data;
  return {http_status:503,result:{...status,fresh:false,sensor_healthy:false,presence_state:'UNKNOWN',presence_confirmed:false,age_seconds:age,status:'unavailable',
    error:{code:'presence_unavailable',message:'This radar frame is stale. Choose Read now for the newest available frame.'}}};
}
function scheduleRadarExpiry() {
  clearTimeout(radarExpiryTimer);
  const entry = sources.radar;
  if (entry?.http_status !== 200) return;
  const remaining = entry.result.stale_after_seconds - (entry.result.age_seconds ?? 0) - (performance.now() - radarLoadedAt) / 1000;
  radarExpiryTimer = setTimeout(()=>{
    updateRadarReading();
  },Math.max(0,remaining)*1000+25);
}
function scheduleRadarPoll() {
  clearTimeout(radarPollTimer);
  const generation = ++radarPollGeneration;
  if (!['overview','radar','distance-test'].includes(view())) return;
  radarPollTimer = setTimeout(async()=>{
    if (generation !== radarPollGeneration) return;
    const started = performance.now();
    let entry;
    try {
      const latest = await api('/v1/latest');
      entry = latest.sources.radar;
      if (generation === radarPollGeneration && view() === 'distance-test') {
        lighting = latest.lighting || {};
        syncLightingControls();
      }
    } catch (error) {
      entry = {http_status:503,result:error.data || {fresh:false,status:'unavailable',
        error:{code:'presence_unavailable',message:error.message}}};
    }
    if (generation !== radarPollGeneration) return;
    sources.radar = entry;
    radarLoadedAt = started;
    updateRadarReading();
    scheduleRadarExpiry();
    scheduleRadarPoll();
  },view()==='distance-test' ? 500 : 200);
}
function updateRadarReading() {
  const reading = content.querySelector('[data-reading="radar"]');
  if (!reading) return;
  const entry = currentEntry('radar');
  const key = JSON.stringify([entry?.http_status, entry?.result?.timestamp, entry?.result?.valid_frames, view(), entry?.result?.error]);
  if (reading.dataset?.radarSnapshot === key) return;
  const wasOpen = reading.querySelector('details.details')?.open;
  const wrapper = document.createElement('div');
  wrapper.innerHTML = sourceCard('radar',['radar','distance-test'].includes(view()));
  const replacement = wrapper.querySelector('[data-reading="radar"]');
  if (replacement.dataset) replacement.dataset.radarSnapshot = key;
  const detailsEl = replacement.querySelector('details.details');
  if (detailsEl && wasOpen) detailsEl.open = true;
  reading.replaceWith(replacement);
}
function badge(source) {
  const entry = currentEntry(source);
  if (!entry) return '<span class="badge empty">Not requested</span>';
  if (entry.http_status !== 200) return '<span class="badge unavailable">Unavailable</span>';
  return `<span class="badge">${source === 'radar' ? 'Fresh frame' : 'Saved result'}</span>`;
}
function resultError(source) {
  const entry = currentEntry(source);
  if (!entry || entry.http_status === 200) return '';
  return `<p class="error-message">${escapeHtml(entry.result?.error?.message || 'Data is unavailable.')}</p>`;
}
function sourceCard(source, expanded = false) {
  const entry = currentEntry(source), success = entry?.http_status === 200, data = success ? entry.result : {};
  const meta = {radar:['◎','Radar','LD2410C'],light:['☼','Light','VEML7700'],birdcast:['↗','BirdCast','MIGRATION DATA']}[source];
  const value = source === 'radar' ? (success ? escapeHtml(data.presence) : '—') : source === 'light' ? `${number(data.lux)} <small>lux</small>` : number(data.metrics?.birds_in_flight);
  const caption = source === 'radar' ? 'Presence in the detection area' : source === 'light' ? 'Latest measured illuminance' : 'Estimated birds in flight';
  const stamp = source === 'birdcast' ? data.retrieved_at : data.timestamp;
  const radarSummary = source === 'radar' ? renderRadarSummaryPill(entry, success, data) : '';
  let body = `<div class="big-value">${value}</div><div class="value-caption">${caption}</div>${radarSummary}<div class="timestamp">${success ? `Measured ${time(stamp)}` : 'No current result'}${entry?.requested_at ? `<br>Last request ${time(entry.requested_at)}` : ''}</div>${resultError(source)}`;
  if (entry?.result.history_error) body += `<p class="error-message">${escapeHtml(entry.result.history_error)}</p>`;
  if (expanded && source === 'radar') body += renderRadarExpanded(entry, success, data);
  if (expanded && source === 'birdcast') {
    const metrics = data.metrics || {};
    body += facts([['Birds crossed',number(metrics.total_birds_crossed)],['Direction',escapeHtml(metrics.direction || '—')],['Speed',metrics.speed_mph == null ? '—' : `${number(metrics.speed_mph)} mph`],['Altitude',metrics.altitude_ft == null ? '—' : `${number(metrics.altitude_ft)} ft`],['Region',escapeHtml(data.region?.name || data.region?.code || '—')],['Selected night',escapeHtml(data.selected_night || '—')]]);
    if (data.warnings?.length) body += `<ul class="warning-list">${data.warnings.map(item=>`<li>${escapeHtml(item)}</li>`).join('')}</ul>`;
    if (data.source?.url) {
      try { const url = new URL(data.source.url); if (url.protocol === 'https:' && url.hostname === 'dashboard.birdcast.org') body += `<p class="timestamp"><a href="${escapeHtml(url.href)}" target="_blank" rel="noopener noreferrer">Open BirdCast source ↗</a></p>`; } catch {}
    }
  }
  if (expanded && source === 'light') body += facts([
    ['Ambient light (raw)',data.ambient_counts == null ? '—' : `${number(data.ambient_counts)} counts`],
    ['White channel (raw)',data.white_counts == null ? '—' : `${number(data.white_counts)} counts`],
    ['Sensor gain',data.gain == null ? '—' : `${escapeHtml(data.gain)}×`],
    ['Integration time',data.integration_time_ms == null ? '—' : `${number(data.integration_time_ms)} ms`],
    ['Resolution',data.resolution_lux_per_count == null ? '—' : `${escapeHtml(data.resolution_lux_per_count)} lux/count`]
  ]) + '<p class="timestamp">Lux is calculated from the ambient channel. Ambient and white channels report raw 16-bit counts (0–65,535). A maximum count may indicate saturation. Channel reads are sequential.</p>';
  if (expanded && source === 'light') body += '<p class="timestamp">The server collects light every second. If the sensor is disconnected, reconnect it and choose Read now.</p>';
  if (expanded && entry?.http_status !== 200 && entry?.last_success && source !== 'radar') body += `<div class="history"><p>Previous successful result · historical data</p>${details(entry.last_success,'View previous successful result')}</div>`;
  const topGraphic = expanded && source === 'radar' ? renderRadarLocatorGraphic(entry, success, data) : '';
  const control = source === 'birdcast' ? action('birdcast','Fetch BirdCast') : action(source,'Read now');
  return `<article class="card" data-source="${source}"><div data-reading="${source}"><div class="card-header"><div class="source-label"><div class="source-icon" aria-hidden="true">${meta[0]}</div><div><h2>${meta[1]}</h2><small>${meta[2]}</small></div></div>${badge(source)}</div>${topGraphic}${body}<div class="card-actions">${control}${expanded ? '' : `<a class="section-link" href="#${source}">Explore data ↗</a>`}</div>${expanded ? details(entry || {status:'not_requested'}) : ''}</div>${expanded && view() !== 'distance-test' ? historyGraph(source) : ''}</article>`;
}
function lightingDevice() {
  const device = lighting.device || {};
  return facts([['Connection','Pi server → USB serial → SP803E'],['USB port',escapeHtml(device.serial_port || 'Not selected yet')],['Serial speed',`${number(device.baudrate ?? 115200)} baud`],['Strip length','3.2 ft · 192 pixels'],['WLED pixels',device.led_count == null ? 'Awaiting controller' : number(device.led_count)],['Last confirmed coverage',device.covered_leds == null ? 'Not verified yet' : `${number(device.covered_leds)} / ${number(device.expected_led_count ?? 192)} pixels`],['Confirmed color',escapeHtml(device.confirmed_color || 'Not verified yet')],['Confirmed brightness',device.confirmed_brightness == null ? 'Not verified yet' : device.confirmed_on === false ? 'Off' : `${Math.round(device.confirmed_brightness / 255 * 100)}% (${number(device.confirmed_brightness)} / 255)`],['Segment brightness',device.segment_brightness == null ? 'Not verified yet' : `${number(device.segment_brightness)} / 255`],['WLED current limit',device.current_limit_ma == null ? 'Not reported' : device.current_limit_ma === 0 ? 'Disabled in WLED' : `${number(device.current_limit_ma)} mA`],['Firmware',escapeHtml(device.firmware || 'Awaiting controller')]]) + '<p class="timestamp">Normal modes cover pixels 1–192 with a solid color and no spacing; Distance test lights one pixel. WLED must be configured for at least 192 pixels; a shorter configured output is reported as an error.</p>';
}
function lightingLuxReading() {
  if (lighting.lux == null) return 'Unavailable';
  const stale = lighting.lux_age_seconds > (lighting.lux_stale_after_seconds ?? 30);
  return `${lighting.lux.toLocaleString(undefined,{maximumFractionDigits:2})} lux${stale ? ' (stale)' : ''}`;
}
function lightingLuxRule() {
  const dark = lighting.lux_dark_threshold ?? 10;
  const light = lighting.lux_light_threshold ?? 30;
  return `<section class="lighting-lux" aria-label="Ambient light and Auto thresholds">
    <h3>Outside light · Auto rule</h3>
    <p class="lighting-lux-reading">Current lux reading <strong data-lighting-lux>${escapeHtml(lightingLuxReading())}</strong></p>
    <div class="lighting-lux-bands">
      <div><strong>&lt; ${number(dark)} lux</strong><span>Dark: use distance control</span></div>
      <div><strong>${number(dark)} ≤ lux ≤ ${number(light)}</strong><span>Keep the previous dark/light state</span></div>
      <div><strong>&gt; ${number(light)} lux</strong><span>Light: keep the strip off</span></div>
    </div>
    <p class="timestamp">These thresholds apply in Auto. At startup, or after a lux failure, the middle range keeps Auto off until darkness is confirmed.</p>
    <p class="timestamp" data-lighting-ambient>${escapeHtml(lightingAmbientSummary())}</p>
  </section>`;
}
function lightingAmbientSummary() {
  const reasons = {waiting_lux:'Waiting for lux to confirm darkness · Auto off',
    daylight:'Daylight · Auto off',lux_unavailable:'Lux unavailable or stale · Auto off',
    distance_control:'Distance control active'};
  const reason = lighting.mode === 'auto' ? (reasons[lighting.auto_gate_reason] || reasons.waiting_lux) : 'Lux gate applies in Auto only';
  return `${reason} · Ambient ${lighting.ambient_state || 'unknown'} · Lux ${lighting.lux == null ? 'unavailable' : lighting.lux.toLocaleString(undefined,{maximumFractionDigits:2})} · Lux age ${lighting.lux_age_seconds == null ? 'unavailable' : lighting.lux_age_seconds.toFixed(1) + ' s'} · Dark <${lighting.lux_dark_threshold ?? 10} lux / Light >${lighting.lux_light_threshold ?? 30} lux · Stale after ${lighting.lux_stale_after_seconds ?? 30} s`;
}
function lightingSummary() {
  const base = `${lighting.mode || 'auto'} · ${lighting.status || 'waiting'}`;
  if (lighting.mode === 'auto') return `${base}${lighting.test_state === 'idle' ? ' · Idle · no distance change' : ''} · Distance ${lighting.distance_cm == null ? '—' : lighting.distance_cm + ' cm'} · Target ${lighting.target_brightness ?? '—'}/255 · Acknowledged ${lighting.brightness ?? '—'}/255 · Sample ${lighting.sample_age_seconds == null ? 'waiting' : lighting.sample_age_seconds.toFixed(1) + ' s ago'} · Ack ${lighting.ack_age_seconds == null ? 'waiting' : lighting.ack_age_seconds.toFixed(1) + ' s ago'}`;
  if (lighting.mode !== 'distance_test') return base;
  const unavailable = currentEntry('radar')?.http_status === 503;
  const detail = unavailable ? 'Radar unavailable' : lighting.led_index == null ? (lighting.test_state || 'waiting').replaceAll('_', ' ') : `${lighting.distance_cm} cm · Target LED ${lighting.led_index + 1} / 192`;
  const confirmed = lighting.on === false ? 'Off' : lighting.confirmed_led_index == null ? 'Awaiting acknowledgment' : `LED ${lighting.confirmed_led_index + 1}`;
  return `${base} · ${detail} · Confirmed: ${confirmed} · 25% amber · every 0.5 s`;
}
function distanceTestPage() {
  return `<article class="card"><h2>Single-LED test</h2><p class="timestamp" data-lighting-summary>${escapeHtml(lightingSummary())}</p>
    <p>The LED target and radar map use the same filtered range and sampled frame. Confirmed shows the last controller acknowledgment; pending output may lag the target.</p>
    <p>LED 1: 1 m or closer. LED 192: 6 m or farther. Distances between move the dot along LEDs 2–191. One amber LED stays at 25% brightness. No target turns the strip off; the radar’s absence delay still applies.</p>
    <div class="card-actions"><button class="button" data-lighting-mode="distance_test">Start distance test</button><button class="button secondary" data-lighting-mode="off">Off · stop test</button><a class="section-link" href="#lighting">Lighting controls ↗</a></div>
    <p class="error-message" data-lighting-error ${(lighting.error || lighting.sensor_error) ? '' : 'hidden'}>${escapeHtml([lighting.error, lighting.sensor_error].filter(Boolean).join(' '))}</p>
    <div data-lighting-device>${lightingDevice()}</div></article>${sourceCard('radar',true)}`;
}
function lightingControls() {
  const brightness = lighting.manual_brightness ?? 255;
  const color = /^#[0-9a-f]{6}$/i.test(lighting.color || '') ? lighting.color : '#ffa028';
  return `<section class="lighting-controls" aria-label="WLED lighting controls">
    <h2>SP803E · WLED-ESP32 · USB</h2>
    <p class="timestamp" data-lighting-summary>${escapeHtml(lightingSummary())}</p>
    ${lightingLuxRule()}
    <div class="card-actions"><button class="button" data-lighting-mode="on">On</button><button class="button secondary" data-lighting-mode="off">Off</button><button class="button secondary" data-lighting-mode="auto">Auto</button><button class="button secondary" data-lighting-mode="distance_test">Distance test</button><button class="button secondary" data-lighting-max>Full brightness · 100%</button></div>
    <p><a class="section-link" href="/distance-test">Open distance test &amp; live radar ↗</a></p>
    <div data-lighting-device>${lightingDevice()}</div>
    <p class="error-message" data-lighting-error ${(lighting.error || lighting.sensor_error) ? '' : 'hidden'}>${escapeHtml([lighting.error, lighting.sensor_error].filter(Boolean).join(' '))}</p>
    <form data-lighting-settings>
      <fieldset data-lighting-appearance ${lighting.mode === 'distance_test' ? 'disabled' : ''}>
      <label for="wled-brightness">Brightness <output id="wled-brightness-value" for="wled-brightness">${Math.round(brightness / 255 * 100)}%</output></label>
      <input id="wled-brightness" name="brightness" ${lighting.mode === 'auto' ? 'disabled' : ''} type="range" min="1" max="255" value="${brightness}" aria-describedby="wled-manual-help">
      <label for="wled-color">Color</label>
      <input id="wled-color" name="color" type="color" value="${color}">
      <button class="button secondary" type="submit">Apply color &amp; brightness</button>
      </fieldset>
    </form>
    <p class="timestamp">Distance test: LED 1 at 1 m or closer; LED 192 at 6 m or farther. Between 1–6 m, the dot moves along LEDs 2–191 at 25% amber. No target turns lights off; the radar’s own absence delay still applies. Stale readings turn lights off and show an error.</p>
    <p class="timestamp">Auto allows distance control below 10 lux and turns the strip off above 30 lux. From 10 through 30 lux it keeps the previous ambient state. Missing, invalid, or over-30-second-old lux keeps Auto off until darkness is confirmed. Manual On and Distance test bypass this rule.</p>
    <p class="timestamp" id="wled-manual-help">Apply saves manual settings. If the light is off, it stays off. On restores your chosen brightness. Auto uses distance for all 192 LEDs: 15% at 1 m or closer, rising to 50% at 6 m. After 5 seconds without a distance change, lights turn off. No target or unavailable radar turns lights off. Color changes preserve Auto.</p>
    <p class="timestamp">Commands go from this server to the USB controller. Status shows whether WLED acknowledged the command. At 100%, both master and segment brightness are 255/255. WLED’s configured power limits and global brightness factor can still reduce physical output.</p>
  </section>`;
}
function syncLightingControls() {
  const device = content.querySelector('[data-lighting-device]');
  if (device) device.innerHTML = lightingDevice();
  const error = content.querySelector('[data-lighting-error]');
  if (error) { error.textContent = [lighting.error, lighting.sensor_error].filter(Boolean).join(' '); error.hidden = !error.textContent; }
  const summary = content.querySelector('[data-lighting-summary]');
  if (summary) summary.textContent = lightingSummary();
  const ambient = content.querySelector('[data-lighting-ambient]');
  if (ambient) ambient.textContent = lightingAmbientSummary();
  const luxReading = content.querySelector('[data-lighting-lux]');
  if (luxReading) luxReading.textContent = lightingLuxReading();
  const appearance = content.querySelector('[data-lighting-appearance]');
  if (appearance) appearance.disabled = lighting.mode === 'distance_test';
  const slider = content.querySelector('[name="brightness"]');
  if (slider) slider.disabled = lighting.mode === 'auto';
  const form = content.querySelector('[data-lighting-settings]');
  if (!form || form.dataset.dirty === 'true' || form.contains(document.activeElement)) return;
  const brightness = form.querySelector('[name="brightness"]');
  const color = form.querySelector('[name="color"]');
  if (brightness) brightness.value = lighting.manual_brightness ?? 255;
  if (color && lighting.color) color.value = lighting.color;
  const output = form.querySelector('output');
  if (output) output.textContent = `${Math.round((lighting.manual_brightness ?? 255) / 255 * 100)}%`;
}
function historyGraph(source) {
  const options = source === 'microphone' ? (historyInfo.microphone?.animals || []).map(animal=>[animal,animalLabel(animal)]) : graphMetrics[source];
  const metric = source === 'microphone' ? selectedAnimal : historyMetrics[source];
  const metricControl = `<label>${source === 'microphone' ? 'Animal' : 'Graph'} <select data-history-metric="${source}" aria-label="${source === 'microphone' ? 'Animal' : names[source]} graph metric">${options.map(([key,title])=>`<option value="${escapeHtml(key)}" ${key===metric?'selected':''}>${escapeHtml(title)}</option>`).join('')}</select></label>`;
  const rangeControl = `<label>Time range <select data-history-range="${source}" aria-label="${source} time range">${rangeOptions.map(([key,title])=>`<option value="${key}" ${key===historyRanges[source]?'selected':''}>${title}</option>`).join('')}</select></label>`;
  return `<section class="reading-history" data-history="${source}"><div class="history-controls">${metricControl}${rangeControl}<button class="button secondary" data-history-refresh="${source}">Refresh graph</button></div>${source==='microphone' ? scoreExplanation : ''}<div data-chart="${source}">${historyPlot(source)}</div></section>`;
}
const radarStateLabels = {
  EMPTY:'Empty (no occupancy)',
  APPROACHING_CANDIDATE:'Approaching / candidate',
  PRESENT_STILL:'Present (still)',
  PRESENT_ACTIVE:'Present (active)',
  DEPARTING_CANDIDATE:'Departing / hold',
  UNKNOWN:'Unknown / unavailable'
};
function inferDerivedState(r) {
  if (!r) return 'UNKNOWN';
  if (r.presence_state) return r.presence_state;
  if (r.presence === 'moving' || r.presence === 'both') return 'PRESENT_ACTIVE';
  if (r.presence === 'stationary') return (r.stationary_energy_score ?? 50) >= 22 ? 'PRESENT_STILL' : 'EMPTY';
  if (r.presence === 'none') return 'EMPTY';
  return 'UNKNOWN';
}
function radarRange(data) {
  // A filtered null is unavailable, not permission to substitute a raw channel.
  return Object.hasOwn(data, 'range_estimate_cm') ? data.range_estimate_cm :
    data.detection_distance_cm ?? data.moving_distance_cm ?? data.stationary_distance_cm ?? null;
}
function renderRadarLocatorGraphic(entry, success, data) {
  const state = success ? inferDerivedState(data) : 'UNKNOWN';
  const confirmed = success ? (data.presence_confirmed ?? ['PRESENT_ACTIVE','PRESENT_STILL','DEPARTING_CANDIDATE'].includes(state)) : false;
  const rangeCm = success ? radarRange(data) : null;
  const movCm = success ? data.moving_distance_cm : null;
  const statCm = success ? data.stationary_distance_cm : null;
  const trend = success ? (data.range_trend || 'UNKNOWN') : 'UNKNOWN';
  const activity = success ? (data.activity_level ?? (state === 'PRESENT_ACTIVE' ? 0.7 : 0.1)) : 0;
  const gates = Array.isArray(data.gate_profile) && data.gate_profile.length ? data.gate_profile : Array.from({length:9},(_,g)=>{
    const mov = success && movCm != null && Math.min(8, Math.floor(movCm / 75)) === g ? (data.moving_energy_score || 0) : 0;
    const stat = success && statCm != null && Math.min(8, Math.floor(statCm / 75)) === g ? (data.stationary_energy_score || 0) : 0;
    const active = rangeCm != null && Math.min(8, Math.floor(rangeCm / 75)) === g;
    return {gate:g,label:`G${g}`,moving_energy:mov,stationary_energy:stat,active};
  });
  const originX = 56, endX = 641, spanW = endX - originX, centerY = 104, maxCm = 675;
  const cmToX = cm => originX + Math.max(0, Math.min(maxCm, cm)) / maxCm * spanW;
  const halfHAt = x => 16 + ((x - originX) / spanW) * 58;
  const gateW = spanW / 9;
  const gateShapes = gates.map((g, i) => {
    const x0 = originX + i * gateW, x1 = x0 + gateW;
    const h0 = halfHAt(x0), h1 = halfHAt(x1);
    const energy = Math.max(g.moving_energy || 0, g.stationary_energy || 0);
    const fillOpacity = g.active ? Math.max(0.22, energy / 180) : (energy > 0 ? Math.max(0.06, energy / 260) : 0.025);
    const fill = g.active ? '#287c54' : (g.moving_energy || 0) > (g.stationary_energy || 0) ? '#287c54' : '#5b8a72';
    const stroke = g.active ? '#287c54' : '#ccd8d0';
    const strokeW = g.active ? 1.6 : 0.8;
    const poly = `${x0.toFixed(1)},${(centerY - h0).toFixed(1)} ${x1.toFixed(1)},${(centerY - h1).toFixed(1)} ${x1.toFixed(1)},${(centerY + h1).toFixed(1)} ${x0.toFixed(1)},${(centerY + h0).toFixed(1)}`;
    const midX = (x0 + x1) / 2;
    const tickM = ((i + 1) * 0.75).toFixed(2).replace(/\.00$/, 'm').replace(/(\.\d)0$/, '$1m');
    return `<g class="locator-gate ${g.active ? 'active' : ''}"><polygon points="${poly}" fill="${fill}" fill-opacity="${fillOpacity.toFixed(2)}" stroke="${stroke}" stroke-width="${strokeW}"/><text x="${midX.toFixed(1)}" y="185" text-anchor="middle" class="locator-gate-name">${escapeHtml(g.label)}</text><text x="${x1.toFixed(1)}" y="201" text-anchor="middle" class="locator-scale-tick">${(i + 1) * 0.75}m</text></g>`;
  }).join('');

  const hasTarget = success && rangeCm != null && state !== 'EMPTY' && state !== 'UNKNOWN';
  let markers = '';
  if (movCm != null && movCm > 0) {
    const mx = cmToX(movCm);
    markers += `<g class="locator-submarker" transform="translate(${mx.toFixed(1)},${centerY - 32})"><circle r="4.5" fill="#287c54"/><text y="-8" text-anchor="middle" class="locator-sub-label">M ${number(movCm)}cm</text></g>`;
  }
  if (statCm != null && statCm > 0) {
    const sx = cmToX(statCm);
    markers += `<g class="locator-submarker" transform="translate(${sx.toFixed(1)},${centerY + 32})"><circle r="4.5" fill="#5b8a72"/><text y="15" text-anchor="middle" class="locator-sub-label">S ${number(statCm)}cm</text></g>`;
  }
  if (hasTarget) {
    const tx = cmToX(rangeCm);
    const th = halfHAt(tx);
    const color = confirmed ? '#1f6f4a' : '#c87a19';
    const haloR = (13 + Math.min(1, Math.max(0, activity)) * 9).toFixed(1);
    const labelX = Math.max(125, Math.min(565, tx));
    const trendGlyph = trend === 'CLOSER' ? '← CLOSER' : trend === 'FARTHER' ? 'FARTHER →' : trend === 'STABLE' ? '• STABLE' : '';
    const arrow = trend === 'CLOSER'
      ? `<path d="M ${(tx - 12).toFixed(1)} ${centerY} L ${(tx - 28).toFixed(1)} ${centerY} M ${(tx - 22).toFixed(1)} ${centerY - 5} L ${(tx - 28).toFixed(1)} ${centerY} L ${(tx - 22).toFixed(1)} ${centerY + 5}" stroke="${color}" stroke-width="2.2" fill="none" stroke-linecap="round"/>`
      : trend === 'FARTHER'
      ? `<path d="M ${(tx + 12).toFixed(1)} ${centerY} L ${(tx + 28).toFixed(1)} ${centerY} M ${(tx + 22).toFixed(1)} ${centerY - 5} L ${(tx + 28).toFixed(1)} ${centerY} L ${(tx + 22).toFixed(1)} ${centerY + 5}" stroke="${color}" stroke-width="2.2" fill="none" stroke-linecap="round"/>`
      : '';
    markers += `<line x1="${originX}" y1="${centerY}" x2="${tx.toFixed(1)}" y2="${centerY}" stroke="${color}" stroke-width="2" stroke-dasharray="4 4"/><line x1="${tx.toFixed(1)}" y1="${(centerY - th).toFixed(1)}" x2="${tx.toFixed(1)}" y2="${(centerY + th).toFixed(1)}" stroke="${color}" stroke-width="2" stroke-opacity="0.55"/>${arrow}<g class="locator-target" data-estimated-range="${escapeHtml(rangeCm)}" transform="translate(${tx.toFixed(1)},${centerY})"><circle r="${haloR}" fill="${color}" fill-opacity="0.18"/><circle r="8" fill="${color}" stroke="#ffffff" stroke-width="2"/></g><g transform="translate(${labelX.toFixed(1)},22)"><rect x="-78" y="-14" width="156" height="22" rx="11" fill="${color}"/><text x="0" y="1" text-anchor="middle" class="locator-callout-text">~${number(rangeCm)} cm (${(rangeCm / 100).toFixed(2)} m)${trendGlyph ? ` · ${trendGlyph}` : ''}</text></g>`;
  } else {
    const emptyMsg = !success ? 'Sensor unavailable — position unknown' : 'No object detected in 0.00–6.75 m range';
    markers += `<text x="${(originX + spanW / 2).toFixed(1)}" y="${centerY + 4}" text-anchor="middle" class="locator-empty-text">${escapeHtml(emptyMsg)}</text>`;
  }
  const activeGate = hasTarget ? `G${Math.min(8, Math.floor(rangeCm / 75))}` : null;
  const statusSummary = hasTarget
    ? `Estimated at ~${number(rangeCm)} cm (${(rangeCm / 100).toFixed(2)} m) in Gate ${activeGate}${trend !== 'UNKNOWN' ? ` · ${escapeHtml(trend)}` : ''}`
    : (!success ? 'Position unavailable' : 'No object in detection range');
  return `<section class="radar-locator" aria-label="Estimated object position"><div class="radar-locator-head"><div><h3>Estimated object position</h3><p class="timestamp" style="margin:2px 0 0">Radial distance along the LD2410C sensing cone (Gates G0–G8, 0.00–6.75 m · sensor does not measure left/right angle)</p></div><strong class="radar-locator-readout">${statusSummary}</strong></div><svg class="radar-locator-svg" viewBox="0 0 680 212" role="img" aria-label="Radar object range map: ${escapeHtml(statusSummary)}"><title>${escapeHtml(statusSummary)}</title><line x1="${originX}" y1="${centerY}" x2="${endX}" y2="${centerY}" stroke="#d5dfd8" stroke-width="1" stroke-dasharray="2 3"/>${gateShapes}<g transform="translate(${originX},${centerY})"><rect x="-38" y="-16" width="38" height="32" rx="7" fill="#172e21"/><circle cx="0" cy="0" r="5" fill="#287c54" stroke="#ffffff" stroke-width="1.5"/><text x="-19" y="4" text-anchor="middle" fill="#ffffff" font-size="9" font-weight="600">RADAR</text></g><text x="${originX}" y="201" text-anchor="middle" class="locator-scale-tick">0m</text>${markers}</svg></section>`;
}
function renderRadarSummaryPill(entry, success, data) {
  const state = success ? inferDerivedState(data) : 'UNKNOWN';
  const confirmed = success ? (data.presence_confirmed ?? ['PRESENT_ACTIVE','PRESENT_STILL','DEPARTING_CANDIDATE'].includes(state)) : false;
  const rangeCm = success ? radarRange(data) : null;
  const trend = success ? (data.range_trend || 'UNKNOWN') : 'UNKNOWN';
  const trendArrow = {CLOSER:'↘ closer',FARTHER:'↗ farther',STABLE:'→ stable'}[trend] || '';
  const tone = !success ? 'unavailable' : confirmed ? 'confirmed' : state === 'APPROACHING_CANDIDATE' ? 'candidate' : 'empty';
  return `<div class="radar-summary-strip"><span class="radar-pill ${tone}">${escapeHtml(state)}</span><span class="radar-confirm-tag">${!success ? 'Stale / offline' : confirmed ? 'Confirmed presence' : state === 'APPROACHING_CANDIDATE' ? 'Debouncing candidate' : 'Idle / unoccupied'}</span>${rangeCm != null ? `<span class="radar-range-tag">~${number(rangeCm)} cm${trendArrow ? ` · ${escapeHtml(trendArrow)}` : ''}</span>` : ''}</div>`;
}
function renderRadarExpanded(entry, success, data) {
  const state = success ? inferDerivedState(data) : 'UNKNOWN';
  const confirmed = success ? (data.presence_confirmed ?? ['PRESENT_ACTIVE','PRESENT_STILL','DEPARTING_CANDIDATE'].includes(state)) : false;
  const rangeCm = success ? radarRange(data) : null;
  const trend = success ? (data.range_trend || 'UNKNOWN') : 'UNKNOWN';
  const activity = success ? (data.activity_level ?? (state === 'PRESENT_ACTIVE' ? Math.min(1, (data.moving_energy_score || 0) / 85) : 0)) : null;
  const confidence = success ? (data.presence_confidence ?? (confirmed ? 0.8 : 0.05)) : null;
  const reason = success ? (data.transition_reason || `Derived from latest ${data.presence || 'radar'} report.`) : (entry?.result?.error?.message || 'Sensor stream unavailable; never treated as absence.');
  const win = data.window_summary || {};
  const gates = Array.isArray(data.gate_profile) && data.gate_profile.length ? data.gate_profile : Array.from({length:9},(_,g)=>{
    const minCm = g * 75, maxCm = (g + 1) * 75;
    const mov = success && data.moving_distance_cm != null && Math.min(8, Math.floor(data.moving_distance_cm / 75)) === g ? (data.moving_energy_score || 0) : 0;
    const stat = success && data.stationary_distance_cm != null && Math.min(8, Math.floor(data.stationary_distance_cm / 75)) === g ? (data.stationary_energy_score || 0) : 0;
    const active = rangeCm != null && Math.min(8, Math.floor(rangeCm / 75)) === g;
    return {gate:g,label:`G${g}`,range_m:`${(g*0.75).toFixed(2)}–${((g+1)*0.75).toFixed(2)} m`,min_cm:minCm,max_cm:maxCm,moving_energy:mov,stationary_energy:stat,active};
  });
  const actPct = activity == null ? 0 : Math.round(Math.max(0, Math.min(1, activity)) * 100);
  const confPct = confidence == null ? 0 : Math.round(Math.max(0, Math.min(1, confidence)) * 100);
  const estimatorHtml = `<section class="radar-section" aria-label="Derived presence and activity estimator"><div class="radar-banner"><div><h3>${escapeHtml(radarStateLabels[state] || state)} <small>(${escapeHtml(state)})</small></h3><p class="radar-reason">${escapeHtml(reason)}</p></div><span class="badge ${!success ? 'unavailable' : confirmed ? '' : 'empty'}">${!success ? 'Sensor unhealthy' : confirmed ? 'Confirmed (debounced)' : 'Unconfirmed'}</span></div><div class="radar-meters"><div class="radar-meter-card"><div class="radar-meter-head"><span>Activity level</span><strong>${activity == null ? '—' : number(activity)} <small>/ 1.0</small></strong></div><div class="radar-bar-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${actPct}"><div class="radar-bar-fill motion" style="width:${actPct}%"></div></div><small>Smoothed moving energy above baseline</small></div><div class="radar-meter-card"><div class="radar-meter-head"><span>Presence confidence heuristic</span><strong>${confidence == null ? '—' : number(confidence)} <small>/ 1.0</small></strong></div><div class="radar-bar-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${confPct}"><div class="radar-bar-fill stationary" style="width:${confPct}%"></div></div><small>Sliding-window consistency heuristic, not probability</small></div></div>` + facts([
    ['Derived presence state',escapeHtml(state)],
    ['Presence confirmed',!success ? 'Unknown' : (confirmed ? 'Yes (debounced)' : 'No')],
    ['Smoothed range estimate',rangeCm == null ? '—' : `${number(rangeCm)} cm (${number(rangeCm / 100)} m)`],
    ['Range filter',escapeHtml(data.range_filter_state || 'Not reported')],
    ['Radar sample timestamp',time(data.timestamp)],
    ['Range trend candidate',escapeHtml(trend)],
    ['Smoothed energy (move / still)',win.smoothed_moving_energy == null ? '—' : `${number(win.smoothed_moving_energy)} / ${number(win.smoothed_stationary_energy)} (floor ${number(win.baseline_stationary_energy)})`],
    ['Sliding window (8 s)',win.frame_count == null ? '—' : `${number(win.frame_count)} frames · ${number(win.frame_rate_hz)} Hz · ${number(Math.round((win.credible_frame_ratio || 0) * 100))}% credible`]
  ]) + `</section>`;
  const gatesHtml = `<section class="radar-section" aria-label="Radial distance gates"><div class="radar-gates-header"><div><h3>Radial distance gates (Gates 0–8 · 0.75 m bands)</h3><p class="timestamp" style="margin-top:4px">${win.gate_source === 'engineering' ? 'Direct per-gate energy from LD2410C engineering mode (0x01).' : 'Radial gate energy profile mapped from the 8-second sliding window (0.75 m resolution).'}</p></div><div class="radar-legend"><span><i class="dot motion"></i>Moving energy</span><span><i class="dot stationary"></i>Stationary energy</span></div></div><div class="radar-gates-grid">${gates.map(g=>`<div class="radar-gate-row ${g.active ? 'active' : ''}"><div class="radar-gate-label"><strong>${escapeHtml(g.label)}</strong><small>${escapeHtml(g.range_m)}</small></div><div class="radar-gate-bars"><div class="radar-gate-bar-line"><span class="bar-tag">M</span><div class="radar-bar-track"><div class="radar-bar-fill motion" style="width:${Math.min(100, g.moving_energy || 0)}%"></div></div><span class="bar-val">${number(g.moving_energy ?? 0)}</span></div><div class="radar-gate-bar-line"><span class="bar-tag">S</span><div class="radar-bar-track"><div class="radar-bar-fill stationary" style="width:${Math.min(100, g.stationary_energy || 0)}%"></div></div><span class="bar-val">${number(g.stationary_energy ?? 0)}</span></div></div></div>`).join('')}</div></section>`;
  const rawHtml = `<section class="radar-section" aria-label="Raw UART frame and hardware health"><h3>Latest raw LD2410C UART frame & health</h3>` + facts([
    ['Moving distance',data.moving_distance_cm == null ? '—' : `${number(data.moving_distance_cm)} cm`],
    ['Stationary distance',data.stationary_distance_cm == null ? '—' : `${number(data.stationary_distance_cm)} cm`],
    ['Detection distance',data.detection_distance_cm == null ? '—' : `${number(data.detection_distance_cm)} cm`],
    ['Moving energy score',number(data.moving_energy_score)],
    ['Stationary energy score',number(data.stationary_energy_score)],
    ['Serial connected',entry ? (entry.result.serial_connected ? 'Yes' : 'No') : '—'],
    ['Frame age',entry?.result.age_seconds == null ? '—' : `${number(entry.result.age_seconds)} s`],
    ['UART frames (valid / bad)',entry?.result.valid_frames == null ? '—' : `${number(entry.result.valid_frames)} / ${number(entry.result.malformed_frames ?? 0)}`],
    ['Serial port & mode',escapeHtml(`${entry?.result.serial_mapping || entry?.result.serial_port || '/dev/serial0'} · ${data.reporting_mode || 'normal'}`)]
  ]) + '<p class="timestamp">Energy is a sensor score from 0–100, not a percentage or probability. Frames older than two seconds are unavailable. The LD2410C measures radial distance and motion/stationary energy bands—not room X/Y coordinates, posture, or headcount.</p></section>';
  return estimatorHtml + gatesHtml + rawHtml;
}
function historyPlot(source) {
  const metric = source === 'microphone' ? selectedAnimal : historyMetrics[source];
  const entries = (histories[source] || []).filter(entry=>source !== 'birdcast' || entry.result.region?.code === region);
  const states = {none:0,moving:1,stationary:2,both:3};
  const derivedStates = {EMPTY:0,APPROACHING_CANDIDATE:1,DEPARTING_CANDIDATE:2,PRESENT_STILL:3,PRESENT_ACTIVE:4};
  const points = entries.map(entry=>{
    let value;
    if (source === 'radar' && metric === 'presence') value = states[entry.result.presence];
    else if (source === 'radar' && metric === 'presence_state') value = derivedStates[inferDerivedState(entry.result)];
    else if (source === 'radar' && metric === 'range_estimate_cm') value = radarRange(entry.result);
    else if (source === 'birdcast') value = entry.result.metrics?.[metric];
    else if (source === 'microphone') value = entry.result.animals?.find(animal=>animal.scientific_name===metric)?.score;
    else value = entry.result[metric];
    return {entry,x:Date.parse(source === 'microphone' ? entry.result.timestamp : entry.read_at),y:entry.http_status === 200 && typeof value === 'number' && Number.isFinite(value) ? value : null};
  }).filter(point=>Number.isFinite(point.x));
  const valid = points.filter(point=>point.y !== null);
  const label = source === 'microphone' ? `${metric ? animalLabel(metric) : 'Animal'} model score` : graphMetrics[source].find(item=>item[0]===metric)[1];
  if (historyErrors[source]) return `<p class="error-message">${escapeHtml(historyErrors[source])}</p>`;
  if (!valid.length) return `<p>No saved ${escapeHtml(label.toLowerCase())} readings in this time range.</p>`;
  const info = historyInfo[source];
  const first = info ? info.start*1000 : points[0].x, last = info ? info.end*1000 : points[points.length-1].x;
  const categorical = source === 'radar' && (metric === 'presence' || metric === 'presence_state');
  const catNames = metric === 'presence_state' ? ['EMPTY','APPROACHING','DEPARTING','STILL','ACTIVE'] : ['none','moving','stationary','both'];
  const catMax = catNames.length - 1;
  const low = categorical ? 0 : Math.min(0,...valid.map(point=>point.y));
  const high = categorical ? catMax : Math.max(low+1,...valid.map(point=>point.y));
  const x = point=>50+(last===first ? 250 : (point.x-first)/(last-first)*500);
  const y = point=>170-(point.y-low)/(high-low)*140;
  let previous = null;
  const marks = points.map(point=>{
    if (point.y === null) { previous = null; return ''; }
    const line = previous ? `<path d="M ${x(previous)} ${y(previous)} ${categorical ? `H ${x(point)} V ${y(point)}` : `L ${x(point)} ${y(point)}`}"/>` : '';
    previous = point;
    const value = categorical ? (metric === 'presence_state' ? inferDerivedState(point.entry.result) : point.entry.result.presence) : point.y;
    return `${line}<circle cx="${x(point)}" cy="${y(point)}" r="3" tabindex="0"><title>${escapeHtml(value)} · Read ${time(point.entry.read_at)} · Measured ${time(point.entry.result.timestamp || point.entry.result.retrieved_at)}</title></circle>`;
  }).join('');
  const ticks = categorical ? catNames.map((state,index)=>`<text x="45" y="${170-index/catMax*140}" text-anchor="end">${state}</text>`).join('') : `<text x="45" y="32" text-anchor="end">${number(high)}</text><text x="45" y="172" text-anchor="end">${number(low)}</text>`;
  return `<svg class="history-chart" viewBox="0 0 600 210" role="img" aria-label="${escapeHtml(label)} over ${source === 'microphone' ? 'recording' : 'read'} time"><title>${escapeHtml(label)} over time</title><path class="chart-axis" d="M 50 25 V 175 H 550"/>${ticks}<g class="chart-values">${marks}</g><text x="50" y="198">${escapeHtml(new Date(first).toLocaleString())}</text><text x="550" y="198" text-anchor="end">${escapeHtml(new Date(last).toLocaleString())}</text></svg><p class="timestamp">${valid.length} plotted readings${info ? ` from ${info.total} saved entries` : ''} · ${time(new Date(first).toISOString())} – ${time(new Date(last).toISOString())}. ${info?.sampled ? 'Last reading in each time bucket shown (up to 1,000 points).' : ''} Unavailable values leave gaps. Hover or focus a point for its value and timestamps.${source === 'microphone' ? ' Model scores are unverified suggestions, not confidence percentages. An unlisted animal has no score, not zero.' : ''}</p>`;
}
function updateHistoryPlot(source) {
  const chart = content.querySelector(`[data-chart="${source}"]`);
  if (chart) chart.innerHTML = historyPlot(source);
}
async function loadHistory(source, force = false) {
  const key = `${historyRanges[source]}:${source==='birdcast' ? region : ''}`;
  if (!force && historyKeys[source]===key) return;
  const generation = (historyRequests[source] || 0)+1;
  historyRequests[source] = generation;
  historyKeys[source] = key;
  try {
    const data = await api(`/v1/history?source=${source}&range=${historyRanges[source]}${source==='birdcast' ? `&region=${region}` : ''}`);
    if (historyRequests[source]!==generation) return;
    histories[source] = data.entries;
    const previousAnimals = historyInfo[source]?.animals || [];
    historyInfo[source] = data;
    historyErrors[source] = '';
    if (source==='microphone') {
      if (!data.animals.includes(selectedAnimal)) selectedAnimal = data.animals[0] || '';
      const selector = content.querySelector('[data-history-metric="microphone"]');
      if (selector && JSON.stringify(previousAnimals)!==JSON.stringify(data.animals)) selector.innerHTML = data.animals.map(animal=>`<option value="${escapeHtml(animal)}" ${animal===selectedAnimal?'selected':''}>${escapeHtml(animalLabel(animal))}</option>`).join('');
      loadAnimalNames(data.animals);
    }
  } catch (error) {
    if (historyRequests[source]!==generation) return;
    historyKeys[source] = '';
    historyErrors[source] = error.message;
  }
  if (historyKeys[source]===key || historyErrors[source]) updateHistoryPlot(source);
}
function scheduleHistoryPoll() {
  clearTimeout(historyTimer);
  const generation = ++historyGeneration;
  const source = view()==='recordings' ? 'microphone' : view();
  if (!historyRanges[source]) return;
  loadHistory(source);
  historyTimer = setTimeout(async()=>{
    if (generation!==historyGeneration) return;
    await loadHistory(source,true);
    if (generation===historyGeneration) scheduleHistoryPoll();
  },10000);
}
function facts(items) { return `<dl class="facts">${items.map(([label,value])=>`<div class="fact"><dt>${label}</dt><dd>${value}</dd></div>`).join('')}</dl>`; }
function animalResults(item) {
  const analysis = item.analysis || {state:'not_analyzed'};
  const busy = ['queued','running'].includes(analysis.state) || analysisRequests.has(item.recording_id);
  const states = {not_analyzed:'Not analyzed',queued:'Waiting for identification…',running:'Identifying animals…',completed:'Species candidates',failed:'Identification failed',unavailable:'Identification unavailable'};
  let body = `<h3>${states[analysis.state] || 'Not analyzed'}</h3>`;
  if (analysis.error) body += `<p class="error-message">${escapeHtml(analysis.error)}</p>`;
  if (analysis.outdated) body += '<p>The recording or model changed. Identify again for current suggestions.</p>';
  if (analysis.state === 'completed') {
    body += `<ul class="animal-candidates">${(analysis.candidates || []).map(candidate=>`<li><span>${escapeHtml(animalLabel(candidate.scientific_name))}</span><span>Raw score ${number(candidate.score)}</span></li>`).join('')}</ul>`;
    body += scoreExplanation;
    body += '<p class="timestamp">Unverified suggestions. Scores are not confidence percentages or animal counts. Background noise can also produce candidates.</p>';
    if (analysis.windows) body += details(analysis.windows,'Five-second window scores');
    else body += `<button class="button secondary" data-analysis-details="${escapeHtml(item.recording_id)}">View window scores</button>`;
  } else if (analysis.state !== 'unavailable') {
    body += `<button class="button secondary" data-identify="${escapeHtml(item.recording_id)}" ${busy ? 'disabled aria-busy="true"' : ''}>${busy ? 'Identification pending' : analysis.state === 'failed' ? 'Retry identification' : 'Identify animals'}</button>`;
  }
  return body;
}
function visibleRecordings() {
  return view() === 'recordings' ? recordings : view() === 'overview' ? recordings.slice(0,3) : [];
}
function updateAnimalResults(item) {
  const panel = content.querySelector(`[data-analysis="${item.recording_id}"]`);
  if (!panel) return;
  const focused = document.activeElement;
  const restoreIdentify = focused?.dataset?.identify === item.recording_id;
  const restoreDetails = focused?.dataset?.analysisDetails === item.recording_id;
  const wasOpen = panel.querySelector('details')?.open;
  panel.innerHTML = animalResults(item);
  const detail = panel.querySelector('details');
  if (detail && wasOpen) detail.open = true;
  if (restoreIdentify) panel.querySelector('[data-identify]')?.focus();
  if (restoreDetails) (panel.querySelector('summary') || panel.querySelector('[data-analysis-details]'))?.focus();
}
function scheduleAnalysisPoll() {
  clearTimeout(analysisTimer);
  const generation = ++analysisPollGeneration;
  const items = visibleRecordings().filter(item=>['queued','running'].includes(item.analysis?.state));
  if (!items.length) return;
  analysisTimer = setTimeout(async()=>{
    for (const item of items) {
      if (generation !== analysisPollGeneration) return;
      try {
        const result = await api(`/v1/recordings/${item.recording_id}/analysis`);
        item.analysis = result.analysis;
      } catch (error) {
        if (error.data?.analysis) item.analysis = error.data.analysis;
        else notice(error.message,true);
      }
      if (generation !== analysisPollGeneration) return;
      updateAnimalResults(item);
      loadAnimalNames((item.analysis?.candidates || []).map(candidate=>candidate.scientific_name));
    }
    scheduleAnalysisPoll();
  },3000);
}
async function identifyAnimals(identifier, detailsOnly = false) {
  if (analysisRequests.has(identifier)) return;
  const item = recordings.find(recording=>recording.recording_id === identifier);
  if (!item) return;
  analysisRequests.add(identifier);
  updateAnimalResults(item);
  try {
    const result = await api(`/v1/recordings/${identifier}/analysis`,detailsOnly ? {} : {method:'POST'});
    item.analysis = result.analysis;
    loadAnimalNames((item.analysis?.candidates || []).map(candidate=>candidate.scientific_name));
    notice(detailsOnly ? '' : 'Animal identification requested.');
  } catch (error) {
    if (error.data?.analysis) item.analysis = error.data.analysis;
    notice(error.message,true);
  } finally {
    analysisRequests.delete(identifier);
    updateAnimalResults(item);
    scheduleAnalysisPoll();
  }
}
function recordingCard(item) {
  return `<article class="card record-row" data-recording="${escapeHtml(item.recording_id)}"><div><h3>${time(item.created_at)}</h3><p>10 seconds · Mono · 48 kHz · 16-bit PCM</p><p>${number(item.byte_length / 1024)} KB</p></div><audio controls preload="none" aria-label="Recording from ${time(item.created_at)}" src="${escapeHtml(item.download_path)}"></audio><a class="button secondary" href="${escapeHtml(item.download_path)}" download="${escapeHtml(item.recording_id)}.wav">↓ Download</a><button class="button secondary" data-delete-recording="${escapeHtml(item.recording_id)}" ${deletingRecordings.has(item.recording_id)?'disabled':''}>Delete recording</button><section class="animal-analysis" data-analysis="${escapeHtml(item.recording_id)}" aria-label="Animal identification">${animalResults(item)}</section>${details(item,'Recording details & integrity')}</article>`;
}
async function deleteRecording(identifier) {
  if (deletingRecordings.has(identifier)) return;
  const item = recordings.find(recording=>recording.recording_id===identifier);
  if (!item || !window.confirm(`Delete the recording from ${new Date(item.created_at).toLocaleString()}, its animal results, and its history? This cannot be undone.`)) return;
  deletingRecordings.add(identifier);
  const button = content.querySelector(`[data-delete-recording="${identifier}"]`);
  if (button) { button.disabled = true; button.textContent = 'Deleting…'; }
  try {
    try { await api(`/v1/recordings/${identifier}`,{method:'DELETE'}); }
    catch (error) { if (error.data?.error?.code!=='recording_not_found') throw error; }
    recordings = recordings.filter(recording=>recording.recording_id!==identifier);
    const card = content.querySelector(`[data-recording="${identifier}"]`);
    card?.remove();
    const counter = content.querySelector('[data-recording-count]');
    if (counter) counter.textContent = `${recordings.length} saved`;
    if (!recordings.length) render();
    if (view()==='recordings') await loadHistory('microphone',true);
    scheduleAnalysisPoll();
    notice('Recording, animal results, and history deleted.');
  } catch (error) {
    notice(error.message,true);
    if (button) { button.disabled = false; button.textContent = 'Delete recording'; }
  } finally { deletingRecordings.delete(identifier); }
}
function library(limit = null) {
  if (loadingLibrary) return '<div class="card empty-state"><p>Loading recordings…</p></div>';
  if (libraryError) return `<div class="card empty-state"><h3>Library unavailable</h3><p>${escapeHtml(libraryError)}</p></div>`;
  if (!recordings.length) return '<div class="card empty-state"><div class="source-icon" aria-hidden="true">≋</div><h3>Your first recording starts here</h3><p>Choose Record 5 seconds to capture audio from the station microphone. Completed recordings are kept for playback and download.</p></div>';
  return `<div class="record-list">${(limit ? recordings.slice(0,limit) : recordings).map(recordingCard).join('')}</div>`;
}
function microphone() {
  return `<article class="card audio-summary"><div class="audio-copy"><div class="sound-icon" aria-hidden="true">${[16,24,38,26,52,65,37,50,26,40,20,12].map(height=>`<i style="--height:${height}px"></i>`).join('')}</div><h2>Listen to your surroundings</h2><p>Record five seconds from the station microphone. Captures stay in your library until you delete them.</p>${resultError('microphone')}</div><div class="audio-tools">${action('microphone','● Record 5 seconds')}<div data-recording-schedule>${scheduleControls()}</div><span class="badge empty" data-recording-count>${recordings.length} saved</span></div></article>`;
}
function scheduleControls() {
  return `<button class="button secondary" data-schedule-recording="${collection.enabled?'stop':'start'}" ${pending.has('schedule')?'disabled':''}>${collection.enabled?'Stop automatic recording':'Start recording every two minutes'}</button><p class="timestamp">${collection.recording?'Recording five seconds…':collection.enabled?'Automatic recording is on.':'Automatic recording is stopped.'}${collection.next_recording_at?`<br>Next recording ${time(collection.next_recording_at)}`:''}<br>Stop prevents future captures; an active capture finishes.${collection.error?`<br>${escapeHtml(collection.error)}`:''}</p>`;
}
function updateScheduleControls() {
  const controls = content.querySelector('[data-recording-schedule]');
  if (controls && !pending.has('schedule')) controls.innerHTML = scheduleControls();
}
async function setRecordingSchedule(enabled) {
  if (pending.has('schedule')) return;
  pending.add('schedule');
  const controls = content.querySelector('[data-recording-schedule]');
  if (controls) controls.innerHTML = scheduleControls();
  try {
    const data = await api('/v1/recording-schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});
    collection = data.collection;
    notice(enabled?'Automatic recording started: five seconds every two minutes.':'Automatic recording stopped.');
  } catch(error) { notice(error.message,true); }
  finally { pending.delete('schedule'); updateScheduleControls(); }
}
function syncLibrary() {
  const wrapper = content.querySelector('[data-recording-library]');
  if (!wrapper) return;
  const visible = visibleRecordings();
  const list = wrapper.querySelector('.record-list');
  if (!list || !visible.length) wrapper.innerHTML = library(view()==='overview'?3:null);
  else {
    for (const item of [...visible].reverse()) {
      if (!list.querySelector(`[data-recording="${item.recording_id}"]`)) list.insertAdjacentHTML('afterbegin',recordingCard(item));
      else updateAnimalResults(item);
    }
    list.querySelectorAll('[data-recording]').forEach(card=>{if(!visible.some(item=>item.recording_id===card.dataset.recording))card.remove();});
  }
  const counter = content.querySelector('[data-recording-count]');
  if (counter) counter.textContent = `${recordings.length} saved`;
  scheduleAnalysisPoll();
}
function scheduleCollectionPoll() {
  clearTimeout(collectionTimer);
  const generation = ++collectionGeneration;
  if (view() === 'distance-test') return;
  collectionTimer = setTimeout(async()=>{
    try {
      const latest = await api('/v1/latest');
      if (generation!==collectionGeneration) return;
      collection = latest.collection || collection;
      lighting = latest.lighting || {};
      syncLightingControls();
      sources.light = latest.sources.light;
      const reading = content.querySelector('[data-reading="light"]');
      if (reading && sources.light) {
        const wrapper = document.createElement('div');
        wrapper.innerHTML = sourceCard('light',view()==='light');
        reading.replaceWith(wrapper.querySelector('[data-reading="light"]'));
      }
      updateScheduleControls();
      if (['overview','recordings'].includes(view())) {
        await loadLibrary();
        if (generation!==collectionGeneration) return;
        syncLibrary();
      }
    } catch(error) { if(generation===collectionGeneration)notice(error.message,true); }
    if(generation===collectionGeneration)scheduleCollectionPoll();
  },view()==='lighting' ? 500 : view()==='light' ? 1000 : 10000);
}
let wledLogTimer, wledLogGeneration = 0;
function wledLogEvent(event) {
  let payload = event.payload;
  try { payload = JSON.stringify(JSON.parse(payload)); } catch { /* Preserve serial text. */ }
  const label = {sent:'Sent',received:'Recv',error:'Error'}[event.direction] || event.direction;
  const dirClass = event.direction === 'sent' ? 'badge-sent' : event.direction === 'error' ? 'badge-error' : 'badge-recv';
  const outcome = event.outcome ? `${escapeHtml(event.outcome)}${event.bytes_written == null ? '' : ` (${event.bytes_written}/${event.bytes_expected})`}` : '—';
  const timeStr = event.timestamp ? new Date(event.timestamp).toLocaleTimeString() : '';
  return `<tr class="wled-log-event" data-event-id="${event.id}">` +
    `<td class="wled-col-time"><time datetime="${escapeHtml(event.timestamp)}">${escapeHtml(timeStr)}</time></td>` +
    `<td class="wled-col-dir"><span class="badge ${dirClass}">${escapeHtml(label)}</span></td>` +
    `<td class="wled-col-outcome">${outcome}</td>` +
    `<td class="wled-col-message" title="Click to expand/collapse"><pre>${escapeHtml(payload)}</pre></td>` +
    `</tr>`;
}
function scheduleWledLog() {
  clearTimeout(wledLogTimer);
  const generation = ++wledLogGeneration;
  if (view() !== 'wled-log') return;
  let cursor = 0, session = null;
  const poll = async () => {
    if (generation !== wledLogGeneration || view() !== 'wled-log') return;
    if (!document.hidden) {
      try {
        const data = await api(`/v1/wled-log?after=${cursor}`);
        if (generation !== wledLogGeneration || view() !== 'wled-log') return;
        // Defer DOM changes while the user selects text; replay from the same cursor.
        if (!window.getSelection?.()?.toString()) {
          const list = content.querySelector('[data-wled-events]');
          const message = content.querySelector('[data-wled-message]');
          if (session && session !== data.session && !data.reset) {
            list.innerHTML = '';
            cursor = 0; session = null;
          } else {
            if (data.reset || (session && session !== data.session)) list.innerHTML = '';
            session = data.session;
            const before = list.getBoundingClientRect().height;
            const preserveScroll = window.scrollY > list.getBoundingClientRect().top + window.scrollY;
            if (data.events.length) list.insertAdjacentHTML('afterbegin',data.events.slice().reverse().map(wledLogEvent).join(''));
            list.querySelectorAll('[data-event-id]').forEach(node=>{
              if (Number(node.dataset.eventId) < data.oldest_id) node.remove();
            });
            if (preserveScroll) window.scrollBy(0,list.getBoundingClientRect().height - before);
            cursor = data.latest_id;
            message.textContent = data.expired || data.oldest_id > 1
              ? 'Older events have expired. Showing the latest 2,000 events.'
              : cursor ? '' : 'No USB traffic observed yet.';
          }
        }
      } catch(error) {
        if (generation === wledLogGeneration && view() === 'wled-log')
          content.querySelector('[data-wled-message]').textContent = `Log unavailable: ${error.message}`;
      }
    }
    if (generation === wledLogGeneration && view() === 'wled-log')
      wledLogTimer = setTimeout(poll,500);
  };
  poll();
}
function syncStationModeUI() {
  const select = document.querySelector('#station-mode');
  if (select && select.value !== stationMode) select.value = stationMode;
  const dirToggle = document.querySelector('#direction-config-toggle');
  if (dirToggle) {
    dirToggle.checked = stationMode === 'direction_config';
    const label = dirToggle.closest?.('.switch-control')?.querySelector?.('.switch-label');
    if (label) label.textContent = stationMode === 'direction_config' ? 'Active' : 'Inactive';
  }
  const manualToggle = document.querySelector('#manual-control-toggle');
  if (manualToggle) {
    manualToggle.checked = stationMode === 'manual_control';
    const label = manualToggle.closest?.('.switch-control')?.querySelector?.('.switch-label');
    if (label) label.textContent = stationMode === 'manual_control' ? 'Active' : 'Inactive';
  }
  const lightingCard = document.querySelector('.lighting-controls-card');
  if (lightingCard) {
    lightingCard.classList.toggle('controls-disabled', stationMode !== 'manual_control');
    const noticeEl = lightingCard.querySelector('.lighting-disabled-notice');
    if (noticeEl) noticeEl.hidden = stationMode === 'manual_control';
  }
}
async function setStationMode(mode) {
  const previous = stationMode;
  stationMode = mode;
  syncStationModeUI();
  try {
    const res = await api('/v1/station-mode', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode})
    });
    stationMode = res.mode || mode;
    const modeLabels = {normal: 'Normal', direction_config: 'Direction config', manual_control: 'Manual control'};
    notice(`Station mode changed to ${modeLabels[stationMode] || stationMode}.`);
  } catch (err) {
    stationMode = previous;
    notice(`Could not change station mode: ${err.message}`, true);
  } finally {
    syncStationModeUI();
    if (['config-directionality', 'lighting'].includes(view())) render();
  }
}
function render() {
  const current = view();
  const section = sectionMap[current];
  const label = itemNames[current] || names[current];
  document.querySelector('#breadcrumb').textContent = section ? `${section} > ${label}` : label;
  document.querySelector('#page-title').textContent = titles[current] || titles.overview;
  document.querySelector('#page-description').textContent = descriptions[current] || descriptions.overview;
  document.querySelectorAll('[data-view]').forEach(link => {
    if (link.dataset.view === current) {
      link.setAttribute('aria-current', 'page');
      const parentSection = link.closest?.('.nav-section');
      if (parentSection) {
        parentSection.classList.remove('collapsed');
        const header = parentSection.querySelector('.nav-section-header');
        if (header) header.setAttribute('aria-expanded', 'true');
      }
    } else {
      link.removeAttribute('aria-current');
    }
  });
  if (current === 'overview') content.innerHTML = `<div class="grid">${['radar','light','birdcast'].map(source=>sourceCard(source)).join('')}</div><div class="section-heading"><div><h2>Sound from the station</h2><p>A permanent home for your microphone recordings.</p></div><a class="section-link" href="#recordings">Open library ↗</a></div>${microphone()}<div class="section-heading"><h2>Latest recordings</h2></div><div data-recording-library>${library(3)}</div>`;
  else if (current === 'distance-test') content.innerHTML = distanceTestPage();
  else if (current === 'wled-log') content.innerHTML = '<article class="card"><div class="wled-log-header"><h2>WLED USB Traffic</h2><p data-wled-message role="status">Loading USB traffic…</p></div><div class="wled-table-container"><table class="wled-log-table"><thead><tr><th class="wled-th-time">Time</th><th class="wled-th-dir">Direction</th><th class="wled-th-outcome">Outcome</th><th class="wled-th-message">Message (click row to expand)</th></tr></thead><tbody data-wled-events></tbody></table></div></article>';
  else if (current === 'lighting') {
    content.innerHTML = `<article class="card manual-control-mode-card">` +
      `<div class="directionality-toggle-row">` +
        `<div class="directionality-toggle-info">` +
          `<h2>Manual Control Mode</h2>` +
          `<p class="timestamp">Activate Manual Control to adjust brightness, color, and power. When inactive, lighting controls are disabled.</p>` +
        `</div>` +
        `<label class="switch-control" for="manual-control-toggle">` +
          `<input type="checkbox" id="manual-control-toggle" ${stationMode === 'manual_control' ? 'checked' : ''}>` +
          `<span class="switch-slider" aria-hidden="true"></span>` +
          `<span class="switch-label">${stationMode === 'manual_control' ? 'Active' : 'Inactive'}</span>` +
        `</label>` +
      `</div>` +
    `</article>` +
    `<article class="card lighting-controls-card ${stationMode === 'manual_control' ? '' : 'controls-disabled'}">` +
      `<div class="lighting-disabled-notice" ${stationMode === 'manual_control' ? 'hidden' : ''}><span aria-hidden="true">🔒</span> Manual control mode is inactive. Activate Manual control mode above to enable these controls.</div>` +
      `${lightingControls()}` +
    `</article>`;
  }
  else if (current === 'recordings') content.innerHTML = `${microphone()}<article class="card"><h2>Animal identification over time</h2>${historyGraph('microphone')}</article><div class="section-heading"><div><h2>All recordings</h2><p>Newest first · delete individual recordings using their Delete recording button</p></div><button class="button secondary" data-action="library" ${pending.has('library') ? 'disabled' : ''}>↻ Refresh library</button></div><div data-recording-library>${library()}</div>`;
  else if (current === 'config-directionality') {
    content.innerHTML = `<article class="card directionality-mode-card">` +
      `<div class="directionality-toggle-row">` +
        `<div class="directionality-toggle-info">` +
          `<h2>Direction Config Mode</h2>` +
          `<p class="timestamp">Toggle Direction Config mode on or off. When active, the station operates in direction configuration mode.</p>` +
        `</div>` +
        `<label class="switch-control" for="direction-config-toggle">` +
          `<input type="checkbox" id="direction-config-toggle" ${stationMode === 'direction_config' ? 'checked' : ''}>` +
          `<span class="switch-slider" aria-hidden="true"></span>` +
          `<span class="switch-label">${stationMode === 'direction_config' ? 'Active' : 'Inactive'}</span>` +
        `</label>` +
      `</div>` +
    `</article>` +
    `<article class="card">` +
      `<div class="card-header">` +
        `<div>` +
          `<h2>Directional Calibration & Settings</h2>` +
          `<p class="timestamp">Configure radar target direction, orientation offsets, and directional filters.</p>` +
        `</div>` +
        `<span class="badge ${stationMode === 'direction_config' ? 'badge-recv' : 'empty'}">${stationMode === 'direction_config' ? 'Mode: Direction Config' : 'Mode: Normal'}</span>` +
      `</div>` +
      `<p style="color:var(--muted); margin-top:12px;">Direction configuration tools ready for setup.</p>` +
    `</article>`;
  }
  else if (current === 'config-schedule') content.innerHTML = `<article class="card audio-summary"><div class="audio-copy"><h2>Automatic microphone collection</h2><p>Audio recording runs on a shared server-side schedule. When enabled, the station captures 5 seconds of audio every 120 seconds.</p>${resultError('microphone')}</div><div class="audio-tools"><div data-recording-schedule>${scheduleControls()}</div><span class="badge empty" data-recording-count>${recordings.length} saved</span></div></article><article class="card"><h2>Recording schedule settings</h2>${facts([['Interval','120 seconds (2 minutes)'],['Sample duration','5 seconds'],['Audio format','WAV 48 kHz mono 16-bit PCM'],['State',collection.enabled ? 'Active (running)' : 'Stopped (manual captures only)'],['Next capture',collection.next_recording_at ? time(collection.next_recording_at) : 'None scheduled']])}</article>`;
  else if (current === 'config-lighting') content.innerHTML = `<article class="card"><h2>WLED USB controller configuration</h2>${wledFacts()}</article><article class="card"><h2>Lighting quick controls</h2>${lightingControls()}</article>`;
  else if (current === 'logs-sensors') content.innerHTML = `<article class="card"><h2>Presence & movement history</h2>${historyGraph('radar')}</article><article class="card"><h2>Luminance history</h2>${historyGraph('light')}</article><article class="card"><h2>BirdCast migration history</h2>${historyGraph('birdcast')}</article><article class="card"><h2>Animal identification history</h2>${historyGraph('microphone')}</article>`;
  else {
    const selector = current === 'birdcast' ? `<div class="selector"><label for="region">US region</label><select id="region">${regions.map(item=>`<option value="${escapeHtml(item.code)}" ${item.code === region ? 'selected' : ''}>${escapeHtml(item.name)}</option>`).join('')}</select></div>` : '';
    content.innerHTML = `${selector}${sourceCard(current,true)}`;
  }
  scheduleAnalysisPoll();
  scheduleRadarPoll();
  scheduleHistoryPoll();
  scheduleCollectionPoll();
  scheduleWledLog();
  syncStationModeUI();
}
async function loadCache() {
  const requestedAt = performance.now();
  try {
    const data = await api('/v1/latest');
    sources = data.sources;
    lighting = data.lighting || {};
    collection = data.collection || collection;
    if (data.mode) stationMode = data.mode;
    syncStationModeUI();
    Object.keys(historyKeys).forEach(source=>historyKeys[source]='');
    radarLoadedAt = requestedAt;
    scheduleRadarExpiry();
    regions = data.regions.sort((a,b)=>a.name.localeCompare(b.name));
    cacheError = '';
  } catch (error) { cacheError = error.message; throw error; }
}
async function loadLibrary() {
  try { const data = await api('/v1/recordings'); recordings = data.recordings; libraryError = ''; loadAnimalNames(recordings.flatMap(item=>(item.analysis?.candidates || []).map(candidate=>candidate.scientific_name))); }
  catch (error) { libraryError = error.message; throw error; }
  finally { loadingLibrary = false; }
}
async function run(source) {
  if (pending.has(source)) return;
  pending.add(source); notice(''); render();
  try {
    if (source === 'library') await loadLibrary();
    else {
      if (source === 'microphone') await api('/v1/recordings',{method:'POST'});
      else if (source === 'birdcast') await api('/v1/snapshots',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({region})});
      else await api(source === 'radar' ? '/presence' : '/reading');
      await loadCache();
      if (source === 'microphone') await loadLibrary();
    }
    notice(source === 'microphone' ? 'Recording saved. Play it in the library below.' : source === 'library' ? 'Recording library refreshed.' : 'Latest result refreshed.');
  } catch (error) {
    await loadCache().catch(()=>{});
    notice(error.message,true);
  } finally { pending.delete(source); render(); }
}
async function setLightingMode(mode, settings = {}) {
  if (pending.has('lighting')) return;
  pending.add('lighting');
  const buttons = content.querySelectorAll('[data-lighting-mode], [data-lighting-max], [data-lighting-settings] button, [data-lighting-settings] input');
  buttons.forEach(button=>button.disabled=true);
  try {
    const data = await api('/v1/lighting',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(mode ? {...settings,mode} : settings)});
    lighting = data.lighting;
    notice('Lighting settings saved.');
    if (mode === 'distance_test' && view() !== 'distance-test') location.href = '/distance-test';
    const form = content.querySelector('[data-lighting-settings]');
    if (form && (settings.color || settings.brightness)) form.dataset.dirty = 'false';
    syncLightingControls();
  } catch(error) { notice(error.message,true); }
  finally {
    await loadCache().catch(()=>{});
    updateRadarReading();
    const reading = content.querySelector('[data-reading="light"]');
    if (reading) {
      const wrapper = document.createElement('div');
      wrapper.innerHTML = sourceCard('light',view()==='light');
      reading.replaceWith(wrapper.querySelector('[data-reading="light"]'));
    }
    syncLightingControls();
    pending.delete('lighting');
    buttons.forEach(button=>button.disabled=false);
    syncLightingControls();
  }
}
content.addEventListener('input', event=>{
  const form = event.target.closest('[data-lighting-settings]');
  if (!form) return;
  form.dataset.dirty = 'true';
  if (event.target.name === 'brightness') form.querySelector('output').textContent = `${Math.round(Number(event.target.value) / 255 * 100)}%`;
});
content.addEventListener('submit', event=>{
  const form = event.target.closest('[data-lighting-settings]');
  if (!form) return;
  event.preventDefault();
  setLightingMode(null, {...(lighting.mode === 'auto' ? {} : {brightness:Number(form.querySelector('[name="brightness"]').value)}),color:form.querySelector('[name="color"]').value});
});
content.addEventListener('click',event=>{
  const maximum = event.target.closest('[data-lighting-max]');
  if (maximum) {
    const form = content.querySelector('[data-lighting-settings]');
    const color = form?.querySelector('[name="color"]')?.value || lighting.color || '#ffa028';
    setLightingMode('on', {brightness:255,color});
    return;
  }
  const lightingButton = event.target.closest('[data-lighting-mode]');
  if (lightingButton) { setLightingMode(lightingButton.dataset.lightingMode); return; }
  const schedule = event.target.closest('[data-schedule-recording]');
  if (schedule) { setRecordingSchedule(schedule.dataset.scheduleRecording==='start'); return; }
  const remove = event.target.closest('[data-delete-recording]');
  if (remove) { deleteRecording(remove.dataset.deleteRecording); return; }
  const refresh = event.target.closest('[data-history-refresh]');
  if (refresh) { loadHistory(refresh.dataset.historyRefresh,true); return; }
  const identify = event.target.closest('[data-identify]');
  if (identify) { identifyAnimals(identify.dataset.identify); return; }
  const detail = event.target.closest('[data-analysis-details]');
  if (detail) { identifyAnimals(detail.dataset.analysisDetails,true); return; }
  const logRow = event.target.closest('.wled-log-event');
  if (logRow && !window.getSelection?.()?.toString()) logRow.classList.toggle('expanded');
  const button = event.target.closest('[data-action]'); if (button) run(button.dataset.action);
});
content.addEventListener('change',event=>{
  if (event.target.id === 'direction-config-toggle') {
    setStationMode(event.target.checked ? 'direction_config' : 'normal');
    return;
  }
  if (event.target.id === 'manual-control-toggle') {
    setStationMode(event.target.checked ? 'manual_control' : 'normal');
    return;
  }
  if(event.target.id === 'region') { region = event.target.value; render(); }
  const source = event.target.dataset?.historyMetric;
  if (source==='microphone') { selectedAnimal = event.target.value; updateHistoryPlot(source); }
  else if (source && graphMetrics[source]?.some(([key])=>key===event.target.value)) { historyMetrics[source] = event.target.value; updateHistoryPlot(source); }
  const rangeSource = event.target.dataset?.historyRange;
  if (historyRanges[rangeSource] && rangeOptions.some(([key])=>key===event.target.value)) {
    historyRanges[rangeSource] = event.target.value;
    loadHistory(rangeSource,true);
  }
});
const mobileNavigation = window.matchMedia('(max-width:700px)');
function setMenu(open) {
  const sidebar = document.querySelector('#sidebar');
  sidebar.classList.toggle('open',open);
  sidebar.inert = mobileNavigation.matches && !open;
  document.querySelector('#menu').setAttribute('aria-expanded',String(open));
}
window.addEventListener('hashchange',()=>{ setMenu(false); render(); document.querySelector('#main').focus(); });
document.querySelector('#menu').addEventListener('click',()=>setMenu(!document.querySelector('#sidebar').classList.contains('open')));
document.querySelector('#close-menu').addEventListener('click',()=>{setMenu(false);document.querySelector('#menu').focus();});
document.addEventListener('keydown',event=>{if(event.key==='Escape' && document.querySelector('#sidebar').classList.contains('open')){setMenu(false);document.querySelector('#menu').focus();}});
document.querySelectorAll('.nav-section-header').forEach(header => {
  header.addEventListener('click', () => {
    const section = header.closest?.('.nav-section');
    if (!section) return;
    const isExpanded = header.getAttribute('aria-expanded') === 'true';
    header.setAttribute('aria-expanded', String(!isExpanded));
    section.classList.toggle('collapsed', isExpanded);
  });
});
document.querySelector('#station-mode')?.addEventListener('change', event => {
  setStationMode(event.target.value);
});
mobileNavigation.addEventListener('change',()=>setMenu(false));
setMenu(false);
document.querySelector('#refresh-cache').addEventListener('click',async event=>{
  const button = event.currentTarget; button.disabled = true;
  const results = await Promise.allSettled([loadCache(),loadLibrary()]);
  render();
  const failure = results.find(item=>item.status === 'rejected');
  notice(failure ? failure.reason.message : 'Saved results and library refreshed.',Boolean(failure)); button.disabled = false;
});
render();
Promise.allSettled([loadCache(),loadLibrary()]).then(results=>{render();const failure = results.find(item=>item.status === 'rejected');if(failure)notice(failure.reason.message,true);});
